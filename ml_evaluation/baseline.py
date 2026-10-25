from typing import Iterable

from ml_ranking import NewsEvent
from ml_ranking.validation import (
    OKVED_PATTERN, company_context, parse_timestamp, validate_event,
)

from .metrics import validate_options


def industry_only_rank(
    ml_view: dict, events: Iterable[NewsEvent], *, top_k: int = 5,
) -> list[str]:
    validate_options(top_k, 1)
    company_context(ml_view, None)
    codes = [
        code for code in ml_view["hooks"]["matching_profile"]["okved_codes"]
        if OKVED_PATTERN.fullmatch(code)
    ]
    seen = set()
    matched = []
    for event in events:
        validate_event(event)
        if event.event_id in seen:
            raise ValueError(f"Повторяющийся event_id: {event.event_id}")
        seen.add(event.event_id)
        if any(code.startswith(scope)
               for code in codes for scope in event.okved_codes):
            matched.append(event)
    return [event.event_id for event in sorted(
        matched,
        key=lambda event: (
            -parse_timestamp(event.published_at).timestamp()
            if event.published_at is not None else float("inf"),
            event.event_id,
        ),
    )[:top_k]]
