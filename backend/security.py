"""Framework-neutral demo authentication and authorization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import secrets
import sqlite3
from typing import Final
from uuid import uuid4


MAX_BEARER_TOKEN_BYTES: Final = 1024


class AuthenticationError(PermissionError):
    """The request did not contain a valid active demo session."""


class AccessDenied(PermissionError):
    """The authenticated user may not access the requested resource."""


@dataclass(frozen=True, slots=True)
class AuthContext:
    user_id: str
    user_label: str
    session_id: str


def _utc_iso(value: datetime | None = None) -> str:
    current = datetime.now(timezone.utc) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds")


def extract_bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise AuthenticationError("Bearer authentication is required")
    scheme, separator, token = authorization.strip().partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("Invalid Authorization header")
    token = token.strip()
    try:
        encoded = token.encode("utf-8")
    except UnicodeEncodeError as error:
        raise AuthenticationError("Invalid bearer token") from error
    if len(encoded) > MAX_BEARER_TOKEN_BYTES:
        raise AuthenticationError("Invalid bearer token")
    return token


def token_digest(token: str, pepper: str) -> str:
    if not token or not pepper:
        raise ValueError("token and pepper must be non-empty")
    return hmac.new(
        pepper.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def issue_demo_session(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    pepper: str,
    expires_at: datetime | None = None,
    token: str | None = None,
) -> tuple[str, str]:
    """Store only a token digest and return the secret token exactly once."""

    if not user_id.strip():
        raise ValueError("user_id must be non-empty")
    exists = connection.execute(
        "SELECT 1 FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if exists is None:
        raise ValueError("Cannot issue a session for an unknown user")
    raw_token = secrets.token_urlsafe(32) if token is None else token
    if not raw_token or len(raw_token.encode("utf-8")) > MAX_BEARER_TOKEN_BYTES:
        raise ValueError("token length is invalid")
    session_id = str(uuid4())
    expiry = _utc_iso(expires_at) if expires_at is not None else None
    connection.execute(
        """
        INSERT INTO demo_sessions
            (id, user_id, token_digest, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (session_id, user_id, token_digest(raw_token, pepper), expiry),
    )
    return session_id, raw_token


def authenticate_bearer(
    connection: sqlite3.Connection,
    authorization: str | None,
    *,
    pepper: str,
    now: datetime | None = None,
) -> AuthContext:
    """Authenticate against server-side sessions using constant-time compare."""

    token = extract_bearer_token(authorization)
    supplied_digest = token_digest(token, pepper)
    current = _utc_iso(now)
    rows = connection.execute(
        """
        SELECT s.id AS session_id, s.token_digest, u.id AS user_id,
               u.label AS user_label
        FROM demo_sessions AS s
        JOIN users AS u ON u.id = s.user_id
        WHERE s.revoked_at IS NULL
          AND (s.expires_at IS NULL OR s.expires_at > ?)
          AND u.is_active = 1
        ORDER BY s.id
        """,
        (current,),
    ).fetchall()

    matched: sqlite3.Row | None = None
    dummy = "0" * len(supplied_digest)
    if not rows:
        hmac.compare_digest(supplied_digest, dummy)
    for row in rows:
        is_match = hmac.compare_digest(
            supplied_digest, str(row["token_digest"])
        )
        if is_match:
            matched = row
    if matched is None:
        raise AuthenticationError("Invalid or expired demo session")
    return AuthContext(
        user_id=str(matched["user_id"]),
        user_label=str(matched["user_label"]),
        session_id=str(matched["session_id"]),
    )


def revoke_demo_session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    cursor = connection.execute(
        """
        UPDATE demo_sessions
        SET revoked_at = COALESCE(revoked_at, ?)
        WHERE id = ?
        """,
        (_utc_iso(now), session_id),
    )
    return cursor.rowcount > 0


def grant_company_access(
    connection: sqlite3.Connection, *, user_id: str, company_id: str
) -> None:
    connection.execute(
        """
        INSERT INTO user_company_access (user_id, company_id)
        VALUES (?, ?)
        ON CONFLICT(user_id, company_id) DO NOTHING
        """,
        (user_id, company_id),
    )


def has_company_access(
    connection: sqlite3.Connection, *, user_id: str, company_id: str
) -> bool:
    return connection.execute(
        """
        SELECT 1
        FROM user_company_access
        WHERE user_id = ? AND company_id = ?
        """,
        (user_id, company_id),
    ).fetchone() is not None


def require_company_access(
    connection: sqlite3.Connection, *, user_id: str, company_id: str
) -> None:
    if not has_company_access(
        connection, user_id=user_id, company_id=company_id
    ):
        raise AccessDenied("Resource is unavailable")


_RESOURCE_COMPANY_QUERIES: Final[dict[str, str]] = {
    "overview": "SELECT company_id FROM overview_snapshots WHERE id = ?",
    "insight": "SELECT company_id FROM insights WHERE id = ?",
    "conversation": "SELECT company_id FROM conversations WHERE id = ?",
    "feedback": "SELECT company_id FROM feedback WHERE id = ?",
}


def require_resource_access(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    resource_kind: str,
    resource_id: str,
) -> str:
    """Resolve a resource to its company, then enforce company isolation."""

    query = _RESOURCE_COMPANY_QUERIES.get(resource_kind)
    if query is None:
        raise ValueError(f"Unsupported resource kind: {resource_kind}")
    row = connection.execute(query, (resource_id,)).fetchone()
    if row is None:
        raise AccessDenied("Resource is unavailable")
    company_id = str(row["company_id"])
    require_company_access(
        connection, user_id=user_id, company_id=company_id
    )
    return company_id


__all__ = [
    "AccessDenied",
    "AuthContext",
    "AuthenticationError",
    "authenticate_bearer",
    "extract_bearer_token",
    "grant_company_access",
    "has_company_access",
    "issue_demo_session",
    "require_company_access",
    "require_resource_access",
    "revoke_demo_session",
    "token_digest",
]
