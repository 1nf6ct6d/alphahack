"""End-to-end HTTP checks against real SQLite, ML ranking and demo fixtures."""

import copy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.db import Database
from backend.main import create_app
from backend.repository import OperationInProgress
from backend.llm_gateway import GatewayError, GatewayResult


ROOT = Path(__file__).resolve().parent.parent
A = {"Authorization": "Bearer demo-test-a"}
B = {"Authorization": "Bearer demo-test-b"}


class BackendAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix=".api-test-", dir=ROOT / "tests")
        self.settings = Settings.from_env({
            "ALFA_ENV": "test", "ALFA_DATABASE_PATH": str(Path(self.temp.name) / "api.sqlite3")
        })
        self.app = create_app(self.settings, seed_demo_data=True)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.repo = self.app.state.repository

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def overview(self, focus="CFO"):
        response = self.client.post(
            "/companies/U_5639/overview/refresh", headers=A,
            json={"analytic_focus": focus},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["items"])
        return response.json()

    def insight(self, focus="CFO"):
        overview = self.overview(focus)
        response = self.client.post(
            "/companies/U_5639/insights", headers=A,
            json={"analytic_focus": focus, "event_id": overview["items"][0]["event_id"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_authentication_and_tenant_isolation(self):
        self.assertEqual(self.client.get("/companies").status_code, 401)
        self.assertEqual(self.client.get("/companies", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual([x["id"] for x in self.client.get("/companies", headers=A).json()["items"]], ["U_5639"])
        self.assertEqual([x["id"] for x in self.client.get("/companies", headers=B).json()["items"]], ["U_123123"])
        for suffix in ("", "/overview"):
            self.assertEqual(self.client.get(f"/companies/U_5639{suffix}", headers=B).status_code, 404)
        self.assertEqual(self.client.post("/companies/U_5639/overview/refresh", headers=B, json={"analytic_focus": "CFO"}).status_code, 404)

    def test_complete_scenario_and_idempotency(self):
        first = self.client.get("/companies/U_5639/overview", headers=A).json()
        self.assertEqual(first["state"], "not_prepared")
        overview = self.overview()
        self.assertEqual(overview, self.overview())
        insight = self.insight()
        self.assertEqual(insight, self.insight())
        self.assertTrue(insight["evidence"])
        identifier = insight["insight_id"]
        self.assertEqual(self.client.get(f"/insights/{identifier}", headers=A).json(), insight)
        answer = self.client.post(f"/insights/{identifier}/questions", headers=A, json={"question": "На каких признаках основан вывод?"})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertTrue(answer.json()["evidence_ids"])
        second = self.client.post(f"/insights/{identifier}/questions", headers=A, json={"question": "Что проверить дальше?", "conversation_id": answer.json()["conversation_id"]})
        self.assertEqual(second.status_code, 200)
        feedback = self.client.put(f"/insights/{identifier}/feedback", headers=A, json={"rating": "useful", "reason": "relevant"})
        self.assertEqual(feedback.status_code, 200, feedback.text)
        edited = self.client.put(f"/insights/{identifier}/feedback", headers=A, json={"rating": "not_useful", "reason": "unclear"})
        self.assertEqual(edited.json()["feedback_id"], feedback.json()["feedback_id"])
        with self.app.state.database.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_history").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_insight_and_conversation_access(self):
        insight = self.insight()
        identifier = insight["insight_id"]
        self.assertEqual(self.client.get(f"/insights/{identifier}", headers=B).status_code, 404)
        self.assertEqual(self.client.post(f"/insights/{identifier}/questions", headers=B, json={"question": "Почему?"}).status_code, 404)
        self.assertEqual(self.client.put(f"/insights/{identifier}/feedback", headers=B, json={"rating": "useful", "reason": "relevant"}).status_code, 404)
        question = self.client.post(f"/insights/{identifier}/questions", headers=A, json={"question": "Признаки?"}).json()
        other = self.insight("CEO")
        response = self.client.post(f"/insights/{other['insight_id']}/questions", headers=A, json={"question": "Признаки?", "conversation_id": question["conversation_id"]})
        self.assertEqual(response.status_code, 400)

    def test_focus_and_validation(self):
        self.assertNotEqual(self.overview("CFO")["context_id"], self.overview("CEO")["context_id"])
        self.assertEqual(self.client.get("/companies/U_5639/overview?analytic_focus=ADMIN", headers=A).status_code, 400)
        self.assertEqual(self.client.post("/companies/U_5639/insights", headers=A, json={"analytic_focus": "CFO", "event_id": "missing"}).status_code, 400)
        self.assertEqual(self.client.post("/companies/U_5639/insights", headers=A, content="{oops").status_code, 400)

    def test_snapshot_is_immutable(self):
        overview = self.overview()
        event_id = overview["items"][0]["event_id"]
        original = self.repo.snapshot_event(overview["snapshot_id"], event_id)
        changed = copy.deepcopy(original)
        changed.pop("extraction_status")
        changed["content"] = "Replacement article with entirely different content."
        self.repo.upsert_event(changed)
        self.assertEqual(self.repo.snapshot_event(overview["snapshot_id"], event_id), original)
        new_snapshot = self.repo.create_news_snapshot()
        self.assertNotEqual(new_snapshot["snapshot_id"], overview["snapshot_id"])
        self.assertEqual(self.repo.latest_news_snapshot()["id"], new_snapshot["snapshot_id"])

    def test_bank_reimport_revokes_evidence_and_invalidates_context(self):
        before = self.overview()["context_id"]
        report = {
            "source_id": "TEST_BANK", "title": "Initial", "report_date": "2026-10-01",
            "content_hash": "a" * 64, "original_filename": "test.pdf",
            "fragments": [{"page_number": 1, "text": "Старая позиция", "ocr_quality": 0.9, "verified": True}],
        }
        self.repo.import_bank_report(report)
        self.assertNotEqual(before, self.overview()["context_id"])
        report["title"] = "Corrected"
        report["report_date"] = "2026-10-02"
        report["fragments"][0].update(text="Исправленный текст", verified=False)
        self.repo.import_bank_report(report)
        available, fragments = self.repo.bank_corpus()
        self.assertTrue(available)
        self.assertEqual(len(fragments), 1)
        self.assertFalse(fragments[0]["verified"])
        self.assertEqual(fragments[0]["title"], "Corrected")
        self.assertEqual(fragments[0]["report_date"], "2026-10-02")

    def test_operation_lease_prevents_duplicate_work(self):
        with self.repo.operation_lease("same-context"):
            with self.assertRaises(OperationInProgress):
                with self.repo.operation_lease("same-context"):
                    self.fail("must not acquire an active lease")
        with self.repo.operation_lease("same-context"):
            pass

    def test_health_openapi_and_admin(self):
        self.assertEqual(self.client.get("/health/ready").status_code, 200)
        document = self.client.get("/openapi.json").json()
        self.assertIn("HTTPBearer", document["components"]["securitySchemes"])
        self.assertEqual(self.client.get("/admin/budget").status_code, 401)
        with patch.dict(os.environ, {"ALFA_ADMIN_TOKEN": "only-for-tests"}):
            response = self.client.get("/admin/budget", headers={"X-Admin-Token": "only-for-tests"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["internal_limit_kopecks"], 45000)

    def test_model_failure_is_503_and_accounted_not_empty(self):
        class FailingGateway:
            max_cost_kopecks = 100

            def enhance_insight(self, base, view):
                raise GatewayError("timeout", code="llm_timeout")

        self.app.state.services.gateway = FailingGateway()
        overview = self.overview()
        response = self.client.post("/companies/U_5639/insights", headers=A, json={
            "analytic_focus": "CFO", "event_id": overview["items"][0]["event_id"]
        })
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["code"], "llm_timeout")
        self.assertEqual(self.app.state.services.budget.snapshot().confirmed_kopecks, 100)
        with self.app.state.database.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0], 0)

    def test_budget_stops_provider_before_dispatch(self):
        class ExpensiveGateway:
            max_cost_kopecks = 45001

            def enhance_insight(self, base, view):
                raise AssertionError("Provider must not be called")

        self.app.state.services.gateway = ExpensiveGateway()
        overview = self.overview()
        response = self.client.post("/companies/U_5639/insights", headers=A, json={
            "analytic_focus": "CFO", "event_id": overview["items"][0]["event_id"]
        })
        self.assertEqual(response.status_code, 429, response.text)

    def test_invalid_event_does_not_poison_news(self):
        with patch.dict(os.environ, {"ALFA_ADMIN_TOKEN": "only-for-tests"}):
            response = self.client.post("/admin/events", headers={"X-Admin-Token": "only-for-tests"}, json={
                "event_id": "bad", "title": "bad", "source_id": "bad", "source_url": "javascript:alert(1)",
                "event_type": "other", "content": "Text", "published_at": "not-a-date",
            })
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.repo.event("bad"))


if __name__ == "__main__":
    unittest.main()
