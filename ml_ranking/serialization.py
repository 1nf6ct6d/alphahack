import copy
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import NewsEvent, RelevanceResult


def serialize_result(result: RelevanceResult) -> dict:
    return {
        "event_id": result.event_id,
        "company_id": result.company_id,
        "profile_id": result.profile_id,
        "title": result.event.title,
        "source_id": result.event.source_id,
        "source_url": result.event.source_url,
        "published_at": result.event.published_at,
        "synthetic": result.event.synthetic,
        "relevance": result.relevance,
        "priority": result.priority,
        "relevance_explanation": result.explanation,
        "impact_mechanism": result.impact_mechanism,
        "conditions": list(result.conditions),
        "evidence": copy.deepcopy(list(result.evidence)),
        "limitations": list(result.limitations),
        "interpretation_constraints": copy.deepcopy(
            list(result.interpretation_constraints)
        ),
        "ranking_factors": copy.deepcopy(result.ranking_factors),
    }


def serialize_ranking(results: list[RelevanceResult]) -> list[dict]:
    return [
        {"rank": position, **serialize_result(result)}
        for position, result in enumerate(results, 1)
    ]


def markdown_text(value) -> str:
    if value is None:
        return "неизвестно"
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_demo_reports(
    comparisons: list[dict], events: tuple[NewsEvent, ...],
    as_of: datetime, output_dir: str | Path,
) -> tuple[Path, Path]:
    report = {
        "report_version": "ml2a-1.0.0",
        "status": "synthetic_demonstration",
        "as_of": as_of.isoformat(),
        "description": (
            "Новости и профили синтетические. Это демонстрация правил, "
            "а не измерение качества на реальных событиях."
        ),
        "events": [asdict(event) for event in events],
        "companies": comparisons,
        "ranking_policy": [
            "grounded_before_conditional", "verified_approaching_deadline",
            "supported_mechanism_specificity", "verified_role_focus",
            "publication_recency", "event_id",
        ],
        "weight_semantics": "ordinal_prior_not_probability",
        "limitations": [
            "Публичный контракт новостей с Backend ещё не согласован.",
            "Сбор новостей, RAG, эмбеддинги и LLM "
            "не реализуются этим модулем.",
            "Подлинность внешних источников не проверяется по сети.",
            "Размеченной оценки качества ранжирования пока нет.",
        ],
    }
    lines = [
        "# ML-2A — демонстрация релевантности новостей", "",
        report["description"], "",
        f"Момент анализа: `{as_of.isoformat()}`.", "",
        "Порядок: grounded → conditional; подтверждённый близкий срок; "
        "специфичность механизма; подтверждённый ролевой фокус; "
        "свежесть; event_id. Числового relevance score и вероятностей нет.",
        "",
    ]
    for company in comparisons:
        lines.extend([
            f"## Компания {markdown_text(company['company_id'])}", "",
            f"MLView: `{company['ml_view_fixture']}`. "
            f"Полный профиль: `{company['profile_fixture']}`.", "",
            "| Место | Событие | Релевантность | Приоритет | Основание |",
            "|---|---|---|---|---|",
        ])
        for result in company["ranked"]:
            lines.append(
                f"| {result['rank']} | {markdown_text(result['event_id'])} "
                f"| {result['relevance']} | {result['priority']} "
                f"| {markdown_text(result['relevance_explanation'])} |"
            )
        if not company["ranked"]:
            lines.extend(["", "Релевантных событий нет."])
        for result in company["ranked"]:
            factors = result["ranking_factors"]
            urgency = (
                "подтверждена" if factors["urgent_deadline_verified"]
                else "не подтверждена"
            )
            lines.extend([
                "", f"### {markdown_text(result['event_id'])}", "",
                f"Механизм: {result['impact_mechanism']}", "",
                f"Факторы порядка: категория `{result['relevance']}`; "
                f"специфичность `{factors['mechanism_specificity']}`; "
                f"срочность {urgency}; "
                f"срок {markdown_text(factors['verified_deadline'])}; "
                "дней до срока "
                f"{markdown_text(factors['days_until_deadline'])}; "
                "публикация "
                f"{markdown_text(result['published_at'])}. "
                "Ролевой фокус в этой демонстрации не задан.", "",
                "Условия:", "",
            ])
            lines.extend(
                f"- {condition}" for condition in result["conditions"]
            )
            lines.extend(["", "Доступные ссылки:", ""])
            for evidence in result["evidence"]:
                if evidence["kind"] == "article_quote":
                    lines.append(
                        f"- `{evidence['evidence_id']}`: "
                        f"{markdown_text(evidence['quote'])} "
                        f"Источник: `{evidence['source_url']}`."
                    )
                elif evidence["kind"] == "company_statement":
                    lines.append(
                        f"- `{evidence['statement_id']}` "
                        f"({evidence['epistemic_status']}), "
                        f"профиль `{evidence['profile_id']}`."
                    )
                else:
                    lines.append(
                        f"- Поле MLView `{evidence['path']}`; "
                        "отдельное подтверждение профилем отсутствует."
                    )
            lines.extend(["", "Ограничения результата:", ""])
            lines.extend(
                f"- {limitation}" for limitation in result["limitations"]
            )
        lines.extend(["", "Исключённые события категории none:", ""])
        lines.extend(
            f"- `{item['event_id']}`: {item['reason']}"
            for item in company["excluded"]
        )
        lines.append("")
    lines.extend(["## Ограничения", ""])
    lines.extend(f"- {limitation}" for limitation in report["limitations"])
    lines.extend([
        "- Grounded означает поддержанную связь с данными профиля; "
        "это не подтверждение размера эффекта или вероятности.",
        "- Для ИТ-профиля ОКВЭД имеет статус code_only_unverified; "
        "отраслевая применимость остаётся conditional.",
    ])
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "ml2a_news_ranking_demo.json"
    markdown_path = output / "ml2a_news_ranking_demo.md"
    json_path.write_text(json.dumps(
        report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n", encoding="utf-8", newline="\n")
    markdown_path.write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )
    return json_path, markdown_path
