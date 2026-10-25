import copy
import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ml_audit import audit_data
from ml_audit.csv_io import DATASET_FILES, file_hash
from ml_evaluation import evaluate_dataset
from ml_evaluation.dataset import freeze_context
from ml_evaluation.demo import build_synthetic_dataset
from ml_evaluation.evidence import audit_ranked_evidence
from ml_features import compute_group_features, compute_payment_features
from ml_features.group_serialization import (
    serialize_group_derived_metrics, serialize_group_features,
)
from ml_features.serialization import (
    serialize_derived_metrics, serialize_features,
)
from ml_ranking import NewsEvent, classify_event, rank_news
from ml_ranking.serialization import serialize_ranking
from ml_ranking.validation import (
    schema_validators, validate_contract, validate_event,
)


class MlIntegrationSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parent.parent
        cls.as_of = datetime.fromisoformat("2026-10-25T12:00:00+03:00")
        cls.news_records = json.loads((
            cls.root / "ml_ranking/examples/synthetic_events.json"
        ).read_text(encoding="utf-8"))
        cls.safety_records = json.loads((
            cls.root / "tests/integration_data/news_safety_cases.json"
        ).read_text(encoding="utf-8"))

    def context(self, scenario: int, with_profile: bool = True) -> dict:
        folder = self.root / "tests/fixtures/scenarios"
        view = json.loads((folder / f"s{scenario}_MLView.json").read_text(
            encoding="utf-8"
        ))
        validate_contract(view, "view")
        profile = None
        if with_profile:
            profile = json.loads((
                folder / f"s{scenario}_expected_profile.json"
            ).read_text(encoding="utf-8"))
            validate_contract(profile, "profile")
        return freeze_context(
            f"SMOKE_S{scenario}", view, profile,
            f"Existing synthetic scenario {scenario}, offline integration",
        )

    def payload(
        self, context: dict, records: list[dict], focus: str | None = None,
    ) -> list[dict]:
        events = tuple(NewsEvent.from_dict(record) for record in records)
        for event in events:
            validate_event(event)
        results = rank_news(
            context["ml_view"], events,
            company_profile=context["company_profile"], analytic_focus=focus,
            as_of=self.as_of,
        )
        payload = serialize_ranking(results)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self.assertEqual(json.loads(encoded), payload)
        self.assertEqual([item["rank"] for item in payload],
                         list(range(1, len(payload) + 1)))
        self.assertLessEqual(len(payload), 5)
        by_id = {record["event_id"]: record for record in records}
        self.assertEqual(len({item["event_id"] for item in payload}),
                         len(payload))
        for item in payload:
            self.assertIn(item["event_id"], by_id)
            self.assertEqual(item["company_id"], context["company_id"])
            self.assertEqual(item["profile_id"], context["profile_version"])
            self.assertIn(item["relevance"], {"grounded", "conditional"})
            for field in ("relevance_explanation", "impact_mechanism"):
                self.assertTrue(item[field].strip())
            self.assertTrue(item["conditions"])
            self.assertTrue(item["limitations"])
            self.assertTrue(item["ranking_factors"])
            self.assertEqual(item["interpretation_constraints"],
                             context["ml_view"]["hooks"][
                                 "interpretation_constraints"
                             ])
            self.assertEqual(audit_ranked_evidence(
                item, by_id[item["event_id"]], context
            ), [])
        return payload

    def test_existing_profiles_news_ranking_json_and_evidence(self):
        steel = self.payload(self.context(1), self.news_records)
        software = self.payload(self.context(3), self.news_records)
        self.assertEqual(len(steel), 4)
        self.assertEqual(len(software), 2)
        self.assertEqual(steel[0]["event_id"], "SYN_STEEL_REG")
        self.assertEqual((steel[0]["relevance"], steel[0]["priority"]),
                         ("grounded", "high"))
        self.assertTrue(all(item["relevance"] == "conditional"
                            for item in software))
        self.assertNotIn("SYN_STEEL_REG",
                         {item["event_id"] for item in software})

    def test_json_output_is_deterministic_for_both_analytic_roles(self):
        context = self.context(1)
        before = copy.deepcopy((context, self.news_records))
        for focus in (None, "CFO", "CEO"):
            with self.subTest(focus=focus):
                first = self.payload(context, self.news_records, focus)
                second = self.payload(context, list(reversed(
                    self.news_records
                )), focus)
                self.assertEqual(
                    json.dumps(first, ensure_ascii=False, sort_keys=True),
                    json.dumps(second, ensure_ascii=False, sort_keys=True),
                )
                self.assertTrue(all(item["ranking_factors"]["analytic_focus"]
                                    == focus for item in first))
        self.assertEqual((context, self.news_records), before)

    def test_missing_full_profile_cannot_create_company_fact_or_high(self):
        payload = self.payload(self.context(1, False), self.news_records)
        self.assertTrue(payload)
        for item in payload:
            self.assertEqual((item["relevance"], item["priority"]),
                             ("conditional", "low"))
            self.assertFalse(any(reference["kind"] == "company_statement"
                                 for reference in item["evidence"]))

    def test_semantic_hotfix_survives_serialization_and_evidence_audit(self):
        context = self.context(1)
        payload = self.payload(context, self.safety_records)
        by_id = {item["event_id"]: item for item in payload}
        exemption = by_id["SMOKE_EXEMPTION"]
        self.assertEqual((exemption["relevance"], exemption["priority"]),
                         ("grounded", "normal"))
        semantic = exemption["ranking_factors"]["regulatory_semantics"]
        self.assertEqual(semantic["direction"], "exemption")
        self.assertEqual(semantic["mandatory_metadata_status"], "contradicted")
        self.assertTrue(semantic["normalized_claims"]["mandatory"])
        self.assertIn("снизить", exemption["impact_mechanism"])
        self.assertNotIn("SMOKE_UNSUPPORTED", by_id)
        disputed = by_id["SMOKE_CONTRADICTORY"]
        self.assertEqual((disputed["relevance"], disputed["priority"]),
                         ("conditional", "low"))
        self.assertFalse(any(item["priority"] == "high" for item in payload))

    def test_empty_relevant_ranking_is_valid_json(self) -> None:
        irrelevant = [record for record in self.news_records
                      if record["event_id"] == "SYN_LOCAL_KEYWORD"]
        self.assertEqual(self.payload(self.context(1), irrelevant), [])
        self.assertEqual(self.payload(self.context(5), self.news_records), [])

    def test_contract_errors_prevent_mixed_company_contexts(self) -> None:
        context = self.context(1)
        other = self.context(3)
        event = NewsEvent.from_dict(self.news_records[0])
        with self.assertRaises(ValueError):
            rank_news(context["ml_view"], [event],
                      company_profile=other["company_profile"])
        view = copy.deepcopy(context["ml_view"])
        del view["hooks"]
        with self.assertRaises(ValueError):
            rank_news(view, [event])

    def test_future_profile_is_rejected_by_both_public_ranking_functions(self):
        context = self.context(1)
        context["ml_view"]["as_of_date"] = "2026-10-30"
        context["company_profile"]["meta"]["as_of_date"] = "2026-10-30"
        validate_contract(context["ml_view"], "view")
        validate_contract(context["company_profile"], "profile")
        event = NewsEvent.from_dict(self.news_records[0])
        for with_profile in (True, False):
            for function, data in ((rank_news, [event]),
                                   (classify_event, event)):
                with self.subTest(function=function.__name__,
                                  with_profile=with_profile):
                    with self.assertRaisesRegex(ValueError, "позже"):
                        function(
                            context["ml_view"], data, as_of=self.as_of,
                            company_profile=context["company_profile"]
                            if with_profile else None,
                        )

    def test_profile_date_check_preserves_calendar_and_missing_clock(self):
        context = self.context(1)
        event = NewsEvent.from_dict(self.news_records[0])
        options = {"company_profile": context["company_profile"]}
        records = copy.deepcopy(self.news_records)
        for record in records:
            record["published_at"] = None
        results = rank_news(
            context["ml_view"], [NewsEvent.from_dict(r) for r in records],
            as_of=datetime.fromisoformat("2026-10-20T00:30:00+03:00"),
            **options,
        )
        self.assertTrue(results)
        classified = classify_event(
            context["ml_view"], NewsEvent.from_dict(records[0]),
            as_of=datetime.fromisoformat("2026-10-20T00:30:00+03:00"),
            **options,
        )
        self.assertEqual(classified.relevance, "grounded")
        self.assertEqual(classified.ranking_factors["days_until_deadline"], 8)
        without_clock = rank_news(context["ml_view"], [event], **options)
        self.assertTrue(without_clock)
        self.assertFalse(any(item.priority == "high"
                             for item in without_clock))

    def test_existing_evaluation_calls_ranker_on_the_same_frozen_inputs(self):
        dataset = build_synthetic_dataset()
        report = evaluate_dataset(dataset)
        contexts = {item["context_id"]: item for item in dataset["contexts"]}
        records = {item["event"]["event_id"]: item["event"]
                   for item in dataset["events"]}
        for query in report["queries"]:
            context = contexts[query["context_id"]]
            payload = self.payload(context, [
                records[key] for key in query["candidate_event_ids"]
            ])
            self.assertEqual(query["personalized_explanations"], payload)
        self.assertEqual(report["coverage"]["resolved_judgments"], 18)
        self.assertEqual(report["coverage"]["eligible_queries"], 2)


class FeatureIntegrationSmokeTests(unittest.TestCase):
    def test_audit_payments_groups_and_schema_without_profile_assembly(self):
        tables = {
            "companies": (["SYNTHETIC_U", "GROUP_ID", "REVENUE_COMPANY"], [
                ["SMOKE_001", "G_001", "2000"],
                ["SMOKE_002", "G_001", "1000"],
            ]),
            "payments": (["PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"], [
                ["SMOKE_001", "SMOKE_002", "30", "3"],
                ["SMOKE_001", "SMOKE_UNKNOWN", "70", "7"],
                ["SMOKE_001", "SMOKE_001", "10", "1"],
            ]),
            "links": (["SYNTHETIC_F", "SYNTHETIC_U", "CITY_NAME", "RC_NAME"],
                      [["SMOKE_F001", "SMOKE_001", "", ""]]),
        }
        with tempfile.TemporaryDirectory(prefix="ml_integration_") as folder:
            root = Path(folder)
            paths = {}
            for kind, (header, rows) in tables.items():
                path = root / DATASET_FILES[kind]
                with path.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream, delimiter=";")
                    writer.writerow(header)
                    writer.writerows(rows)
                paths[kind] = path
            before = {kind: file_hash(path) for kind, path in paths.items()}
            audit = audit_data(root)
            self.assertEqual(audit["status"], "complete")
            payments = compute_payment_features(
                "SMOKE_001", paths["companies"], paths["payments"],
                assume_additive_rows=True,
            )
            groups = compute_group_features(
                "SMOKE_001", paths["companies"], payment_features=payments
            )
            payment_payload = serialize_features(payments)
            group_payload = serialize_group_features(groups)
            self.assertIs(groups.payments, payments)
            self.assertEqual(groups.payment_mode, "reused")
            self.assertEqual(payment_payload["metrics"][
                "external_outgoing_amount"
            ], "100")
            self.assertEqual(payment_payload["metrics"]["top1_share"], 0.7)
            self.assertIsNone(group_payload["payments"]["metrics"][
                "intragroup_share_out"
            ])
            self.assertEqual(group_payload["group_membership"][
                "observed_confirmed_member_count"
            ], 2)
            self.assertIsNone(
                group_payload["confirmed_consolidated_group_revenue"]
            )
            metrics = serialize_derived_metrics(payments) + (
                serialize_group_derived_metrics(groups)
            )
            validator = schema_validators()[0]["profile"].evolve(schema={
                "$ref": "https://bce.local/schemas/"
                "company_business_profile.v1.json#/$defs/DerivedMetric"
            })
            self.assertEqual(len(metrics), 7)
            for metric in metrics:
                validator.validate(metric)
            encoded = json.dumps([payment_payload, group_payload],
                                 allow_nan=False)
            self.assertEqual(len(json.loads(encoded)), 2)
            self.assertEqual(before, {
                kind: file_hash(path) for kind, path in paths.items()
            })


if __name__ == "__main__":
    unittest.main()
