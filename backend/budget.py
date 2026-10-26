

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import Database


class BudgetError(RuntimeError):



class BudgetExceeded(BudgetError):



class ReservationStateError(BudgetError):



@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    budget_date: str
    confirmed_kopecks: int
    reserved_kopecks: int
    internal_limit_kopecks: int

    @property
    def available_kopecks(self) -> int:
        return max(
            0,
            self.internal_limit_kopecks
            - self.confirmed_kopecks
            - self.reserved_kopecks,
        )


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    budget_date: str
    operation: str
    max_cost_kopecks: int
    status: str
    actual_cost_kopecks: int | None


def _validate_kopecks(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class BudgetManager:

    def __init__(
        self,
        database: Database,
        *,
        internal_limit_kopecks: int,
        timezone_name: str = "Europe/Moscow",
    ):
        _validate_kopecks(internal_limit_kopecks, "internal_limit_kopecks")
        self.database = database
        self.internal_limit_kopecks = internal_limit_kopecks
        self.timezone = ZoneInfo(timezone_name)

    def _now(self, value: datetime | None) -> datetime:
        current = datetime.now(timezone.utc) if value is None else value
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("datetime must include a timezone")
        return current

    def _budget_date(self, value: datetime) -> str:
        return value.astimezone(self.timezone).date().isoformat()

    def _snapshot_on(
        self, connection: sqlite3.Connection, budget_date: str
    ) -> BudgetSnapshot:
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status = 'committed'
                    THEN actual_cost_kopecks ELSE 0 END), 0) AS confirmed,
                COALESCE(SUM(CASE WHEN status = 'reserved'
                    THEN max_cost_kopecks ELSE 0 END), 0) AS reserved
            FROM budget_reservations
            WHERE budget_date = ?
            """,
            (budget_date,),
        ).fetchone()
        return BudgetSnapshot(
            budget_date=budget_date,
            confirmed_kopecks=int(row["confirmed"]),
            reserved_kopecks=int(row["reserved"]),
            internal_limit_kopecks=self.internal_limit_kopecks,
        )

    def snapshot(
        self, *, value: datetime | date | None = None
    ) -> BudgetSnapshot:
        if isinstance(value, datetime) or value is None:
            budget_date = self._budget_date(self._now(value))
        elif isinstance(value, date):
            budget_date = value.isoformat()
        else:
            raise TypeError("value must be date, datetime, or None")
        with self.database.connection() as connection:
            return self._snapshot_on(connection, budget_date)

    def reserve(
        self,
        *,
        operation: str,
        max_cost_kopecks: int,
        user_id: str | None = None,
        context_id: str | None = None,
        reservation_id: str | None = None,
        now: datetime | None = None,
    ) -> BudgetReservation:
        if not operation.strip():
            raise ValueError("operation must be non-empty")
        _validate_kopecks(max_cost_kopecks, "max_cost_kopecks")
        current = self._now(now)
        budget_date = self._budget_date(current)
        identifier = str(uuid4()) if reservation_id is None else reservation_id
        if not identifier.strip():
            raise ValueError("reservation_id must be non-empty")

        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM budget_reservations WHERE id = ?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                expected = (
                    budget_date,
                    operation,
                    max_cost_kopecks,
                    user_id,
                    context_id,
                )
                actual = (
                    existing["budget_date"],
                    existing["operation"],
                    existing["max_cost_kopecks"],
                    existing["user_id"],
                    existing["context_id"],
                )
                if actual != expected:
                    raise ReservationStateError(
                        "reservation_id already exists with different data"
                    )
                return self._reservation(existing)

            snapshot = self._snapshot_on(connection, budget_date)
            projected = (
                snapshot.confirmed_kopecks
                + snapshot.reserved_kopecks
                + max_cost_kopecks
            )
            if projected > self.internal_limit_kopecks:
                raise BudgetExceeded(
                    "confirmed + reserved + requested maximum exceeds "
                    "the internal daily limit"
                )
            connection.execute(
                """
                INSERT INTO budget_reservations (
                    id, budget_date, user_id, context_id, operation,
                    max_cost_kopecks, status, reserved_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    identifier,
                    budget_date,
                    user_id,
                    context_id,
                    operation,
                    max_cost_kopecks,
                    _utc_iso(current),
                ),
            )
            row = connection.execute(
                "SELECT * FROM budget_reservations WHERE id = ?",
                (identifier,),
            ).fetchone()
            return self._reservation(row)

    def commit(
        self,
        reservation_id: str,
        *,
        actual_cost_kopecks: int,
        model_id: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        elapsed_ms: int | None = None,
        cache_hit: bool = False,
        now: datetime | None = None,
    ) -> BudgetReservation:
        _validate_kopecks(actual_cost_kopecks, "actual_cost_kopecks")
        for name, value in (
            ("tokens_in", tokens_in),
            ("tokens_out", tokens_out),
            ("elapsed_ms", elapsed_ms),
        ):
            if value is not None:
                _validate_kopecks(value, name)
        current = self._now(now)
        with self.database.transaction(immediate=True) as connection:
            row = self._get_required(connection, reservation_id)
            if row["status"] == "committed":
                if int(row["actual_cost_kopecks"]) != actual_cost_kopecks:
                    raise ReservationStateError(
                    )
                return self._reservation(row)
            if row["status"] != "reserved":
                raise ReservationStateError(
                    f"cannot commit a {row['status']} reservation"
                )
            if actual_cost_kopecks > int(row["max_cost_kopecks"]):
                raise ReservationStateError(
                )
            finalized = _utc_iso(current)
            connection.execute(
                """
                UPDATE budget_reservations
                SET status = 'committed', actual_cost_kopecks = ?,
                    finalized_at = ?
                WHERE id = ?
                """,
                (actual_cost_kopecks, finalized, reservation_id),
            )
            connection.execute(
                """
                INSERT INTO usage_ledger (
                    id, reservation_id, budget_date, user_id, context_id,
                    operation, model_id, tokens_in, tokens_out,
                    cost_kopecks, elapsed_ms, cache_hit, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)
                """,
                (
                    str(uuid4()),
                    reservation_id,
                    row["budget_date"],
                    row["user_id"],
                    row["context_id"],
                    row["operation"],
                    model_id,
                    tokens_in,
                    tokens_out,
                    actual_cost_kopecks,
                    elapsed_ms,
                    int(cache_hit),
                    finalized,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM budget_reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
            return self._reservation(updated)

    def release(
        self, reservation_id: str, *, now: datetime | None = None
    ) -> BudgetReservation:
        current = self._now(now)
        with self.database.transaction(immediate=True) as connection:
            row = self._get_required(connection, reservation_id)
            if row["status"] == "released":
                return self._reservation(row)
            if row["status"] == "committed":
                raise ReservationStateError(
                    "a committed reservation cannot be released"
                )
            connection.execute(
                """
                UPDATE budget_reservations
                SET status = 'released', finalized_at = ?
                WHERE id = ?
                """,
                (_utc_iso(current), reservation_id),
            )
            updated = connection.execute(
                "SELECT * FROM budget_reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
            return self._reservation(updated)

    @staticmethod
    def _get_required(
        connection: sqlite3.Connection, reservation_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM budget_reservations WHERE id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ReservationStateError("unknown reservation")
        return row

    @staticmethod
    def _reservation(row: sqlite3.Row) -> BudgetReservation:
        actual = row["actual_cost_kopecks"]
        return BudgetReservation(
            reservation_id=str(row["id"]),
            budget_date=str(row["budget_date"]),
            operation=str(row["operation"]),
            max_cost_kopecks=int(row["max_cost_kopecks"]),
            status=str(row["status"]),
            actual_cost_kopecks=int(actual) if actual is not None else None,
        )


__all__ = [
    "BudgetError",
    "BudgetExceeded",
    "BudgetManager",
    "BudgetReservation",
    "BudgetSnapshot",
    "ReservationStateError",
]
