import argparse

from .dataset import load_dataset
from .demo import build_synthetic_dataset
from .reporting import write_reports
from .runner import evaluate_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ML-3A: оценка ранжирования")
    parser.add_argument("--dataset")
    parser.add_argument("--split", choices=("development", "held_out"),
                        default="development")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--relevant-threshold", type=int, choices=(1, 2),
                        default=1)
    parser.add_argument("--output-dir", default="reports")
    args = parser.parse_args(argv)
    try:
        dataset = load_dataset(args.dataset) if args.dataset else (
            build_synthetic_dataset()
        )
        report = evaluate_dataset(
            dataset, split=args.split, k=args.k,
            relevant_threshold=args.relevant_threshold,
        )
        for path in write_reports(report, args.output_dir):
            print(f"Отчёт сохранён: {path}")
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.exit(2, f"Ошибка оценки: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
