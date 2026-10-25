import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ml_ranking import ArticleEvidence, NewsEvent, classify_event, rank_news
from ml_ranking.demo import load_demo_events
from ml_ranking.semantics import verify_regulatory_event
from ml_ranking.serialization import serialize_ranking, serialize_result
from ml_ranking.validation import article_support


class RegulatorySemanticTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / "fixtures/scenarios"
        self.view = json.loads((base / "s1_MLView.json").read_text(
            encoding="utf-8"
        ))
        self.profile = json.loads((
            base / "s1_expected_profile.json"
        ).read_text(encoding="utf-8"))
        self.as_of = datetime.fromisoformat("2026-10-25T12:00:00+03:00")
        self.supports = (
            "event_type", "factor_classes", "okved_codes", "mandatory",
            "applies_to_all_in_scope", "deadline",
        )

    def event(
        self, text: str, *, mandatory: bool | None = True,
        identifier: str = "SYN_REGRESSION",
    ) -> NewsEvent:
        return NewsEvent(
            identifier, "Синтетическая проверка смысла нормы", "SYNTHETIC",
            "https://example.invalid/semantic-regression", "regulation_change",
            content=text, published_at="2026-10-24T12:00:00+03:00",
            factor_classes=("industry_regulation",), okved_codes=("24.10",),
            evidence=(ArticleEvidence(
                "QUOTE_REGRESSION", text, self.supports
            ),),
            mandatory=mandatory, applies_to_all_in_scope=True,
            deadline="2026-10-28", synthetic=True,
        )

    def classify(self, event: NewsEvent, *, with_profile: bool = True):
        return classify_event(
            self.view, event,
            company_profile=self.profile if with_profile else None,
            as_of=self.as_of,
        )

    def semantic_result(self, result) -> dict:
        return result.ranking_factors["regulatory_semantics"]

    def introduced(self) -> NewsEvent:
        return self.event(
            "Для всех организаций с ОКВЭД 24.10 введена новая обязанность "
            "до 2026-10-28 представить сведения в производственный реестр."
        )

    def exemption(self, *, mandatory: bool = False) -> NewsEvent:
        return self.event(
            "Все организации с ОКВЭД 24.10 освобождены от обязанности "
            "до 2026-10-28 подавать сведения в реестр.", mandatory=mandatory,
        )

    def test_introduced_obligation_retains_grounded_high(self) -> None:
        result = self.classify(self.introduced())
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "high"))
        semantics = self.semantic_result(result)
        self.assertEqual(semantics["direction"], "introduced")
        self.assertTrue(semantics["obligation_verified"])
        self.assertTrue(semantics["deadline_verified"])
        self.assertEqual(semantics["mandatory_metadata_status"], "supported")

    def test_existing_duty_does_not_invent_its_introduction(self) -> None:
        event = self.event(
            "Все организации с ОКВЭД 24.10 обязаны до 2026-10-28 "
            "представить сведения в реестр."
        )
        result = self.classify(event)
        self.assertEqual(self.semantic_result(result)["direction"], "required")
        self.assertEqual(result.priority, "high")
        self.assertNotIn("введение", result.explanation)

    def test_confirmed_exemption_keeps_reduction_mechanism(self) -> None:
        result = self.classify(self.exemption())
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "normal"))
        semantics = self.semantic_result(result)
        self.assertEqual(semantics["direction"], "exemption")
        self.assertFalse(semantics["obligation_verified"])
        self.assertFalse(semantics["deadline_verified"])
        self.assertIn("снизить регуляторную нагрузку", result.impact_mechanism)
        self.assertIn("освобождение", result.explanation)

    def test_cancellation_is_not_a_new_obligation(self) -> None:
        event = self.event(
            "Для всех организаций с ОКВЭД 24.10 отменена обязанность "
            "до 2026-10-28 представлять сведения в реестр.", mandatory=False,
        )
        result = self.classify(event)
        self.assertEqual(self.semantic_result(result)["direction"], "removed")
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "normal"))
        self.assertIn("Отмена", result.impact_mechanism)
        self.assertFalse(result.ranking_factors["urgent_deadline_verified"])

    def test_exemption_explanation_does_not_invent_absent_text_or_duty(self):
        result = self.classify(self.exemption(mandatory=True))
        self.assertFalse(any("нет момента анализа" in text
                             for text in result.limitations))
        self.assertFalse(any("исполнение требования" in text
                             for text in result.conditions))
        self.assertTrue(any("согласованности флагов" in text
                            for text in result.limitations))

    def test_strengthened_obligation_retains_meaning_and_urgency(self):
        event = self.event(
            "Для всех организаций с ОКВЭД 24.10 ужесточены требования: "
            "они обязаны до 2026-10-28 представить расширенные сведения."
        )
        result = self.classify(event)
        self.assertEqual(self.semantic_result(result)["direction"],
                         "tightened")
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "high"))
        self.assertIn("Усиление", result.impact_mechanism)

    def test_original_bug_flags_conflict_and_preserves_exemption(self):
        event = self.exemption(mandatory=True)
        result = self.classify(event)
        semantics = self.semantic_result(result)
        self.assertEqual(semantics["direction"], "exemption")
        self.assertEqual(semantics["mandatory_metadata_status"],
                         "contradicted")
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "normal"))
        self.assertIn("противоречат источнику", result.explanation)
        self.assertIn("снизить", result.impact_mechanism)
        self.assertTrue(result.event.mandatory)
        self.assertFalse(article_support(event, "mandatory"))
        self.assertFalse(article_support(event, "deadline", event.deadline))

    def test_false_mandatory_flag_does_not_hide_supported_obligation(self):
        event = replace(self.introduced(), mandatory=False)
        result = self.classify(event)
        semantics = self.semantic_result(result)
        self.assertEqual(semantics["direction"], "introduced")
        self.assertEqual(semantics["mandatory_metadata_status"],
                         "contradicted")
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "normal"))
        self.assertTrue(semantics["obligation_verified"])
        self.assertFalse(result.event.mandatory)
        self.assertFalse(result.ranking_factors["urgent_deadline_verified"])

    def test_missing_excerpts_cannot_confirm_obligation(self) -> None:
        event = replace(self.introduced(), evidence=())
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority), ("none", "low"))
        self.assertEqual(self.semantic_result(result)["evidence_status"],
                         "unverified")
        self.assertFalse(article_support(event, "mandatory"))

    def test_metadata_without_source_text_has_no_verified_direction(self):
        event = replace(self.introduced(), content=None, evidence=())
        verified = verify_regulatory_event(event)
        self.assertEqual(verified.direction, "unknown")
        self.assertFalse(verified.source_verified)
        self.assertEqual(self.classify(event).relevance, "none")

    def test_missing_support_for_mandatory_does_not_promote_metadata(self):
        original = self.introduced()
        evidence = replace(original.evidence[0], supports=tuple(
            field for field in self.supports if field != "mandatory"
        ))
        result = self.classify(replace(original, evidence=(evidence,)))
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))
        self.assertFalse(self.semantic_result(result)["obligation_verified"])

    def test_contradictory_excerpts_remain_uncertain(self):
        positive = self.introduced()
        exemption = self.exemption()
        event = replace(
            positive, content=positive.content + "\n\n" + exemption.content,
            evidence=(positive.evidence[0], replace(
                exemption.evidence[0], evidence_id="QUOTE_EXEMPTION"
            )),
        )
        result = self.classify(event)
        self.assertEqual(self.semantic_result(result)["direction"], "unknown")
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))
        self.assertIn("направление изменения", result.impact_mechanism)
        self.assertEqual(len([item for item in result.evidence
                              if item["kind"] == "article_quote"]), 2)

    def test_contradictory_body_is_not_ignored_outside_excerpts(self):
        positive = self.introduced()
        event = replace(positive, content=(
            positive.content + "\n\n" + self.exemption().content
        ))
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))
        self.assertEqual(self.semantic_result(result)["direction"], "unknown")
        self.assertEqual(
            self.semantic_result(result)["supporting_evidence_ids"],
            ["QUOTE_REGRESSION"],
        )

    def test_conditional_legal_language_cannot_confirm_adoption(self) -> None:
        event = self.event(
            "Если проект будет принят, все организации с ОКВЭД 24.10 "
            "будут обязаны до 2026-10-28 представить сведения."
        )
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))
        self.assertEqual(self.semantic_result(result)["direction"], "unknown")

    def test_proposed_exemption_does_not_claim_confirmed_reduction(self):
        event = self.event(
            "Предлагается, чтобы все организации с ОКВЭД 24.10 "
            "освобождались от обязанности представлять сведения.",
            mandatory=False,
        )
        result = self.classify(event)
        self.assertEqual(result.relevance, "conditional")
        self.assertEqual(self.semantic_result(result)["direction"], "unknown")
        self.assertNotIn("снизить регуляторную нагрузку",
                         result.impact_mechanism)

    def test_speaker_quote_is_not_an_adopted_regulation(self) -> None:
        positive = self.introduced()
        event = replace(positive, content=(
            "Эксперт заявил: «" + positive.content + "». Закон не принят."
        ))
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))
        self.assertFalse(self.semantic_result(result)["obligation_verified"])

    def test_quoted_assertion_without_adoption_context_remains_uncertain(self):
        positive = self.introduced()
        event = replace(positive, content="«" + positive.content + "»")
        result = self.classify(event)
        self.assertEqual(self.semantic_result(result)["direction"], "unknown")
        self.assertNotEqual(result.priority, "high")

    def test_double_negation_does_not_imply_introduction_or_exemption(self):
        for text in (
            "Не отменена обязанность всех организаций с ОКВЭД 24.10 "
            "до 2026-10-28 представить сведения.",
            "Все организации с ОКВЭД 24.10 не освобождены от обязанности "
            "не подавать сведения до 2026-10-28.",
        ):
            with self.subTest(text=text):
                result = self.classify(self.event(text))
                self.assertEqual(self.semantic_result(result)["direction"],
                                 "unknown")
                self.assertEqual((result.relevance, result.priority),
                                 ("conditional", "low"))

    def test_negative_expression_does_not_mean_cancellation(self) -> None:
        event = self.event(
            "Все организации с ОКВЭД 24.10 обязаны не позднее 2026-10-28 "
            "представить сведения в реестр."
        )
        result = self.classify(event)
        self.assertEqual(self.semantic_result(result)["direction"], "required")
        self.assertEqual(result.priority, "high")

    def test_unrelated_negative_sentence_does_not_cancel_supported_duty(self):
        positive = self.introduced()
        event = replace(positive, content=(
            positive.content + " Других отраслей сообщение не касается."
        ))
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "high"))

    def test_unrelated_industry_remains_none(self) -> None:
        event = replace(self.exemption(), okved_codes=("62.01",))
        event = replace(event, content=event.content.replace("24.10", "62.01"),
                        evidence=(replace(
                            event.evidence[0], quote=event.content.replace(
                                "24.10", "62.01"
                            )
                        ),))
        result = self.classify(event)
        self.assertEqual(result.relevance, "none")
        self.assertIsNone(result.impact_mechanism)

    def test_missing_company_evidence_keeps_exemption_conditional(self):
        result = self.classify(self.exemption(), with_profile=False)
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))
        self.assertIn("снизить", result.impact_mechanism)
        self.assertFalse(result.ranking_factors["company_basis_verified"])
        self.assertFalse(any(item["kind"] == "company_statement"
                             for item in result.evidence))

    def test_universal_word_for_documents_does_not_prove_all_companies(self):
        event = self.event(
            "Некоторые организации с ОКВЭД 24.10 обязаны до 2026-10-28 "
            "представить все сведения в реестр."
        )
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))
        self.assertFalse(self.semantic_result(result)["scope_verified"])

    def test_qualifying_exception_blocks_universal_scope(self) -> None:
        event = self.event(
            "Все организации с ОКВЭД 24.10, кроме отдельных предприятий, "
            "обязаны до 2026-10-28 представить сведения в реестр."
        )
        self.assertEqual(self.classify(event).relevance, "conditional")

    def test_deadline_cannot_be_grafted_from_an_unrelated_sentence(self):
        event = self.event(
            "Все организации с ОКВЭД 24.10 обязаны представить сведения. "
            "Конференция состоится до 2026-10-28."
        )
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "normal"))
        self.assertFalse(self.semantic_result(result)["deadline_verified"])
        self.assertIsNone(result.ranking_factors["verified_deadline"])

    def test_scope_and_duty_cannot_be_grafted_from_disjoint_fragments(self):
        event = self.event(
            "Все организации с ОКВЭД 24.10 упомянуты в новости. "
            "Они обязаны до 2026-10-28 представить сведения."
        )
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))
        self.assertFalse(self.semantic_result(result)["obligation_verified"])

    def test_independent_financing_mechanism_survives_regulatory_noise(self):
        original = next(event for event in load_demo_events()
                        if event.event_id == "SYN_RATE")
        event = replace(
            original,
            content=original.content + "\n\n" + self.exemption().content,
            mandatory=True, applies_to_all_in_scope=True,
            deadline="2026-10-28",
        )
        result = self.classify(event)
        control = self.classify(original)
        self.assertEqual(result.relevance, control.relevance)
        self.assertEqual(result.impact_mechanism, control.impact_mechanism)
        self.assertEqual(result.conditions, control.conditions)
        self.assertEqual(result.priority, "low")
        self.assertEqual(result.ranking_factors["impact_domain"], "financial")
        self.assertNotIn("regulatory_semantics", result.ranking_factors)

    def test_non_regulatory_results_retain_their_original_behavior(self):
        expected = {
            "SYN_STEEL_INPUT": ("conditional", "low"),
            "SYN_IT_DEMAND": ("none", "low"),
            "SYN_RATE": ("conditional", "low"),
            "SYN_LOCAL_KEYWORD": ("none", "low"),
            "SYN_METADATA_ONLY": ("none", "low"),
        }
        for event in load_demo_events():
            if event.event_id in expected:
                with self.subTest(event_id=event.event_id):
                    result = self.classify(event)
                    self.assertEqual((result.relevance, result.priority),
                                     expected[event.event_id])
                    self.assertNotIn("regulatory_semantics",
                                     result.ranking_factors)

    def test_ranking_is_stable_and_exemption_never_wins_by_false_urgency(self):
        events = [
            replace(self.introduced(), event_id="INTRODUCED"),
            replace(self.exemption(mandatory=True), event_id="EXEMPTION"),
        ]
        options = {"company_profile": self.profile, "as_of": self.as_of}
        first = serialize_ranking(rank_news(self.view, events, **options))
        second = serialize_ranking(rank_news(
            self.view, reversed(events), **options
        ))
        self.assertEqual(first, second)
        self.assertEqual([item["event_id"] for item in first],
                         ["INTRODUCED", "EXEMPTION"])
        self.assertEqual(first[1]["priority"], "normal")

    def test_original_evidence_and_flags_are_preserved(self) -> None:
        event = self.exemption(mandatory=True)
        original = copy.deepcopy((self.view, self.profile, event))
        result = self.classify(event)
        serialized = serialize_result(result)
        self.assertEqual((self.view, self.profile, event), original)
        reference = next(item for item in serialized["evidence"]
                         if item["kind"] == "article_quote")
        self.assertEqual(reference["evidence_id"],
                         event.evidence[0].evidence_id)
        self.assertEqual(reference["quote"], event.evidence[0].quote)
        self.assertEqual(reference["source_url"], event.source_url)
        self.assertEqual(json.loads(json.dumps(serialized)), serialized)
        self.assertTrue(self.semantic_result(result)["normalized_claims"][
            "mandatory"
        ])
        self.assertTrue(event.mandatory)

    def test_uncertain_rule_does_not_outrank_verified_financing_basis(self):
        uncertain = self.event(
            "Не отменена обязанность всех организаций с ОКВЭД 24.10 "
            "до 2026-10-28 представить сведения."
        )
        rate = next(event for event in load_demo_events()
                    if event.event_id == "SYN_RATE")
        results = rank_news(
            self.view, [uncertain, rate], company_profile=self.profile,
            as_of=self.as_of,
        )
        self.assertEqual([item.event_id for item in results],
                         [rate.event_id, uncertain.event_id])
        self.assertEqual(results[1].ranking_factors["mechanism_specificity"],
                         "sector_condition")

    def test_opposing_excerpt_without_repeated_code_is_not_ignored(self):
        positive = self.introduced()
        opposing = "Они освобождены от обязанности представлять сведения."
        event = replace(
            positive, content=positive.content + "\n\n" + opposing,
            evidence=(positive.evidence[0], ArticleEvidence(
                "QUOTE_OPPOSING", opposing, self.supports
            )),
        )
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))
        self.assertEqual(self.semantic_result(result)["direction"], "unknown")

    def test_other_industry_relief_does_not_cancel_verified_duty(self):
        positive = self.introduced()
        other = (
            "Для всех организаций с ОКВЭД 62.01 отменена обязанность "
            "представлять сведения в другой реестр."
        )
        event = replace(positive, content=positive.content + "\n\n" + other)
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "high"))

    def broad_obligation(self) -> NewsEvent:
        original = self.introduced()
        text = original.content.replace("24.10", "24")
        return replace(original, content=text, okved_codes=("24",),
                       evidence=(replace(original.evidence[0], quote=text),))

    def test_company_exemption_can_contradict_broad_industry_duty(self):
        positive = self.broad_obligation()
        event = replace(positive, content=(
            positive.content + "\n\n" + self.exemption().content
        ))
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("conditional", "low"))

    def test_other_subindustry_relief_does_not_cancel_company_duty(self):
        positive = self.broad_obligation()
        other = self.exemption().content.replace("24.10", "24.20")
        event = replace(positive, content=positive.content + "\n\n" + other)
        result = self.classify(event)
        self.assertEqual((result.relevance, result.priority),
                         ("grounded", "high"))


if __name__ == "__main__":
    unittest.main()
