import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from math import log2
from pathlib import Path
from unittest.mock import patch

from ml_evaluation import (
    dcg, evaluate_dataset, evaluate_ranking, industry_only_rank,
    ndcg_at_k, precision_at_k, recall_at_k, validate_dataset,
)
from ml_evaluation.__main__ import main
from ml_evaluation.dataset import (
    canonical_sha256, freeze_context, freeze_event, load_dataset,
    resolve_judgment,
)
from ml_evaluation.demo import build_synthetic_dataset
from ml_evaluation.evidence import audit_ranked_evidence
from ml_evaluation.metrics import ideal_ranking
from ml_evaluation.reporting import render_markdown, write_reports
from ml_ranking import NewsEvent, rank_news
from ml_ranking.serialization import serialize_result


class MetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grades = {"A": 2, "B": 1, "C": 0}

    def test_perfect_ranking(self) -> None:
        result = evaluate_ranking(["A", "B", "C"], self.grades, 2)
        self.assertEqual(result["precision_at_k"], 1)
        self.assertEqual(result["recall_at_k"], 1)
        self.assertEqual(result["ndcg_at_k"], 1)

    def test_completely_reversed_ranking_manual_calculation(self) -> None:
        result = evaluate_ranking(["C", "B", "A"], self.grades, 3)
        expected = (1 / log2(3) + 3 / 2) / (3 + 1 / log2(3))
        self.assertAlmostEqual(result["ndcg_at_k"], expected)
        self.assertAlmostEqual(result["precision_at_k"], 2 / 3)
        self.assertEqual(result["recall_at_k"], 1)

    def test_irrelevant_top_position_lowers_precision_and_dcg(self) -> None:
        result = evaluate_ranking(["C", "A", "B"], self.grades, 2)
        self.assertEqual(result["precision_at_k"], 0.5)
        self.assertEqual(result["recall_at_k"], 0.5)
        self.assertAlmostEqual(
            result["ndcg_at_k"], (3 / log2(3)) / (3 + 1 / log2(3))
        )

    def test_missing_relevant_event_remains_in_recall_denominator(self):
        result = evaluate_ranking(["A"], self.grades, 2)
        self.assertEqual(result["recall_at_k"], 0.5)
        self.assertEqual(result["relevant_count"], 2)
        self.assertAlmostEqual(
            result["ndcg_at_k"], 3 / (3 + 1 / log2(3))
        )

    def test_fewer_than_k_results_use_exact_k_denominator(self) -> None:
        self.assertEqual(precision_at_k(["A"], self.grades, 5), 0.2)
        result = evaluate_ranking(["A"], {"A": 2}, 5)
        self.assertEqual(result["precision_at_k"], 0.2)
        self.assertEqual(result["recall_at_k"], 1)
        self.assertEqual(result["ndcg_at_k"], 1)
        self.assertEqual(result["returned_count"], 1)

    def test_empty_ranking_is_zero_when_relevant_pool_exists(self) -> None:
        result = evaluate_ranking([], self.grades)
        for key in ("precision_at_k", "recall_at_k", "ndcg_at_k"):
            self.assertEqual(result[key], 0)

    def test_empty_candidate_pool_is_not_applicable(self) -> None:
        result = evaluate_ranking([], {})
        for key in ("precision_at_k", "recall_at_k", "ndcg_at_k"):
            self.assertIsNone(result[key])
        self.assertEqual(result["not_applicable_reason"],
                         "empty_candidate_pool")

    def test_no_relevant_labels_have_no_recall_or_ndcg(self) -> None:
        result = evaluate_ranking(["A"], {"A": 0, "B": 0})
        self.assertEqual(result["precision_at_k"], 0)
        self.assertIsNone(result["recall_at_k"])
        self.assertIsNone(result["ndcg_at_k"])
        self.assertEqual(result["idcg_at_k"], 0)

    def test_threshold_is_explicit_and_does_not_change_graded_gain(self):
        first = evaluate_ranking(["B"], self.grades, 1, 1)
        strict = evaluate_ranking(["B"], self.grades, 1, 2)
        self.assertEqual(first["precision_at_k"], 1)
        self.assertEqual(strict["precision_at_k"], 0)
        self.assertEqual(first["recall_at_k"], 0.5)
        self.assertEqual(strict["recall_at_k"], 0)
        self.assertEqual(first["ndcg_at_k"], strict["ndcg_at_k"])

    def test_partial_judgments_do_not_become_zero_or_get_filtered(self):
        result = evaluate_ranking(["A"], {"A": 2, "B": None}, 1)
        for key in ("precision_at_k", "recall_at_k", "ndcg_at_k"):
            self.assertIsNone(result[key])
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["judged_count"], 1)

    def test_duplicate_ranked_ids_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_ranking(["A", "A"], self.grades)

    def test_result_outside_candidate_pool_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_ranking(["UNKNOWN"], self.grades)

    def test_invalid_grades_and_options_are_rejected(self) -> None:
        for grade in (True, -1, 3, 1.5, "2"):
            with self.subTest(grade=grade), self.assertRaises(ValueError):
                evaluate_ranking([], {"A": grade})
        for k in (0, -1, True, 2.5):
            with self.subTest(k=k), self.assertRaises(ValueError):
                evaluate_ranking([], self.grades, k)
        with self.assertRaises(ValueError):
            evaluate_ranking([], self.grades, relevant_threshold=0)

    def test_manual_dcg_idcg_and_public_metric_functions(self) -> None:
        ideal = 3 + 1 / log2(3)
        self.assertAlmostEqual(dcg([2, 1, 0]), ideal)
        self.assertEqual(dcg([]), 0)
        result = evaluate_ranking(["B", "A"], self.grades, 2)
        self.assertAlmostEqual(result["dcg_at_k"], 1 + 3 / log2(3))
        self.assertAlmostEqual(result["idcg_at_k"], ideal)
        self.assertEqual(recall_at_k(["A", "B"], self.grades, 2), 1)
        self.assertEqual(ndcg_at_k(["A", "B"], self.grades, 2), 1)

    def test_ideal_ties_are_resolved_by_event_identity(self) -> None:
        self.assertEqual(
            ideal_ranking({"Z": 2, "B": 1, "A": 2}), ["A", "Z", "B"]
        )


class DatasetEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original = build_synthetic_dataset()

    def setUp(self) -> None:
        self.dataset = copy.deepcopy(self.original)
        self.view = self.dataset["contexts"][0]["ml_view"]

    def event(self, event_id: str) -> NewsEvent:
        return NewsEvent.from_dict(next(
            record["event"] for record in self.dataset["events"]
            if record["event"]["event_id"] == event_id
        ))

    def label(self, event_id="SYN_STEEL_REG", query_id="Q_S1") -> dict:
        return next(item for item in self.dataset["annotations"]
                    if item["event_id"] == event_id
                    and item["query_id"] == query_id)

    def add_conflicting_review(self) -> dict:
        label = copy.deepcopy(self.label())
        label["reviewer_id"] = "SECOND_SYNTHETIC_REVIEWER"
        label["relevance_grade"] = 1
        label["relevance_reason"] = "Учебное независимое несогласие"
        self.dataset["annotations"].append(label)
        return label

    def test_industry_baseline_uses_only_industry_and_publication(self):
        events = [self.event(key) for key in (
            "SYN_STEEL_REG", "SYN_STEEL_TAX", "SYN_STEEL_INPUT", "SYN_RATE"
        )]
        expected = ["SYN_STEEL_INPUT", "SYN_STEEL_REG", "SYN_STEEL_TAX"]
        self.assertEqual(industry_only_rank(self.view, events), expected)
        changed = copy.deepcopy(self.view)
        changed["numeric_features"] = []
        changed["hooks"]["relevance_priors"] = []
        changed["hooks"]["matching_profile"]["factor_classes_weighted"] = {}
        self.assertEqual(industry_only_rank(changed, events), expected)

    def test_industry_baseline_has_stable_date_and_id_tiebreaks(self):
        event = self.event("SYN_STEEL_INPUT")
        events = [replace(event, event_id="Z"), replace(event, event_id="A"),
                  replace(event, event_id="MISSING", published_at=None)]
        expected = ["A", "Z", "MISSING"]
        self.assertEqual(industry_only_rank(self.view, events), expected)
        self.assertEqual(
            industry_only_rank(self.view, reversed(events)), expected
        )

    def test_industry_matching_is_directional(self) -> None:
        event = self.event("SYN_STEEL_INPUT")
        broad = replace(event, event_id="BROAD", okved_codes=("24",))
        self.assertEqual(industry_only_rank(self.view, [broad]), ["BROAD"])
        self.view["hooks"]["matching_profile"]["okved_codes"] = ["24"]
        self.assertEqual(industry_only_rank(self.view, [event]), [])

    def test_baseline_can_match_metadata_without_article_content(self):
        event = self.event("SYN_METADATA_ONLY")
        self.assertEqual(
            industry_only_rank(self.view, [event]), ["SYN_METADATA_ONLY"]
        )

    def test_baseline_rejects_duplicate_events(self) -> None:
        event = self.event("SYN_STEEL_INPUT")
        with self.assertRaises(ValueError):
            industry_only_rank(self.view, [event, event])

    def test_changed_news_hash_is_rejected(self) -> None:
        self.dataset["events"][0]["event"]["title"] = "Изменено"
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_changed_profile_snapshot_is_rejected(self) -> None:
        context = self.dataset["contexts"][0]
        context["company_profile"]["facts"][0]["statement"] += " Изменено"
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_profile_version_must_have_one_frozen_content(self) -> None:
        context = copy.deepcopy(self.dataset["contexts"][0])
        context["company_profile"]["facts"][0]["statement"] += " Изменено"
        self.dataset["contexts"].append(freeze_context(
            "OTHER_CONTEXT", context["ml_view"], context["company_profile"],
            "Synthetic second snapshot with the same version",
        ))
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_duplicate_company_event_pair_is_rejected(self) -> None:
        self.dataset["queries"][0]["candidate_event_ids"].append(
            "SYN_STEEL_REG"
        )
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_duplicate_query_context_is_rejected(self) -> None:
        duplicate = copy.deepcopy(self.dataset["queries"][0])
        duplicate["query_id"] = "DUPLICATE"
        self.dataset["queries"].append(duplicate)
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_duplicate_reviewer_pair_is_rejected(self) -> None:
        self.dataset["annotations"].append(copy.deepcopy(self.label()))
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_conflicting_reviewers_are_preserved_and_block_query_metrics(self):
        self.add_conflicting_review()
        report = evaluate_dataset(self.dataset)
        query = report["queries"][0]
        judgment = query["judgments"]["SYN_STEEL_REG"]
        self.assertEqual(judgment["status"], "reviewer_conflict")
        self.assertEqual(len(judgment["annotations"]), 2)
        self.assertIsNone(judgment["grade"])
        self.assertIsNone(query["metrics"]["personalized"]["precision_at_k"])
        self.assertIsNone(query["metrics"]["industry_only"]["precision_at_k"])
        self.assertEqual(report["coverage"]["eligible_queries"], 1)

    def test_adjudication_resolves_and_preserves_explicit_reviews(self):
        self.add_conflicting_review()
        decision = copy.deepcopy(self.label())
        decision.update({
            "reviewer_id": "SYNTHETIC_ADJUDICATOR",
            "annotation_status": "adjudicated", "relevance_grade": 1,
            "adjudicates": ["SYNTHETIC_EXPECTATION_AUTHOR",
                            "SECOND_SYNTHETIC_REVIEWER"],
        })
        self.dataset["annotations"].append(decision)
        validate_dataset(self.dataset)
        judgment = resolve_judgment(
            self.dataset["annotations"], "Q_S1", "SYN_STEEL_REG"
        )
        self.assertEqual(judgment["grade"], 1)
        self.assertEqual(judgment["status"], "adjudicated")
        self.assertEqual(len(judgment["annotations"]), 3)

    def test_invalid_adjudication_is_rejected(self) -> None:
        label = self.label()
        label["annotation_status"] = "adjudicated"
        label["adjudicates"] = ["MISSING_A", "MISSING_B"]
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_pending_grade_is_not_used_even_if_present(self) -> None:
        self.label()["annotation_status"] = "pending"
        report = evaluate_dataset(self.dataset)
        query = report["queries"][0]
        self.assertIsNone(query["judgments"]["SYN_STEEL_REG"]["grade"])
        self.assertEqual(report["coverage"]["resolved_judgments"], 17)

    def test_positive_label_missing_evidence_is_reported_not_invented(self):
        self.label()["supporting_evidence_refs"] = []
        report = evaluate_dataset(self.dataset)
        query = report["queries"][0]
        self.assertIsNone(query["judgments"]["SYN_STEEL_REG"]["grade"])
        self.assertTrue(any(item["reason"] == "positive_label_without_evidence"
                            for item in report["missing_evidence"]))

    def test_fabricated_evidence_reference_is_rejected(self) -> None:
        self.label()["supporting_evidence_refs"][0]["evidence_id"] = "FAKE"
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_labels_must_match_company_focus_and_query_pool(self) -> None:
        for field, value in (("company_id", "UNKNOWN"),
                             ("analytic_focus", "CFO"),
                             ("event_id", "UNKNOWN")):
            with self.subTest(field=field):
                data = copy.deepcopy(self.original)
                data["annotations"][0][field] = value
                with self.assertRaises(ValueError):
                    validate_dataset(data)

    def test_reviewed_positive_label_requires_reason_and_mechanism(self):
        for field in ("relevance_reason", "impact_mechanism"):
            with self.subTest(field=field):
                data = copy.deepcopy(self.original)
                data["annotations"][0][field] = None
                with self.assertRaises(ValueError):
                    validate_dataset(data)

    def test_ranker_generated_ground_truth_provenance_is_rejected(self):
        self.dataset["labeling_provenance"]["method"] = "ranker_output"
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_same_event_cannot_be_in_development_and_held_out_queries(self):
        self.dataset["queries"][1]["split"] = "held_out"
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_related_articles_cannot_cross_splits(self) -> None:
        record = self.dataset["events"][0]
        event = replace(self.event("SYN_STEEL_REG"), event_id="RELATED",
                        content=self.event("SYN_STEEL_REG").content + " Иное.")
        self.dataset["events"].append(freeze_event(
            event, record["event_group_id"], "held_out"
        ))
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_duplicate_text_cannot_evade_split_check_with_another_id(self):
        event = replace(self.event("SYN_STEEL_REG"), event_id="ALIAS")
        self.dataset["events"].append(freeze_event(
            event, "DIFFERENT_GROUP", "held_out"
        ))
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_evaluation_splits_are_explicitly_isolated(self) -> None:
        event = NewsEvent(
            "HELD_EVENT", "Учебное другое событие", "SYNTHETIC",
            "https://example.invalid/held", "other", synthetic=True,
            content="Отдельный учебный текст для проверки изоляции.",
        )
        self.dataset["events"].append(freeze_event(
            event, "HELD_GROUP", "held_out"
        ))
        query = copy.deepcopy(self.dataset["queries"][0])
        query.update({"query_id": "Q_HELD", "split": "held_out",
                      "candidate_event_ids": ["HELD_EVENT"]})
        self.dataset["queries"].append(query)
        label = copy.deepcopy(self.label("SYN_LOCAL_KEYWORD"))
        label.update({"query_id": "Q_HELD", "event_id": "HELD_EVENT"})
        self.dataset["annotations"].append(label)
        development = evaluate_dataset(self.dataset)
        held = evaluate_dataset(self.dataset, split="held_out")
        self.assertEqual(len(development["queries"]), 2)
        self.assertNotIn("HELD_EVENT", development["event_manifest"])
        self.assertEqual([item["query_id"] for item in held["queries"]],
                         ["Q_HELD"])
        self.assertEqual(held["counts"]["unique_events"], 1)

    def test_future_profile_or_article_is_rejected(self) -> None:
        self.dataset["queries"][0]["as_of"] = "2020-01-01T00:00:00+03:00"
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)
        self.dataset = copy.deepcopy(self.original)
        self.dataset["queries"][0]["as_of"] = "2026-10-20T00:00:00+03:00"
        with self.assertRaises(ValueError):
            validate_dataset(self.dataset)

    def test_cfo_ceo_labels_are_kept_as_separate_judgments(self) -> None:
        query = self.dataset["queries"][0]
        query["analytic_focus"] = "CFO"
        query["candidate_event_ids"] = ["SYN_STEEL_REG", "SYN_STEEL_TAX"]
        other = copy.deepcopy(query)
        other.update({"query_id": "Q_CEO", "analytic_focus": "CEO"})
        self.dataset["queries"] = [query, other]
        cfo_reg = copy.deepcopy(self.label())
        cfo_tax = copy.deepcopy(self.label("SYN_STEEL_TAX"))
        labels = []
        for focus, query_id, grades in (
            ("CFO", "Q_S1", (1, 2)), ("CEO", "Q_CEO", (2, 1))
        ):
            for source, grade in zip((cfo_reg, cfo_tax), grades):
                label = copy.deepcopy(source)
                label.update({"analytic_focus": focus, "query_id": query_id,
                              "relevance_grade": grade})
                labels.append(label)
        self.dataset["annotations"] = labels
        report = evaluate_dataset(self.dataset)
        queries = {item["analytic_focus"]: item for item in report["queries"]}
        self.assertEqual(
            queries["CFO"]["judgments"]["SYN_STEEL_REG"]["grade"], 1
        )
        self.assertEqual(
            queries["CEO"]["judgments"]["SYN_STEEL_REG"]["grade"], 2
        )
        self.assertEqual(report["counts"]["company_event_pairs"], 2)
        self.assertEqual(report["counts"]["company_event_context_pairs"], 4)

    def test_both_algorithms_receive_the_same_unfiltered_frozen_pool(self):
        self.label()["annotation_status"] = "pending"
        received = {"personalized": [], "industry_only": []}

        def personalized(view, events, **options):
            received["personalized"].append(tuple(e.event_id for e in events))
            return rank_news(view, events, **options)

        def baseline(view, events, **options):
            received["industry_only"].append(tuple(e.event_id for e in events))
            return industry_only_rank(view, events, **options)

        with patch("ml_evaluation.runner.rank_news", side_effect=personalized):
            with patch("ml_evaluation.runner.industry_only_rank",
                       side_effect=baseline):
                evaluate_dataset(self.dataset)
        self.assertEqual(received["personalized"], received["industry_only"])
        self.assertTrue(all(len(pool) == 9
                            for pool in received["personalized"]))

    def test_demo_counts_coverage_and_macro_metrics(self) -> None:
        report = evaluate_dataset(self.dataset)
        self.assertEqual(report["counts"]["companies"], 2)
        self.assertEqual(report["counts"]["unique_events"], 9)
        self.assertEqual(report["counts"]["evaluated_company_event_pairs"], 18)
        self.assertEqual(report["label_distribution"],
                         {"0": 11, "1": 5, "2": 2})
        self.assertEqual(report["coverage"]["judgment_fraction"], 1)
        metrics = report["aggregate"]["personalized"]
        self.assertAlmostEqual(metrics["precision_at_k"]["macro_mean"], 0.6)
        self.assertAlmostEqual(metrics["recall_at_k"]["macro_mean"], 0.9)
        self.assertEqual(metrics["ndcg_at_k"]["applicable_query_count"], 2)
        self.assertEqual(set(report["per_company"]), {"U_T1", "U_T3"})

    def test_false_positives_negatives_and_missing_content_are_reported(self):
        report = evaluate_dataset(self.dataset)
        query = report["queries"][0]
        self.assertIn("EVAL_LABOR",
                      query["examples"]["personalized"]["false_negatives"])
        self.assertIn("SYN_METADATA_ONLY",
                      query["examples"]["industry_only"]["false_positives"])
        self.assertTrue(any(item["event_id"] == "SYN_METADATA_ONLY"
                            for item in report["missing_evidence"]))
        self.assertTrue(query["ranking_disagreements"])

    def test_evaluation_is_deterministic_and_does_not_mutate_inputs(self):
        before = copy.deepcopy(self.dataset)
        first = evaluate_dataset(self.dataset)
        self.assertEqual(first, evaluate_dataset(self.dataset))
        self.assertEqual(self.dataset, before)
        self.assertEqual(first["dataset_sha256"], canonical_sha256(before))

    def test_report_is_reproducible_and_preserves_machine_readable_null(self):
        report = evaluate_dataset(self.dataset)
        with tempfile.TemporaryDirectory() as directory:
            paths = write_reports(report, directory)
            contents = [path.read_bytes() for path in paths]
            write_reports(report, directory)
            self.assertEqual(contents, [path.read_bytes() for path in paths])
            self.assertEqual(json.loads(contents[0]), report)
            self.assertIn("СИНТЕТИЧЕСКАЯ ДЕМОНСТРАЦИЯ", contents[1].decode())

    def test_blank_real_template_is_awaiting_annotation_with_na_metrics(self):
        path = Path(__file__).resolve().parent.parent / (
            "ml_evaluation/templates/real_dataset.json"
        )
        report = evaluate_dataset(load_dataset(path), split="held_out")
        self.assertEqual(report["evaluation_status"],
                         "awaiting_independent_annotation")
        self.assertEqual(report["counts"]["unique_events"], 0)
        for algorithm in ("personalized", "industry_only"):
            self.assertIsNone(
                report["aggregate"][algorithm]["ndcg_at_k"]["macro_mean"]
            )
        self.assertIn("N/A", render_markdown(report))

    def test_cli_evaluates_blank_real_template_without_inventing_data(self):
        path = Path(__file__).resolve().parent.parent / (
            "ml_evaluation/templates/real_dataset.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch("builtins.print"):
                code = main([
                    "--dataset", str(path), "--output-dir", directory
                ])
            self.assertEqual(code, 0)
            report = json.loads((Path(directory) / (
                "ml3a_news_ranking_evaluation.json"
            )).read_text(encoding="utf-8"))
            self.assertEqual(report["counts"]["companies"], 0)

    def test_existing_explanation_references_can_be_verified(self) -> None:
        context = self.dataset["contexts"][0]
        event = self.event("SYN_STEEL_REG")
        result = serialize_result(rank_news(
            self.view, [event], company_profile=context["company_profile"],
            as_of=datetime.fromisoformat("2026-10-25T12:00:00+03:00"),
        )[0])
        payload = next(item["event"] for item in self.dataset["events"]
                       if item["event"]["event_id"] == event.event_id)
        self.assertEqual(audit_ranked_evidence(result, payload, context), [])
        result["evidence"][0]["quote"] = "Выдуманная цитата"
        self.assertTrue(audit_ranked_evidence(result, payload, context))

    def test_analysis_timezone_preserves_calendar_deadline_semantics(self):
        self.dataset["queries"][0]["as_of"] = "2026-10-28T00:30:00+03:00"
        report = evaluate_dataset(self.dataset)
        prediction = next(
            item for item in report["queries"][0]["personalized_explanations"]
            if item["event_id"] == "SYN_STEEL_REG"
        )
        self.assertEqual(
            prediction["ranking_factors"]["days_until_deadline"], 0
        )


if __name__ == "__main__":
    unittest.main()
