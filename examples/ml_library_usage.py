import csv
import json
import tempfile
from datetime import datetime
from pathlib import Path

from ml_features import (
    compute_group_features, compute_payment_features,
    serialize_derived_metrics, serialize_features,
    serialize_group_derived_metrics, serialize_group_features,
)
from ml_ranking import NewsEvent, rank_news, serialize_ranking
from ml_ranking.validation import validate_contract

ROOT = Path(__file__).resolve().parent.parent


def run_example() -> dict:
    with tempfile.TemporaryDirectory(prefix="ml_library_example_") as folder:
        company_csv = Path(folder) / "companies.csv"
        payment_csv = Path(folder) / "payments.csv"
        with company_csv.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["SYNTHETIC_U", "GROUP_ID", "REVENUE_COMPANY"])
            writer.writerows([
                ["EXAMPLE_A", "EXAMPLE_GROUP", "2000"],
                ["EXAMPLE_B", "EXAMPLE_GROUP", "1000"],
            ])
        with payment_csv.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"])
            writer.writerows([
                ["EXAMPLE_A", "EXAMPLE_B", "30", "3"],
                ["EXAMPLE_A", "EXAMPLE_UNLISTED", "70", "7"],
                ["EXAMPLE_A", "EXAMPLE_A", "10", "1"],
            ])
        payments = compute_payment_features(
            "EXAMPLE_A", company_csv, payment_csv, assume_additive_rows=True
        )
        groups = compute_group_features(
            "EXAMPLE_A", company_csv, payment_features=payments
        )
        payment_payload = serialize_features(payments)
        group_payload = serialize_group_features(groups)
        derived = (
            serialize_derived_metrics(payments)
            + serialize_group_derived_metrics(groups)
        )
    base = ROOT / "tests/fixtures/scenarios"
    view = json.loads((base / "s1_MLView.json").read_text(encoding="utf-8"))
    profile = json.loads((base / "s1_expected_profile.json").read_text(
        encoding="utf-8"
    ))
    records = json.loads((
        ROOT / "ml_ranking/examples/synthetic_events.json"
    ).read_text(encoding="utf-8"))
    validate_contract(view, "view")
    ranked = rank_news(
        view, [NewsEvent.from_dict(record) for record in records],
        company_profile=profile, analytic_focus="CFO",
        as_of=datetime.fromisoformat("2026-10-25T12:00:00+03:00"),
    )
    return {
        "status": "synthetic_library_example",
        "feature_company_id": payments.company_id,
        "derived_metrics_version": payment_payload["derived_metrics_version"],
        "payment_metrics": payment_payload["metrics"],
        "recipient_diagnostics": payment_payload["recipients"],
        "group_membership": group_payload["group_membership"],
        "group_revenue": group_payload["revenue"],
        "derived_metrics": derived,
        "ranking_company_id": view["company_id"],
        "ranked_candidates": serialize_ranking(ranked),
        "integration_dependency": (
            "EXAMPLE_A и U_T1 — отдельные синтетические примеры. "
            "Рассчитанные признаки не вставлялись в готовый профиль U_T1. "
            "Сборка CompanyBusinessProfile и проекция MLView "
            "принадлежат Backend."
        ),
    }


if __name__ == "__main__":
    print(json.dumps(
        run_example(), ensure_ascii=False, sort_keys=True, indent=2,
        allow_nan=False,
    ))
