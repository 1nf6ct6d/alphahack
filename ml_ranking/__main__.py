import argparse

from .demo import run_demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ML-2A: синтетическая демонстрация ранжирования без API"
    )
    parser.add_argument("--output-dir", default="reports")
    arguments = parser.parse_args(argv)
    for path in run_demo(arguments.output_dir):
        print(f"Отчёт сохранён: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
