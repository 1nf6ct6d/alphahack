from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from os import environ as process_environ
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):


def _required_text(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise ConfigurationError(f"Environment variable {key} is required")
    return value


def _positive_float(values: Mapping[str, str], key: str, default: str) -> float:
    raw = values.get(key, default).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigurationError(f"{key} must be a number") from error
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{key} must be greater than zero")
    return value


def _rubles_to_kopecks(
    values: Mapping[str, str], key: str, default: str
) -> int:
    raw = values.get(key, default).strip()
    try:
        rubles = Decimal(raw)
    except InvalidOperation as error:
        raise ConfigurationError(f"{key} must be a decimal amount") from error
    if not rubles.is_finite() or rubles < 0:
        raise ConfigurationError(f"{key} must be a finite non-negative amount")
    kopecks = (rubles * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(kopecks)


def _csv_hosts(values: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw = values.get(key, "www.cbr.ru,cbr.ru,www.interfax.ru,interfax.ru")
    hosts = tuple(sorted({
        item.strip().lower().rstrip(".")
        for item in raw.split(",")
        if item.strip()
    }))
    if not hosts:
        raise ConfigurationError(f"{key} must contain at least one host")
    if any("/" in host or ":" in host for host in hosts):
        raise ConfigurationError(f"{key} must contain host names, not URLs")
    return hosts


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings loaded from ``ALFA_*`` environment variables."""

    environment: str
    database_path: Path
    sqlite_timeout_seconds: float
    auth_token_pepper: str
    budget_timezone: str
    daily_budget_limit_kopecks: int
    internal_budget_limit_kopecks: int
    allowed_source_hosts: tuple[str, ...]

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "Settings":
        source = process_environ if values is None else values
        environment = source.get("ALFA_ENV", "development").strip().lower()
        if environment not in {"development", "test", "production"}:
            raise ConfigurationError(
                "ALFA_ENV must be development, test, or production"
            )

        raw_path = source.get("ALFA_DATABASE_PATH", "data/backend.sqlite3").strip()
        if not raw_path:
            raise ConfigurationError("ALFA_DATABASE_PATH must not be empty")
        database_path = Path(raw_path).expanduser()

        pepper = source.get("ALFA_AUTH_TOKEN_PEPPER", "").strip()
        if not pepper:
            if environment == "production":
                raise ConfigurationError(
                    "ALFA_AUTH_TOKEN_PEPPER is required in production"
                )
            pepper = "development-only-change-before-production"
        if len(pepper.encode("utf-8")) < 24:
            raise ConfigurationError(
                "ALFA_AUTH_TOKEN_PEPPER must be at least 24 UTF-8 bytes"
            )

        timezone_name = source.get(
            "ALFA_BUDGET_TIMEZONE", "Europe/Moscow"
        ).strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ConfigurationError(
                f"Unknown ALFA_BUDGET_TIMEZONE: {timezone_name}"
            ) from error

        daily_limit = _rubles_to_kopecks(
            source, "ALFA_DAILY_BUDGET_RUB", "500"
        )
        internal_limit = _rubles_to_kopecks(
            source, "ALFA_INTERNAL_BUDGET_RUB", "450"
        )
        if internal_limit > daily_limit:
            raise ConfigurationError(
                "ALFA_INTERNAL_BUDGET_RUB must not exceed "
                "ALFA_DAILY_BUDGET_RUB"
            )

        return cls(
            environment=environment,
            database_path=database_path,
            sqlite_timeout_seconds=_positive_float(
                source, "ALFA_SQLITE_TIMEOUT_SECONDS", "10"
            ),
            auth_token_pepper=pepper,
            budget_timezone=timezone_name,
            daily_budget_limit_kopecks=daily_limit,
            internal_budget_limit_kopecks=internal_limit,
            allowed_source_hosts=_csv_hosts(
                source, "ALFA_ALLOWED_SOURCE_HOSTS"
            ),
        )


__all__ = ["ConfigurationError", "Settings"]
