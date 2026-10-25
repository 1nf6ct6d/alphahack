import json
from pathlib import Path

from ml_ranking import ArticleEvidence, NewsEvent
from ml_ranking.demo import load_demo_events

from .dataset import freeze_context, freeze_event, validate_dataset

ROOT = Path(__file__).resolve().parent.parent


def build_synthetic_dataset() -> dict:
    content = (
        "Учебный сценарий: для отрасли с ОКВЭД 24.10 сообщается "
        "о дефиците специалистов. Численность и планы найма "
        "отдельных предприятий неизвестны."
    )
    labor_event = NewsEvent(
        "EVAL_LABOR", "Учебный дефицит специалистов в металлургии",
        "SYNTHETIC_EDITORIAL", "https://example.invalid/eval/labor",
        "other", content=content,
        published_at="2026-10-24T20:00:00+03:00",
        factor_classes=("labor_market",), okved_codes=("24.10",),
        evidence=(ArticleEvidence(
            "QUOTE_LABOR", content,
            ("event_type", "factor_classes", "okved_codes"),
        ),), synthetic=True,
    )
    events = (*load_demo_events(), labor_event)
    contexts = []
    queries = []
    for scenario in (1, 3):
        folder = ROOT / "tests/fixtures/scenarios"
        view = json.loads((folder / f"s{scenario}_MLView.json").read_text(
            encoding="utf-8"
        ))
        profile = json.loads((
            folder / f"s{scenario}_expected_profile.json"
        ).read_text(encoding="utf-8"))
        context_id = f"S{scenario}_FROZEN"
        contexts.append(freeze_context(
            context_id, view, profile,
            f"Existing synthetic fixtures s{scenario}; development only",
        ))
        queries.append({
            "query_id": f"Q_S{scenario}", "context_id": context_id,
            "company_id": view["company_id"], "analytic_focus": None,
            "split": "development", "as_of": "2026-10-25T12:00:00+03:00",
            "candidate_event_ids": [event.event_id for event in events],
        })
    annotations = json.loads((
        Path(__file__).resolve().parent / "examples/synthetic_annotations.json"
    ).read_text(encoding="utf-8"))
    return validate_dataset({
        "format_version": "ml3a-1", "dataset_id": "ml3a-synthetic-demo",
        "dataset_version": "1.0.0", "kind": "synthetic_demo",
        "status": "synthetic_demonstration",
        "labeling_provenance": {
            "method": "synthetic_test_definition",
            "independent_of_ranker": True,
            "description": (
                "Ожидания заданы отдельно в статическом файле тестовых "
                "оценок, без вызова ранжировщика. Это искусственные "
                "условия проверки, не разметка реальных людей. "
                "EVAL_LABOR проверяет известный пробел покрытия ML-2A."
            ),
        },
        "contexts": contexts,
        "events": [freeze_event(
            event, "GROUP_" + event.event_id, "development"
        ) for event in events],
        "queries": queries, "annotations": annotations,
    })
