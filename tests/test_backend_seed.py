from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.db import Database
from backend.repository import Repository
from backend.security import authenticate_bearer, issue_demo_session, revoke_demo_session
from backend.seed import SeedConflictError, seed_demo
from backend.services import RULES_VERSION, load_demo_bundle


ROOT = Path(__file__).resolve().parent.parent
PEPPER = "seed-test-pepper-with-at-least-24-bytes"


class BackendSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.migrate()
        self.repository = Repository(self.database)

    def tearDown(self) -> None:
        self.database.close()

    def seed(self, **kwargs) -> dict[str, str]:
        return seed_demo(self.database, root=ROOT, pepper=PEPPER, **kwargs)

    def dump(self) -> list[str]:
        with self.database.connection() as connection:
            return list(connection.iterdump())

    def test_seed_is_idempotent_and_users_keep_separate_tokens(self) -> None:
        first = self.seed()
        before = self.dump()
        second = self.seed()
        self.assertEqual(first, second)
        self.assertEqual(before, self.dump())
        with self.database.connection() as connection:
            for user_id, token in second.items():
                actual = authenticate_bearer(
                    connection, f"Bearer {token}", pepper=PEPPER
                )
                self.assertEqual(actual.user_id, user_id)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM demo_sessions").fetchone()[0], 2
            )

    def test_duplicate_configured_tokens_fail_before_writes(self) -> None:
        before = self.dump()
        with self.assertRaisesRegex(SeedConflictError, "unique token"):
            self.seed(tokens={"TEST_A": "same-token", "TEST_B": "same-token"})
        self.assertEqual(before, self.dump())

    def test_token_owned_by_another_user_fails_without_partial_seed(self) -> None:
        self.repository.upsert_user("ANOTHER_USER", "Existing user")
        with self.database.transaction(immediate=True) as connection:
            issue_demo_session(
                connection, user_id="ANOTHER_USER", pepper=PEPPER, token="demo-test-b"
            )
        before = self.dump()
        with self.assertRaisesRegex(SeedConflictError, "owned by another user"):
            self.seed()
        self.assertEqual(before, self.dump())

    def test_revoked_token_is_not_resurrected(self) -> None:
        self.seed()
        with self.database.transaction(immediate=True) as connection:
            session_id = connection.execute(
                "SELECT id FROM demo_sessions WHERE user_id = 'TEST_B'"
            ).fetchone()[0]
            revoke_demo_session(connection, session_id)
        before = self.dump()
        with self.assertRaisesRegex(SeedConflictError, "was revoked"):
            self.seed()
        self.assertEqual(before, self.dump())

    def test_expired_token_is_not_silently_returned(self) -> None:
        self.repository.upsert_user("TEST_A", "Test A")
        with self.database.transaction(immediate=True) as connection:
            issue_demo_session(
                connection,
                user_id="TEST_A",
                pepper=PEPPER,
                token="demo-test-a",
                expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        before = self.dump()
        with self.assertRaisesRegex(SeedConflictError, "expired"):
            self.seed()
        self.assertEqual(before, self.dump())

    def test_real_profile_is_not_overwritten_or_exposed_to_demo_user(self) -> None:
        bundle = list(deepcopy(load_demo_bundle(ROOT, 1, "U_5639")))
        bundle[0]["meta"]["profile_id"] = "real-profile-1"
        bundle[0]["meta"]["input_hash"] = "b" * 64
        for view in bundle[1:]:
            view["profile_id"] = "real-profile-1"
        self.repository.save_profile_bundle(
            *bundle,
            rules_version=RULES_VERSION,
            display_name="Real company",
            source_record={"synthetic": False},
        )
        before = self.dump()
        with self.assertRaisesRegex(SeedConflictError, "non-demo profiles"):
            self.seed()
        self.assertEqual(before, self.dump())
        with self.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_real_news_collision_is_not_overwritten(self) -> None:
        event = json.loads(
            (ROOT / "ml_ranking" / "examples" / "synthetic_events.json").read_text(encoding="utf-8")
        )[0]
        event.update(synthetic=False, title="Existing real news")
        self.repository.upsert_event(event)
        before = self.dump()
        with self.assertRaisesRegex(SeedConflictError, "real news"):
            self.seed()
        self.assertEqual(before, self.dump())

    def test_existing_synthetic_event_is_preserved(self) -> None:
        self.seed()
        event = self.repository.event("SYN_STEEL_REG")
        event.pop("extraction_status")
        event["title"] = "Locally edited synthetic event"
        self.repository.upsert_event(event)
        with self.database.connection() as connection:
            before = dict(connection.execute(
                "SELECT * FROM news_events WHERE id = 'SYN_STEEL_REG'"
            ).fetchone())
        self.seed()
        with self.database.connection() as connection:
            after = dict(connection.execute(
                "SELECT * FROM news_events WHERE id = 'SYN_STEEL_REG'"
            ).fetchone())
        self.assertEqual(before, after)

    def test_failure_rolls_back_all_seed_operations(self) -> None:
        before = self.dump()
        with patch.object(Repository, "upsert_event", side_effect=RuntimeError("storage failure")):
            with self.assertRaisesRegex(RuntimeError, "storage failure"):
                self.seed()
        self.assertEqual(before, self.dump())


if __name__ == "__main__":
    unittest.main()
