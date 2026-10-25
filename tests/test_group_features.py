import copy
import csv
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import localcontext
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from ml_features import (
    GroupFeatures,
    compute_group_features,
    compute_payment_features,
)
from ml_features.group_cli import main
from ml_features.group_serialization import (
    serialize_group_derived_metrics,
    serialize_group_features,
    write_group_reports,
)
from ml_features.serialization import exact_decimal
from ml_features.sources import CsvSource


class GroupFeaturesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ml1b_unit_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.company_csv = self.root / "df_Uid_data_hack.csv"
        self.payment_csv = self.root / "df_Uid_payment_hack.csv"
        self.company_header = [
            "SYNTHETIC_U", "GROUP_ID", "REVENUE_COMPANY", "INFO"
        ]
        self.payment_header = ["PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"]
        self.companies = [
            ["0001", "g1", "2000", "a"],
            ["0002", "g1", "3000", "a"],
            ["placeholder", "g1", "1000.0", "a"],
            ["missing_revenue", "g1", "", "a"],
            ["other", "g2", "5000", "a"],
            ["no_group", "", "7000", "a"],
            ["ambiguous", "g1", "9000", "a"],
            ["ambiguous", "g2", "8000", "b"],
            ["U_428697", "g1", "100000", "a"],
        ]
        self.payments = [
            ["0001", "0002", "10", "1"],
            ["0001", "other", "20", "2"],
            ["0001", "unknown", "30", "3"],
            ["0001", "no_group", "40", "4"],
            ["0001", "ambiguous", "50", "5"],
            ["0001", "U_428697", "60", "6"],
            ["0001", "0001", "70", "7"],
        ]
        self.write_companies(self.companies)
        self.write_payments(self.payments)

    def write_csv(
        self, path: Path, header: list[str], rows: list[list[str]]
    ) -> None:
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)

    def write_companies(self, rows: list[list[str]]) -> None:
        self.write_csv(self.company_csv, self.company_header, rows)

    def write_payments(self, rows: list[list[str]]) -> None:
        self.write_csv(self.payment_csv, self.payment_header, rows)

    def features(
        self, company_id: str = "0001", payments: bool = True
    ) -> GroupFeatures:
        return compute_group_features(
            company_id, self.company_csv,
            self.payment_csv if payments else None,
            assume_additive_rows=True,
        )

    def test_multiple_group_members_are_unique_and_confirmed(self) -> None:
        features = self.features()
        self.assertEqual(features.confirmed_group_id, "g1")
        self.assertTrue(features.group_id_present)
        self.assertEqual(features.group_size, 4)
        self.assertEqual(
            [member.company_id for member in features.members],
            ["0001", "0002", "missing_revenue", "placeholder"],
        )

    def test_single_member_group_does_not_imply_independence(self) -> None:
        features = self.features("other", payments=False)
        self.assertEqual(features.confirmed_group_id, "g2")
        self.assertEqual(features.group_size, 1)
        report = serialize_group_features(features)
        self.assertTrue(report["group_membership"]["belongs_to_group"])
        self.assertFalse(
            report["group_membership"]["full_group_population_known"]
        )

    def test_missing_group_does_not_become_confirmed_independence(self):
        features = self.features("no_group", payments=False)
        self.assertFalse(features.group_id_present)
        self.assertIsNone(features.confirmed_group_id)
        self.assertIsNone(features.group_size)
        self.assertIsNone(features.members)
        self.assertIsNone(features.revenue)
        report = serialize_group_features(features)
        self.assertIsNone(report["group_membership"]["belongs_to_group"])
        self.assertIsNone(report["known_members"])
        self.assertEqual(report["derived_metrics"], [])

    def test_conflicting_group_assignment_is_never_chosen(self) -> None:
        features = self.features("ambiguous", payments=False)
        self.assertTrue(features.group_id_present)
        self.assertEqual(features.company_profile_status, "conflicting")
        self.assertIsNone(features.confirmed_group_id)
        self.assertIsNone(features.group_size)
        self.assertIsNone(features.revenue)

    def test_duplicate_records_do_not_inflate_members_or_revenue(self):
        self.write_companies(self.companies + [self.companies[1]] * 3)
        features = self.features(payments=False)
        self.assertEqual(features.group_size, 4)
        self.assertEqual(features.revenue["reported_member_revenue_sum"], 5000)
        member = next(value for value in features.members
                      if value.company_id == "0002")
        self.assertEqual(member.source_record_count, 4)
        self.assertEqual(member.revenue_amount, 3000)

    def test_technical_id_is_not_counted_even_with_one_record(self) -> None:
        features = self.features(payments=False)
        self.assertNotIn("U_428697", {
            member.company_id for member in features.members
        })
        self.assertIn("U_428697", {
            candidate["company_id"]
            for candidate in features.unresolved_candidates
        })
        technical = self.features("U_428697", payments=False)
        self.assertIsNone(technical.group_size)
        self.assertEqual(
            technical.company_profile_status, "technical_ambiguous"
        )

    def test_unassigned_counts_use_unique_ids_and_explicit_scope(self):
        features = self.features(payments=False)
        diagnostics = features.directory_diagnostics
        self.assertEqual(diagnostics["directory_unassigned_company_count"], 3)
        self.assertEqual(diagnostics["unresolved_candidate_member_count"], 2)
        self.assertEqual(diagnostics["unassigned_company_reason_counts"], {
            "missing_group_id": 1, "conflicting": 1,
            "technical_ambiguous": 1,
        })

    def test_conflicting_revenue_does_not_select_one_company_record(self):
        self.write_companies(self.companies + [["0002", "g1", "3001", "a"]])
        features = self.features(payments=False)
        self.assertEqual(features.group_size, 3)
        self.assertEqual(features.revenue["reported_member_revenue_sum"], 2000)
        candidate = next(value for value in features.unresolved_candidates
                         if value["company_id"] == "0002")
        self.assertEqual(candidate["conflicting_fields"], ["REVENUE_COMPANY"])

    def test_missing_and_present_group_records_are_ambiguous(self) -> None:
        self.write_companies(self.companies + [["0002", "", "3000", "a"]])
        features = self.features(payments=False)
        self.assertEqual(features.group_size, 3)
        candidate = next(value for value in features.unresolved_candidates
                         if value["company_id"] == "0002")
        self.assertTrue(candidate["has_missing_group_observations"])
        self.assertEqual(candidate["observed_group_ids"], ["g1"])

    def test_revenue_missing_and_placeholder_counts_are_per_member(self):
        self.write_companies(self.companies + [self.companies[2]] * 4)
        revenue = self.features(payments=False).revenue
        self.assertEqual(revenue["member_count"], 4)
        self.assertEqual(revenue["valid_revenue_member_count"], 2)
        self.assertEqual(revenue["missing_revenue_member_count"], 1)
        self.assertEqual(revenue["placeholder_revenue_member_count"], 1)
        self.assertEqual(revenue["valid_revenue_coverage"], Fraction(1, 2))
        self.assertFalse(revenue["complete_for_observed_members"])

    def test_revenue_sum_is_never_confirmed_consolidated_revenue(self):
        features = self.features(payments=False)
        report = serialize_group_features(features)
        self.assertEqual(
            report["revenue"]["reported_member_revenue_sum"], "5000"
        )
        self.assertIsNone(report["confirmed_consolidated_group_revenue"])
        self.assertIsNone(
            report["revenue"]["confirmed_consolidated_group_revenue"]
        )
        self.assertEqual(report["revenue"]["year"], 2025)
        self.assertEqual(report["revenue"]["currency"], "RUB")

    def test_all_placeholder_revenue_stays_unknown(self) -> None:
        self.write_companies([
            ["0001", "g1", "1000", "a"],
            ["0002", "g1", "1e3", "a"],
        ])
        revenue = self.features(payments=False).revenue
        self.assertIsNone(revenue["reported_member_revenue_sum"])
        self.assertEqual(revenue["valid_revenue_coverage"], 0)
        self.assertEqual(revenue["placeholder_revenue_member_count"], 2)

    def test_valid_zero_revenue_is_not_missing(self) -> None:
        self.write_companies([["0001", "g1", "0.0", "a"]])
        revenue = self.features(payments=False).revenue
        self.assertEqual(revenue["reported_member_revenue_sum"], 0)
        self.assertEqual(revenue["valid_revenue_member_count"], 1)
        self.assertEqual(revenue["valid_revenue_coverage"], 1)

    def test_negative_nonfinite_and_invalid_revenues_are_explicit(self):
        self.write_companies(self.companies + [
            ["negative", "g1", "-1", "a"],
            ["infinite", "g1", "Infinity", "a"],
            ["invalid", "g1", "bad", "a"],
        ])
        revenue = self.features(payments=False).revenue
        self.assertEqual(revenue["member_count"], 7)
        self.assertEqual(revenue["invalid_revenue_member_count"], 3)
        self.assertEqual(revenue["reported_member_revenue_sum"], 5000)
        self.assertEqual(revenue["valid_revenue_coverage"], Fraction(2, 7))

    def test_missing_revenue_column_keeps_membership_available(self):
        self.write_csv(self.company_csv, ["SYNTHETIC_U", "GROUP_ID"], [
            ["0001", "g1"], ["0002", "g1"],
        ])
        features = self.features(payments=False)
        self.assertEqual(features.group_size, 2)
        self.assertFalse(
            features.directory_diagnostics["revenue_column_present"]
        )
        self.assertIsNone(features.revenue["reported_member_revenue_sum"])
        self.assertEqual(features.revenue["missing_revenue_member_count"], 2)

    def test_revenue_precision_is_independent_of_decimal_context(self):
        self.write_companies([
            ["0001", "g1", "9007199254740993.00000000000000000001", "a"],
            ["0002", "g1", "0.00000000000000000002", "a"],
        ])
        with localcontext() as context:
            context.prec = 2
            features = self.features(payments=False)
        self.assertEqual(
            exact_decimal(features.revenue["reported_member_revenue_sum"]),
            "9007199254740993.00000000000000000003",
        )

    def test_all_unresolved_recipient_types_expand_upper_bound(self):
        features = self.features()
        metrics = features.payments.metrics
        self.assertEqual(metrics["external_outgoing_amount"], 210)
        self.assertEqual(metrics["confirmed_same_group_amount"], 10)
        self.assertEqual(metrics["confirmed_other_group_amount"], 20)
        self.assertEqual(metrics["unknown_group_amount"], 180)
        self.assertEqual(
            metrics["intragroup_share_lower_bound"], Fraction(1, 21)
        )
        self.assertEqual(
            metrics["intragroup_share_upper_bound"], Fraction(19, 21)
        )
        self.assertIsNone(metrics["intragroup_share_out"])
        relationships = features.payment_relationships
        self.assertEqual(relationships["unknown_group_reason_counts"], {
            "recipient_not_in_directory": 1, "recipient_group_missing": 1,
            "conflicting_recipient_profile": 1,
            "technical_ambiguous_recipient": 1,
        })
        self.assertEqual(
            relationships["unresolved_recipients_in_directory_count"], 3
        )

    def test_known_recipient_without_group_is_not_a_different_group(self):
        self.write_payments([["0001", "no_group", "40", "4"]])
        features = self.features()
        self.assertEqual(features.payments.metrics["unknown_group_amount"], 40)
        self.assertEqual(
            features.payments.metrics["confirmed_other_group_amount"], 0
        )
        self.assertEqual(
            features.payments.metrics["intragroup_share_upper_bound"], 1
        )
        self.assertEqual(features.payment_relationships[
            "unresolved_unambiguous_profiles_missing_group_count"
        ], 1)

    def test_complete_payment_groups_have_an_exact_share(self) -> None:
        self.write_payments([
            ["0001", "0002", "10", "1"],
            ["0001", "other", "20", "2"],
        ])
        features = self.features()
        self.assertEqual(features.payments.metrics["intragroup_share_out"],
                         Fraction(1, 3))
        self.assertEqual(features.payment_relationships[
            "same_group_recipients"
        ], ["0002"])
        self.assertEqual(features.payment_relationships[
            "different_group_recipients"
        ], ["other"])

    def test_no_outgoing_payments_leave_ratios_undefined(self) -> None:
        self.write_payments([["other", "0002", "10", "1"]])
        features = self.features()
        self.assertEqual(features.group_size, 4)
        self.assertEqual(
            features.payments.metrics["external_outgoing_amount"], 0
        )
        self.assertIsNone(features.payments.metrics["intragroup_share_out"])
        self.assertIsNone(
            features.payments.metrics["intragroup_share_lower_bound"]
        )
        self.assertEqual(features.payment_relationships[
            "unknown_group_recipients"
        ], [])

    def test_group_and_outgoing_amount_conservation(self) -> None:
        features = self.features()
        metrics = features.payments.metrics
        self.assertEqual(metrics["outgoing_amount"], 280)
        self.assertEqual(metrics["self_payment_amount"], 70)
        self.assertEqual(metrics["external_outgoing_amount"], sum(
            metrics[name] for name in (
                "confirmed_same_group_amount", "confirmed_other_group_amount",
                "unknown_group_amount",
            )
        ))
        self.assertTrue(features.payment_relationships[
            "external_group_partition_conservation_verified"
        ])
        self.assertTrue(features.payment_relationships[
            "ml1a_outgoing_valid_amount_conservation_verified"
        ])

    def test_reused_payments_do_not_read_payment_csv_again(self) -> None:
        payments = compute_payment_features(
            "0001", self.company_csv, self.payment_csv,
            assume_additive_rows=True,
        )
        original = CsvSource.rows

        def company_rows_only(source):
            if source.path == self.payment_csv:
                raise AssertionError("Повторное чтение платежей")
            return original(source)

        with patch.object(CsvSource, "rows", company_rows_only):
            features = compute_group_features(
                "0001", self.company_csv, payment_features=payments
            )
        self.assertIs(features.payments, payments)
        self.assertEqual(features.payment_mode, "reused")
        self.assertEqual(
            features.payments.metrics["external_outgoing_amount"], 210
        )

    def test_csv_payment_extraction_runs_once(self) -> None:
        original = CsvSource.rows
        seen = []

        def track_rows(source):
            seen.append(source.path)
            return original(source)

        with patch.object(CsvSource, "rows", track_rows):
            self.features()
        self.assertEqual(seen.count(self.payment_csv), 1)

    def test_group_only_mode_does_not_compute_payments(self) -> None:
        with patch("ml_features.groups.compute_payment_features",
                   side_effect=AssertionError("Платежи не запрашивались")):
            features = self.features(payments=False)
        self.assertIsNone(features.payments)
        self.assertIsNone(features.payment_relationships)
        self.assertIsNone(serialize_group_features(features)["payments"])

    def test_mismatched_cached_company_and_source_version_are_rejected(self):
        payments = compute_payment_features(
            "0001", self.company_csv, self.payment_csv,
            assume_additive_rows=True,
        )
        with self.assertRaises(ValueError):
            compute_group_features("other", self.company_csv,
                                   payment_features=payments)
        self.write_companies(self.companies + [["new", "g3", "2000", "a"]])
        with self.assertRaises(ValueError):
            compute_group_features("0001", self.company_csv,
                                   payment_features=payments)

    def test_inconsistent_cached_bounds_are_rejected(self) -> None:
        payments = copy.deepcopy(self.features().payments)
        payments.metrics["intragroup_share_upper_bound"] = Fraction(1, 21)
        with self.assertRaises(ValueError):
            compute_group_features("0001", self.company_csv,
                                   payment_features=payments)

    def test_additivity_is_not_silently_enabled_for_cached_features(self):
        payments = compute_payment_features(
            "0001", self.company_csv, self.payment_csv
        )
        features = compute_group_features(
            "0001", self.company_csv, payment_features=payments
        )
        self.assertIsNone(
            features.payments.metrics["external_outgoing_amount"]
        )
        self.assertEqual(features.revenue["reported_member_revenue_sum"], 5000)
        with self.assertRaises(ValueError):
            compute_group_features("0001", self.company_csv,
                                   payment_features=payments,
                                   assume_additive_rows=True)

    def test_missing_company_and_missing_identifier_rows_remain_unknown(self):
        self.write_companies(self.companies + [["", "g1", "9999", "a"]])
        known = self.features(payments=False)
        self.assertEqual(known.group_size, 4)
        self.assertEqual(known.directory_diagnostics[
            "directory_rows_without_company_id"
        ], 1)
        unknown = self.features("absent", payments=False)
        self.assertIsNone(unknown.group_id_present)
        self.assertIsNone(unknown.group_size)
        self.assertEqual(serialize_group_derived_metrics(unknown), [])

    def test_group_size_and_payment_metrics_match_existing_schema(self):
        features = self.features()
        report = serialize_group_features(features)
        root = Path(__file__).resolve().parent.parent
        schemas = [json.loads((root / "schemas" / filename).read_text(
            encoding="utf-8"
        )) for filename in ("common.v1.schema.json",
                            "company_business_profile.v1.schema.json")]
        registry = Registry().with_resources([
            (schema["$id"], Resource.from_contents(
                schema, default_specification=DRAFT202012
            )) for schema in schemas
        ])
        validator = Draft202012Validator({
            "$ref": schemas[1]["$id"] + "#/$defs/DerivedMetric"
        }, registry=registry)
        derived = report["derived_metrics"]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["name"], "group_size")
        self.assertEqual(derived[0]["value"], 4)
        for metric in derived + report["payments"]["derived_metrics"]:
            validator.validate(metric)
        with self.company_csv.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        for evidence in derived[0]["inputs"]:
            self.assertTrue(any(
                row["SYNTHETIC_U"] == "0001"
                and row[evidence["ref"]] == evidence["value"] for row in rows
            ))
        self.assertEqual(derived, serialize_group_derived_metrics(features))
        self.assertIsNone(derived[0]["coverage"])

    def test_reports_are_deterministic_and_sources_are_unchanged(self):
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.company_csv, self.payment_csv)
        }
        first = write_group_reports(self.features(), self.root / "reports1")
        second = write_group_reports(self.features(), self.root / "reports2")
        self.assertEqual(first[0].read_bytes(), second[0].read_bytes())
        self.assertEqual(first[1].read_bytes(), second[1].read_bytes())
        report = json.loads(first[0].read_text(encoding="utf-8"))
        self.assertEqual(
            report["revenue"]["reported_member_revenue_sum"], "5000"
        )
        self.assertEqual(report["payments"]["metrics"]["unknown_group_amount"],
                         "180")
        self.assertEqual(before, {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in before
        })

    def test_cli_generates_group_reports_without_touching_payment_mode(self):
        with redirect_stdout(io.StringIO()):
            result = main([
                "--company-id", "0001", "--data-dir", str(self.root),
                "--output-dir", str(self.root / "reports"),
                "--assume-additive-rows",
            ])
        self.assertEqual(result, 0)
        path = self.root / "reports/ml1b_group_features_0001.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["group_membership"][
            "observed_confirmed_member_count"
        ], 4)
        self.assertEqual(report["payments"]["metrics"]["unknown_group_amount"],
                         "180")


if __name__ == "__main__":
    unittest.main()
