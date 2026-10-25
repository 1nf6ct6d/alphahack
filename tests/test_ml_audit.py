import codecs
import csv
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ml_audit import audit_data, write_reports
from ml_audit.__main__ import main
from ml_audit.csv_io import DATASET_FILES, discover_datasets


class DataAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ml0_unit_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.company_header = ["SYNTHETIC_U", "GROUP_ID", "REVENUE_COMPANY"]
        self.payment_header = ["PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"]
        self.link_header = [
            "SYNTHETIC_F", "CITY_NAME", "RC_NAME", "SYNTHETIC_U"
        ]
        self.write("companies", self.company_header, [
            ["0001", "g1", "2000"], ["0002", "g1", "1000"],
        ])
        self.write("payments", self.payment_header, [
            ["0001", "0002", "10", "1"],
        ])
        self.write("links", self.link_header, [
            ["0007", 'Мск "Юг"', "МОСКОВСКИЙ", "0001"],
        ])

    def write(
        self, kind: str, header: list[str], rows: list[list[str]],
        encoding: str = "utf-8",
    ) -> Path:
        path = self.root / DATASET_FILES[kind]
        with path.open("w", encoding=encoding, newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def datasets(self) -> dict:
        return audit_data(self.root)["datasets"]

    def test_identifier_and_okved_leading_zeros_are_preserved(self) -> None:
        self.write(
            "companies", self.company_header + ["MAINOKVED_CODE"],
            [["0001", "g1", "2000", "01.10"],
             ["1", "g1", "3000", "01.10"]],
        )
        self.write("payments", self.payment_header, [
            ["0001", "1", "10", "1"], ["01", "1", "20", "2"],
        ])
        datasets = self.datasets()
        companies = datasets["companies"]
        self.assertEqual(
            companies["analysis"]["identifiers"]["unique_count"], 2
        )
        self.assertEqual(
            companies["columns"]["MAINOKVED_CODE"]["inferred_type"], "string"
        )
        self.assertEqual(
            datasets["payments"]["analysis"]["missing_payer_examples"], ["01"]
        )
        self.assertEqual(
            datasets["payments"]["analysis"][
                "beneficiaries_missing_from_directory"
            ], 0
        )

    def test_missing_values_do_not_become_zero(self) -> None:
        self.write("companies", self.company_header, [
            ["0001", "", ""], ["0002", "null", "N/A"],
            ["0003", " ", "NaN"], ["0004", "NA", "0"],
        ])
        companies = self.datasets()["companies"]
        revenue = companies["analysis"]["revenue"]
        self.assertEqual(companies["columns"]["GROUP_ID"]["missing_count"], 3)
        self.assertEqual(revenue["missing_count"], 3)
        self.assertEqual(revenue["usable_count"], 1)
        self.assertEqual(revenue["usable_percentage"], 25)
        self.assertEqual(
            companies["analysis"]["groups"]["distinct_non_null_group_ids"], 1
        )

    def test_exact_duplicates_are_counted_without_removal(self) -> None:
        self.write("companies", self.company_header, [
            ["0001", "g1", "2000"], ["0001", "g1", "2000"],
            ["0001", "g1", "2000"],
        ])
        companies = self.datasets()["companies"]
        duplicates = companies["exact_duplicates"]
        self.assertEqual(companies["row_count"], 3)
        self.assertEqual(duplicates["distinct_records"], 1)
        self.assertEqual(duplicates["extra_occurrences"], 2)
        self.assertEqual(duplicates["records_in_repeated_groups"], 3)
        self.assertEqual(companies["analysis"]["conflicting_company_count"], 0)
        self.assertEqual(companies["analysis"]["revenue"]["usable_count"], 3)
        self.assertEqual(
            companies["analysis"]["groups"]["group_size_distribution"],
            {"1": 1},
        )

    def test_conflicting_company_records_are_not_resolved(self) -> None:
        self.write("companies", self.company_header, [
            ["0001", "g1", "2000"], ["0001", "g2", "3000"],
            ["0002", "g2", "4000"],
        ])
        companies = self.datasets()["companies"]["analysis"]
        self.assertEqual(companies["conflicting_company_count"], 1)
        self.assertEqual(companies["records_with_conflicting_company_id"], 2)
        self.assertEqual(
            companies["conflict_examples"][0]["differing_columns"],
            ["GROUP_ID", "REVENUE_COMPANY"],
        )
        self.assertEqual(
            companies["conflict_examples"][0]["company_id"], "0001"
        )
        groups = companies["groups"]
        self.assertEqual(groups["companies_with_conflicting_group_id"], 1)
        self.assertEqual(groups["group_size_distribution"], {"0": 1, "1": 1})

    def test_missing_and_present_group_membership_is_ambiguous(self) -> None:
        self.write("companies", self.company_header, [
            ["0001", "", "2000"], ["0001", "g1", "2000"],
        ])
        groups = self.datasets()["companies"]["analysis"]["groups"]
        self.assertEqual(groups["companies_with_conflicting_group_id"], 1)
        self.assertEqual(groups["companies_with_consistent_group_id"], 0)
        self.assertEqual(groups["group_size_distribution"], {"0": 1})

    def test_missing_tokens_do_not_create_false_conflicts(self) -> None:
        self.write("companies", self.company_header, [
            ["0001", "", "2000"], ["0001", "NULL", "2000"],
        ])
        companies = self.datasets()["companies"]
        self.assertEqual(companies["analysis"]["conflicting_company_count"], 0)
        self.assertEqual(companies["exact_duplicates"]["distinct_records"], 2)

    def test_revenue_placeholder_is_not_usable_revenue(self) -> None:
        self.write("companies", self.company_header, [
            ["1", "", "1000"], ["2", "", "1000.0"],
            ["3", "", "1e3"], ["4", "", "0"], ["5", "", "-1"],
        ])
        revenue = self.datasets()["companies"]["analysis"]["revenue"]
        self.assertEqual(revenue["technical_placeholder_count"], 3)
        self.assertEqual(revenue["negative_count"], 1)
        self.assertEqual(revenue["usable_count"], 1)
        self.assertEqual(revenue["year"], 2025)
        self.assertEqual(revenue["currency"], "RUB")

    def test_payment_numeric_issues_are_counted_separately(self) -> None:
        values = [
            ("bad", "1"), ("", ""), ("-3", "-1"),
            ("Infinity", "1.5"), ("1,25", "abc"),
            ("0", "0"), ("1_000", "2"), ("NaN", "NaN"),
        ]
        self.write("payments", self.payment_header, [
            ["0001", "0002", amount, count] for amount, count in values
        ])
        payments = self.datasets()["payments"]
        amount = payments["columns"]["PAY_AMT"]["numeric_quality"]
        count = payments["columns"]["PAY_CNT"]["numeric_quality"]
        self.assertEqual(amount["missing_count"], 2)
        self.assertEqual(amount["invalid_format_count"], 2)
        self.assertEqual(amount["negative_count"], 1)
        self.assertEqual(amount["non_finite_count"], 1)
        self.assertEqual(amount["usable_count"], 2)
        self.assertEqual(count["missing_count"], 2)
        self.assertEqual(count["invalid_format_count"], 1)
        self.assertEqual(count["fractional_count"], 1)
        self.assertEqual(count["negative_count"], 1)
        self.assertEqual(count["usable_count"], 3)
        coverage = payments["analysis"]["directory_coverage"]
        self.assertEqual(
            coverage["external_outgoing_records"]["total_count"], 8
        )

    def test_unknown_counterparties_stay_in_coverage_denominator(self) -> None:
        self.write("payments", self.payment_header, [
            ["0001", "0002", "10", "1"],
            ["0001", "0009", "20", "1"],
            ["0001", "0009", "30", "2"],
            ["0001", "0001", "5", "1"],
            ["X", "0002", "6", "1"],
            ["", "0002", "7", "1"],
            ["0002", "0002", "8", "1"],
        ])
        payments = self.datasets()["payments"]["analysis"]
        self.assertEqual(payments["payers"]["unique_count"], 3)
        self.assertEqual(payments["beneficiaries"]["unique_count"], 3)
        self.assertEqual(payments["self_payment_records"], 2)
        self.assertEqual(payments["records_missing_payer_or_beneficiary"], 1)
        self.assertEqual(payments["payers_missing_from_directory"], 1)
        self.assertEqual(payments["beneficiaries_missing_from_directory"], 1)
        self.assertEqual(
            payments["beneficiary_records_missing_from_directory"], 2
        )
        coverage = payments["directory_coverage"]
        self.assertEqual(coverage["all_unique_endpoints"]["percentage"], 50)
        self.assertEqual(
            coverage["external_outgoing_unique_counterparties"]["percentage"],
            50,
        )
        records = coverage["external_outgoing_records"]
        self.assertEqual(records["known_count"], 1)
        self.assertEqual(records["total_count"], 3)
        self.assertEqual(records["percentage"], 33.333333)

    def test_repeated_pairs_are_separate_from_exact_duplicates(self) -> None:
        self.write("payments", self.payment_header, [
            ["0001", "0002", "10", "1"],
            ["0001", "0002", "10", "1"],
            ["0001", "0002", "20", "2"],
            ["0002", "0001", "20", "2"],
        ])
        payments = self.datasets()["payments"]
        self.assertEqual(payments["row_count"], 4)
        self.assertEqual(payments["exact_duplicates"]["extra_occurrences"], 1)
        pairs = payments["analysis"]["repeated_directed_pairs"]
        self.assertEqual(pairs["distinct_records"], 2)
        self.assertEqual(pairs["extra_occurrences"], 2)
        self.assertEqual(pairs["records_in_repeated_groups"], 3)

    def test_cp1251_and_quoted_semicolons_are_supported(self) -> None:
        self.write("companies", self.company_header + ["MAINOKVED_NAME"], [
            ["0001", "группа;1", "2000", "Торговля; оборудованием"],
        ], encoding="cp1251")
        companies = self.datasets()["companies"]
        self.assertEqual(companies["status"], "ok")
        self.assertEqual(companies["source"]["encoding"], "cp1251")
        self.assertEqual(companies["column_count"], 4)
        self.assertEqual(
            companies["columns"]["MAINOKVED_NAME"]["inferred_type"], "string"
        )

    def test_utf8_bom_and_utf16_are_supported(self) -> None:
        for encoding in ("utf-8-sig", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                self.write("companies", self.company_header, [
                    ["0001", "группа", "2000"],
                ], encoding=encoding)
                companies = self.datasets()["companies"]
                self.assertEqual(companies["status"], "ok")
                identifiers = companies["analysis"]["identifiers"]
                self.assertEqual(identifiers["unique_count"], 1)

    def test_wrong_explicit_encoding_is_reported(self) -> None:
        self.write("companies", self.company_header + ["NAME"], [
            ["0001", "g1", "2000", "Компания"],
        ], encoding="cp1251")
        report = audit_data(self.root, encoding="utf-8")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["datasets"]["companies"]["status"], "error")
        self.assertIsNone(
            report["datasets"]["payments"]["analysis"]["directory_coverage"]
        )

    def test_malformed_rows_are_reported_without_disappearing(self) -> None:
        self.write("payments", self.payment_header, [
            ["0001", "0002", "10", "1"], ["0001", "0009"],
            ["0001", "0002", "20", "2", "extra"],
        ])
        payments = self.datasets()["payments"]
        self.assertEqual(payments["status"], "incomplete")
        self.assertEqual(payments["row_count"], 3)
        self.assertEqual(payments["malformed_row_count"], 2)
        self.assertEqual(payments["structurally_valid_row_count"], 1)
        self.assertEqual(payments["columns"]["PAY_AMT"]["missing_count"], 1)
        self.assertEqual(payments["exact_duplicates"]["distinct_records"], 3)
        self.assertEqual(
            payments["analysis"]["beneficiaries"]["unique_count"], 1
        )

    def test_missing_columns_disable_relational_analysis(self) -> None:
        self.write("payments", ["PAYER", "PAY_AMT"], [["0001", "10"]])
        payments = self.datasets()["payments"]
        self.assertEqual(payments["status"], "incomplete")
        self.assertEqual(
            payments["missing_required_columns"], ["BENEFICIARY", "PAY_CNT"]
        )
        self.assertIsNone(payments["analysis"])

    def test_absent_datasets_leave_unknown_counts_and_coverage(self) -> None:
        (self.root / DATASET_FILES["companies"]).unlink()
        datasets = self.datasets()
        self.assertEqual(datasets["companies"]["status"], "missing")
        self.assertIsNone(datasets["companies"]["row_count"])
        payments = datasets["payments"]["analysis"]
        self.assertIsNone(payments["payers_missing_from_directory"])
        self.assertIsNone(payments["directory_coverage"])

    def test_empty_datasets_do_not_produce_zero_percentages(self) -> None:
        self.write("payments", self.payment_header, [])
        payments = self.datasets()["payments"]
        self.assertEqual(payments["row_count"], 0)
        self.assertIsNone(payments["columns"]["PAY_AMT"]["missing_percentage"])
        coverage = payments["analysis"]["directory_coverage"]
        self.assertIsNone(coverage["external_outgoing_records"]["percentage"])

    def test_duplicate_headers_and_unclosed_quotes_are_errors(self) -> None:
        self.write("companies", ["SYNTHETIC_U", "SYNTHETIC_U"], [["1", "2"]])
        payments_path = self.root / DATASET_FILES["payments"]
        payments_path.write_text(
            'PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\n"unfinished', encoding="utf-8"
        )
        datasets = self.datasets()
        self.assertEqual(datasets["companies"]["status"], "error")
        self.assertEqual(datasets["payments"]["status"], "error")

    def test_repeated_links_are_not_company_record_conflicts(self) -> None:
        self.write("links", self.link_header, [
            ["0007", "Город", "Регион", "0001"],
            ["0008", "Город", "Регион", "0001"],
            ["0008", "Город", "Регион", "0001"],
            ["0009", "Город", "Регион", "UNKNOWN"],
        ])
        links = self.datasets()["links"]
        self.assertEqual(links["exact_duplicates"]["extra_occurrences"], 1)
        self.assertEqual(
            links["analysis"]["company_identifiers"][
                "repeated_identifier_count"
            ], 1
        )
        self.assertEqual(
            links["analysis"]["companies_missing_from_directory"], 1
        )

    def test_groups_with_missing_company_identifiers_are_visible(self) -> None:
        self.write("companies", self.company_header, [
            ["", "orphan_group", "2000"], ["0001", "g1", "2000"],
        ])
        companies = self.datasets()["companies"]["analysis"]
        self.assertEqual(companies["missing_identifier_records"], 1)
        self.assertEqual(companies["groups"]["distinct_non_null_group_ids"], 2)
        self.assertEqual(
            companies["groups"]["groups_without_unambiguous_members"], 1
        )

    def test_identifier_whitespace_is_preserved_and_visible(self) -> None:
        self.write("payments", self.payment_header, [
            [" 0001", "0002", "10", "1"],
        ])
        payments = self.datasets()["payments"]
        self.assertEqual(
            payments["columns"]["PAYER"]["surrounding_whitespace_count"], 1
        )
        self.assertEqual(
            payments["analysis"]["missing_payer_examples"], [" 0001"]
        )

    def test_invalid_flag_domain_is_reported(self) -> None:
        self.write("companies", self.company_header + ["FTS_FLAG"], [
            ["1", "g1", "2000", "2"], ["2", "g1", "2000", "0"],
        ])
        column = self.datasets()["companies"]["columns"]["FTS_FLAG"]
        flag = column["numeric_quality"]
        self.assertEqual(flag["invalid_domain_count"], 1)
        self.assertEqual(flag["usable_count"], 1)

    def test_reports_are_deterministic_and_raw_files_unchanged(self) -> None:
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.glob("*.csv")
        }
        first = audit_data(self.root)
        second = audit_data(self.root)
        self.assertEqual(first, second)
        first_paths = write_reports(first, self.root / "report1")
        second_paths = write_reports(second, self.root / "report2")
        for first_path, second_path in zip(first_paths, second_paths):
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
        parsed = json.loads(first_paths[0].read_text(encoding="utf-8"))
        self.assertIsNone(parsed["semantics"]["payments"]["year"])
        self.assertIsNone(parsed["semantics"]["payments"]["end_date"])
        self.assertFalse(
            first_paths[0].read_bytes().startswith(codecs.BOM_UTF8)
        )
        markdown = first_paths[1].read_text(encoding="utf-8")
        self.assertIn("Выручка REVENUE_COMPANY — за 2025 год", markdown)
        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.glob("*.csv")
        }
        self.assertEqual(before, after)

    def test_discovery_searches_subfolders_and_rejects_ambiguity(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        path = self.root / DATASET_FILES["companies"]
        moved = nested / path.name
        path.rename(moved)
        self.assertEqual(discover_datasets(self.root)["companies"], moved)
        path.write_bytes(moved.read_bytes())
        with self.assertRaises(ValueError):
            discover_datasets(self.root)

    def test_cli_writes_real_reports_and_signals_missing_inputs(self) -> None:
        with redirect_stdout(io.StringIO()):
            complete = main([
                "--data-dir", str(self.root),
                "--output-dir", str(self.root / "reports"),
            ])
            incomplete = main([
                "--data-dir", str(self.root / "absent"),
                "--output-dir", str(self.root / "missing_reports"),
            ])
        self.assertEqual(complete, 0)
        self.assertEqual(incomplete, 1)
        path = self.root / "missing_reports/ml0_data_audit.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "incomplete")
        self.assertIsNone(report["datasets"]["payments"]["row_count"])


if __name__ == "__main__":
    unittest.main()
