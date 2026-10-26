import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.db import Database
from backend.imports import ImportStateError, ImportTracker, import_profile_bundle
from backend.repository import Repository
from ml_ranking.validation import validate_contract


class FakeRepository:
    def __init__(self, database: Database):
        self.database = database
        self.save_calls = 0

    def save_profile_bundle(
        self,
        profile: dict,
        ml_view: dict,
        interpreter_view: dict,
        ui_view: dict,
        **options,
    ) -> str:
        self.save_calls += 1
        self.last = {
            "profile": profile,
            "ml_view": ml_view,
            "interpreter_view": interpreter_view,
            "ui_view": ui_view,
            "options": options,
        }
        return str(profile["meta"]["profile_id"])


def profile_bundle(profile_hash: str = "a" * 64) -> dict[str, dict]:
    profile = {
        "meta": {
            "company_id": "U_TEST",
            "profile_id": f"profile-{profile_hash[:16]}",
            "input_hash": profile_hash,
        },
        "input_quality": {"usability": "full"},
        "facts": [{"id": "F-1"}],
        "derived_metrics": [{"id": "D-1"}],
        "data_conflicts": [],
    }
    return {
        "profile": profile,
        "ml_view": {"schema_version": "1.0.0"},
        "interpreter_view": {"schema_version": "1.0.0"},
        "ui_view": {"schema_version": "1.0.0"},
    }


class BackendImportsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(
            prefix=".backend-imports-",
            dir=Path(__file__).resolve().parent,
        )
        self.database = Database(
            Path(self.temporary.name) / "backend.sqlite3"
        )
        self.database.migrate()
        self.tracker = ImportTracker(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def test_tracker_start_succeed_fail_and_row_errors(self) -> None:
        successful = self.tracker.start(
            "news", source_ref="fixture.json", manifest_hash="hash-1"
        )
        row_error = self.tracker.add_error(
            successful.import_id,
            row_ref="row-2",
            code="invalid_row",
            message="Row is invalid",
            details={"field": "event_id"},
        )
        self.assertEqual(row_error.row_ref, "row-2")
        finished = self.tracker.succeed(
            successful.import_id,
            stats={"accepted": 1, "rejected": 1},
        )
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(finished.stats["accepted"], 1)
        self.assertEqual(self.tracker.errors(successful.import_id), [row_error])
        self.assertEqual(
            self.tracker.find_succeeded(kind="news", manifest_hash="hash-1"),
            finished,
        )
        with self.assertRaises(ImportStateError):
            self.tracker.fail(successful.import_id, "late failure")

        failed = self.tracker.start("news", source_ref="broken.json")
        failed = self.tracker.fail(
            failed.import_id,
            "parse failed",
            stats={"accepted": 0},
            manifest_hash="hash-2",
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_summary, "parse failed")

    def test_profile_import_is_idempotent_by_profile_hash(self) -> None:
        repository = FakeRepository(self.database)
        bundle = profile_bundle()
        with patch("backend.imports._build_profile_bundle", return_value=bundle):
            first = import_profile_bundle(
                repository,
                self.tracker,
                "U_TEST",
                "companies.csv",
                "payments.csv",
                display_name="Test company",
            )
            second = import_profile_bundle(
                repository,
                self.tracker,
                "U_TEST",
                "companies.csv",
                "payments.csv",
                display_name="Test company",
            )

        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(second.import_id, first.import_id)
        self.assertEqual(second.profile_hash, bundle["profile"]["meta"]["input_hash"])
        self.assertEqual(repository.save_calls, 1)
        with self.database.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM imports WHERE kind = 'profile_bundle'"
            ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_profile_build_failure_is_tracked_and_reraised(self) -> None:
        repository = FakeRepository(self.database)
        with patch(
            "backend.imports._build_profile_bundle",
            side_effect=ValueError("bad company row"),
        ):
            with self.assertRaisesRegex(ValueError, "bad company row"):
                import_profile_bundle(
                    repository,
                    self.tracker,
                    "U_BAD",
                    "companies.csv",
                    "payments.csv",
                )

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id, status, error_summary FROM imports"
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_summary"], "bad company row")
        errors = self.tracker.errors(str(row["id"]))
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "profile_build_failed")
        self.assertEqual(errors[0].row_ref, "U_BAD")

    def test_profile_save_failure_is_tracked_and_can_be_retried(self) -> None:
        repository = FakeRepository(self.database)
        bundle = profile_bundle()
        with (
            patch("backend.imports._build_profile_bundle", return_value=bundle),
            patch.object(
                repository,
                "save_profile_bundle",
                side_effect=RuntimeError("storage unavailable"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                import_profile_bundle(
                    repository, self.tracker, "U_TEST", "companies.csv", "payments.csv"
                )

        with self.database.connection() as connection:
            failed = connection.execute("SELECT * FROM imports").fetchone()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            self.tracker.errors(failed["id"])[0].code, "profile_save_failed"
        )
        with patch("backend.imports._build_profile_bundle", return_value=bundle):
            result = import_profile_bundle(
                repository, self.tracker, "U_TEST", "companies.csv", "payments.csv"
            )
        self.assertFalse(result.reused)
        self.assertNotEqual(result.import_id, failed["id"])
        self.assertEqual(self.tracker.get(result.import_id).status, "succeeded")

    def test_real_csv_profile_is_persisted_and_content_change_creates_version(self) -> None:
        root = Path(self.temporary.name)
        companies = root / "companies.csv"
        payments = root / "payments.csv"
        with companies.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow([
                "SYNTHETIC_U", "GROUP_ID", "MAINOKVED_CODE", "MAINOKVED_NAME",
                "CL_TYPE", "CL_PRODUCT_LOA_MARK", "REVENUE_COMPANY", "REGION",
            ])
            writer.writerow([
                "U_TEST", "G_TEST", "46.90", "Оптовая торговля",
                "existing", 1, 25_000_000, "REGION_1",
            ])
        with payments.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"])
            writer.writerow(["U_TEST", "U_EXTERNAL", 100, 1])

        repository = Repository(self.database)
        first = import_profile_bundle(
            repository, self.tracker, "U_TEST", companies, payments,
            as_of_date="2026-10-26", display_name="Synthetic test company",
        )
        duplicate = import_profile_bundle(
            repository, self.tracker, "U_TEST", companies, payments,
            as_of_date="2026-10-26", display_name="Synthetic test company",
        )
        self.assertEqual(duplicate.profile_id, first.profile_id)
        self.assertTrue(duplicate.reused)
        stored = repository.profile_bundle("U_TEST")
        validate_contract(stored["profile"], "profile")
        validate_contract(stored["ml_view"], "view")
        self.assertEqual(stored["company"]["display_name"], "Synthetic test company")
        self.assertEqual(self.tracker.get(first.import_id).manifest_hash, first.profile_hash)

        with payments.open("a", encoding="utf-8", newline="") as stream:
            csv.writer(stream, delimiter=";").writerow(
                ["U_TEST", "U_ANOTHER", 50, 2]
            )
        changed = import_profile_bundle(
            repository, self.tracker, "U_TEST", companies, payments,
            as_of_date="2026-10-26", display_name="Synthetic test company",
        )
        self.assertNotEqual(changed.profile_id, first.profile_id)
        self.assertNotEqual(changed.profile_hash, first.profile_hash)
        self.assertFalse(changed.reused)
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM company_profile_versions WHERE company_id = ?",
                    ("U_TEST",),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
