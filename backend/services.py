"""Application orchestration for profiles, ranking, insights, and feedback."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from ml_ranking import NewsEvent, rank_news, serialize_ranking

from .budget import BudgetManager
from .domain import (
    answer_question,
    build_deterministic_insight,
    candidate_from_ranking,
    make_context_id,
    score_bank_fragments,
    sha256_json,
    utc_now,
)
from .repository import Repository


PROMPT_VERSION = "backend-evidence-template-1"
MODEL_ID = "deterministic-evidence-bound"
RULES_VERSION = "ml-ranking-1.0.0"
INSIGHT_SCHEMA_VERSION = "2.2.0"


class ServiceError(RuntimeError):
    code = "service_error"
    http_status = 400

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.details = details


class ResourceNotFound(ServiceError):
    code = "not_found"
    http_status = 404


class NotPrepared(ServiceError):
    code = "not_prepared"
    http_status = 409


class InvalidRequest(ServiceError):
    code = "invalid_request"
    http_status = 400


class DependencyUnavailable(ServiceError):
    code = "dependency_unavailable"
    http_status = 503


def _parse_as_of(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise InvalidRequest("as_of must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidRequest("as_of must include a timezone")
    return parsed


class ApplicationServices:
    def __init__(
        self,
        repository: Repository,
        budget: BudgetManager,
        *,
        top_k: int = 5,
        gateway: Any = None,
    ):
        self.repository = repository
        self.budget = budget
        self.top_k = top_k
        self.gateway = gateway
        self.model_id = gateway.model_id if gateway else MODEL_ID
        self.prompt_version = gateway.prompt_version if gateway else PROMPT_VERSION

    def company_detail(self, company_id: str) -> dict:
        bundle = self.repository.profile_bundle(company_id)
        if bundle is None:
            raise ResourceNotFound("Company profile is unavailable")
        return {
            "company_id": company_id,
            "display_name": bundle["company"]["display_name"],
            "profile": bundle["profile"],
            "views": {
                "ml": bundle["ml_view"],
                "interpreter": bundle["interpreter_view"],
                "ui": bundle["ui_view"],
            },
            "profile_id": bundle["profile_id"],
            "as_of_date": bundle["as_of_date"],
        }

    def normalize_event(self, event_id: str) -> dict:
        if self.gateway is None:
            raise DependencyUnavailable("Configure the LLM gateway or import reviewed annotations through /admin/events")
        event = self.repository.event(event_id)
        if event is None:
            raise ResourceNotFound("Event is unavailable")
        if event.get("extraction_status") != "full_text":
            raise InvalidRequest("A full extracted article is required")
        context = sha256_json([event_id, event["content"], self.model_id, self.prompt_version, "normalizer-1"])
        cache_key = "normalized:" + context
        with self.repository.operation_lease(cache_key):
            with self.repository.database.connection() as connection:
                marker = connection.execute("SELECT value FROM runtime_state WHERE key = ?", (cache_key,)).fetchone()
            if marker is not None and marker["value"] == sha256_json(event):
                return {"event_id": event_id, "cached": True, **self.repository.create_news_snapshot()}
            reservation = self.budget.reserve(operation="normalize_event", max_cost_kopecks=self.gateway.max_cost_kopecks, context_id=context)
            result = None
            started = monotonic()
            try:
                result = self.gateway.normalize_event(event)
            finally:
                self.budget.commit(reservation.reservation_id, actual_cost_kopecks=self.gateway.max_cost_kopecks,
                    model_id=self.model_id, tokens_in=result.tokens_in if result else None,
                    tokens_out=result.tokens_out if result else None, elapsed_ms=int((monotonic() - started) * 1000))
            normalized = dict(result.payload)
            extraction_status = normalized.pop("extraction_status")
            self.repository.upsert_event(normalized, extraction_status=extraction_status)
            with self.repository.database.transaction(immediate=True) as connection:
                connection.execute("INSERT INTO runtime_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (cache_key, sha256_json(result.payload)))
            return {"event_id": event_id, "cached": False, **self.repository.create_news_snapshot()}

    def _current_context(
        self, user_id: str, company_id: str, analytic_focus: str,
        *, snapshot: dict | None = None,
    ) -> tuple[dict, dict, str]:
        bundle = self.repository.profile_bundle(company_id)
        if bundle is None or bundle["ml_view"] is None:
            raise NotPrepared("Company profile has not been prepared")
        snapshot = snapshot or self.repository.latest_news_snapshot()
        if snapshot is None:
            raise NotPrepared("News snapshot has not been prepared")
        context_id = make_context_id(
            company_id=company_id,
            analytic_focus=analytic_focus,
            access_scope=self.repository.access_scope(user_id),
            profile_id=bundle["profile_id"],
            news_snapshot_hash=sha256_json({
                "news": snapshot["manifest_hash"],
                "bank": self.repository.bank_corpus_hash(),
                "ranking_hour_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H"),
            }),
            prompt_version=self.prompt_version,
            model_id=self.model_id,
            rules_version=f"{RULES_VERSION}:{bundle['rules_version']}:top{self.top_k}",
            schema_version=INSIGHT_SCHEMA_VERSION,
        )
        return bundle, snapshot, context_id

    def get_overview(
        self, user_id: str, company_id: str, analytic_focus: str
    ) -> dict:
        try:
            bundle, snapshot, context_id = self._current_context(
                user_id, company_id, analytic_focus
            )
        except NotPrepared as error:
            return {
                "company_id": company_id,
                "analytic_focus": analytic_focus,
                "snapshot_id": None,
                "context_id": None,
                "generated_at": None,
                "state": "not_prepared",
                "coverage_note": str(error),
                "items": [],
            }
        stored = self.repository.overview_by_context(context_id)
        if stored is not None:
            return stored
        return {
            "company_id": company_id,
            "analytic_focus": analytic_focus,
            "snapshot_id": snapshot["id"],
            "context_id": context_id,
            "profile_id": bundle["profile_id"],
            "generated_at": None,
            "state": "not_prepared",
            "coverage_note": "Overview has not been prepared for this context.",
            "items": [],
        }

    def refresh_overview(
        self,
        user_id: str,
        company_id: str,
        analytic_focus: str,
        *,
        as_of: str | None = None,
    ) -> dict:
        snapshot = self.repository.create_news_snapshot()
        bundle, _latest, context_id = self._current_context(
            user_id, company_id, analytic_focus, snapshot=snapshot
        )
        existing = self.repository.overview_by_context(context_id)
        if existing is not None:
            return existing
        records = self.repository.snapshot_events(snapshot["snapshot_id"])
        rankable = [
            record
            for record in records
            if record.pop("extraction_status", "full_text") == "full_text"
        ]
        try:
            events = [NewsEvent.from_dict(record) for record in rankable]
            results = rank_news(
                bundle["ml_view"],
                events,
                company_profile=bundle["profile"],
                analytic_focus=analytic_focus,
                top_k=self.top_k,
                as_of=_parse_as_of(as_of),
            )
        except (TypeError, ValueError) as error:
            raise InvalidRequest(
                "Profile or normalized event contract is invalid",
                details=str(error),
            ) from error
        serialized = serialize_ranking(results)
        candidates = [candidate_from_ranking(item) for item in serialized]
        state = "ready" if candidates else "empty"
        generated_at = utc_now()
        note = (
            f"Ranked {len(rankable)} full-text events from "
            f"{len(records)} stored events; metadata-only events were excluded."
        )
        overview_id = f"ov-{sha256_json(context_id)[:20]}"
        return self.repository.save_overview(
            overview_id=overview_id,
            company_id=company_id,
            profile_id=bundle["profile_id"],
            news_snapshot_id=snapshot["snapshot_id"],
            analytic_focus=analytic_focus,
            context_id=context_id,
            state=state,
            generated_at=generated_at,
            coverage_note=note,
            candidates=candidates,
        )

    def create_insight(
        self, user_id: str, company_id: str, analytic_focus: str, event_id: str
    ) -> dict:
        key = sha256_json(["insight", company_id, analytic_focus, event_id])
        with self.repository.operation_lease(key):
            return self._create_insight(user_id, company_id, analytic_focus, event_id)

    def _create_insight(
        self,
        user_id: str,
        company_id: str,
        analytic_focus: str,
        event_id: str,
    ) -> dict:
        bundle, snapshot, context_id = self._current_context(
            user_id, company_id, analytic_focus
        )
        existing = self.repository.existing_insight(context_id, event_id)
        if existing is not None:
            return existing
        candidate_row = self.repository.candidate(context_id, event_id)
        if candidate_row is None:
            raise InvalidRequest(
                "event_id is not a candidate in the current overview"
            )
        candidate, overview_id = candidate_row
        event = self.repository.snapshot_event(snapshot["id"], event_id)
        if event is None:
            raise ResourceNotFound("Event is unavailable")
        if event.get("extraction_status") != "full_text" or not event.get(
            "content"
        ):
            raise InvalidRequest(
                "A detailed insight requires an extracted full article"
            )
        corpus_available, fragments = self.repository.bank_corpus()
        query = " ".join(
            [event["title"], *event.get("factor_classes", [])]
        )
        bank_hits = score_bank_fragments(query, fragments)
        insight_id = f"ins-{sha256_json([context_id, event_id])[:20]}"
        reservation_id = f"res-{uuid4().hex}"
        max_cost = self.gateway.max_cost_kopecks if self.gateway else 0
        reservation = self.budget.reserve(
            operation="llm_insight" if self.gateway else "deterministic_insight",
            max_cost_kopecks=max_cost,
            user_id=user_id,
            context_id=context_id,
            reservation_id=reservation_id,
        )
        called_provider = False
        accounted = False
        started = monotonic()
        try:
            payload = build_deterministic_insight(
                insight_id=insight_id,
                version=1,
                company_id=company_id,
                analytic_focus=analytic_focus,
                context_id=context_id,
                candidate=candidate,
                event=event,
                bank_corpus_available=corpus_available,
                bank_hits=bank_hits,
            )
            tokens_in, tokens_out = 0, 0
            if self.gateway:
                called_provider = True
                result = self.gateway.enhance_insight(payload, bundle["interpreter_view"])
                payload = result.payload
                payload["mode"] = "llm_evidence_bound"
                tokens_in, tokens_out = result.tokens_in, result.tokens_out
                payload["agent_trace"]["steps"].append({
                    "step": "llm_interpretation_validation", "status": "ok",
                    "details": {"model_id": self.model_id, "prompt_version": self.prompt_version},
                })
            self.budget.commit(
                reservation.reservation_id,
                actual_cost_kopecks=max_cost,
                model_id=self.model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                elapsed_ms=int((monotonic() - started) * 1000),
                cache_hit=False,
            )
            accounted = True
            saved = self.repository.save_insight(
                payload,
                overview_id=overview_id,
                provider="llm" if self.gateway else "deterministic",
                model_id=self.model_id,
                prompt_version=self.prompt_version,
            )
            self.repository.save_trace(
                user_id=user_id, payload=saved,
                model_id=self.model_id, prompt_version=self.prompt_version,
            )
            return saved
        except BaseException:
            if not accounted:
                if called_provider:
                    # A timed-out/invalid response may still be billed upstream.
                    self.budget.commit(
                        reservation.reservation_id, actual_cost_kopecks=max_cost,
                        model_id=self.model_id,
                        elapsed_ms=int((monotonic() - started) * 1000),
                    )
                else:
                    self.budget.release(reservation.reservation_id)
            raise

    def insight(self, insight_id: str) -> dict:
        value = self.repository.insight(insight_id)
        if value is None:
            raise ResourceNotFound("Insight is unavailable")
        return value

    def ask(
        self,
        *,
        user_id: str,
        insight_id: str,
        question: str,
        conversation_id: str | None,
    ) -> dict:
        insight = self.insight(insight_id)
        self.repository.validate_conversation(user_id, insight, conversation_id)
        if self.gateway:
            reservation = self.budget.reserve(
                operation="llm_question", max_cost_kopecks=self.gateway.max_cost_kopecks,
                user_id=user_id, context_id=insight["context_id"],
            )
            started = monotonic()
            result = None
            try:
                result = self.gateway.ask(insight, question)
                answer = result.payload
            finally:
                self.budget.commit(
                    reservation.reservation_id,
                    actual_cost_kopecks=self.gateway.max_cost_kopecks,
                    model_id=self.model_id,
                    tokens_in=result.tokens_in if result else None,
                    tokens_out=result.tokens_out if result else None,
                    elapsed_ms=int((monotonic() - started) * 1000),
                )
        else:
            answer = answer_question(insight, question)
        return self.repository.save_question(
            user_id=user_id,
            insight=insight,
            question=question,
            answer=answer,
            conversation_id=conversation_id,
        )

    def feedback(
        self,
        *,
        user_id: str,
        insight_id: str,
        rating: str,
        reason: str,
        comment: str | None,
    ) -> dict:
        insight = self.insight(insight_id)
        return self.repository.upsert_feedback(
            user_id=user_id,
            insight=insight,
            rating=rating,
            reason=reason,
            comment=comment,
        )


def remap_fixture_profile(
    profile: dict,
    ml_view: dict,
    interpreter_view: dict,
    ui_view: dict,
    *,
    company_id: str,
) -> tuple[dict, dict, dict, dict]:
    """Clone repository fixtures under demo IDs without changing evidence."""

    values = [
        copy.deepcopy(profile),
        copy.deepcopy(ml_view),
        copy.deepcopy(interpreter_view),
        copy.deepcopy(ui_view),
    ]
    new_profile_id = f"{company_id}-demo-p1"
    values[0]["meta"]["company_id"] = company_id
    values[0]["meta"]["profile_id"] = new_profile_id
    values[0]["meta"]["input_hash"] = sha256_json(
        {"demo_company": company_id, "source": profile["meta"]["input_hash"]}
    )
    for view in values[1:]:
        view["company_id"] = company_id
        view["profile_id"] = new_profile_id
    return tuple(values)  # type: ignore[return-value]


def load_demo_bundle(root: str | Path, scenario: int, company_id: str):
    root = Path(root)
    fixtures = root / "tests" / "fixtures" / "scenarios"
    profile = json.loads(
        (fixtures / f"s{scenario}_expected_profile.json").read_text(
            encoding="utf-8"
        )
    )
    ml_view = json.loads(
        (fixtures / f"s{scenario}_MLView.json").read_text(encoding="utf-8")
    )
    interpreter = json.loads(
        (fixtures / f"s{scenario}_InterpreterView.json").read_text(
            encoding="utf-8"
        )
    )
    ui = json.loads(
        (fixtures / f"s{scenario}_UIView.json").read_text(encoding="utf-8")
    )
    return remap_fixture_profile(
        profile, ml_view, interpreter, ui, company_id=company_id
    )


__all__ = [
    "ApplicationServices",
    "DependencyUnavailable",
    "INSIGHT_SCHEMA_VERSION",
    "InvalidRequest",
    "MODEL_ID",
    "NotPrepared",
    "PROMPT_VERSION",
    "RULES_VERSION",
    "ResourceNotFound",
    "ServiceError",
    "load_demo_bundle",
    "remap_fixture_profile",
]

