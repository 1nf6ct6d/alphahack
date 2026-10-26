"""Fields of the HTTP responses that the frontend adapter in api.ts depends on."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


ROOT = Path(__file__).resolve().parent.parent
A = {"Authorization": "Bearer demo-test-a"}


class FrontendContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix=".fe-contract-", dir=ROOT / "tests")
        settings = Settings.from_env({
            "ALFA_ENV": "test",
            "ALFA_DATABASE_PATH": str(Path(self.temp.name) / "api.sqlite3"),
        })
        self.client = TestClient(create_app(settings, seed_demo_data=True))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def insight(self):
        overview = self.client.post(
            "/companies/U_5639/overview/refresh", headers=A,
            json={"analytic_focus": "CFO"},
        ).json()
        response = self.client.post(
            "/companies/U_5639/insights", headers=A,
            json={"analytic_focus": "CFO", "event_id": overview["items"][0]["event_id"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_company_list_and_overview_state_fields(self):
        item = self.client.get("/companies", headers=A).json()["items"][0]
        self.assertIn("id", item)
        self.assertIn("display_name", item)
        overview = self.client.get(
            "/companies/U_5639/overview?analytic_focus=CFO", headers=A
        ).json()
        self.assertEqual(overview["state"], "not_prepared")
        self.assertEqual(overview["items"], [])

    def test_insight_exposes_source_and_evidence_fields(self):
        insight = self.insight()
        source = insight["sources"][0]
        self.assertTrue({"source_id", "type", "title", "url", "date"} <= source.keys())
        evidence = insight["evidence"][0]
        self.assertTrue({"evidence_id", "source_type", "source_id", "observation"} <= evidence.keys())
        profile = [item for item in insight["evidence"] if item["source_type"] == "profile"]
        self.assertTrue(profile and profile[0]["raw"]["inputs"])
        self.assertIn("bank_retrieval_status", insight)
        self.assertIn("bank_position", insight)

    def test_error_body_and_feedback_reason_contract(self):
        denied = self.client.get("/companies")
        self.assertEqual(denied.status_code, 401)
        self.assertTrue({"code", "message"} <= denied.json().keys())
        insight = self.insight()
        url = f"/insights/{insight['insight_id']}/feedback"
        rejected = self.client.put(url, headers=A, json={"rating": "useful", "reason": None})
        self.assertEqual(rejected.status_code, 400)
        accepted = self.client.put(url, headers=A, json={"rating": "useful", "reason": "relevant"})
        self.assertEqual(accepted.status_code, 200, accepted.text)


if __name__ == "__main__":
    unittest.main()
