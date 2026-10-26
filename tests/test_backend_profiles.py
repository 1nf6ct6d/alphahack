import csv
import tempfile
import unittest
from pathlib import Path

from backend.profiles import (
    build_company_profile,
    build_profile_views,
)
from ml_ranking.validation import company_context, validate_contract


COMPANY_COLUMNS = [
    "SYNTHETIC_U",
    "GROUP_ID",
    "MAINOKVED_CODE",
    "MAINOKVED_NAME",
    "CL_TYPE",
    "CL_PRODUCT_LOA_MARK",
    "REVENUE_COMPANY",
    "REGION",
]


def write_csv(path: Path, columns: list[str], rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";", lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)


class BackendProfilesTest(unittest.TestCase):
    def setUp(self) -> None:
        # Keep fixtures inside the checkout so the tests also run in a
        # workspace-write sandbox whose global Windows temp is read-only.
        self.temp = tempfile.TemporaryDirectory(
            prefix=".backend-profile-", dir=Path.cwd()
        )
        self.root = Path(self.temp.name)
        self.companies = self.root / "companies.csv"
        self.payments = self.root / "payments.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_payments(self) -> None:
        write_csv(
            self.payments,
            ["PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"],
            [
                ["U_MAIN", "U_MAIN", 500, 5],
                ["U_MAIN", "U_KNOWN", 100, 1],
                ["U_MAIN", "U_UNKNOWN", 300, 3],
            ],
        )

    def _write_unambiguous_companies(self) -> None:
        write_csv(
            self.companies,
            COMPANY_COLUMNS,
            [
                [
                    "U_MAIN", "G_MAIN", "46.90", "Оптовая торговля",
                    "existing", 1, 25_000_000_000, "REGION_1",
                ],
                [
                    "U_KNOWN", "G_OTHER", "52.29", "Логистика",
                    "potential", 0, 2_000_000_000, "REGION_2",
                ],
            ],
        )

    def test_profile_uses_ml_features_and_builds_valid_views(self) -> None:
        self._write_unambiguous_companies()
        self._write_payments()

        profile = build_company_profile(
            "U_MAIN", self.companies, self.payments,
            as_of_date="2026-10-26",
        )
        views = build_profile_views(profile)

        validate_contract(profile, "profile")
        validate_contract(views["ml_view"], "view")
        context = company_context(views["ml_view"], profile)
        self.assertTrue(context.verified)
        self.assertEqual(profile["meta"]["pipeline_mode"], "kb_fallback")
        self.assertEqual(profile["meta"]["degradation_reasons"], ["kb_miss"])
        self.assertEqual(profile["hypotheses"], [])
        self.assertEqual(profile["sensitivities"], [])
        self.assertEqual(
            views["interpreter_view"]["profile_id"],
            profile["meta"]["profile_id"],
        )
        self.assertEqual(
            views["ui_view"]["profile_id"], profile["meta"]["profile_id"]
        )

    def test_self_payment_excluded_and_unknown_recipient_preserved(self) -> None:
        self._write_unambiguous_companies()
        self._write_payments()
        profile = build_company_profile(
            "U_MAIN", self.companies, self.payments,
            as_of_date="2026-10-26",
        )
        metrics = {item["name"]: item for item in profile["derived_metrics"]}

        top1 = metrics["top1_counterparty_share_out"]
        self.assertAlmostEqual(top1["value"], 0.75)
        self.assertEqual(
            top1["breakdown"], [{"key": "U_UNKNOWN", "share": 0.75}]
        )
        top5 = metrics["top5_counterparty_share_out"]
        shares = {item["key"]: item["share"] for item in top5["breakdown"]}
        self.assertEqual(set(shares), {"U_KNOWN", "U_UNKNOWN"})
        self.assertAlmostEqual(sum(shares.values()), 1.0)
        self.assertNotIn("U_MAIN", shares)

    def test_ids_and_profile_snapshot_are_deterministic(self) -> None:
        self._write_unambiguous_companies()
        self._write_payments()
        first = build_company_profile(
            "U_MAIN", self.companies, self.payments,
            as_of_date="2026-10-26",
        )
        second = build_company_profile(
            "U_MAIN", self.companies, self.payments,
            as_of_date="2026-10-26",
        )
        self.assertEqual(first, second)

    def test_conflicting_rows_are_not_selected_arbitrarily(self) -> None:
        write_csv(
            self.companies,
            COMPANY_COLUMNS,
            [
                ["U_MAIN", "G_1", "46.90", "Торговля", "existing", 1, 20, "R1"],
                ["U_MAIN", "G_2", "52.29", "Логистика", "existing", 1, 20, "R1"],
            ],
        )
        self._write_payments()
        profile = build_company_profile(
            "U_MAIN", self.companies, self.payments,
            as_of_date="2026-10-26",
        )

        self.assertEqual(profile["input_quality"]["usability"], "unusable")
        self.assertEqual(profile["meta"]["pipeline_mode"], "no_inference")
        self.assertIsNone(profile["meta"]["group_id"])
        self.assertTrue(profile["data_conflicts"])
        fact_refs = {
            evidence["ref"]
            for fact in profile["facts"]
            for evidence in fact["evidence"]
        }
        self.assertNotIn("GROUP_ID", fact_refs)
        self.assertNotIn("MAINOKVED_CODE", fact_refs)
        self.assertEqual(
            profile["downstream_hooks"]["matching_profile"]["okved_codes"], []
        )
        validate_contract(profile, "profile")

    def test_missing_company_returns_valid_no_inference_profile(self) -> None:
        self._write_unambiguous_companies()
        self._write_payments()
        profile = build_company_profile(
            "U_MISSING", self.companies, self.payments,
            as_of_date="2026-10-26",
        )
        view = build_profile_views(profile)["ml_view"]

        self.assertEqual(profile["input_quality"]["usability"], "unusable")
        self.assertEqual(profile["meta"]["pipeline_mode"], "no_inference")
        self.assertEqual(profile["facts"], [])
        validate_contract(profile, "profile")
        validate_contract(view, "view")


if __name__ == "__main__":
    unittest.main()
