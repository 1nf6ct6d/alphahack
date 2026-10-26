import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.datasets import import_dataset
from backend.db import Database
from backend.ocr import extract_bank_pdf, OCRError
from backend.llm_gateway import Gateway, GatewayError
from tests.test_backend_llm_gateway import completion


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).parent, prefix=".ingestion-")
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "test.sqlite3")
        self.db.migrate()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_csv_preserves_duplicates_null_and_line_provenance(self):
        file = self.root / "payments.csv"
        file.write_text("PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\nU_1;U_2;12,50;1\nU_1;U_2;12,50;1\nU_1;;nan;2\nbad;row\n", encoding="utf-8")
        result = import_dataset(self.db, "payments", file)
        self.assertEqual(result["rows"], 4)
        self.assertEqual(result["rows_with_issues"], 1)
        self.assertTrue(import_dataset(self.db, "payments", file)["reused"])
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM dataset_rows ORDER BY record_number").fetchall()
            self.assertEqual(rows[0]["raw_json"], rows[1]["raw_json"])
            self.assertIsNone(json.loads(rows[2]["normalized_json"])["BENEFICIARY"])
            self.assertIsNone(rows[3]["normalized_json"])
            self.assertEqual(rows[3]["source_line_end"], 5)

    def test_person_links_and_failed_header_import(self):
        file = self.root / "links.csv"
        file.write_text("SYNTHETIC_F,SYNTHETIC_U,CITY_NAME,RC_NAME\nF_1,U_1,Москва,Центр\n", encoding="utf-8")
        self.assertEqual(import_dataset(self.db, "links", file)["rows"], 1)
        file.write_text("wrong,header\n1,2\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            import_dataset(self.db, "companies", file)
        with self.db.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM import_errors").fetchone()[0], 1)

    def test_pdf_native_text_requires_explicit_verification(self):
        file = self.root / "report.pdf"
        file.write_bytes(b"%PDF-test-fixture")
        with patch("backend.ocr._binary", side_effect=lambda name: name), patch("backend.ocr._run", side_effect=["Pages: 1\n", "Банковский аналитический текст. " * 10]):
            result = extract_bank_pdf(file, source_id="BANK", title="Report")
        self.assertFalse(result["fragments"][0]["verified"])
        self.assertEqual(result["fragments"][0]["page_number"], 1)
        with patch("backend.ocr._binary", return_value="pdfinfo"), patch("backend.ocr._run", return_value="Pages: 101\n"):
            with self.assertRaises(OCRError):
                extract_bank_pdf(file, source_id="BANK", title="Report")

    def test_news_normalizer_keeps_identity_and_rejects_fabricated_quotes(self):
        event = {"event_id": "EV", "title": "Title", "source_id": "SRC", "source_url": "https://news.example/a",
                 "event_type": "other", "content": "Банк изменил ключевую ставку.", "extraction_status": "full_text"}
        annotations = {"event_type": "monetary_policy_change", "factor_classes": ["monetary_policy"],
                       "okved_codes": [], "geography": [], "entities": [], "deadline": None,
                       "mandatory": None, "applies_to_all_in_scope": None,
                       "evidence": [{"evidence_id": "Q1", "quote": event["content"], "supports": ["event_type", "factor_classes"]}]}
        gateway = Gateway(base_url="https://llm.example/v1", api_key="test", model_id="test", max_cost_kopecks=10,
                          transport=lambda *args: completion(annotations))
        result = gateway.normalize_event(event)
        self.assertEqual(result.payload["event_id"], "EV")
        self.assertEqual(result.payload["event_type"], "monetary_policy_change")
        annotations["evidence"][0]["quote"] = "Несуществующая цитата"
        with self.assertRaises(GatewayError):
            gateway.normalize_event(event)


if __name__ == "__main__":
    unittest.main()
