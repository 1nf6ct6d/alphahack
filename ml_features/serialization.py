import hashlib
import json
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any

from .payments import AMOUNT_METRICS, RATIO_METRICS, PaymentFeatures

DERIVED_METRICS_VERSION = "ml1a-1.1.0"

DERIVED_METRICS = (
    ("counterparty_count_out", "external_recipient_count", "count",
     "count(distinct BENEFICIARY) on valid external PAY_AMT rows"),
    ("top1_counterparty_share_out", "top1_share", "share",
     "max(sum(valid PAY_AMT by external BENEFICIARY)) / external_amount"),
    ("top5_counterparty_share_out", "top5_share", "share",
     "sum(top5 external recipient amounts) / external_amount"),
    ("hhi_out", "hhi_out", "index",
     "sum(external recipient amount squared) / external_amount squared"),
    ("intragroup_share_out", "intragroup_share_out", "share",
     "confirmed same-group external amount / external_amount; "
     "null if group allocation is incomplete"),
    ("counterparty_coverage_in_df_uid", "counterparty_coverage", "share",
     "unambiguous external recipient profiles / all distinct recipients "
     "on valid external PAY_AMT rows"),
)
UNDEFINED_EXPLANATIONS = {
    "additivity_not_assumed": (
        "Суммирование строк явно не разрешено допущением."
    ),
    "no_valid_amounts": "Строки есть, но пригодных денежных значений нет.",
    "incomplete_pay_cnt": (
        "PAY_CNT неполон; полный итог количества неизвестен."
    ),
    "zero_external_amount": "Внешняя пригодная сумма равна нулю.",
    "no_valid_external_recipients": "Нет получателей пригодных внешних сумм.",
    "unknown_group_membership": "Часть суммы имеет неизвестную группу.",
}


def exact_decimal(value: Fraction) -> str:
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError(
            "Денежное значение не имеет конечной десятичной записи"
        )
    scale = max(twos, fives)
    integer = (
        value.numerator * 2 ** (scale - twos) * 5 ** (scale - fives)
    )
    if not scale:
        return str(integer)
    digits = str(abs(integer)).rjust(scale + 1, "0")
    fractional = digits[-scale:].rstrip("0")
    sign = "-" if integer < 0 else ""
    return sign + digits[:-scale] + ("." + fractional if fractional else "")


def exact_ratio(value: Fraction) -> dict[str, str]:
    return {
        "numerator": str(value.numerator),
        "denominator": str(value.denominator),
    }


def display_ratio(value: Fraction | None, places: int = 9) -> str:
    if value is None:
        return "не определено"
    with localcontext() as context:
        context.prec = max(
            places + 20,
            len(str(abs(value.numerator))) + len(str(value.denominator)) + 20,
        )
        decimal = Decimal(value.numerator) / Decimal(value.denominator)
        return f"{decimal:.{places}f}"


def _counterparty_breakdown(features: PaymentFeatures, key: str) -> list[dict]:
    limits = {"top1_share": 1, "top5_share": 5, "hhi_out": None}
    denominator = features.metrics["external_outgoing_amount"]
    if key not in limits or features.metrics[key] is None or (
        denominator is None or denominator <= 0
    ):
        return []
    recipients = sorted(
        features.recipients,
        key=lambda item: (-(item.amount or Fraction(0)), item.company_id),
    )
    if any(item.amount is None for item in recipients) or sum(
        (item.amount for item in recipients), Fraction(0)
    ) != denominator:
        raise ValueError("Суммы получателей не совпадают с внешней суммой")
    return [
        {"key": item.company_id, "share": float(item.amount / denominator)}
        for item in recipients[:limits[key]]
    ]


def serialize_derived_metrics(features: PaymentFeatures) -> list[dict]:
    if not features.diagnostics["cohorts"]["outgoing"]["record_count"]:
        return []
    results = []
    for name, key, unit, formula in DERIVED_METRICS:
        value = features.metrics[key]
        inputs = [{
            "source": "payments", "ref": "PAYER",
            "value": features.company_id, "role": "supports",
        }]
        coverage = features.amount_row_coverage
        if key == "intragroup_share_out":
            coverage = features.group_amount_coverage
            if features.company_group_id is not None:
                inputs.append({
                    "source": "df_Uid", "ref": "GROUP_ID",
                    "value": features.company_group_id, "role": "supports",
                })
        caveats = list(features.limitations)
        reason = features.undefined_reasons.get(key)
        if reason:
            caveats.append(UNDEFINED_EXPLANATIONS[reason])
        if key == "intragroup_share_out" and value is None:
            caveats.append(
                "Подтверждённая нижняя граница "
                "хранится отдельно от точной доли."
            )
        low_quality = (
            value is None
            or bool(features.diagnostics["cohorts"]["outgoing"][
                "amount_issues"
            ])
        )
        identity = {
            "version": DERIVED_METRICS_VERSION,
            "company_id": features.company_id,
            "name": name, "additive": features.additive,
            "source_hashes": {
                kind: source["sha256"]
                for kind, source in features.sources.items()
            },
        }
        digest = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest()[:8]
        results.append({
            "id": "D-" + digest,
            "epistemic_status": "DERIVED",
            "name": name,
            "value": float(value) if isinstance(value, Fraction) else value,
            "unit": unit,
            "unit_dependency": "unit_free",
            "formula": formula,
            "inputs": inputs,
            "coverage": float(coverage) if coverage is not None else None,
            "data_reliability": "low" if low_quality else "medium",
            "caveats": caveats,
        })
        breakdown = _counterparty_breakdown(features, key)
        if breakdown:
            results[-1]["breakdown"] = breakdown
    return results


def serialize_features(features: PaymentFeatures) -> dict[str, Any]:
    metrics = {}
    for name, value in sorted(features.metrics.items()):
        if name in AMOUNT_METRICS and value is not None:
            metrics[name] = exact_decimal(value)
        elif isinstance(value, Fraction):
            metrics[name] = float(value)
        else:
            metrics[name] = value
    denominator = features.metrics["external_outgoing_amount"]
    recipients = []
    for recipient in sorted(
        features.recipients,
        key=lambda item: (-(item.amount or Fraction(0)), item.company_id),
    ):
        share = (
            recipient.amount / denominator
            if denominator is not None and denominator > 0
            and recipient.amount is not None else None
        )
        recipients.append({
            "company_id": recipient.company_id,
            "profile_status": recipient.profile_status,
            "group_relation": recipient.group_relation,
            "confirmed_group_id": recipient.group_id,
            "valid_amount_records": recipient.valid_amount_records,
            "amount_rub": (
                exact_decimal(recipient.amount)
                if recipient.amount is not None else None
            ),
            "share": float(share) if share is not None else None,
            "exact_share": exact_ratio(share) if share is not None else None,
        })
    return {
        "report_version": "ml1a-1.0.0",
        "derived_metrics_version": DERIVED_METRICS_VERSION,
        "company_id": features.company_id,
        "semantics": {
            "currency": "RUB", "start_month_day": "01-01",
            "year": None, "end_date": None,
        },
        "aggregation": {
            "additivity_assumed": features.additive,
            "additivity_verified_from_source": False,
            "exact_duplicates_removed": False,
        },
        "metrics": metrics,
        "exact_ratios": {
            name: exact_ratio(features.metrics[name])
            for name in sorted(RATIO_METRICS)
            if isinstance(features.metrics[name], Fraction)
        },
        "coverage": {
            "valid_external_amount_row_share": (
                float(features.amount_row_coverage)
                if features.amount_row_coverage is not None else None
            ),
            "confirmed_group_amount_share": (
                float(features.group_amount_coverage)
                if features.group_amount_coverage is not None else None
            ),
        },
        "company_confirmed_group_id": features.company_group_id,
        "sources": features.sources,
        "diagnostics": features.diagnostics,
        "recipients": recipients,
        "derived_metrics": serialize_derived_metrics(features),
        "undefined_metrics": {
            name: {"code": reason,
                   "explanation": UNDEFINED_EXPLANATIONS[reason]}
            for name, reason in sorted(features.undefined_reasons.items())
        },
        "limitations": list(features.limitations),
    }


def render_markdown(features: PaymentFeatures) -> str:
    metrics = features.metrics
    amount_keys = (
        ("Все пригодные исходящие", "outgoing_amount"),
        ("Пригодные внешние исходящие", "external_outgoing_amount"),
        ("Исключённые самоплатежи", "self_payment_amount"),
        ("Без ID получателя", "unassigned_outgoing_amount"),
        ("Подтверждённая та же группа", "confirmed_same_group_amount"),
        ("Подтверждённая другая группа", "confirmed_other_group_amount"),
        ("Неизвестная группа", "unknown_group_amount"),
    )
    lines = [
        f"# ML-1A — платежи компании {features.company_id}", "",
        "Денежные суммы условны: использовано явное допущение аддитивности "
        "строк, которое не подтверждается самим CSV."
        if features.additive else "Суммирование отключено.",
        "", "## Суммы", "",
        "| Показатель | RUB |", "|---|---|",
    ]
    for title, key in amount_keys:
        value = metrics[key]
        text = exact_decimal(value) if value is not None else "не определено"
        lines.append(f"| {title} | {text} |")
    count = metrics["external_outgoing_payment_count"]
    cohorts = features.diagnostics["cohorts"]
    lines.extend([
        "", "## Количество и концентрация", "",
        f"Внешних строк: **{cohorts['external']['record_count']}**; "
        f"самоплатёжных строк: **{metrics['self_payment_record_count']}**.",
        "",
        "Сумма внешнего PAY_CNT: "
        f"**{count if count is not None else 'не определено'}**; "
        "известная частичная сумма: "
        f"{cohorts['external']['known_payment_count_on_valid_amounts']}.",
        "",
        f"Уникальных внешних получателей: "
        f"**{metrics['external_recipient_count']}**; с однозначным профилем: "
        f"**{metrics['unambiguous_recipient_count']}**.",
        "",
        "| Показатель | Доля от 0 до 1 |", "|---|---|",
    ])
    for title, key in (
        ("Top-1", "top1_share"), ("Top-5", "top5_share"),
        ("HHI", "hhi_out"), ("Покрытие K / N", "counterparty_coverage"),
        ("Денежное покрытие", "counterparty_amount_coverage"),
        ("Точная внутригрупповая доля", "intragroup_share_out"),
        ("Подтверждённая внутригрупповая нижняя граница",
         "intragroup_share_lower_bound"),
        ("Внутригрупповая верхняя граница", "intragroup_share_upper_bound"),
        ("Неизвестная групповая принадлежность", "unknown_group_amount_share"),
    ):
        lines.append(f"| {title} | {display_ratio(metrics[key])} |")
    lines.extend([
        "", "Знаменатель денежных долей — пригодная внешняя сумма без "
        "самоплатежей. Знаменатель K / N — все уникальные получатели "
        "пригодных внешних сумм, включая неизвестных и технических.",
        "", "## Получатели", "",
        "| ID | RUB | Доля | Профиль | Отношение к группе |",
        "|---|---|---|---|---|",
    ])
    profile_titles = {
        "unambiguous": "однозначный", "missing": "нет в справочнике",
        "conflicting": "конфликтующий", "technical_ambiguous": "технический",
    }
    relation_titles = {
        "same": "та же", "other": "другая подтверждённая",
        "unknown": "неизвестно",
    }
    for recipient in serialize_features(features)["recipients"]:
        exact = recipient["exact_share"]
        share = (
            Fraction(int(exact["numerator"]), int(exact["denominator"]))
            if exact else None
        )
        amount = recipient["amount_rub"]
        lines.append(
            f"| {recipient['company_id']} "
            f"| {amount if amount is not None else 'не определено'} "
            f"| {display_ratio(share)} "
            f"| {profile_titles[recipient['profile_status']]} "
            f"| {relation_titles[recipient['group_relation']]} |"
        )
    lines.extend(["", "## Качество данных", ""])
    for title, key in (
        ("Точных повторов сверх первой строки",
         "exact_duplicate_extra_records"),
        ("Повторных направленных пар", "repeated_directed_pair_count"),
        ("Повторов пар сверх первой строки", "repeated_pair_extra_records"),
    ):
        lines.append(f"- {title}: {features.diagnostics[key]}.")
    lines.append("")
    cohort_titles = {
        "outgoing": "Все исходящие", "external": "Внешние",
        "self": "Самоплатежи", "unassigned": "Без ID получателя",
    }
    for name, cohort in cohorts.items():
        lines.append(
            f"- {cohort_titles[name]}: строк {cohort['record_count']}; "
            f"ошибки сумм {cohort['amount_issues']}; "
            f"ошибки PAY_CNT {cohort['count_issues_on_all_records']}."
        )
    lines.extend(["", "## Неопределённые показатели", ""])
    for name, reason in sorted(features.undefined_reasons.items()):
        lines.append(f"- {name}: {UNDEFINED_EXPLANATIONS[reason]}")
    if not features.undefined_reasons:
        lines.append("Нет неопределённых показателей.")
    lines.extend(["", "## Источники", ""])
    for kind, source in features.sources.items():
        lines.append(
            f"- {kind}: `{source['path']}`, SHA-256 `{source['sha256']}`."
        )
    lines.extend(["", "## Ограничения", ""])
    lines.extend(f"- {limitation}" for limitation in features.limitations)
    return "\n".join(lines) + "\n"


def write_reports(
    features: PaymentFeatures, output_dir: str | Path
) -> tuple[Path, Path]:
    output = Path(output_dir)
    identity = hashlib.sha256(features.company_id.encode("utf-8")).hexdigest()
    safe_id = "".join(
        character if character.isascii() and (
            character.isalnum() or character in "_-"
        ) else "_" for character in features.company_id
    )[:80]
    if safe_id != features.company_id:
        safe_id += "_" + identity[:8]
    stem = "ml1a_payment_features_" + safe_id
    json_content = json.dumps(
        serialize_features(features), ensure_ascii=False, indent=2,
        sort_keys=True, allow_nan=False,
    ) + "\n"
    markdown = render_markdown(features)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / (stem + ".json")
    markdown_path = output / (stem + ".md")
    json_path.write_text(json_content, encoding="utf-8", newline="\n")
    markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
    return json_path, markdown_path
