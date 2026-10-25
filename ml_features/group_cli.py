import argparse
import csv
import sys
from pathlib import Path

from ml_audit.csv_io import discover_datasets

from .group_serialization import write_group_reports
from .groups import compute_group_features


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ML-1B: состав группы, выручка и платёжные отношения"
    )
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--encoding", default="auto")
    parser.add_argument("--assume-additive-rows", action="store_true")
    parser.add_argument("--without-payments", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        paths = discover_datasets(arguments.data_dir)
        if paths["companies"] is None:
            raise ValueError("Нужен CSV справочника компаний")
        features = compute_group_features(
            arguments.company_id, paths["companies"],
            None if arguments.without_payments else paths["payments"],
            assume_additive_rows=arguments.assume_additive_rows,
            encoding=arguments.encoding,
        )
        for path in write_group_reports(features, arguments.output_dir):
            print(f"Отчёт сохранён: {path}")
    except (OSError, ValueError, TypeError, csv.Error, LookupError) as error:
        print(f"Расчёт группы не выполнен: {error}", file=sys.stderr)
        return 2
    print("Групповые признаки рассчитаны; ограничения сохранены в отчёте.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
