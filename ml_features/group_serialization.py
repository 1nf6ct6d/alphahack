import hashlib
import json
from pathlib import Path
from typing import Any

from .groups import GroupFeatures
from .serialization import (
    display_ratio,
    exact_decimal,
    exact_ratio,
    serialize_features,
)

REVENUE_TITLES = {
    "valid": "пригодна", "missing": "нет данных",
    "placeholder": "техническая заглушка 1000",
    "negative": "отрицательная", "invalid": "нечисловая",
    "non_finite": "не конечное число",
}
RELATIONSHIP_TITLES = {
    "recipient_not_in_directory": "нет в справочнике",
    "technical_ambiguous_recipient": "технический неоднозначный ID",
    "conflicting_recipient_profile": "конфликт профиля",
    "recipient_group_missing": "профиль есть, GROUP_ID отсутствует",
    "target_group_unconfirmed": "неизвестна группа плательщика",
}


def serialize_group_derived_metrics(features: GroupFeatures) -> list[dict]:
    if features.confirmed_group_id is None:
        return []
    identity = {
        "version": "ml1b-1.0.0", "name": "group_size",
        "company_id": features.company_id,
        "group_id": features.confirmed_group_id,
        "company_source_sha256": features.source["sha256"],
    }
    digest = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()[:8]
    uncertain = features.directory_diagnostics[
        "unresolved_candidate_member_count"
    ]
    return [{
        "id": "D-" + digest,
        "epistemic_status": "DERIVED",
        "name": "group_size",
        "value": features.group_size,
        "unit": "count",
        "unit_dependency": "unit_free",
        "formula": (
            "count(distinct SYNTHETIC_U) with unambiguous profile "
            "and confirmed GROUP_ID equal to target GROUP_ID; "
            "technical IDs excluded"
        ),
        "inputs": [
            {"source": "df_Uid", "ref": "SYNTHETIC_U",
             "value": features.company_id, "role": "supports"},
            {"source": "df_Uid", "ref": "GROUP_ID",
             "value": features.confirmed_group_id, "role": "supports"},
        ],
        "coverage": None,
        "data_reliability": "low" if uncertain else "medium",
        "caveats": [
            "Посчитаны только однозначные участники данной выгрузки.",
            "Полнота реального состава группы неизвестна; coverage = null.",
            "Дубликаты ID не увеличивают число юрлиц.",
            "Спорные кандидаты и U_428697 "
            "исключены из подтверждённого состава.",
            "GROUP_ID не доказывает контроль собственности или зависимость.",
        ],
    }]


def serialize_group_features(features: GroupFeatures) -> dict[str, Any]:
    revenue = None
    if features.revenue is not None:
        revenue = dict(features.revenue)
        amount = revenue["reported_member_revenue_sum"]
        coverage = revenue["valid_revenue_coverage"]
        revenue["reported_member_revenue_sum"] = (
            exact_decimal(amount) if amount is not None else None
        )
        revenue["valid_revenue_coverage"] = (
            float(coverage) if coverage is not None else None
        )
        revenue["exact_valid_revenue_coverage"] = (
            exact_ratio(coverage) if coverage is not None else None
        )
    payments = None
    if features.payments is not None:
        previous = serialize_features(features.payments)
        relationships = dict(features.payment_relationships)
        relationships["unknown_group_recipients"] = [
            {**recipient, "amount": (
                exact_decimal(recipient["amount"])
                if recipient["amount"] is not None else None
            )}
            for recipient in relationships["unknown_group_recipients"]
        ]
        payments = {
            "mode": features.payment_mode,
            "aggregation": previous["aggregation"],
            "semantics": previous["semantics"],
            "metrics": previous["metrics"],
            "exact_ratios": previous["exact_ratios"],
            "relationships": relationships,
            "source": previous["sources"]["payments"],
            "derived_metrics": previous["derived_metrics"],
            "undefined_metrics": previous["undefined_metrics"],
        }
    return {
        "report_version": "ml1b-1.0.0",
        "company_id": features.company_id,
        "company_profile_status": features.company_profile_status,
        "group_membership": {
            "group_id_present_in_observations": features.group_id_present,
            "confirmed_group_id": features.confirmed_group_id,
            "belongs_to_group": (
                True if features.confirmed_group_id is not None else None
            ),
            "observed_confirmed_member_count": features.group_size,
            "full_group_population_known": False,
        },
        "known_members": None if features.members is None else [
            {"company_id": member.company_id,
             "source_record_count": member.source_record_count,
             "revenue_raw": member.revenue_raw,
             "revenue_status": member.revenue_status,
             "valid_revenue_rub": (
                 exact_decimal(member.revenue_amount)
                 if member.revenue_amount is not None else None
             )}
            for member in features.members
        ],
        "revenue": revenue,
        "revenue_semantics": {
            "year": 2025, "currency": "RUB", "technical_placeholder": "1000",
        },
        "confirmed_consolidated_group_revenue": None,
        "consolidated_revenue_unavailable_reason": (
            "В CSV нет подтверждённой консолидированной отчётности группы."
        ),
        "directory_diagnostics": features.directory_diagnostics,
        "unresolved_candidate_members": list(features.unresolved_candidates),
        "payments": payments,
        "source": features.source,
        "derived_metrics": serialize_group_derived_metrics(features),
        "limitations": list(features.limitations),
    }


def text(value: Any) -> str:
    if value is None:
        return "не определено"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_group_markdown(features: GroupFeatures) -> str:
    diagnostics = features.directory_diagnostics
    lines = [
        f"# ML-1B — группа компании {text(features.company_id)}", "",
        "## Подтверждённый состав в выгрузке", "",
        f"GROUP_ID: **{text(features.confirmed_group_id)}**. "
        f"Уникальных однозначных участников: **{text(features.group_size)}**.",
        "",
        "Полный состав реальной группы неизвестен. "
        "Отсутствие GROUP_ID не означает независимость.",
        "",
        "Неразрешённых уникальных ID во всём справочнике: "
        f"**{diagnostics['directory_unassigned_company_count']}**. "
        "Спорных кандидатов именно в эту группу: "
        f"**{text(diagnostics['unresolved_candidate_member_count'])}**.",
        "",
        "Строк справочника без ID компании: "
        f"{diagnostics['directory_rows_without_company_id']}. "
        "Они не считаются отдельными установленными юрлицами.", "",
        "## Выручка за 2025 год", "",
    ]
    revenue = features.revenue
    if revenue is None:
        lines.extend([
            "Состав группы не подтверждён; групповая выручка неизвестна.", ""
        ])
    else:
        amount = revenue["reported_member_revenue_sum"]
        coverage = revenue["valid_revenue_coverage"]
        amount_text = exact_decimal(amount) if amount is not None else None
        lines.extend([
            "Сумма пригодной выручки уникальных подтверждённых юрлиц: "
            f"**{text(amount_text)} RUB**.", "",
            "Это сумма наблюдений, **не консолидированная выручка**. "
            "Внутригрупповые обороты не исключены. "
            "Подтверждённая консолидированная выручка неизвестна.", "",
            "Пригодная выручка есть у "
            f"**{revenue['valid_revenue_member_count']} "
            f"из {revenue['member_count']}** участников "
            f"({display_ratio(coverage * 100, 2)}%). "
            f"Пропусков: {revenue['missing_revenue_member_count']}; "
            "заглушек 1000: "
            f"{revenue['placeholder_revenue_member_count']}; "
            f"прочих ошибок: {revenue['invalid_revenue_member_count']}.", "",
        ])
    if features.payments is not None:
        payments = features.payments
        relationships = features.payment_relationships
        metrics = payments.metrics
        lines.extend([
            "## Платёжные отношения целевой компании", "",
            "Суммы относятся к пригодным внешним платежам ML-1A; "
            "самоплатежи исключены, неизвестные получатели сохранены.", "",
            "Аддитивность строк явно допущена, но не подтверждена CSV."
            if payments.additive else "Суммирование строк отключено.", "",
            "| Показатель | Значение |", "|---|---|",
        ])
        for label, key in (
            ("Внешние исходящие, RUB", "external_outgoing_amount"),
            ("Подтверждённая та же группа, RUB",
             "confirmed_same_group_amount"),
            ("Подтверждённая другая группа, RUB",
             "confirmed_other_group_amount"),
            ("Неизвестная группа, RUB", "unknown_group_amount"),
        ):
            value = metrics[key]
            value_text = exact_decimal(value) if value is not None else None
            lines.append(f"| {label} | {text(value_text)} |")
        for label, key in (
            ("Нижняя граница внутригрупповой доли",
             "intragroup_share_lower_bound"),
            ("Верхняя граница внутригрупповой доли",
             "intragroup_share_upper_bound"),
            ("Точная внутригрупповая доля", "intragroup_share_out"),
        ):
            lines.append(f"| {label} | {display_ratio(metrics[key])} |")
        missing_group_count = relationships[
            "unresolved_unambiguous_profiles_missing_group_count"
        ]
        partition_verified = relationships[
            "external_group_partition_conservation_verified"
        ]
        lines.extend([
            "",
            "Неразрешённых получателей, присутствующих в справочнике: "
            "**"
            f"{relationships['unresolved_recipients_in_directory_count']}**. "
            "Из них однозначных профилей без GROUP_ID: "
            f"**{missing_group_count}**.",
            "",
            "Проверка разбиения внешней суммы: "
            f"{text(partition_verified)}. "
            "Согласованность границ с ML-1A: "
            f"{text(relationships['bounds_consistent_with_ml1a'])}.", "",
        ])
        unresolved = relationships["unknown_group_recipients"]
        if unresolved:
            lines.extend([
                "| Неразрешённый получатель | Причина | RUB |",
                "|---|---|---|",
            ])
            for recipient in unresolved:
                value = recipient["amount"]
                amount_text = (
                    exact_decimal(value) if value is not None else None
                )
                lines.append(
                    f"| {text(recipient['company_id'])} "
                    f"| {RELATIONSHIP_TITLES[recipient['reason']]} "
                    f"| {text(amount_text)} |"
                )
            lines.append("")
    else:
        lines.extend([
            "## Платёжные отношения", "",
            "Платежи не переданы; суммы и границы неизвестны.", "",
        ])
    lines.extend([
        "## Известные участники", "",
        "| ID компании | Строк источника | Статус выручки | Пригодная RUB |",
        "|---|---|---|---|",
    ])
    if features.members is None:
        lines.extend(["", "Подтверждённый список участников неизвестен."])
    else:
        for member in features.members:
            value = member.revenue_amount
            amount_text = exact_decimal(value) if value is not None else None
            lines.append(
                f"| {text(member.company_id)} | {member.source_record_count} "
                f"| {REVENUE_TITLES[member.revenue_status]} "
                f"| {text(amount_text)} |"
            )
    if features.unresolved_candidates:
        lines.extend(["", "Спорные кандидаты, исключённые из подсчёта:", ""])
        lines.extend(
            f"- {text(candidate['company_id'])}: "
            f"{candidate['source_record_count']} записей."
            for candidate in features.unresolved_candidates
        )
    lines.extend([
        "", "## Источник состава и выручки", "",
        f"`{text(features.source['path'])}`, "
        f"SHA-256 `{features.source['sha256']}`.",
        "", "## Ограничения", "",
    ])
    lines.extend(f"- {limitation}" for limitation in features.limitations)
    return "\n".join(lines) + "\n"


def write_group_reports(
    features: GroupFeatures, output_dir: str | Path
) -> tuple[Path, Path]:
    safe_id = "".join(
        character if character.isascii() and (
            character.isalnum() or character in "_-"
        ) else "_" for character in features.company_id
    )[:80]
    if safe_id != features.company_id:
        safe_id += "_" + hashlib.sha256(
            features.company_id.encode("utf-8")
        ).hexdigest()[:8]
    stem = "ml1b_group_features_" + safe_id
    content = json.dumps(
        serialize_group_features(features), ensure_ascii=False,
        sort_keys=True, indent=2, allow_nan=False,
    ) + "\n"
    markdown = render_group_markdown(features)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / (stem + ".json")
    markdown_path = output / (stem + ".md")
    json_path.write_text(content, encoding="utf-8", newline="\n")
    markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
    return json_path, markdown_path
