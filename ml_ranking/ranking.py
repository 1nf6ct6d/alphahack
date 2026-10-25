from dataclasses import replace
from datetime import datetime
from typing import Iterable

from .matching import match_event
from .models import NewsEvent, RelevanceResult
from .validation import company_context, parse_timestamp, validate_event

SPECIFICITY_ORDER = {
    "industry_obligation": 0, "company_condition": 1,
    "sector_condition": 2, "observed_payment_link": 3, "none": 4,
}
FOCUS_DOMAINS = {"CFO": {"financial"}, "CEO": {"operational", "strategic"}}


def _validate_profile_date(ml_view: dict, as_of: datetime | None) -> None:
    if as_of is not None and (
        ml_view["as_of_date"] > as_of.date().isoformat()
    ):
        raise ValueError("Дата версии профиля позже момента анализа")


def validate_options(
    as_of: datetime | None, urgency_horizon_days: int
) -> None:
    if as_of is not None and (
        not isinstance(as_of, datetime) or as_of.utcoffset() is None
    ):
        raise ValueError("as_of должен быть datetime с часовым поясом")
    if type(urgency_horizon_days) is not int or urgency_horizon_days < 0:
        raise ValueError(
            "Горизонт срочности должен быть целым неотрицательным"
        )


def classify_event(
    ml_view: dict, event: NewsEvent, *, company_profile: dict | None = None,
    as_of: datetime | None = None, urgency_horizon_days: int = 7,
) -> RelevanceResult:
    validate_options(as_of, urgency_horizon_days)
    context = company_context(ml_view, company_profile)
    _validate_profile_date(context.view, as_of)
    validate_event(event)
    if as_of is not None and event.published_at is not None:
        if parse_timestamp(event.published_at) > as_of:
            raise ValueError("Дата публикации позже момента анализа")
    return match_event(context, event, as_of, urgency_horizon_days)


def rank_news(
    ml_view: dict, events: Iterable[NewsEvent], *,
    company_profile: dict | None = None, analytic_focus: str | None = None,
    top_k: int = 5, as_of: datetime | None = None,
    urgency_horizon_days: int = 7,
) -> list[RelevanceResult]:
    validate_options(as_of, urgency_horizon_days)
    if type(top_k) is not int or top_k < 0:
        raise ValueError("top_k должен быть целым неотрицательным")
    if analytic_focus is not None and analytic_focus not in FOCUS_DOMAINS:
        raise ValueError("Аналитический фокус должен быть CFO, CEO или null")
    context = company_context(ml_view, company_profile)
    _validate_profile_date(context.view, as_of)
    events = tuple(events)
    ids = set()
    candidates = []
    for event in events:
        validate_event(event)
        if event.event_id in ids:
            raise ValueError(f"Повторяющийся event_id: {event.event_id}")
        ids.add(event.event_id)
        if as_of is not None and event.published_at is not None:
            if parse_timestamp(event.published_at) > as_of:
                raise ValueError("Дата публикации позже момента анализа")
        result = match_event(context, event, as_of, urgency_horizon_days)
        if result.relevance == "none":
            continue
        factors = dict(result.ranking_factors)
        factors["analytic_focus"] = analytic_focus
        factors["analytic_focus_applied"] = (
            result.relevance == "grounded"
            and factors["company_basis_verified"]
            and factors["impact_domain"] in FOCUS_DOMAINS.get(
                analytic_focus, set()
            )
        )
        candidates.append(replace(result, ranking_factors=factors))
    return sorted(candidates, key=ranking_key)[:top_k]


def ranking_key(result: RelevanceResult) -> tuple:
    factors = result.ranking_factors
    urgent = factors["urgent_deadline_verified"]
    publication = (
        parse_timestamp(result.event.published_at).timestamp()
        if result.event.published_at is not None else float("-inf")
    )
    return (
        0 if result.relevance == "grounded" else 1,
        0 if urgent else 1,
        factors["days_until_deadline"] if urgent else 0,
        SPECIFICITY_ORDER[factors["mechanism_specificity"]],
        0 if factors.get("analytic_focus_applied") else 1,
        -publication,
        result.event_id,
    )
