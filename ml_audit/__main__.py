import argparse
import sys
from pathlib import Path

from . import audit_data, write_reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ML-0: локальный аудит трёх CSV без LLM"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument(
        "--encoding", default="auto",
        choices=("auto", "utf-8", "utf-8-sig", "cp1251", "utf-16", "utf-32"),
    )
    arguments = parser.parse_args(argv)
    try:
        report = audit_data(arguments.data_dir, arguments.encoding)
        paths = write_reports(report, arguments.output_dir)
    except (OSError, ValueError) as error:
        print(f"Аудит не выполнен: {error}", file=sys.stderr)
        return 2
    for path in paths:
        print(f"Отчёт сохранён: {path}")
    if report["status"] != "complete":
        print(
            "Аудит неполный: проверьте наличие файлов, "
            "структуру и ошибки чтения."
        )
        return 1
    print(
        "Все три файла обработаны. "
        "Результаты проверок качества — в отчётах."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
