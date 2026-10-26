import unittest

from backend.domain import (
    answer_question,
    build_deterministic_insight,
    candidate_from_ranking,
    make_context_id,
    score_bank_fragments,
)


class DomainTests(unittest.TestCase):
    def test_context_is_stable_and_focus_scoped(self):
        common = dict(
            company_id="U_1",
            access_scope=["U_1"],
            profile_id="p1",
            news_snapshot_hash="news",
            prompt_version="v1",
            model_id="deterministic",
            rules_version="v1",
            schema_version="2.2.0",
        )
        cfo = make_context_id(analytic_focus="CFO", **common)
        self.assertEqual(cfo, make_context_id(analytic_focus="CFO", **common))
        self.assertNotEqual(cfo, make_context_id(analytic_focus="CEO", **common))

    def test_bank_search_uses_only_verified_fragments(self):
        hits = score_bank_fragments(
            "ключевая ставка финансирование",
            [
                {
                    "source_id": "B1",
                    "page_number": 1,
                    "text": "Ключевая ставка влияет на финансирование.",
                    "verified": True,
                },
                {
                    "source_id": "B2",
                    "page_number": 1,
                    "text": "Ключевая ставка влияет на финансирование.",
                    "verified": False,
                },
            ],
        )
        self.assertEqual(["B1"], [item["source_id"] for item in hits])

    def test_deterministic_insight_keeps_conditions_and_evidence(self):
        ranking = {
            "event_id": "E1",
            "title": "Событие",
            "source_id": "SRC",
            "source_url": "https://example.org/e1",
            "published_at": "2026-10-20T10:00:00+03:00",
            "relevance": "conditional",
            "priority": "low",
            "relevance_explanation": "Есть условный канал влияния.",
            "impact_mechanism": "Условия финансирования могут измениться.",
            "conditions": ["Нужно подтвердить условия договора."],
            "evidence": [
                {
                    "kind": "article_quote",
                    "event_id": "E1",
                    "evidence_id": "NEWS-1",
                    "quote": "Точная цитата.",
                    "supports": ["event_type"],
                    "source_id": "SRC",
                    "source_url": "https://example.org/e1",
                }
            ],
            "limitations": ["Размер эффекта неизвестен."],
            "ranking_factors": {"mechanism_specificity": "sector_condition"},
        }
        candidate = candidate_from_ranking(ranking)
        insight = build_deterministic_insight(
            insight_id="I1",
            version=1,
            company_id="U_1",
            analytic_focus="CFO",
            context_id="ctx-1",
            candidate=candidate,
            event={
                "event_id": "E1",
                "title": "Событие",
                "source_id": "SRC",
                "source_url": "https://example.org/e1",
                "published_at": "2026-10-20T10:00:00+03:00",
            },
            bank_corpus_available=False,
            bank_hits=[],
            generated_at="2026-10-25T12:00:00+03:00",
        )
        self.assertEqual("limited", insight["status"])
        self.assertEqual("unavailable", insight["bank_retrieval_status"])
        self.assertIn(
            "NEWS-1", insight["ai_interpretation"]["summary"]["evidence_ids"]
        )
        self.assertIn(
            "услов", answer_question(insight, "При каких условиях риск?")["answer"]
        )


if __name__ == "__main__":
    unittest.main()

