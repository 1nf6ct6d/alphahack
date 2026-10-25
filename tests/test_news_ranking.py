import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ml_ranking import ArticleEvidence, NewsEvent, classify_event, rank_news
from ml_ranking.demo import load_demo_events, run_demo
from ml_ranking.serialization import serialize_ranking, serialize_result


class NewsRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent
        self.view, self.profile = self.scenario(1)
        self.events = load_demo_events()
        self.as_of = datetime.fromisoformat("2026-10-25T12:00:00+03:00")

    def scenario(self, number: int) -> tuple[dict, dict]:
        folder = self.root / "tests/fixtures/scenarios"
        return tuple(json.loads((folder / filename).read_text(
            encoding="utf-8"
        )) for filename in (
            f"s{number}_MLView.json", f"s{number}_expected_profile.json"
        ))

    def event(self, identifier: str) -> NewsEvent:
        return next(
            event for event in self.events if event.event_id == identifier
        )

    def classify(self, identifier: str, with_profile: bool = True):
        return classify_event(
            self.view, self.event(identifier),
            company_profile=self.profile if with_profile else None,
            as_of=self.as_of,
        )

    def rank(self, events=None, with_profile: bool = True, **options):
        return rank_news(
            self.view, self.events if events is None else events,
            company_profile=self.profile if with_profile else None,
            as_of=self.as_of, **options,
        )

    def update_hooks(self) -> None:
        self.profile["downstream_hooks"] = copy.deepcopy(self.view["hooks"])

    def counterparty_event(self, identifier: str) -> NewsEvent:
        content = f"Учебное событие: деятельность {identifier} остановлена."
        return NewsEvent(
            "SYN_COUNTERPARTY", "Учебная остановка деятельности",
            "SYNTHETIC_EDITORIAL", "https://example.invalid/counterparty",
            "counterparty_disruption", content=content,
            factor_classes=("counterparty_credit",), entities=(identifier,),
            evidence=(ArticleEvidence(
                "QUOTE_COUNTERPARTY", content,
                ("event_type", "factor_classes", "entities"),
            ),), synthetic=True,
        )

    def test_same_news_has_different_relevance_for_two_companies(self):
        it_view, it_profile = self.scenario(3)
        steel = self.classify("SYN_STEEL_REG")
        software = classify_event(
            it_view, steel.event, company_profile=it_profile, as_of=self.as_of
        )
        self.assertEqual(steel.relevance, "grounded")
        self.assertEqual(software.relevance, "none")
        self.assertEqual(software.event, steel.event)

    def test_verified_direct_industry_rule_is_grounded_with_real_fact(self):
        result = self.classify("SYN_STEEL_REG")
        self.assertEqual(result.relevance, "grounded")
        self.assertEqual(result.priority, "high")
        self.assertEqual(result.ranking_factors["days_until_deadline"], 3)
        references = [item for item in result.evidence
                      if item["kind"] == "company_statement"]
        self.assertEqual(references[0]["statement_id"],
                         self.profile["facts"][0]["id"])
        self.assertEqual(references[0]["epistemic_status"], "FACT")

    def test_mlview_without_full_profile_remains_conditional(self) -> None:
        result = self.classify("SYN_STEEL_REG", with_profile=False)
        self.assertEqual(result.relevance, "conditional")
        self.assertNotEqual(result.priority, "high")
        self.assertFalse(any(item["kind"] == "company_statement"
                             for item in result.evidence))
        self.assertTrue(any(item["kind"] == "ml_view_field"
                            for item in result.evidence))

    def test_input_cost_mechanism_requires_unknown_business_conditions(self):
        result = self.classify("SYN_STEEL_INPUT")
        self.assertEqual(result.relevance, "conditional")
        self.assertIn("если компания использует", result.impact_mechanism)
        self.assertTrue(any("условия закупок неизвестны" in condition
                            for condition in result.conditions))

    def test_missing_industry_is_not_inferred_from_keywords(self) -> None:
        self.view["hooks"]["matching_profile"]["okved_codes"] = []
        self.update_hooks()
        self.assertEqual(self.classify("SYN_STEEL_REG").relevance, "none")
        self.assertEqual(self.classify("SYN_STEEL_INPUT").relevance, "none")

    def test_missing_region_does_not_remove_supported_industry_relevance(self):
        self.view["hooks"]["matching_profile"]["regions"] = []
        self.update_hooks()
        self.assertEqual(self.classify("SYN_STEEL_REG").relevance, "grounded")

    def test_missing_factor_weights_and_priors_do_not_block_evidence(self):
        self.view["hooks"]["matching_profile"]["factor_classes_weighted"] = {}
        self.view["hooks"]["relevance_priors"] = []
        self.update_hooks()
        self.assertEqual(self.classify("SYN_STEEL_REG").relevance, "grounded")
        self.assertEqual(self.classify("SYN_RATE").relevance, "conditional")

    def test_prior_and_keywords_alone_do_not_prove_credit_exposure(self):
        self.assertEqual(self.classify("SYN_RATE", False).relevance, "none")
        self.profile["facts"] = [fact for fact in self.profile["facts"]
                                 if fact["category"] != "bank_products"]
        self.assertEqual(self.classify("SYN_RATE").relevance, "none")

    def test_financing_conditions_are_not_invented_from_product_flag(self):
        result = self.classify("SYN_RATE")
        self.assertEqual(result.relevance, "conditional")
        self.assertTrue(any("тип ставки" in condition
                            for condition in result.conditions))
        self.assertNotIn("увеличится", result.impact_mechanism)
        self.assertNotEqual(result.priority, "high")

    def test_keyword_and_registration_region_are_not_enough(self) -> None:
        self.assertEqual(self.classify("SYN_LOCAL_KEYWORD").relevance, "none")

    def test_metadata_without_article_content_cannot_support_impact(self):
        result = self.classify("SYN_METADATA_ONLY")
        self.assertEqual(result.relevance, "none")
        self.assertIsNone(result.impact_mechanism)
        self.assertEqual(self.rank([result.event]), [])

    def test_fabricated_article_quote_is_rejected(self) -> None:
        event = replace(self.event("SYN_STEEL_REG"), content="Другой текст.")
        with self.assertRaises(ValueError):
            self.rank([event])

    def test_duplicate_event_ids_are_rejected_without_arbitrary_choice(self):
        event = self.event("SYN_STEEL_REG")
        with self.assertRaises(ValueError):
            self.rank([event, replace(event, title="Другая версия")])

    def test_unknown_publication_date_sorts_after_known_date(self) -> None:
        event = self.event("SYN_STEEL_INPUT")
        missing = replace(event, event_id="A_MISSING", published_at=None)
        known = replace(event, event_id="Z_KNOWN")
        result = self.rank([missing, known])
        self.assertEqual([item.event_id for item in result],
                         ["Z_KNOWN", "A_MISSING"])
        self.assertIsNone(serialize_result(result[1])["published_at"])

    def test_cfo_and_ceo_use_supported_mechanisms_without_fact_changes(self):
        regulation = replace(self.event("SYN_STEEL_REG"), deadline=None)
        tax = self.event("SYN_STEEL_TAX")
        cfo = self.rank([regulation, tax], analytic_focus="CFO")
        ceo = self.rank([regulation, tax], analytic_focus="CEO")
        self.assertEqual(cfo[0].event_id, "SYN_STEEL_TAX")
        self.assertEqual(ceo[0].event_id, "SYN_STEEL_REG")
        by_id = {item.event_id: item for item in cfo}
        for item in ceo:
            other = by_id[item.event_id]
            self.assertEqual(item.event, other.event)
            self.assertEqual(item.evidence, other.evidence)
            self.assertEqual(item.relevance, other.relevance)
            self.assertEqual(item.priority, other.priority)

    def test_focus_does_not_manufacture_difference_without_verified_data(self):
        events = [self.event("SYN_STEEL_REG"), self.event("SYN_STEEL_TAX")]
        cfo = self.rank(events, with_profile=False, analytic_focus="CFO")
        ceo = self.rank(events, with_profile=False, analytic_focus="CEO")
        self.assertEqual([item.event_id for item in cfo],
                         [item.event_id for item in ceo])
        self.assertFalse(any(item.ranking_factors["analytic_focus_applied"]
                             for item in cfo + ceo))

    def test_ranking_is_stable_under_repetition_and_input_permutations(self):
        first = serialize_ranking(self.rank())
        second = serialize_ranking(self.rank(list(reversed(self.events))))
        self.assertEqual(first, second)
        self.assertEqual(first, serialize_ranking(self.rank()))

    def test_no_relevant_events_returns_empty_list(self) -> None:
        self.assertEqual(self.rank([
            self.event("SYN_IT_REG"), self.event("SYN_LOCAL_KEYWORD"),
            self.event("SYN_METADATA_ONLY"),
        ]), [])

    def test_top_k_is_a_limit_and_not_a_required_result_count(self) -> None:
        self.assertEqual(len(self.rank(top_k=2)), 2)
        self.assertEqual(len(self.rank(top_k=20)), 4)
        self.assertEqual(self.rank(top_k=0), [])

    def test_source_and_evidence_references_are_preserved(self) -> None:
        result = self.classify("SYN_STEEL_REG")
        serialized = serialize_result(result)
        self.assertEqual(serialized["source_id"], result.event.source_id)
        self.assertEqual(serialized["source_url"], result.event.source_url)
        article = [item for item in serialized["evidence"]
                   if item["kind"] == "article_quote"]
        self.assertEqual(article[0]["evidence_id"],
                         result.event.evidence[0].evidence_id)
        self.assertEqual(article[0]["quote"], result.event.evidence[0].quote)
        self.assertTrue(all(item["statement_id"] in {
            fact["id"] for fact in self.profile["facts"]
        } for item in serialized["evidence"]
            if item["kind"] == "company_statement"))

    def test_unknown_counterparty_is_not_invented_from_concentration(self):
        event = self.counterparty_event("CP_UNKNOWN")
        result = classify_event(
            self.view, event, company_profile=self.profile, as_of=self.as_of
        )
        self.assertEqual(result.relevance, "none")

    def test_verified_payment_link_stays_conditional_not_supplier_fact(self):
        metric = self.profile["derived_metrics"][0]
        metric["breakdown"] = [{"key": "CP_KNOWN", "share": 0.55}]
        result = classify_event(
            self.view, self.counterparty_event("CP_KNOWN"),
            company_profile=self.profile, as_of=self.as_of,
        )
        self.assertEqual(result.relevance, "conditional")
        self.assertIn(
            "не доказывает отношения поставки", " ".join(result.conditions)
        )
        reference = next(item for item in result.evidence
                         if item["kind"] == "company_statement")
        self.assertEqual(reference["statement_id"], metric["id"])
        self.assertEqual(reference["epistemic_status"], "DERIVED")

    def test_mismatched_profiles_and_numeric_values_are_rejected(self):
        for change in ("company_id", "profile_id", "as_of_date", "metric"):
            with self.subTest(change=change):
                view, profile = self.scenario(1)
                if change == "metric":
                    view["numeric_features"][0]["value"] = 0.99
                elif change == "as_of_date":
                    profile["meta"][change] = "2026-10-19"
                else:
                    profile["meta"][change] = "different"
                with self.assertRaises(ValueError):
                    rank_news(view, self.events, company_profile=profile)

    def test_missing_or_incomplete_mlview_fails_contract_validation(self):
        for field in ("hooks", "numeric_features", "company_id", "usability"):
            with self.subTest(field=field):
                view = copy.deepcopy(self.view)
                del view[field]
                with self.assertRaises(ValueError):
                    rank_news(view, self.events)
        view = copy.deepcopy(self.view)
        del view["hooks"]["matching_profile"]["okved_codes"]
        with self.assertRaises(ValueError):
            rank_news(view, self.events)

    def test_weights_are_not_probabilities_or_relevance_evidence(self):
        before = [(item.event_id, item.relevance) for item in self.rank()]
        self.view["hooks"]["matching_profile"]["factor_classes_weighted"] = {
            "monetary_policy": 0.01, "commodity_price": 1.0,
        }
        for prior in self.view["hooks"]["relevance_priors"]:
            prior["weight"] = 0.0
        self.update_hooks()
        self.assertEqual(before, [(item.event_id, item.relevance)
                                  for item in self.rank()])
        self.view["weight_semantics"] = "probability"
        with self.assertRaises(ValueError):
            self.rank()

    def test_unusable_profile_returns_no_personalized_results(self):
        view, profile = self.scenario(5)
        self.assertEqual(
            rank_news(view, self.events, company_profile=profile), []
        )

    def test_zero_bank_flag_does_not_prove_debt_or_financing_need(self):
        for fact in self.profile["facts"]:
            for evidence in fact["evidence"]:
                if evidence["ref"] == "CL_PRODUCT_LOA_MARK":
                    evidence["value"] = 0
        states = self.profile["input_quality"]["field_states"]
        states["CL_PRODUCT_LOA_MARK"] = {
            "state": "confirmed_absent_in_scope", "scope": "bank"
        }
        self.assertEqual(self.classify("SYN_RATE").relevance, "none")

    def test_critical_company_conflict_blocks_grounded_relevance(self):
        self.profile["data_conflicts"].append({
            "id": "C-00000001", "rule_id": "CONF-OKVED-NAME-MISMATCH",
            "kind": "hard_contradiction", "severity": "critical",
            "fields": copy.deepcopy(self.profile["facts"][0]["evidence"]),
            "description": "Синтетический конфликт отрасли",
            "resolution": "preferred_primary_field",
            "plausible_explanations": ["Нужна проверка"],
        })
        result = self.classify("SYN_STEEL_REG")
        self.assertEqual(result.relevance, "conditional")
        self.assertNotEqual(result.priority, "high")

    def test_unverified_provenance_cannot_create_grounded_relevance(self):
        validation = self.profile["provenance"]["validation"]
        validation["evidence_refs_verified"] = False
        self.assertEqual(
            self.classify("SYN_STEEL_REG").relevance, "conditional"
        )

    def test_universal_and_mandatory_metadata_need_article_support(self):
        original = self.event("SYN_STEEL_REG")
        content = (
            "Некоторые организации с ОКВЭД 24.10 могут до 2026-10-28 "
            "подать сведения в реестр."
        )
        event = replace(original, content=content, evidence=(ArticleEvidence(
            "QUOTE_CONDITIONAL", content, original.evidence[0].supports
        ),))
        result = classify_event(
            self.view, event, company_profile=self.profile, as_of=self.as_of
        )
        self.assertEqual(result.relevance, "conditional")
        self.assertNotEqual(result.priority, "high")

    def test_unsupported_deadline_cannot_create_high_priority(self) -> None:
        event = replace(self.event("SYN_STEEL_REG"), deadline="2026-10-26")
        result = classify_event(
            self.view, event, company_profile=self.profile, as_of=self.as_of
        )
        self.assertEqual(result.relevance, "grounded")
        self.assertEqual(result.priority, "normal")
        self.assertFalse(result.ranking_factors["urgent_deadline_verified"])

    def test_past_deadline_is_not_an_approaching_deadline(self) -> None:
        result = classify_event(
            self.view, self.event("SYN_STEEL_REG"),
            company_profile=self.profile,
            as_of=datetime.fromisoformat("2026-11-05T12:00:00+03:00"),
        )
        self.assertEqual(result.priority, "normal")
        self.assertLess(result.ranking_factors["days_until_deadline"], 0)

    def test_no_analysis_clock_means_no_verified_urgency(self) -> None:
        result = classify_event(
            self.view, self.event("SYN_STEEL_REG"),
            company_profile=self.profile,
        )
        self.assertEqual(result.priority, "normal")

    def test_grounding_and_urgency_precede_recency(self) -> None:
        old = replace(self.event("SYN_STEEL_REG"),
                      published_at="2026-10-20T08:00:00+03:00")
        events = [
            self.event("SYN_STEEL_INPUT"), self.event("SYN_STEEL_TAX"), old
        ]
        result = self.rank(events)
        self.assertEqual(result[0].event_id, "SYN_STEEL_REG")
        self.assertEqual(result[1].event_id, "SYN_STEEL_TAX")

    def test_event_id_is_final_stable_tiebreaker(self) -> None:
        event = self.event("SYN_STEEL_INPUT")
        result = self.rank([
            replace(event, event_id="Z_EVENT"),
            replace(event, event_id="A_EVENT"),
        ])
        self.assertEqual([item.event_id for item in result],
                         ["A_EVENT", "Z_EVENT"])

    def test_unknown_dates_and_invalid_options_are_not_invented(self):
        event = replace(self.event("SYN_STEEL_INPUT"),
                        published_at="2026-10-24T12:00:00")
        with self.assertRaises(ValueError):
            self.rank([event])
        for top_k in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                self.rank(top_k=top_k)
        with self.assertRaises(ValueError):
            self.rank(analytic_focus="unknown")
        with self.assertRaises(ValueError):
            rank_news(self.view, self.events, as_of=datetime(2026, 10, 25))

    def test_constraints_and_input_facts_are_preserved(self) -> None:
        before = copy.deepcopy((self.view, self.profile))
        ranked = self.rank()
        self.assertEqual((self.view, self.profile), before)
        self.assertEqual(list(ranked[0].interpretation_constraints),
                         self.view["hooks"]["interpretation_constraints"])
        serialized = serialize_ranking(ranked)
        json.dumps(serialized, allow_nan=False)
        self.assertTrue(all(
            "relevance_score" not in item and "probability" not in item
            for item in serialized
        ))

    def test_demonstration_is_synthetic_and_reproducible(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="ml2a_demo_test_"
        )
        with temporary_directory as temporary:
            root = Path(temporary)
            first = run_demo(root / "first")
            second = run_demo(root / "second")
            self.assertEqual(first[0].read_bytes(), second[0].read_bytes())
            self.assertEqual(first[1].read_bytes(), second[1].read_bytes())
            report = json.loads(first[0].read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "synthetic_demonstration")
            self.assertTrue(all(
                event["synthetic"] for event in report["events"]
            ))
            self.assertEqual(len(report["companies"]), 2)
            self.assertEqual(len(report["companies"][0]["ranked"]), 4)
            self.assertEqual(len(report["companies"][1]["ranked"]), 2)

    def test_registration_does_not_verify_geographic_rule_applicability(self):
        event = replace(self.event("SYN_STEEL_REG"), geography=("REGION_1",))
        result = classify_event(
            self.view, event, company_profile=self.profile, as_of=self.as_of
        )
        self.assertEqual(result.relevance, "conditional")
        self.assertNotEqual(result.priority, "high")
        self.assertTrue(any("место регистрации" in condition
                            for condition in result.conditions))

    def test_derived_payment_caveats_are_preserved(self) -> None:
        metric = self.profile["derived_metrics"][0]
        metric["breakdown"] = [{"key": "CP_KNOWN", "share": 0.55}]
        metric["caveats"] = ["Аддитивность строк принята как допущение."]
        result = classify_event(
            self.view, self.counterparty_event("CP_KNOWN"),
            company_profile=self.profile, as_of=self.as_of,
        )
        self.assertEqual(result.relevance, "conditional")
        self.assertIn(metric["caveats"][0], result.limitations)

    def test_unknown_units_do_not_verify_unit_dependent_payment_basis(self):
        metric = self.profile["derived_metrics"][0]
        metric["breakdown"] = [{"key": "CP_KNOWN", "share": 0.55}]
        metric["unit_dependency"] = "requires_confirmed_unit"
        self.profile["input_quality"]["payments_semantics_status"] = "unknown"
        result = classify_event(
            self.view, self.counterparty_event("CP_KNOWN"),
            company_profile=self.profile, as_of=self.as_of,
        )
        self.assertEqual(result.relevance, "none")


if __name__ == "__main__":
    unittest.main()
