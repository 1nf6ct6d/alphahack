"""Import lifecycle tracking and deterministic profile import orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from .db import Database
from .domain import canonical_json, utc_now

if TYPE_CHECKING:
    from .repository import Repository


class ImportStateError(RuntimeError):
    """Raised when an import run cannot make the requested transition."""


@dataclass(frozen=True, slots=True)
class ImportRun:
    import_id: str
    kind: str
    status: str
    source_ref: str | None
    manifest_hash: str | None
    stats: dict[str, Any]
    error_summary: str | None
    started_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class ImportRowError:
    error_id: str
    import_id: str
    row_ref: str | None
    code: str
    message: str
    details: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ProfileImportResult:
    import_id: str
    profile_id: str
    profile_hash: str
    reused: bool


def _non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _json_object(value: dict[str, Any] | None) -> str:
    return canonical_json(value or {})


class ImportTracker:
    """Persist import runs and row-level errors in the existing schema."""

    def __init__(self, database: Database):
        self.database = database

    def start(
        self,
        kind: str,
        *,
        source_ref: str | None = None,
        manifest_hash: str | None = None,
        stats: dict[str, Any] | None = None,
        import_id: str | None = None,
    ) -> ImportRun:
        kind = _non_empty(kind, "kind")
        identifier = (
            f"import-{uuid4().hex}"
            if import_id is None
            else _non_empty(import_id, "import_id")
        )
        if manifest_hash is not None:
            manifest_hash = _non_empty(manifest_hash, "manifest_hash")
        started_at = utc_now()
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM imports WHERE id = ?", (identifier,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["kind"] != kind
                    or existing["source_ref"] != source_ref
                    or existing["manifest_hash"] != manifest_hash
                ):
                    raise ImportStateError(
                        "import_id already exists with different metadata"
                    )
                return self._run(existing)
            connection.execute(
                """
                INSERT INTO imports (
                    id, kind, status, source_ref, manifest_hash,
                    stats_json, started_at
                ) VALUES (?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    identifier,
                    kind,
                    source_ref,
                    manifest_hash,
                    _json_object(stats),
                    started_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM imports WHERE id = ?", (identifier,)
            ).fetchone()
        return self._run(row)

    def succeed(
        self,
        import_id: str,
        *,
        stats: dict[str, Any] | None = None,
        manifest_hash: str | None = None,
    ) -> ImportRun:
        return self._finish(
            import_id,
            status="succeeded",
            stats=stats,
            manifest_hash=manifest_hash,
            error_summary=None,
        )

    def fail(
        self,
        import_id: str,
        error_summary: str,
        *,
        stats: dict[str, Any] | None = None,
        manifest_hash: str | None = None,
    ) -> ImportRun:
        return self._finish(
            import_id,
            status="failed",
            stats=stats,
            manifest_hash=manifest_hash,
            error_summary=_non_empty(error_summary, "error_summary"),
        )

    def add_error(
        self,
        import_id: str,
        *,
        code: str,
        message: str,
        row_ref: str | None = None,
        details: dict[str, Any] | None = None,
        error_id: str | None = None,
    ) -> ImportRowError:
        identifier = (
            f"import-error-{uuid4().hex}"
            if error_id is None
            else _non_empty(error_id, "error_id")
        )
        code = _non_empty(code, "code")
        message = _non_empty(message, "message")
        created_at = utc_now()
        with self.database.transaction(immediate=True) as connection:
            run = connection.execute(
                "SELECT status FROM imports WHERE id = ?", (import_id,)
            ).fetchone()
            if run is None:
                raise ImportStateError("unknown import_id")
            connection.execute(
                """
                INSERT INTO import_errors (
                    id, import_id, row_ref, code, message,
                    details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    import_id,
                    row_ref,
                    code,
                    message,
                    _json_object(details),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM import_errors WHERE id = ?", (identifier,)
            ).fetchone()
        return self._row_error(row)

    def get(self, import_id: str) -> ImportRun | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM imports WHERE id = ?", (import_id,)
            ).fetchone()
        return self._run(row) if row is not None else None

    def errors(self, import_id: str) -> list[ImportRowError]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM import_errors
                WHERE import_id = ? ORDER BY created_at, id
                """,
                (import_id,),
            ).fetchall()
        return [self._row_error(row) for row in rows]

    def find_succeeded(
        self, *, kind: str, manifest_hash: str
    ) -> ImportRun | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM imports
                WHERE kind = ? AND manifest_hash = ? AND status = 'succeeded'
                ORDER BY finished_at, id LIMIT 1
                """,
                (kind, manifest_hash),
            ).fetchone()
        return self._run(row) if row is not None else None

    def _finish(
        self,
        import_id: str,
        *,
        status: str,
        stats: dict[str, Any] | None,
        manifest_hash: str | None,
        error_summary: str | None,
    ) -> ImportRun:
        if manifest_hash is not None:
            manifest_hash = _non_empty(manifest_hash, "manifest_hash")
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM imports WHERE id = ?", (import_id,)
            ).fetchone()
            if row is None:
                raise ImportStateError("unknown import_id")
            if row["status"] != "running":
                if row["status"] == status:
                    return self._run(row)
                raise ImportStateError(
                    f"cannot change a {row['status']} import to {status}"
                )
            final_hash = manifest_hash or row["manifest_hash"]
            final_stats = (
                _json_object(stats)
                if stats is not None
                else row["stats_json"] or "{}"
            )
            connection.execute(
                """
                UPDATE imports
                SET status = ?, manifest_hash = ?, stats_json = ?,
                    error_summary = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    final_hash,
                    final_stats,
                    error_summary,
                    utc_now(),
                    import_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM imports WHERE id = ?", (import_id,)
            ).fetchone()
        return self._run(updated)

    @staticmethod
    def _run(row: sqlite3.Row) -> ImportRun:
        return ImportRun(
            import_id=str(row["id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            source_ref=row["source_ref"],
            manifest_hash=row["manifest_hash"],
            stats=json.loads(row["stats_json"] or "{}"),
            error_summary=row["error_summary"],
            started_at=str(row["started_at"]),
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _row_error(row: sqlite3.Row) -> ImportRowError:
        return ImportRowError(
            error_id=str(row["id"]),
            import_id=str(row["import_id"]),
            row_ref=row["row_ref"],
            code=str(row["code"]),
            message=str(row["message"]),
            details=json.loads(row["details_json"] or "{}"),
            created_at=str(row["created_at"]),
        )


def _build_profile_bundle(*args: Any, **kwargs: Any) -> dict[str, dict]:
    from .profiles import build_profile_bundle

    return build_profile_bundle(*args, **kwargs)


def _source_ref(
    company_id: str, company_csv: str | Path, payment_csv: str | Path
) -> str:
    return canonical_json({
        "company_id": company_id,
        "company_csv": str(Path(company_csv)),
        "payment_csv": str(Path(payment_csv)),
    })


def _profile_stats(bundle: dict[str, dict]) -> dict[str, Any]:
    profile = bundle["profile"]
    return {
        "company_id": profile["meta"]["company_id"],
        "profile_id": profile["meta"]["profile_id"],
        "usability": profile["input_quality"]["usability"],
        "fact_count": len(profile.get("facts", [])),
        "derived_metric_count": len(profile.get("derived_metrics", [])),
        "conflict_count": len(profile.get("data_conflicts", [])),
    }


def import_profile_bundle(
    repository: "Repository",
    tracker: ImportTracker,
    company_id: str,
    company_csv: str | Path,
    payment_csv: str | Path,
    *,
    as_of_date: str | None = None,
    assume_additive_rows: bool = True,
    encoding: str = "auto",
    ttl_hours: int = 168,
    rules_version: str = "backend-profile-1.0.0",
    display_name: str | None = None,
    source_record: dict[str, Any] | None = None,
) -> ProfileImportResult:
    """Build, persist, and track one profile bundle.

    Repeating an import whose assembled profile has the same ``input_hash``
    returns the earlier successful import without calling the repository again.
    """

    source_ref = _source_ref(company_id, company_csv, payment_csv)
    try:
        bundle = _build_profile_bundle(
            company_id,
            company_csv,
            payment_csv,
            as_of_date=as_of_date,
            assume_additive_rows=assume_additive_rows,
            encoding=encoding,
            ttl_hours=ttl_hours,
        )
    except Exception as error:
        run = tracker.start("profile_bundle", source_ref=source_ref)
        tracker.add_error(
            run.import_id,
            row_ref=company_id,
            code="profile_build_failed",
            message=str(error) or type(error).__name__,
            details={"exception_type": type(error).__name__},
        )
        tracker.fail(
            run.import_id,
            str(error) or type(error).__name__,
            stats={"company_id": company_id},
        )
        raise

    profile = bundle["profile"]
    profile_hash = _non_empty(
        str(profile["meta"]["input_hash"]), "profile input_hash"
    )
    profile_id = _non_empty(
        str(profile["meta"]["profile_id"]), "profile_id"
    )
    existing = tracker.find_succeeded(
        kind="profile_bundle", manifest_hash=profile_hash
    )
    if existing is not None:
        existing_profile_id = str(existing.stats.get("profile_id") or profile_id)
        return ProfileImportResult(
            import_id=existing.import_id,
            profile_id=existing_profile_id,
            profile_hash=profile_hash,
            reused=True,
        )

    run = tracker.start(
        "profile_bundle",
        source_ref=source_ref,
        manifest_hash=profile_hash,
        stats={"company_id": company_id},
    )
    try:
        stored_profile_id = repository.save_profile_bundle(
            profile,
            bundle["ml_view"],
            bundle["interpreter_view"],
            bundle["ui_view"],
            rules_version=rules_version,
            display_name=display_name,
            source_record=source_record,
        )
    except Exception as error:
        tracker.add_error(
            run.import_id,
            row_ref=company_id,
            code="profile_save_failed",
            message=str(error) or type(error).__name__,
            details={"exception_type": type(error).__name__},
        )
        tracker.fail(
            run.import_id,
            str(error) or type(error).__name__,
            stats={"company_id": company_id, "profile_id": profile_id},
            manifest_hash=profile_hash,
        )
        raise

    stats = _profile_stats(bundle)
    stats["profile_id"] = stored_profile_id
    tracker.succeed(
        run.import_id, stats=stats, manifest_hash=profile_hash
    )
    return ProfileImportResult(
        import_id=run.import_id,
        profile_id=stored_profile_id,
        profile_hash=profile_hash,
        reused=False,
    )


__all__ = [
    "ImportRowError",
    "ImportRun",
    "ImportStateError",
    "ImportTracker",
    "ProfileImportResult",
    "import_profile_bundle",
]
