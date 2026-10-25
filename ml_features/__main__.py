import argparse
import csv
import sys
from pathlib import Path

from ml_audit.csv_io import discover_datasets

from .payments import compute_payment_features
from .serialization import write_reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ML-1A: исходящие платёжные признаки одной компании"
    )
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--encoding", default="auto")
    parser.add_argument("--assume-additive-rows", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        paths = discover_datasets(arguments.data_dir)
        if paths["companies"] is None or paths["payments"] is None:
            raise ValueError("Нужны CSV компаний и платежей")
        features = compute_payment_features(
            arguments.company_id, paths["companies"], paths["payments"],
            assume_additive_rows=arguments.assume_additive_rows,
            encoding=arguments.encoding,
        )
        for path in write_reports(features, arguments.output_dir):
            print(f"Отчёт сохранён: {path}")
    except (OSError, ValueError, csv.Error, LookupError) as error:
        print(f"Расчёт не выполнен: {error}", file=sys.stderr)
        return 2
    print(
        "Расчёт выполнен с явным допущением аддитивности строк."
        if features.additive else
        "Структурные показатели рассчитаны; суммирование отключено."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
