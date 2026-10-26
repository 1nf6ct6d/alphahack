"""Idempotent synthetic demo seed used by tests and Docker demo mode."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from typing import Iterator

from ml_ranking.validation import validate_contract

from .db import Database
from .repository import Repository
from .security import MAX_BEARER_TOKEN_BYTES, issue_demo_session, token_digest
from .services import RULES_VERSION, load_demo_bundle


DEMO_USERS = (
    ("TEST_A", "Тестовый пользователь A", "U_5639", "demo-test-a", 1),
    ("TEST_B", "Тестовый пользователь B", "U_123123", "demo-test-b", 2),
)


class SeedConflictError(ValueError):
    """Demo initialization would overwrite data or misassign credentials."""


class _SeedDatabase(Database):
    """Reuse one writer transaction for all repository seed operations."""

    def __init__(self, connection: sqlite3.Connection):
        self._seed_connection = connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        yield self._seed_connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        yield self._seed_connection


def _configured_tokens(tokens: dict[str, str] | None) -> dict[str, str]:
    supplied = tokens or {}
    expected_users = {item[0] for item in DEMO_USERS}
    if set(supplied) - expected_users:
        raise SeedConflictError("Demo token configuration contains an unknown user")
    result = {
        user_id: supplied.get(user_id, default_token)
        for user_id, _label, _company_id, default_token, _scenario in DEMO_USERS
    }
    for user_id, token in result.items():
        if (
            not isinstance(token, str)
            or not token
            or any(character.isspace() for character in token)
            or len(token.encode("utf-8")) > MAX_BEARER_TOKEN_BYTES
        ):
            raise SeedConflictError(f"Invalid demo token configured for {user_id}")
    if len(set(result.values())) != len(result):
        raise SeedConflictError("Each demo user must have a unique token")
    return result


def _existing_session(
    connection: sqlite3.Connection, user_id: str, token: str, pepper: str
) -> bool:
    row = connection.execute(
        "SELECT user_id, revoked_at, expires_at FROM demo_sessions WHERE token_digest = ?",
        (token_digest(token, pepper),),
    ).fetchone()
    if row is None:
        return False
    if row["user_id"] != user_id:
        raise SeedConflictError(
            f"Configured demo token for {user_id} is already owned by another user"
        )
    if row["revoked_at"] is not None:
        raise SeedConflictError(
            f"Configured demo token for {user_id} was revoked; configure a new token"
        )
    if row["expires_at"] is not None:
        try:
            expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise SeedConflictError("Existing demo session has an invalid expiry") from error
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise SeedConflictError(
                f"Configured demo token for {user_id} expired; configure a new token"
            )
    return True


def _validate_existing_data(
    connection: sqlite3.Connection, bundles: dict[str, tuple], issued: dict[str, str], pepper: str
) -> None:
    expected_profiles = {
        bundle[0]["meta"]["profile_id"]: bundle for bundle in bundles.values()
    }
    for row in connection.execute(
        "SELECT profile_id, company_id, profile_json FROM company_profile_versions"
    ):
        expected = expected_profiles.get(row["profile_id"])
        if (
            expected is None
            or row["company_id"] != expected[0]["meta"]["company_id"]
            or json.loads(row["profile_json"]) != expected[0]
        ):
            raise SeedConflictError(
                "Demo seed refused: database contains non-demo profiles. "
                "Set ALFA_DEMO_SEED=0 or use a separate demo database."
            )
        expected_views = dict(zip(("MLView", "InterpreterView", "UIView"), expected[1:]))
        stored_views = {
            view["view_kind"]: json.loads(view["view_json"])
            for view in connection.execute(
                "SELECT view_kind, view_json FROM profile_views WHERE profile_id = ?",
                (row["profile_id"],),
            )
        }
        if stored_views != expected_views:
            raise SeedConflictError("Demo seed refused: existing profile views differ from fixtures")

    if connection.execute(
        "SELECT 1 FROM news_events WHERE synthetic = 0 LIMIT 1"
    ).fetchone() is not None:
        raise SeedConflictError(
            "Demo seed refused: database contains real news. "
            "Set ALFA_DEMO_SEED=0 or use a separate demo database."
        )

    for user_id, _label, company_id, _default_token, _scenario in DEMO_USERS:
        company = connection.execute(
            "SELECT source_record_json FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        if company is not None:
            metadata = json.loads(company["source_record_json"] or "{}")
            if not isinstance(metadata, dict) or metadata.get("synthetic") is not True:
                raise SeedConflictError(
                    f"Demo seed refused: {company_id} is not marked as synthetic. "
                    "Set ALFA_DEMO_SEED=0 or use a separate demo database."
                )
        user = connection.execute(
            "SELECT is_active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is not None and not user["is_active"]:
            raise SeedConflictError(f"Demo seed will not reactivate disabled user {user_id}")
        if connection.execute(
            "SELECT 1 FROM user_company_access WHERE user_id = ? AND company_id <> ? LIMIT 1",
            (user_id, company_id),
        ).fetchone() is not None:
            raise SeedConflictError(
                f"Demo seed refused: {user_id} has access beyond its demo company"
            )
        _existing_session(connection, user_id, issued[user_id], pepper)


def seed_demo(
    database: Database,
    *,
    root: str | Path,
    pepper: str,
    tokens: dict[str, str] | None = None,
) -> dict[str, str]:
    """Atomically add absent demo fixtures without overwriting existing data.

    A database containing real news or non-demo profiles is rejected with an
    actionable error. Turn off ``ALFA_DEMO_SEED`` before importing real data.
    Existing active tokens are reused only for their original owner.
    """

    root = Path(root)
    issued = _configured_tokens(tokens)
    bundles = {}
    for _user_id, _label, company_id, _default_token, scenario in DEMO_USERS:
        bundle = load_demo_bundle(
            root, scenario, company_id
        )
        validate_contract(bundle[0], "profile")
        validate_contract(bundle[1], "view")
        bundles[company_id] = bundle

    records = json.loads(
        (root / "ml_ranking" / "examples" / "synthetic_events.json").read_text(
            encoding="utf-8"
        )
    )
    if not isinstance(records, list) or any(record.get("synthetic") is not True for record in records):
        raise SeedConflictError("Demo event fixtures must all be explicitly synthetic")

    with database.transaction(immediate=True) as connection:
        _validate_existing_data(connection, bundles, issued, pepper)
        repository = Repository(_SeedDatabase(connection))
        for user_id, label, company_id, _default_token, scenario in DEMO_USERS:
            # Existing users, companies and profiles are deliberately not upserted.
            connection.execute(
                "INSERT INTO users (id, label) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                (user_id, label),
            )
            bundle = bundles[company_id]
            if connection.execute(
                "SELECT 1 FROM company_profile_versions WHERE profile_id = ?",
                (bundle[0]["meta"]["profile_id"],),
            ).fetchone() is None:
                repository.save_profile_bundle(
                    *bundle,
                    rules_version=RULES_VERSION,
                    display_name=f"{company_id} — синтетический демо-профиль",
                    source_record={
                        "synthetic": True,
                        "source_fixture": f"scenario_s{scenario}",
                    },
                )
            repository.grant_access(user_id, company_id)
            if not _existing_session(connection, user_id, issued[user_id], pepper):
                issue_demo_session(
                    connection,
                    user_id=user_id,
                    pepper=pepper,
                    token=issued[user_id],
                )
        for record in records:
            if connection.execute(
                "SELECT 1 FROM news_events WHERE id = ?", (record["event_id"],)
            ).fetchone() is None:
                repository.upsert_event(
                    record,
                    extraction_status=(
                        "full_text" if record.get("content") else "metadata_only"
                    ),
                )
        repository.create_news_snapshot()
    return issued


__all__ = ["DEMO_USERS", "SeedConflictError", "seed_demo"]

