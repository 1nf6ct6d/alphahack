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

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from ml_features import PaymentFeatures, compute_payment_features
from ml_features.__main__ import main
from ml_features.serialization import (
    exact_decimal,
    serialize_derived_metrics,
    serialize_features,
    write_reports,
)


class PaymentFeaturesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ml1a_unit_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.company_csv = self.root / "df_Uid_data_hack.csv"
        self.payment_csv = self.root / "df_Uid_payment_hack.csv"
        self.company_header = ["SYNTHETIC_U", "GROUP_ID", "INFO"]
        self.payment_header = ["PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"]
        self.companies = [
            ["0001", "g1", "a"], ["0002", "g1", "a"],
            ["0003", "g2", "a"], ["ambiguous", "g1", "a"],
            ["ambiguous", "g1", "b"], ["U_428697", "g1", "a"],
            ["no_group", "", "a"],
        ]
        self.payments = [
            ["0001", "0002", "30", "3"],
            ["0001", "0002", "20", "2"],
            ["0001", "0003", "20", "2"],
            ["0001", "missing", "10", "1"],
            ["0001", "ambiguous", "8", "1"],
            ["0001", "U_428697", "5", "1"],
            ["0001", "no_group", "7", "1"],
            ["0001", "0001", "40", "4"],
        ]
        self.write_companies(self.companies)
        self.write_payments(self.payments)

    def write_csv(
        self, path: Path, header: list[str], rows: list[list[str]],
        encoding: str = "utf-8",
    ) -> None:
        with path.open("w", encoding=encoding, newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)

    def write_companies(self, rows: list[list[str]]) -> None:
        self.write_csv(self.company_csv, self.company_header, rows)

    def write_payments(self, rows: list[list[str]]) -> None:
        self.write_csv(self.payment_csv, self.payment_header, rows)

    def features(
        self, company_id: str = "0001", additive: bool = True
    ) -> PaymentFeatures:
        return compute_payment_features(
            company_id, self.company_csv, self.payment_csv,
            assume_additive_rows=additive,
        )

    def test_multiple_recipients_and_pay_cnt(self) -> None:
        features = self.features()
        metrics = features.metrics
        self.assertEqual(metrics["outgoing_amount"], 140)
        self.assertEqual(metrics["external_outgoing_amount"], 100)
        self.assertEqual(metrics["external_outgoing_payment_count"], 11)
        self.assertEqual(metrics["external_recipient_count"], 6)
        self.assertEqual(metrics["unambiguous_recipient_count"], 3)
        self.assertEqual(
            features.diagnostics["cohorts"]["external"]["record_count"], 7
        )

    def test_self_payments_are_excluded_from_denominators(self) -> None:
        metrics = self.features().metrics
        self.assertEqual(metrics["self_payment_record_count"], 1)
        self.assertEqual(metrics["self_payment_amount"], 40)
        self.assertEqual(metrics["external_outgoing_amount"], 100)
        self.assertEqual(metrics["top1_share"], Fraction(1, 2))

    def test_top1_top5_and_hhi_use_exact_shares(self) -> None:
        metrics = self.features().metrics
        self.assertEqual(metrics["top1_share"], Fraction(1, 2))
        self.assertEqual(metrics["top5_share"], Fraction(19, 20))
        self.assertEqual(metrics["hhi_out"], Fraction(1569, 5000))

    def test_unknown_and_ambiguous_profiles_remain_in_denominators(self):
        features = self.features()
        self.assertEqual(features.metrics["counterparty_coverage"],
                         Fraction(1, 2))
        self.assertEqual(features.metrics["counterparty_amount_coverage"],
                         Fraction(77, 100))
        recipients = {item.company_id: item for item in features.recipients}
        self.assertEqual(recipients["missing"].amount, 10)
        self.assertEqual(recipients["ambiguous"].amount, 8)
        self.assertEqual(recipients["ambiguous"].profile_status, "conflicting")
        self.assertEqual(recipients["missing"].group_relation, "unknown")
        self.assertEqual(recipients["ambiguous"].group_relation, "unknown")

    def test_technical_identifier_is_never_an_unambiguous_profile(self):
        features = self.features()
        recipient = next(item for item in features.recipients
                         if item.company_id == "U_428697")
        self.assertEqual(recipient.profile_status, "technical_ambiguous")
        self.assertEqual(recipient.group_relation, "unknown")
        self.assertIsNone(recipient.group_id)

    def test_unknown_groups_are_not_confirmed_non_membership(self) -> None:
        metrics = self.features().metrics
        self.assertEqual(metrics["confirmed_same_group_amount"], 50)
        self.assertEqual(metrics["confirmed_other_group_amount"], 20)
        self.assertEqual(metrics["unknown_group_amount"], 30)
        self.assertIsNone(metrics["intragroup_share_out"])
        self.assertEqual(metrics["intragroup_share_lower_bound"],
                         Fraction(1, 2))
        self.assertEqual(metrics["intragroup_share_upper_bound"],
                         Fraction(4, 5))
        self.assertEqual(metrics["unknown_group_amount_share"],
                         Fraction(3, 10))

    def test_complete_group_information_produces_exact_share(self) -> None:
        self.write_payments([
            ["0001", "0002", "30", "3"],
            ["0001", "0003", "70", "7"],
        ])
        features = self.features()
        self.assertEqual(features.metrics["intragroup_share_out"],
                         Fraction(3, 10))
        self.assertEqual(features.group_amount_coverage, 1)

    def test_unknown_zero_amount_group_is_still_unknown(self) -> None:
        self.write_payments([
            ["0001", "0002", "30", "3"],
            ["0001", "0003", "70", "7"],
            ["0001", "missing", "0", "0"],
        ])
        features = self.features()
        self.assertEqual(features.metrics["intragroup_share_out"],
                         Fraction(3, 10))
        recipient = next(item for item in features.recipients
                         if item.company_id == "missing")
        self.assertEqual(recipient.group_relation, "unknown")
        self.assertEqual(features.metrics["external_recipient_count"], 3)

    def test_missing_target_group_makes_all_relations_unknown(self) -> None:
        self.write_companies([
            ["0001", "", "a"], ["0002", "g1", "a"],
            ["0003", "g2", "a"],
        ])
        metrics = self.features().metrics
        self.assertEqual(metrics["confirmed_other_group_amount"], 0)
        self.assertEqual(metrics["unknown_group_amount"], 100)
        self.assertIsNone(metrics["intragroup_share_out"])
        self.assertEqual(metrics["intragroup_share_upper_bound"], 1)

    def test_conflicting_target_profile_cannot_confirm_group(self) -> None:
        self.write_companies(self.companies + [["0001", "g1", "changed"]])
        features = self.features()
        self.assertIsNone(features.company_group_id)
        self.assertEqual(features.metrics["unknown_group_amount"], 100)
        self.assertEqual(features.diagnostics["company_profile_status"],
                         "conflicting")

    def test_identical_directory_duplicates_do_not_multiply_payments(self):
        self.write_companies(self.companies + [["0002", "g1", "a"]] * 4)
        features = self.features()
        self.assertEqual(features.metrics["external_outgoing_amount"], 100)
        self.assertEqual(features.metrics["unambiguous_recipient_count"], 3)
        self.assertEqual(features.metrics["confirmed_same_group_amount"], 50)

    def test_repeated_pairs_are_summed_only_with_explicit_assumption(self):
        blocked = self.features(additive=False)
        self.assertIsNone(blocked.metrics["outgoing_amount"])
        self.assertIsNone(blocked.metrics["external_outgoing_amount"])
        self.assertIsNone(blocked.metrics["external_outgoing_payment_count"])
        self.assertIsNone(blocked.metrics["hhi_out"])
        self.assertTrue(all(item.amount is None
                            for item in blocked.recipients))
        self.assertEqual(blocked.metrics["counterparty_coverage"],
                         Fraction(1, 2))
        allowed = self.features()
        recipient = next(item for item in allowed.recipients
                         if item.company_id == "0002")
        self.assertEqual(recipient.amount, 50)
        self.assertEqual(allowed.diagnostics["repeated_directed_pair_count"],
                         1)
        self.assertFalse(serialize_features(allowed)["aggregation"][
            "additivity_verified_from_source"
        ])

    def test_exact_duplicate_rows_are_preserved_and_reported(self) -> None:
        self.write_payments([["0001", "0002", "10", "1"]] * 2)
        features = self.features()
        self.assertEqual(features.metrics["external_outgoing_amount"], 20)
        self.assertEqual(
            features.metrics["external_outgoing_payment_count"], 2
        )
        self.assertEqual(features.diagnostics["exact_duplicate_groups"], 1)
        self.assertEqual(features.diagnostics["exact_duplicate_extra_records"],
                         1)
        self.assertEqual(features.metrics["hhi_out"], 1)

    def test_missing_and_invalid_pay_cnt_do_not_become_zero(self) -> None:
        self.write_payments([
            ["0001", "0002", "30", "2"],
            ["0001", "0003", "20", ""],
            ["0001", "missing", "bad", "9"],
        ])
        features = self.features()
        self.assertIsNone(features.metrics["external_outgoing_payment_count"])
        cohort = features.diagnostics["cohorts"]["external"]
        self.assertEqual(cohort["known_payment_count_on_valid_amounts"], 2)
        self.assertEqual(features.metrics["external_outgoing_amount"], 50)
        self.assertEqual(features.amount_row_coverage, Fraction(2, 3))

    def test_negative_fractional_and_nonfinite_counts_are_invalid(self):
        self.write_payments([
            ["0001", "0002", "1", count]
            for count in ("-1", "1.5", "Infinity", "bad")
        ])
        features = self.features()
        self.assertIsNone(features.metrics["external_outgoing_payment_count"])
        issues = features.diagnostics["cohorts"]["external"][
            "count_issues_on_all_records"
        ]
        self.assertEqual(issues, {
            "negative": 1, "fractional": 1, "non_finite": 1, "invalid": 1,
        })

    def test_all_invalid_amounts_produce_unknown_aggregate(self) -> None:
        self.write_payments([
            ["0001", "0002", amount, "1"]
            for amount in ("", "bad", "-1", "Infinity")
        ])
        features = self.features()
        self.assertIsNone(features.metrics["external_outgoing_amount"])
        self.assertIsNone(features.metrics["outgoing_amount"])
        self.assertIsNone(features.metrics["hhi_out"])
        self.assertEqual(features.metrics["external_recipient_count"], 0)
        issues = features.diagnostics["cohorts"]["external"]["amount_issues"]
        self.assertEqual(issues, {
            "missing": 1, "invalid": 1, "negative": 1, "non_finite": 1,
        })

    def test_zero_external_amount_produces_null_ratios(self) -> None:
        self.write_payments([
            ["0001", "0002", "0", "1"],
            ["0001", "0003", "0.0", "2"],
        ])
        features = self.features()
        self.assertEqual(features.metrics["external_outgoing_amount"], 0)
        self.assertEqual(
            features.metrics["external_outgoing_payment_count"], 3
        )
        self.assertEqual(features.metrics["external_recipient_count"], 2)
        for name in ("top1_share", "top5_share", "hhi_out",
                     "intragroup_share_out"):
            self.assertIsNone(features.metrics[name])
            self.assertEqual(features.undefined_reasons[name],
                             "zero_external_amount")

    def test_only_self_payments_have_no_external_recipients(self) -> None:
        self.write_payments([["0001", "0001", "10", "2"]])
        features = self.features()
        self.assertEqual(features.metrics["outgoing_amount"], 10)
        self.assertEqual(features.metrics["external_outgoing_amount"], 0)
        self.assertIsNone(features.metrics["counterparty_coverage"])
        self.assertIsNone(features.metrics["top1_share"])

    def test_missing_beneficiary_is_accounted_for_separately(self) -> None:
        self.write_payments([
            ["0001", "0002", "10", "1"],
            ["0001", "0001", "4", "1"],
            ["0001", "", "6", "2"],
        ])
        features = self.features()
        metrics = features.metrics
        self.assertEqual(metrics["outgoing_amount"], 20)
        self.assertEqual(metrics["external_outgoing_amount"], 10)
        self.assertEqual(metrics["self_payment_amount"], 4)
        self.assertEqual(metrics["unassigned_outgoing_amount"], 6)
        self.assertTrue(features.diagnostics["amount_conservation_verified"])
        self.assertEqual(metrics["outgoing_amount"], sum(
            metrics[key] for key in ("external_outgoing_amount",
                                    "self_payment_amount",
                                    "unassigned_outgoing_amount")
        ))

    def test_money_is_exact_beyond_decimal_context_and_float_precision(self):
        self.write_payments([
            ["0001", "0002", "9007199254740993.00000000000000000001", "1"],
            ["0001", "0002", "0.00000000000000000002", "1"],
        ])
        with localcontext() as context:
            context.prec = 2
            features = self.features()
        self.assertEqual(
            exact_decimal(features.metrics["external_outgoing_amount"]),
            "9007199254740993.00000000000000000003",
        )
        self.assertEqual(features.metrics["hhi_out"], 1)

    def test_hhi_does_not_reuse_display_rounded_shares(self) -> None:
        self.write_payments([
            ["0001", "0002", "1", "1"],
            ["0001", "0003", "2", "1"],
        ])
        features = self.features()
        self.assertEqual(features.metrics["top1_share"], Fraction(2, 3))
        self.assertEqual(features.metrics["hhi_out"], Fraction(5, 9))
        self.assertEqual(serialize_features(features)["exact_ratios"][
            "hhi_out"
        ], {"numerator": "5", "denominator": "9"})

    def test_identifier_leading_zeros_and_exact_matches_are_preserved(self):
        self.write_payments([
            ["0001", "0002", "10", "1"],
            ["1", "0002", "100", "10"],
            [" 0001", "0002", "1000", "100"],
        ])
        features = self.features()
        self.assertEqual(features.metrics["external_outgoing_amount"], 10)
        self.assertEqual(features.company_id, "0001")
        self.assertEqual(features.recipients[0].company_id, "0002")
        for company_id in (1, None, "", " "):
            with self.subTest(company_id=company_id):
                with self.assertRaises(ValueError):
                    self.features(company_id=company_id)

    def test_no_payment_rows_do_not_fabricate_evidence(self) -> None:
        features = self.features(company_id="absent")
        self.assertEqual(features.metrics["outgoing_amount"], 0)
        self.assertEqual(features.metrics["external_recipient_count"], 0)
        self.assertEqual(serialize_derived_metrics(features), [])

    def test_semicolon_quoting_and_cp1251_are_supported(self) -> None:
        self.write_csv(self.company_csv, self.company_header, [
            ["0001", "группа;1", "Компания"],
            ["0002", "группа;1", "Другая компания"],
        ], encoding="cp1251")
        self.write_payments([["0001", "0002", "1,25", "1"]])
        features = self.features()
        self.assertEqual(features.metrics["external_outgoing_amount"],
                         Fraction(5, 4))
        self.assertEqual(features.metrics["intragroup_share_out"], 1)
        self.assertEqual(features.sources["companies"]["encoding"], "cp1251")

    def test_bad_csv_structure_stops_instead_of_skipping_rows(self) -> None:
        self.write_payments([["0001", "0002"]])
        with self.assertRaises(ValueError):
            self.features()
        self.write_csv(self.payment_csv, ["PAYER", "PAY_AMT"], [])
        with self.assertRaises(ValueError):
            self.features()

    def test_serialized_metrics_match_existing_schema_and_real_inputs(self):
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
        features = self.features()
        derived = serialize_derived_metrics(features)
        self.assertEqual(len(derived), 6)
        for metric in derived:
            with self.subTest(metric=metric["name"]):
                validator.validate(metric)
                for evidence in metric["inputs"]:
                    path = (self.payment_csv if evidence["source"] ==
                            "payments" else self.company_csv)
                    with path.open(encoding="utf-8", newline="") as stream:
                        values = list(csv.DictReader(stream, delimiter=";"))
                    self.assertTrue(any(
                        row[evidence["ref"]] == evidence["value"]
                        for row in values
                    ))
        self.assertEqual(len({metric["id"] for metric in derived}), 6)
        self.assertEqual(derived, serialize_derived_metrics(features))
        self.assertTrue(all(metric["unit_dependency"] == "unit_free"
                            for metric in derived))

    def test_reports_preserve_exact_money_and_do_not_change_sources(self):
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.company_csv, self.payment_csv)
        }
        features = self.features()
        first = write_reports(features, self.root / "reports1")
        second = write_reports(self.features(), self.root / "reports2")
        self.assertEqual(first[0].read_bytes(), second[0].read_bytes())
        self.assertEqual(first[1].read_bytes(), second[1].read_bytes())
        report = json.loads(first[0].read_text(encoding="utf-8"))
        self.assertEqual(report["metrics"]["external_outgoing_amount"], "100")
        self.assertIsNone(report["semantics"]["year"])
        self.assertIsNone(report["semantics"]["end_date"])
        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in before
        }
        self.assertEqual(before, after)

    def test_cli_requires_explicit_additivity_and_generates_reports(self):
        with redirect_stdout(io.StringIO()):
            result = main([
                "--company-id", "0001", "--data-dir", str(self.root),
                "--output-dir", str(self.root / "reports"),
            ])
        self.assertEqual(result, 0)
        path = self.root / "reports/ml1a_payment_features_0001.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(report["metrics"]["external_outgoing_amount"])
        self.assertFalse(report["aggregation"]["additivity_assumed"])


if __name__ == "__main__":
    unittest.main()
