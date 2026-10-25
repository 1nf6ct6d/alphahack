import json
from datetime import datetime
from pathlib import Path

from .models import NewsEvent
from .ranking import classify_event, rank_news
from .serialization import serialize_ranking, write_demo_reports

ROOT = Path(__file__).resolve().parent.parent


def load_demo_events() -> tuple[NewsEvent, ...]:
    path = Path(__file__).resolve().parent / "examples/synthetic_events.json"
    return tuple(NewsEvent.from_dict(item) for item in json.loads(
        path.read_text(encoding="utf-8")
    ))


def run_demo(output_dir: str | Path = "reports") -> tuple[Path, Path]:
    events = load_demo_events()
    as_of = datetime.fromisoformat("2026-10-25T12:00:00+03:00")
    comparisons = []
    for scenario in (1, 3):
        base = Path("tests/fixtures/scenarios")
        view_path = base / f"s{scenario}_MLView.json"
        profile_path = base / f"s{scenario}_expected_profile.json"
        view = json.loads((ROOT / view_path).read_text(encoding="utf-8"))
        profile = json.loads((ROOT / profile_path).read_text(encoding="utf-8"))
        ranked = rank_news(view, events, company_profile=profile, as_of=as_of)
        excluded = []
        for event in events:
            result = classify_event(
                view, event, company_profile=profile, as_of=as_of
            )
            if result.relevance == "none":
                excluded.append({
                    "event_id": event.event_id, "reason": result.explanation
                })
        comparisons.append({
            "company_id": view["company_id"],
            "ml_view_fixture": view_path.as_posix(),
            "profile_fixture": profile_path.as_posix(),
            "ranked": serialize_ranking(ranked), "excluded": excluded,
        })
    return write_demo_reports(comparisons, events, as_of, output_dir)
