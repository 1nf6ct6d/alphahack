import json
from pathlib import Path

from .runner import ALGORITHMS, METRICS


def display(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict) -> str:
    counts = report["counts"]
    coverage = report["coverage"]
    lines = [
        "# ML-3A — оценка ранжирования новостей", "",
        "СИНТЕТИЧЕСКАЯ ДЕМОНСТРАЦИЯ. Значения ниже проверяют работу "
        "инфраструктуры и не измеряют качество на реальных новостях."
        if report["kind"] == "synthetic_demo" else (
            "Оценка предоставленного пула с независимой разметкой. "
            "Отсутствующая метрика обозначена N/A."
        ), "",
        f"Набор: `{display(report['dataset_id'])}`, "
        f"версия `{display(report['dataset_version'])}`; "
        f"часть `{report['split']}`; "
        f"статус `{report['evaluation_status']}`.", "",
        f"SHA-256 набора: `{report['dataset_sha256']}`.", "",
        f"Компаний: {counts['companies']}; запросов: {counts['queries']}; "
        f"уникальных новостей: {counts['unique_events']}; "
        f"групп событий: {counts['underlying_event_groups']}.", "",
        "Пар компания–новость: "
        f"{counts['company_event_pairs']}; оценено: "
        f"{counts['evaluated_company_event_pairs']}. "
        "С учётом версии профиля и фокуса: "
        f"{counts['company_event_context_pairs']}; оценено: "
        f"{counts['evaluated_company_event_context_pairs']}.", "",
        "Покрытие разметкой: "
        f"{coverage['resolved_judgments']}/{coverage['required_judgments']} "
        f"({display(coverage['judgment_fraction'])}); "
        f"пригодных запросов: {coverage['eligible_queries']}/"
        f"{coverage['total_queries']}.", "",
        f"Распределение принятых оценок: {report['label_distribution']}.", "",
        f"Статусы записей разметки: {report['annotation_status_counts']}.", "",
        "## Правила расчёта", "",
        f"k = {report['configuration']['k']}; релевантна оценка ≥ "
        f"{report['configuration']['relevant_threshold']}.", "",
        "Precision@k = число релевантных результатов в первых k / k. "
        "Незаполненные позиции остаются в знаменателе. Recall@k = число "
        "найденных релевантных / все релевантные в размеченном пуле.", "",
        "DCG@k = Σ (2^grade − 1) / log₂(position + 1); "
        "IDCG использует идеальный порядок оценок, nDCG = DCG / IDCG. "
        "При IDCG = 0 nDCG равен N/A; при отсутствии релевантных "
        "по порогу Recall равен N/A. Пустой или не полностью размеченный "
        "пул даёт N/A для всех метрик.", "",
        "Агрегация — среднее по применимым запросам; число таких запросов "
        "указано рядом с каждой метрикой. Обе системы получают один "
        "и тот же пул до любой фильтрации и одну и ту же разметку.", "",
        "## Общие метрики", "",
        "| Алгоритм | Precision@k | Recall@k | nDCG@k | Запросов P/R/N |",
        "|---|---|---|---|---|",
    ]
    for algorithm in ALGORITHMS:
        metrics = report["aggregate"][algorithm]
        values = [display(metrics[key]["macro_mean"]) for key in METRICS]
        denominators = "/".join(str(
            metrics[key]["applicable_query_count"]
        ) for key in METRICS)
        lines.append(
            f"| {algorithm} | {' | '.join(values)} | {denominators} |"
        )
    lines.extend(["", "## Результаты по компаниям", ""])
    for company, algorithms in report["per_company"].items():
        lines.append(f"Компания `{display(company)}`:")
        lines.append("")
        for name, metrics in algorithms.items():
            values = "; ".join(
                f"{key}: {display(metrics[key]['macro_mean'])} "
                f"(запросов {metrics[key]['applicable_query_count']})"
                for key in METRICS
            )
            lines.append(f"- {name}: {values}.")
        lines.append("")
    lines.extend(["## Запросы, расхождения и ошибки", ""])
    for query in report["queries"]:
        lines.extend([
            f"### {display(query['query_id'])}", "",
            f"Компания `{display(query['company_id'])}`, "
            f"профиль `{display(query['profile_version'])}`, "
            f"фокус `{display(query['analytic_focus'])}`, "
            f"момент `{query['as_of']}`.", "",
            f"Хеш общего пула: `{query['candidate_pool_sha256']}`.", "",
        ])
        for algorithm in ALGORITHMS:
            ranking = ", ".join(query["rankings"][algorithm]) or "пусто"
            metrics = query["metrics"][algorithm]
            examples = query["examples"][algorithm]
            lines.extend([
                f"{algorithm}: {ranking}.", "",
                "; ".join(f"{key}: {display(metrics[key])}"
                          for key in METRICS) + ".", "",
                "Ложноположительные в top-k: "
                f"{', '.join(examples['false_positives']) or 'нет'}.", "",
                "Пропущенные релевантные из пула: "
                f"{', '.join(examples['false_negatives']) or 'нет'}.", "",
            ])
            if metrics["precision_at_k"] is None:
                lines.extend([
                    "Диагностика ошибок недоступна: "
                    f"`{metrics['not_applicable_reason']}`.", "",
                ])
            for key in (
                examples["false_positives"] + examples["false_negatives"]
            ):
                judgment = query["judgments"][key]
                label = next(
                    item for item in judgment["annotations"]
                    if item["reviewer_id"] == judgment["selected_reviewer_id"]
                )
                lines.extend([
                    f"- `{key}`, оценка {judgment['grade']}: "
                    f"{display(label['relevance_reason'])} "
                    "Механизм по разметке: "
                    f"{display(label['impact_mechanism'])}.", "",
                ])
        lines.extend(["Расхождения позиций:", ""])
        for item in query["ranking_disagreements"]:
            lines.append(
                f"- `{item['event_id']}`: personalized "
                f"{display(item['personalized'])}, industry_only "
                f"{display(item['industry_only'])}."
            )
        if not query["ranking_disagreements"]:
            lines.append("Расхождений нет.")
        lines.append("")
    lines.extend(["## Неразрешённые оценки и недостаток доказательств", ""])
    for item in report["unresolved_judgments"]:
        lines.append(
            f"- `{item['query_id']}` / `{item['event_id']}`: "
            f"{display(item['reason'])}."
        )
    for item in report["missing_evidence"]:
        lines.append(
            f"- `{item['event_id']}`: {display(item['reason'])}."
        )
    if not report["unresolved_judgments"] and not report["missing_evidence"]:
        lines.append("Проблем со ссылками или разметкой не обнаружено.")
    lines.extend(["", "## Ограничения", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def write_reports(report: dict, output_dir: str | Path) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "ml3a_news_ranking_evaluation.json"
    markdown_path = output / "ml3a_news_ranking_evaluation.md"
    json_path.write_text(json.dumps(
        report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n", encoding="utf-8", newline="\n")
    markdown_path.write_text(
        render_markdown(report), encoding="utf-8", newline="\n"
    )
    return json_path, markdown_path
