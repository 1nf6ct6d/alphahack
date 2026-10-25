import copy
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from examples.ml_library_usage import run_example
from ml_evaluation.dataset import freeze_context
from ml_evaluation.evidence import audit_ranked_evidence
from ml_features import (
    compute_payment_features, serialize_derived_metrics, serialize_features,
)
from ml_ranking import (
    ArticleEvidence, NewsEvent, rank_news, serialize_ranking,
)
from ml_ranking.validation import schema_validators


class CounterpartyBreakdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ml_final_unit_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.companies = self.root / "companies.csv"
        self.payments = self.root / "payments.csv"
        self.write(self.companies,
                   ["SYNTHETIC_U", "GROUP_ID", "REVENUE_COMPANY"], [
                       ["0001", "G", "2000"], ["0002", "G", "1000"],
                       ["U_428697", "G", "3000"],
                   ])
        self.write_payments([
            ["0001", "UNKNOWN", "70", "7"],
            ["0001", "0002", "30", "3"],
            ["0001", "0001", "500", "50"],
            ["0001", "", "20", "2"],
        ])

    def write(self, path: Path, header: list, rows: list) -> None:
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)

    def write_payments(self, rows: list) -> None:
        self.write(self.payments,
                   ["PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"], rows)

    def features(self, additive: bool = True):
        return compute_payment_features(
            "0001", self.companies, self.payments,
            assume_additive_rows=additive,
        )

    def metrics(self, features=None) -> dict:
        return {item["name"]: item for item in serialize_derived_metrics(
            features if features is not None else self.features()
        )}

    def test_unknown_recipients_and_correct_external_denominator(self):
        features = self.features()
        metrics = self.metrics(features)
        self.assertEqual(features.metrics["external_outgoing_amount"], 100)
        self.assertEqual(features.metrics["outgoing_amount"], 620)
        self.assertEqual(metrics["top1_counterparty_share_out"]["breakdown"],
                         [{"key": "UNKNOWN", "share": 0.7}])
        self.assertEqual(metrics["top5_counterparty_share_out"]["breakdown"],
                         [{"key": "UNKNOWN", "share": 0.7},
                          {"key": "0002", "share": 0.3}])
        self.assertEqual(features.metrics["counterparty_coverage"],
                         Fraction(1, 2))
        self.assertEqual(features.metrics["intragroup_share_lower_bound"],
                         Fraction(3, 10))
        self.assertEqual(features.metrics["intragroup_share_upper_bound"], 1)

    def test_hhi_breakdown_contains_payment_shares_not_squared_values(self):
        metric = self.metrics()["hhi_out"]
        self.assertAlmostEqual(metric["value"], 0.58)
        self.assertAlmostEqual(
            sum(row["share"] for row in metric["breakdown"]), 1
        )
        self.assertAlmostEqual(
            sum(row["share"] ** 2 for row in metric["breakdown"]),
            metric["value"],
        )
        self.assertEqual(metric["breakdown"][0]["share"], 0.7)

    def test_top5_truncates_without_renormalizing_and_hhi_keeps_tail(self):
        self.write_payments([
            ["0001", f"R{index:02}", str(index + 1), "1"]
            for index in range(7)
        ])
        metrics = self.metrics()
        top5 = metrics["top5_counterparty_share_out"]
        self.assertEqual(len(top5["breakdown"]), 5)
        self.assertAlmostEqual(sum(row["share"] for row in top5["breakdown"]),
                               25 / 28)
        self.assertAlmostEqual(top5["value"], 25 / 28)
        self.assertEqual(len(metrics["hhi_out"]["breakdown"]), 7)
        self.assertEqual(top5["breakdown"][0]["key"], "R06")

    def test_ties_are_resolved_by_preserved_string_identifier(self):
        self.write_payments([
            ["0001", "UNKNOWN", "10", "1"],
            ["0001", "0002", "10", "1"],
        ])
        self.assertEqual(
            self.metrics()["top1_counterparty_share_out"]["breakdown"],
            [{"key": "0002", "share": 0.5}],
        )

    def test_no_additivity_keeps_shares_null_and_breakdown_absent(self):
        metrics = self.metrics(self.features(False))
        for name in ("top1_counterparty_share_out",
                     "top5_counterparty_share_out", "hhi_out"):
            self.assertIsNone(metrics[name]["value"])
            self.assertNotIn("breakdown", metrics[name])

    def test_zero_or_invalid_amounts_do_not_invent_shares(self) -> None:
        for amount in ("0", "invalid", "-10"):
            with self.subTest(amount=amount):
                self.write_payments([["0001", "0002", amount, "1"]])
                for name, metric in self.metrics().items():
                    if name in {"top1_counterparty_share_out",
                                "top5_counterparty_share_out", "hhi_out"}:
                        self.assertIsNone(metric["value"])
                        self.assertNotIn("breakdown", metric)

    def test_technical_id_is_preserved_with_uncertain_diagnostics(self):
        self.write_payments([
            ["0001", "U_428697", "100", "1"],
            ["0001", "0002", "100", "1"],
        ])
        features = self.features()
        rows = serialize_features(features)["recipients"]
        technical = next(row for row in rows
                         if row["company_id"] == "U_428697")
        self.assertEqual(technical["profile_status"], "technical_ambiguous")
        self.assertEqual(technical["group_relation"], "unknown")
        self.assertIn({"key": "U_428697", "share": 0.5},
                      self.metrics(features)["hhi_out"]["breakdown"])
        self.assertIsNone(features.metrics["intragroup_share_out"])

    def test_partial_amount_quality_stays_low_with_separate_diagnostics(self):
        with self.payments.open("a", encoding="utf-8", newline="") as stream:
            csv.writer(stream, delimiter=";").writerow(
                ["0001", "0002", "invalid", "1"]
            )
        metric = self.metrics()["hhi_out"]
        self.assertEqual(metric["data_reliability"], "low")
        self.assertEqual(metric["breakdown"][0]["share"], 0.7)
        self.assertTrue(all(set(row) == {"key", "share"}
                            for row in metric["breakdown"]))

    def test_schema_evidence_and_source_files_are_preserved(self) -> None:
        before = {p: p.read_bytes() for p in (self.companies, self.payments)}
        validator = schema_validators()[0]["profile"].evolve(schema={
            "$ref": "https://bce.local/schemas/"
            "company_business_profile.v1.json#/$defs/DerivedMetric"
        })
        features = self.features()
        for metric in serialize_derived_metrics(features):
            validator.validate(metric)
            self.assertEqual(metric["inputs"][0], {
                "source": "payments", "ref": "PAYER", "value": "0001",
                "role": "supports",
            })
        self.assertNotIn("breakdown", self.metrics()["counterparty_count_out"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_inconsistent_cached_recipient_total_is_rejected(self) -> None:
        features = self.features()
        features.metrics["external_outgoing_amount"] = Fraction(200)
        with self.assertRaisesRegex(ValueError, "внешней суммой"):
            serialize_derived_metrics(features)

    def test_metric_identity_is_versioned_for_new_serialized_content(self):
        features = self.features()
        report = serialize_features(features)
        self.assertEqual(report["derived_metrics_version"], "ml1a-1.1.0")
        for metric in report["derived_metrics"]:
            old_identity = {
                "version": "ml1a-1.0.0", "company_id": "0001",
                "name": metric["name"], "additive": True,
                "source_hashes": {key: source["sha256"]
                                  for key, source in features.sources.items()},
            }
            old_id = "D-" + hashlib.sha256(json.dumps(
                old_identity, ensure_ascii=False, sort_keys=True
            ).encode()).hexdigest()[:8]
            self.assertNotEqual(metric["id"], old_id)


class EvidenceAuditRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parent.parent
        folder = root / "tests/fixtures/scenarios"
        view = json.loads((folder / "s1_MLView.json").read_text(
            encoding="utf-8"
        ))
        profile = json.loads((folder / "s1_expected_profile.json").read_text(
            encoding="utf-8"
        ))
        self.context = freeze_context(
            "FINAL_PROBE", view, profile, "Synthetic"
        )
        self.event = json.loads((
            root / "ml_ranking/examples/synthetic_events.json"
        ).read_text(encoding="utf-8"))[0]
        self.result = serialize_ranking(rank_news(
            view, [NewsEvent.from_dict(self.event)], company_profile=profile,
            as_of=datetime.fromisoformat("2026-10-25T12:00:00+03:00"),
        ))[0]

    def company_reference(self) -> dict:
        return next(row for row in self.result["evidence"]
                    if row["kind"] == "company_statement")

    def audit(self) -> list[str]:
        return audit_ranked_evidence(self.result, self.event, self.context)

    def test_original_references_pass(self) -> None:
        self.assertEqual(self.audit(), [])

    def test_tampered_company_profile_and_status_are_detected(self) -> None:
        self.company_reference().update({
            "company_id": "WRONG", "profile_id": "WRONG",
            "epistemic_status": "HYPOTHESIS",
        })
        self.assertTrue(self.audit())

    def test_hypothesis_cannot_masquerade_as_fact_for_grounded(self) -> None:
        hypothesis = self.context["company_profile"]["hypotheses"][0]
        self.company_reference().update({
            "statement_id": hypothesis["id"], "epistemic_status": "FACT",
            "inputs": copy.deepcopy(hypothesis["evidence"]),
            "caveats": list(hypothesis["caveats"]),
            "data_reliability": hypothesis["data_reliability"],
        })
        self.assertTrue(self.audit())

    def test_result_identity_cannot_be_replaced(self) -> None:
        self.result["company_id"] = "ANOTHER_COMPANY"
        self.result["event_id"] = "ANOTHER_EVENT"
        self.assertTrue(self.audit())

    def test_missing_statement_id_is_reported_without_key_error(self) -> None:
        del self.company_reference()["statement_id"]
        self.assertTrue(self.audit())

    def test_unverified_provenance_does_not_confirm_reference(self) -> None:
        self.context["company_profile"]["provenance"]["validation"][
            "evidence_refs_verified"
        ] = False
        self.assertTrue(self.audit())

    def test_quote_support_fields_must_match_source(self) -> None:
        self.result["evidence"][0]["supports"] = ["entities"]
        self.assertTrue(self.audit())

    def test_changed_publication_or_quote_absent_from_body_is_detected(self):
        self.result["published_at"] = "2026-10-25T12:00:00+03:00"
        self.assertTrue(self.audit())
        self.result["published_at"] = self.event["published_at"]
        self.event["content"] = "Другой текст без исходной цитаты."
        self.assertTrue(self.audit())

    def test_selected_mlview_code_cannot_be_replaced_or_marked_verified(self):
        context = freeze_context(
            "NO_PROFILE", self.context["ml_view"], None, "Synthetic"
        )
        result = serialize_ranking(rank_news(
            context["ml_view"], [NewsEvent.from_dict(self.event)],
            as_of=datetime.fromisoformat("2026-10-25T12:00:00+03:00"),
        ))[0]
        reference = next(row for row in result["evidence"]
                         if row["kind"] == "ml_view_field")
        reference["value"] = "62.01"
        self.assertTrue(audit_ranked_evidence(result, self.event, context))
        reference["value"] = "24.10"
        reference["verified_in_profile"] = True
        self.assertTrue(audit_ranked_evidence(result, self.event, context))


class PublicMlInterfaceTests(unittest.TestCase):
    def test_example_runs_twice_without_mixing_companies(self):
        first = run_example()
        self.assertEqual(first, run_example())
        self.assertNotEqual(first["feature_company_id"],
                            first["ranking_company_id"])
        self.assertEqual(first["payment_metrics"]["external_outgoing_amount"],
                         "100")
        self.assertEqual(len(first["derived_metrics"]), 7)
        self.assertEqual(len(first["ranked_candidates"]), 4)
        self.assertEqual(json.loads(json.dumps(first)), first)

    def test_public_serializers_are_same_functions_as_existing_imports(self):
        import ml_features
        import ml_features.group_serialization as group_serialization
        import ml_features.serialization as serialization
        import ml_ranking
        import ml_ranking.serialization as ranking_serialization
        self.assertIs(ml_features.serialize_derived_metrics,
                      serialization.serialize_derived_metrics)
        self.assertIs(ml_features.serialize_group_derived_metrics,
                      group_serialization.serialize_group_derived_metrics)
        self.assertIs(ml_ranking.serialize_ranking,
                      ranking_serialization.serialize_ranking)

    def test_unknown_factor_is_rejected_and_unsupported_factor_is_safe(self):
        root = Path(__file__).resolve().parent.parent
        view = json.loads((root / (
            "tests/fixtures/scenarios/s1_MLView.json"
        )).read_text(encoding="utf-8"))
        text = (
            "Учебное сообщение о технологии для ОКВЭД 24.10. "
            "Использование технологии компанией неизвестно."
        )
        event = NewsEvent(
            "SYN_TECHNOLOGY", "Учебная технология", "SYNTHETIC",
            "https://example.invalid/technology", "other", content=text,
            factor_classes=("technology",), okved_codes=("24.10",),
            evidence=(ArticleEvidence(
                "QUOTE_TECHNOLOGY", text,
                ("event_type", "factor_classes", "okved_codes"),
            ),), synthetic=True,
        )
        self.assertEqual(rank_news(view, [event]), [])
        with self.assertRaises(ValueError):
            rank_news(view, [replace(
                event, factor_classes=("UNKNOWN_FACTOR",)
            )])

    def test_four_packages_import_and_rank_without_prompt_engineer_files(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="ml_runtime_only_") as folder:
            destination = Path(folder)
            for name in ("ml_audit", "ml_features", "ml_ranking",
                         "ml_evaluation"):
                shutil.copytree(root / name, destination / name,
                                ignore=shutil.ignore_patterns("__pycache__"))
            (destination / "schemas").mkdir()
            for name in (
                "common.v1.schema.json", "profile_views.v1.schema.json",
                "company_business_profile.v1.schema.json",
            ):
                shutil.copyfile(root / "schemas" / name,
                                destination / "schemas" / name)
            env = dict(os.environ)
            env.pop("BCE_LLM_API_KEY", None)
            env.pop("PYTHONPATH", None)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            base = root / "tests/fixtures/scenarios"
            payload = {
                "view": json.loads((base / "s1_MLView.json").read_text(
                    encoding="utf-8"
                )),
                "profile": json.loads((
                    base / "s1_expected_profile.json"
                ).read_text(encoding="utf-8")),
                "event": json.loads((root / (
                    "ml_ranking/examples/synthetic_events.json"
                )).read_text(encoding="utf-8"))[0],
            }
            program = (
                "import importlib.util,json,sys; "
                "import ml_audit,ml_features,ml_ranking,ml_evaluation; "
                "from datetime import datetime; "
                "assert importlib.util.find_spec('bce_lab') is None; "
                "p=json.load(sys.stdin); "
                "r=ml_ranking.rank_news(p['view'],"
                "[ml_ranking.NewsEvent.from_dict(p['event'])],"
                "company_profile=p['profile'],"
                "as_of=datetime.fromisoformat('2026-10-25T12:00:00+03:00')); "
                "print(json.dumps(ml_ranking.serialize_ranking(r)))"
            )
            completed = subprocess.run(
                [sys.executable, "-B", "-c", program], cwd=destination,
                env=env, input=json.dumps(payload), text=True,
                encoding="utf-8", capture_output=True, timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result[0]["event_id"], "SYN_STEEL_REG")


if __name__ == "__main__":
    unittest.main()
