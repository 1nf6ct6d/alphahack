from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.budget import (
    BudgetExceeded,
    BudgetManager,
    ReservationStateError,
)
from backend.db import Database
from backend.security import (
    AccessDenied,
    AuthenticationError,
    authenticate_bearer,
    grant_company_access,
    issue_demo_session,
    require_company_access,
)


ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "backend" / "migrations" / "001_initial.sql"
PEPPER = "test-only-pepper-with-at-least-24-bytes"


class BackendFoundationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(
            prefix=".backend-foundation-",
            dir=Path(__file__).resolve().parent,
        )
        self.database = Database(
            Path(self.temporary.name) / "backend.sqlite3"
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def test_migrate_is_idempotent_and_records_checksum(self) -> None:
        self.assertEqual(self.database.migrate(), [path.name for path in sorted(MIGRATION.parent.glob("*.sql"))])
        self.assertEqual(self.database.migrate(), [])

        expected = sha256(MIGRATION.read_bytes()).hexdigest()
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT filename, checksum
                FROM schema_migrations
                WHERE version = '001'
                """
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["filename"], "001_initial.sql")
            self.assertEqual(row["checksum"], expected)
            violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            self.assertEqual(violations, [])

    def test_bearer_authentication_and_company_isolation(self) -> None:
        self.database.migrate()
        with self.database.transaction(immediate=True) as connection:
            connection.executemany(
                "INSERT INTO users(id, label) VALUES (?, ?)",
                (("TEST_A", "Test A"), ("TEST_B", "Test B")),
            )
            connection.executemany(
                "INSERT INTO companies(id, display_name) VALUES (?, ?)",
                (("U_A", "Company A"), ("U_B", "Company B")),
            )
            grant_company_access(
                connection, user_id="TEST_A", company_id="U_A"
            )
            grant_company_access(
                connection, user_id="TEST_B", company_id="U_B"
            )
            _, token = issue_demo_session(
                connection,
                user_id="TEST_A",
                pepper=PEPPER,
                token="test-a-secret-token",
            )

        with self.database.connection() as connection:
            context = authenticate_bearer(
                connection, f"Bearer {token}", pepper=PEPPER
            )
            self.assertEqual(context.user_id, "TEST_A")
            require_company_access(
                connection, user_id=context.user_id, company_id="U_A"
            )
            with self.assertRaises(AccessDenied):
                require_company_access(
                    connection,
                    user_id=context.user_id,
                    company_id="U_B",
                )
            with self.assertRaises(AuthenticationError):
                authenticate_bearer(
                    connection, "Bearer incorrect", pepper=PEPPER
                )
            stored = connection.execute(
                "SELECT token_digest FROM demo_sessions"
            ).fetchone()["token_digest"]
            self.assertNotEqual(stored, token)
            self.assertNotIn(token, stored)

    def test_budget_reserve_commit_release_and_limit(self) -> None:
        self.database.migrate()
        manager = BudgetManager(
            self.database,
            internal_limit_kopecks=450,
            timezone_name="Europe/Moscow",
        )
        now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)

        reserved = manager.reserve(
            reservation_id="R1",
            operation="insight",
            max_cost_kopecks=300,
            context_id="CTX-1",
            now=now,
        )
        self.assertEqual(reserved.status, "reserved")
        self.assertEqual(manager.snapshot(value=now).reserved_kopecks, 300)

        same = manager.reserve(
            reservation_id="R1",
            operation="insight",
            max_cost_kopecks=300,
            context_id="CTX-1",
            now=now,
        )
        self.assertEqual(same, reserved)
        with self.assertRaises(BudgetExceeded):
            manager.reserve(
                operation="question", max_cost_kopecks=151, now=now
            )

        committed = manager.commit(
            "R1",
            actual_cost_kopecks=275,
            model_id="demo-model",
            tokens_in=10,
            tokens_out=5,
            elapsed_ms=25,
            now=now,
        )
        self.assertEqual(committed.status, "committed")
        snapshot = manager.snapshot(value=now)
        self.assertEqual(snapshot.confirmed_kopecks, 275)
        self.assertEqual(snapshot.reserved_kopecks, 0)

        manager.reserve(
            reservation_id="R2",
            operation="question",
            max_cost_kopecks=175,
            now=now,
        )
        self.assertEqual(manager.snapshot(value=now).available_kopecks, 0)
        released = manager.release("R2", now=now)
        self.assertEqual(released.status, "released")
        self.assertEqual(manager.snapshot(value=now).available_kopecks, 175)
        with self.assertRaises(ReservationStateError):
            manager.release("R1", now=now)


if __name__ == "__main__":
    unittest.main()
