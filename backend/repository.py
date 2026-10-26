"""Persistence repository for immutable profiles, snapshots, and insights."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable
from uuid import uuid4

from .db import Database
from .domain import canonical_json, sha256_json, utc_now


def _dump(value: Any) -> str:
    return canonical_json(value)


def _load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class Repository:
    def __init__(self, database: Database):
        self.database = database

    # --- users and companies -------------------------------------------------

    def upsert_user(self, user_id: str, label: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO users (id, label, is_active)
                VALUES (?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET label = excluded.label,
                    is_active = 1
                """,
                (user_id, label),
            )

    def upsert_company(
        self,
        company_id: str,
        display_name: str | None = None,
        *,
        profile_status: str = "not_prepared",
        source_record: dict | None = None,
    ) -> None:
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO companies (
                    id, display_name, profile_status, source_record_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    profile_status = excluded.profile_status,
                    source_record_json = COALESCE(
                        excluded.source_record_json,
                        companies.source_record_json
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    company_id,
                    display_name or company_id,
                    profile_status,
                    _dump(source_record) if source_record is not None else None,
                    now,
                    now,
                ),
            )

    def grant_access(self, user_id: str, company_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO user_company_access (user_id, company_id)
                VALUES (?, ?)
                ON CONFLICT(user_id, company_id) DO NOTHING
                """,
                (user_id, company_id),
            )

    def list_companies(self, user_id: str) -> list[dict]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.display_name, c.profile_status, c.updated_at
                FROM companies AS c
                JOIN user_company_access AS a ON a.company_id = c.id
                WHERE a.user_id = ?
                ORDER BY c.display_name, c.id
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def access_scope(self, user_id: str) -> list[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT company_id FROM user_company_access
                WHERE user_id = ? ORDER BY company_id
                """,
                (user_id,),
            ).fetchall()
        return [str(row["company_id"]) for row in rows]

    # --- profiles ------------------------------------------------------------

    def save_profile_bundle(
        self,
        profile: dict,
        ml_view: dict,
        interpreter_view: dict,
        ui_view: dict,
        *,
        rules_version: str,
        display_name: str | None = None,
        source_record: dict | None = None,
    ) -> str:
        meta = profile["meta"]
        profile_id = str(meta["profile_id"])
        company_id = str(meta["company_id"])
        status = str(profile["input_quality"]["usability"])
        now = utc_now()
        views = {
            "MLView": ml_view,
            "InterpreterView": interpreter_view,
            "UIView": ui_view,
        }
        with self.database.transaction(immediate=True) as connection:
            previous = connection.execute(
                "SELECT company_id, profile_json, rules_version FROM company_profile_versions WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if previous is not None:
                if (previous["company_id"] != company_id
                    or previous["profile_json"] != _dump(profile)
                    or previous["rules_version"] != rules_version):
                    raise ValueError("An immutable profile_id cannot be reused for different content")
                stored_views = connection.execute(
                    "SELECT view_kind, view_json FROM profile_views WHERE profile_id = ?",
                    (profile_id,),
                ).fetchall()
                if any(_dump(views[row["view_kind"]]) != row["view_json"] for row in stored_views):
                    raise ValueError("An immutable profile view cannot be replaced")
            connection.execute(
                """
                INSERT INTO companies (
                    id, display_name, profile_status, source_record_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    profile_status = excluded.profile_status,
                    source_record_json = COALESCE(
                        excluded.source_record_json,
                        companies.source_record_json
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    company_id,
                    display_name or company_id,
                    status,
                    _dump(source_record) if source_record is not None else None,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO company_profile_versions (
                    profile_id, company_id, as_of_date, schema_version,
                    input_hash, rules_version, status, profile_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO NOTHING
                """,
                (
                    profile_id,
                    company_id,
                    meta["as_of_date"],
                    meta["schema_version"],
                    meta["input_hash"],
                    rules_version,
                    status,
                    _dump(profile),
                    now,
                ),
            )
            for kind, view in views.items():
                connection.execute(
                    """
                    INSERT INTO profile_views (
                        profile_id, view_kind, schema_version, content_hash,
                        view_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id, view_kind) DO UPDATE SET
                        content_hash = excluded.content_hash,
                        view_json = excluded.view_json
                    """,
                    (
                        profile_id,
                        kind,
                        view["schema_version"],
                        sha256_json(view),
                        _dump(view),
                        now,
                    ),
                )
            for field_name, field in profile["input_quality"].get(
                "field_states", {}
            ).items():
                connection.execute(
                    """
                    INSERT OR REPLACE INTO profile_observations (
                        id, profile_id, field_name, field_state, value_json,
                        evidence_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"obs-{sha256_json([profile_id, field_name])[:16]}",
                        profile_id,
                        field_name,
                        field["state"],
                        None,
                        None,
                    ),
                )
            for metric in profile.get("derived_metrics", []):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO derived_metrics (
                        id, profile_id, name, value_json, coverage,
                        data_reliability, metric_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        metric["id"],
                        profile_id,
                        metric["name"],
                        _dump(metric.get("value")),
                        metric.get("coverage"),
                        metric["data_reliability"],
                        _dump(metric),
                    ),
                )
            for statement in (
                profile.get("facts", [])
                + profile.get("hypotheses", [])
                + profile.get("sensitivities", [])
            ):
                for position, evidence in enumerate(statement.get("evidence", [])):
                    eid = f"pe-{sha256_json([profile_id, statement['id'], position, evidence])[:16]}"
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO profile_evidence (
                            id, profile_id, source_kind, source_ref,
                            value_json, evidence_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            eid,
                            profile_id,
                            evidence["source"],
                            evidence["ref"],
                            _dump(evidence.get("value")),
                            _dump(evidence),
                        ),
                    )
        return profile_id

    def profile_bundle(self, company_id: str) -> dict | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM company_profile_versions
                WHERE company_id = ?
                ORDER BY as_of_date DESC, created_at DESC, rowid DESC LIMIT 1
                """,
                (company_id,),
            ).fetchone()
            if row is None:
                return None
            view_rows = connection.execute(
                """
                SELECT view_kind, view_json FROM profile_views
                WHERE profile_id = ?
                """,
                (row["profile_id"],),
            ).fetchall()
            company = connection.execute(
                "SELECT * FROM companies WHERE id = ?", (company_id,)
            ).fetchone()
        views = {r["view_kind"]: _load(r["view_json"]) for r in view_rows}
        return {
            "company": dict(company) if company is not None else None,
            "profile": _load(row["profile_json"]),
            "profile_id": row["profile_id"],
            "as_of_date": row["as_of_date"],
            "rules_version": row["rules_version"],
            "ml_view": views.get("MLView"),
            "interpreter_view": views.get("InterpreterView"),
            "ui_view": views.get("UIView"),
        }

    # --- normalized news -----------------------------------------------------

    def upsert_event(
        self, event: dict, *, extraction_status: str = "full_text"
    ) -> str:
        source_id = str(event["source_id"])
        source_url = str(event["source_url"])
        content_hash = sha256_json({
            "url": source_url,
            "content": event.get("content"),
            "published_at": event.get("published_at"),
        })
        document_id = f"doc-{sha256_json([source_id, content_hash])[:20]}"
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO news_sources (id, name, kind, base_url)
                VALUES (?, ?, 'news', ?)
                ON CONFLICT(id) DO UPDATE SET base_url = excluded.base_url
                """,
                (source_id, source_id, source_url),
            )
            connection.execute(
                """
                INSERT INTO source_documents (
                    id, source_id, external_id, url, title, published_at,
                    fetched_at, content_hash, content_text,
                    extraction_status, metadata_json, synthetic
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    extraction_status = excluded.extraction_status
                """,
                (
                    document_id,
                    source_id,
                    event["event_id"],
                    source_url,
                    event["title"],
                    event.get("published_at"),
                    now,
                    content_hash,
                    event.get("content"),
                    extraction_status,
                    _dump({"untrusted_external_text": True}),
                    int(bool(event.get("synthetic"))),
                ),
            )
            connection.execute(
                """
                INSERT INTO news_events (
                    id, title, event_type, content, published_at,
                    extraction_status, normalization_version, event_json,
                    synthetic, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '1.0.0', ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    event_type = excluded.event_type,
                    content = excluded.content,
                    published_at = excluded.published_at,
                    extraction_status = excluded.extraction_status,
                    event_json = excluded.event_json,
                    synthetic = excluded.synthetic,
                    updated_at = excluded.updated_at
                """,
                (
                    event["event_id"],
                    event["title"],
                    event["event_type"],
                    event.get("content"),
                    event.get("published_at"),
                    extraction_status,
                    _dump(event),
                    int(bool(event.get("synthetic"))),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO event_sources (event_id, document_id, is_primary)
                VALUES (?, ?, 1)
                ON CONFLICT(event_id, document_id) DO UPDATE SET is_primary = 1
                """,
                (event["event_id"], document_id),
            )
        return str(event["event_id"])

    def event(self, event_id: str) -> dict | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM news_events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        payload = _load(row["event_json"])
        payload["extraction_status"] = row["extraction_status"]
        return payload

    def create_news_snapshot(self) -> dict:
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT id, event_json, extraction_status
                FROM news_events ORDER BY id
                """
            ).fetchall()
            manifest = [
                {
                    "event_id": row["id"],
                    "event_hash": sha256_json(_load(row["event_json"])),
                    "extraction_status": row["extraction_status"],
                }
                for row in rows
            ]
            manifest_hash = sha256_json(manifest)
            snapshot_id = f"news-{manifest_hash[:20]}"
            connection.execute(
                """
                INSERT INTO news_snapshots (
                    id, manifest_hash, status, item_count
                ) VALUES (?, ?, 'ready', ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (snapshot_id, manifest_hash, len(rows)),
            )
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO news_snapshot_events (
                        snapshot_id, event_id, event_json, extraction_status
                    ) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING
                    """,
                    (snapshot_id, row["id"], row["event_json"], row["extraction_status"]),
                )
            connection.execute(
                "INSERT INTO runtime_state(key, value) VALUES ('news_snapshot', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (snapshot_id,),
            )
        return {
            "snapshot_id": snapshot_id,
            "manifest_hash": manifest_hash,
            "item_count": len(rows),
        }

    def latest_news_snapshot(self) -> dict | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM news_snapshots
                WHERE id = (SELECT value FROM runtime_state WHERE key = 'news_snapshot')
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def snapshot_events(self, snapshot_id: str) -> list[dict]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT s.event_json, s.extraction_status
                FROM news_snapshot_events AS s
                JOIN news_events AS e ON e.id = s.event_id
                WHERE s.snapshot_id = ? ORDER BY e.id
                """,
                (snapshot_id,),
            ).fetchall()
        values = []
        for row in rows:
            value = _load(row["event_json"])
            value["extraction_status"] = row["extraction_status"]
            values.append(value)
        return values

    # --- overviews -----------------------------------------------------------

    def snapshot_event(self, snapshot_id: str, event_id: str) -> dict | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT event_json, extraction_status FROM news_snapshot_events WHERE snapshot_id = ? AND event_id = ?",
                (snapshot_id, event_id),
            ).fetchone()
        if row is None:
            return None
        return {**_load(row["event_json"]), "extraction_status": row["extraction_status"]}

    @contextmanager
    def operation_lease(self, key: str, *, seconds: int = 120):
        owner = uuid4().hex
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM operation_leases WHERE key = ? AND expires_at < ?", (key, time.time()))
            try:
                connection.execute(
                    "INSERT INTO operation_leases(key, owner, expires_at) VALUES (?, ?, ?)",
                    (key, owner, time.time() + seconds),
                )
            except sqlite3.IntegrityError as error:
                raise OperationInProgress("An operation for this context is already running") from error
        try:
            yield
        finally:
            with self.database.transaction(immediate=True) as connection:
                connection.execute("DELETE FROM operation_leases WHERE key = ? AND owner = ?", (key, owner))

    def save_overview(
        self,
        *,
        overview_id: str,
        company_id: str,
        profile_id: str,
        news_snapshot_id: str,
        analytic_focus: str,
        context_id: str,
        state: str,
        generated_at: str,
        coverage_note: str,
        candidates: list[dict],
    ) -> dict:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO overview_snapshots (
                    id, company_id, profile_id, news_snapshot_id,
                    analytic_focus, context_id, state, generated_at,
                    coverage_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(context_id) DO NOTHING
                """,
                (
                    overview_id,
                    company_id,
                    profile_id,
                    news_snapshot_id,
                    analytic_focus,
                    context_id,
                    state,
                    generated_at,
                    coverage_note,
                ),
            )
            stored = connection.execute(
                "SELECT id FROM overview_snapshots WHERE context_id = ?",
                (context_id,),
            ).fetchone()
            actual_id = str(stored["id"])
            for rank, candidate in enumerate(candidates, 1):
                connection.execute(
                    """
                    INSERT INTO event_candidates (
                        overview_id, event_id, rank, relevance, priority,
                        candidate_json, insight_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(overview_id, event_id) DO NOTHING
                    """,
                    (
                        actual_id,
                        candidate["event_id"],
                        rank,
                        candidate["relevance"],
                        candidate["priority"],
                        _dump(candidate),
                        candidate.get("insight_id"),
                    ),
                )
        return self.overview_by_context(context_id)

    def overview_by_context(self, context_id: str) -> dict | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM overview_snapshots WHERE context_id = ?",
                (context_id,),
            ).fetchone()
            if row is None:
                return None
            candidates = connection.execute(
                """
                SELECT candidate_json, insight_id FROM event_candidates
                WHERE overview_id = ? ORDER BY rank
                """,
                (row["id"],),
            ).fetchall()
        items = []
        for candidate_row in candidates:
            item = _load(candidate_row["candidate_json"])
            item["insight_id"] = candidate_row["insight_id"]
            items.append(item)
        return {
            "overview_id": row["id"],
            "company_id": row["company_id"],
            "analytic_focus": row["analytic_focus"],
            "snapshot_id": row["news_snapshot_id"],
            "context_id": row["context_id"],
            "generated_at": row["generated_at"],
            "state": row["state"],
            "coverage_note": row["coverage_note"],
            "items": items,
        }

    def candidate(
        self, context_id: str, event_id: str
    ) -> tuple[dict, str] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT c.candidate_json, c.overview_id
                FROM event_candidates AS c
                JOIN overview_snapshots AS o ON o.id = c.overview_id
                WHERE o.context_id = ? AND c.event_id = ?
                """,
                (context_id, event_id),
            ).fetchone()
        if row is None:
            return None
        return _load(row["candidate_json"]), str(row["overview_id"])

    # --- insights, conversations, feedback ---------------------------------

    def existing_insight(self, context_id: str, event_id: str) -> dict | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT i.*, v.payload_json
                FROM insights AS i
                JOIN insight_versions AS v
                  ON v.insight_id = i.id AND v.version = i.current_version
                WHERE i.context_id = ? AND i.event_id = ?
                """,
                (context_id, event_id),
            ).fetchone()
        return _load(row["payload_json"]) if row is not None else None

    def save_insight(
        self,
        payload: dict,
        *,
        overview_id: str,
        provider: str,
        model_id: str | None,
        prompt_version: str | None,
    ) -> dict:
        now = payload["generated_at"]
        insight_id = payload["insight_id"]
        version = int(payload["insight_version"])
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT id, current_version FROM insights
                WHERE context_id = ? AND event_id = ?
                """,
                (payload["context_id"], payload["event"]["event_id"]),
            ).fetchone()
            if existing is not None:
                row = connection.execute(
                    """
                    SELECT payload_json FROM insight_versions
                    WHERE insight_id = ? AND version = ?
                    """,
                    (existing["id"], existing["current_version"]),
                ).fetchone()
                return _load(row["payload_json"])
            connection.execute(
                """
                INSERT INTO insights (
                    id, company_id, event_id, context_id, analytic_focus,
                    current_version, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    insight_id,
                    payload["company_id"],
                    payload["event"]["event_id"],
                    payload["context_id"],
                    payload["analytic_focus"],
                    version,
                    payload["status"],
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO insight_versions (
                    insight_id, version, schema_version, payload_json,
                    provider, model_id, prompt_version, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    insight_id,
                    version,
                    payload["schema_version"],
                    _dump(payload),
                    provider,
                    model_id,
                    prompt_version,
                    now,
                ),
            )
            for evidence in payload.get("evidence", []):
                connection.execute(
                    """
                    INSERT INTO evidence (
                        id, insight_id, insight_version, source_kind,
                        source_ref, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence["evidence_id"],
                        insight_id,
                        version,
                        evidence["source_type"],
                        str(evidence.get("source_id") or "unknown"),
                        _dump(evidence),
                    ),
                )
            connection.execute(
                """
                UPDATE event_candidates SET insight_id = ?
                WHERE overview_id = ? AND event_id = ?
                """,
                (insight_id, overview_id, payload["event"]["event_id"]),
            )
        return payload

    def insight(self, insight_id: str) -> dict | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT v.payload_json FROM insights AS i
                JOIN insight_versions AS v
                  ON v.insight_id = i.id AND v.version = i.current_version
                WHERE i.id = ?
                """,
                (insight_id,),
            ).fetchone()
        return _load(row["payload_json"]) if row is not None else None

    def save_question(
        self,
        *,
        user_id: str,
        insight: dict,
        question: str,
        answer: dict,
        conversation_id: str | None,
    ) -> dict:
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            if conversation_id:
                row = connection.execute(
                    "SELECT * FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is None or row["user_id"] != user_id:
                    raise ValueError("Conversation is unavailable")
                if (
                    row["insight_id"] != insight["insight_id"]
                    or int(row["insight_version"])
                    != int(insight["insight_version"])
                ):
                    raise ValueError("Conversation context does not match")
            else:
                conversation_id = f"conv-{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO conversations (
                        id, user_id, company_id, insight_id, insight_version,
                        analytic_focus, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        user_id,
                        insight["company_id"],
                        insight["insight_id"],
                        insight["insight_version"],
                        insight["analytic_focus"],
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO messages (id, conversation_id, role, content)
                VALUES (?, ?, 'user', ?)
                """,
                (f"msg-{uuid4().hex}", conversation_id, question),
            )
            connection.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, role, content,
                    evidence_json, unknowns_json
                ) VALUES (?, ?, 'assistant', ?, ?, ?)
                """,
                (
                    f"msg-{uuid4().hex}",
                    conversation_id,
                    answer["answer"],
                    _dump(answer["evidence_ids"]),
                    _dump(answer["unknowns_and_conditions"]),
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        return {
            "conversation_id": conversation_id,
            "insight_id": insight["insight_id"],
            "insight_version": insight["insight_version"],
            "company_id": insight["company_id"],
            "analytic_focus": insight["analytic_focus"],
            **answer,
        }

    def validate_conversation(self, user_id: str, insight: dict, conversation_id: str | None) -> None:
        if conversation_id is None:
            return
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT user_id, insight_id, insight_version FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None or row["user_id"] != user_id:
            raise ValueError("Conversation is unavailable")
        if row["insight_id"] != insight["insight_id"] or row["insight_version"] != insight["insight_version"]:
            raise ValueError("Conversation context does not match")

    def save_trace(self, *, user_id: str, payload: dict, model_id: str, prompt_version: str) -> None:
        trace = payload["agent_trace"]
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO agent_runs (
                    id, user_id, company_id, event_id, insight_id, context_id,
                    operation, status, plan_json, versions_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'insight', 'completed', ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING""",
                (trace["run_id"], user_id, payload["company_id"], payload["event"]["event_id"],
                 payload["insight_id"], payload["context_id"],
                 _dump([step["step"] for step in trace["steps"]]),
                 _dump({"model": model_id, "prompt": prompt_version, "schema": payload["schema_version"]}),
                 payload["generated_at"], now),
            )
            for index, step in enumerate(trace["steps"]):
                connection.execute(
                    """INSERT INTO agent_steps (
                        id, run_id, sequence_number, step_kind, status,
                        output_summary_json, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                    (f"{trace['run_id']}-{index}", trace["run_id"], index, step["step"],
                     step["status"], _dump(step.get("details", {})), payload["generated_at"], now),
                )

    def upsert_feedback(
        self,
        *,
        user_id: str,
        insight: dict,
        rating: str,
        reason: str,
        comment: str | None,
    ) -> dict:
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM feedback
                WHERE user_id = ? AND insight_id = ? AND insight_version = ?
                """,
                (user_id, insight["insight_id"], insight["insight_version"]),
            ).fetchone()
            if existing is None:
                feedback_id = f"fb-{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO feedback (
                        id, user_id, company_id, insight_id, insight_version,
                        rating, reason, comment, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                    """,
                    (
                        feedback_id,
                        user_id,
                        insight["company_id"],
                        insight["insight_id"],
                        insight["insight_version"],
                        rating,
                        reason,
                        comment,
                        now,
                        now,
                    ),
                )
            else:
                feedback_id = str(existing["id"])
                connection.execute(
                    """
                    INSERT INTO feedback_history (
                        id, feedback_id, rating, reason, comment, state,
                        changed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"fbh-{uuid4().hex}",
                        feedback_id,
                        existing["rating"],
                        existing["reason"],
                        existing["comment"],
                        existing["state"],
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE feedback SET rating = ?, reason = ?, comment = ?,
                        state = 'new', updated_at = ? WHERE id = ?
                    """,
                    (rating, reason, comment, now, feedback_id),
                )
            row = connection.execute(
                "SELECT * FROM feedback WHERE id = ?", (feedback_id,)
            ).fetchone()
        return {
            "feedback_id": feedback_id,
            "saved_at": row["updated_at"],
            "rating": row["rating"],
            "reason": row["reason"],
            "state": row["state"],
        }

    # --- bank report corpus --------------------------------------------------

    def import_bank_report(self, report: dict) -> str:
        source_id = str(report["source_id"])
        identity = sha256_json([source_id, report["content_hash"]])[:20]
        document_id = f"bankdoc-{identity}"
        report_id = f"bank-{identity}"
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO news_sources (id, name, kind)
                VALUES (?, ?, 'bank_report')
                ON CONFLICT(id) DO NOTHING
                """,
                (source_id, report["title"]),
            )
            connection.execute(
                """
                INSERT INTO source_documents (
                    id, source_id, external_id, title, published_at,
                    fetched_at, content_hash, extraction_status,
                    metadata_json, synthetic
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ocr_imported', ?, 0)
                ON CONFLICT(id) DO UPDATE SET title = excluded.title,
                    published_at = excluded.published_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    document_id,
                    source_id,
                    report["original_filename"],
                    report["title"],
                    report.get("report_date"),
                    now,
                    report["content_hash"],
                    _dump({"original_filename": report["original_filename"]}),
                ),
            )
            quality = min(
                fragment["ocr_quality"] for fragment in report["fragments"]
            )
            status = (
                "ready"
                if any(fragment["verified"] for fragment in report["fragments"])
                else "needs_review"
            )
            connection.execute(
                """
                INSERT INTO bank_reports (
                    id, source_document_id, report_date, import_status,
                    ocr_quality, report_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    report_date = excluded.report_date,
                    import_status = excluded.import_status,
                    ocr_quality = excluded.ocr_quality,
                    report_json = excluded.report_json
                """,
                (
                    report_id,
                    document_id,
                    report.get("report_date"),
                    status,
                    quality,
                    _dump(report),
                ),
            )
            # Replace the complete OCR revision atomically, including revoked
            # verification and pages removed by an operator.
            connection.execute("DELETE FROM bank_report_fragments WHERE report_id = ?", (report_id,))
            connection.execute("DELETE FROM bank_report_pages WHERE report_id = ?", (report_id,))
            for fragment in report["fragments"]:
                page = int(fragment["page_number"])
                connection.execute(
                    """
                    INSERT INTO bank_report_pages (
                        report_id, page_number, ocr_text, ocr_quality, verified
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(report_id, page_number) DO UPDATE SET
                        ocr_text = bank_report_pages.ocr_text || char(10) || excluded.ocr_text,
                        ocr_quality = MIN(bank_report_pages.ocr_quality, excluded.ocr_quality),
                        verified = MIN(bank_report_pages.verified, excluded.verified)
                    """,
                    (
                        report_id,
                        page,
                        fragment["text"],
                        fragment["ocr_quality"],
                        int(fragment["verified"]),
                    ),
                )
                fragment_id = f"frag-{sha256_json([report_id, page, fragment['text']])[:20]}"
                connection.execute(
                    """
                    INSERT OR REPLACE INTO bank_report_fragments (
                        id, report_id, page_number, fragment_text,
                        locator_json, verified
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fragment_id,
                        report_id,
                        page,
                        fragment["text"],
                        _dump({"page_number": page}),
                        int(fragment["verified"]),
                    ),
                )
        return report_id

    def bank_corpus(self) -> tuple[bool, list[dict]]:
        with self.database.connection() as connection:
            report_count = connection.execute(
                "SELECT COUNT(*) AS n FROM bank_reports"
            ).fetchone()["n"]
            rows = connection.execute(
                """
                SELECT d.source_id, d.title, r.report_date,
                       f.page_number, f.fragment_text AS text,
                       p.ocr_quality, f.verified
                FROM bank_report_fragments AS f
                JOIN bank_reports AS r ON r.id = f.report_id
                JOIN source_documents AS d ON d.id = r.source_document_id
                JOIN bank_report_pages AS p
                  ON p.report_id = f.report_id
                 AND p.page_number = f.page_number
                ORDER BY d.source_id, f.page_number, f.id
                """
            ).fetchall()
        return bool(report_count), [dict(row) for row in rows]

    def bank_corpus_hash(self) -> str:
        with self.database.connection() as connection:
            rows = connection.execute("SELECT id, report_json FROM bank_reports ORDER BY id").fetchall()
        return sha256_json([dict(row) for row in rows])


class OperationInProgress(RuntimeError):
    pass


__all__ = ["Repository"]

