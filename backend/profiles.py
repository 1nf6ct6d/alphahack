

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ml_audit.audit import FLAG_COLUMNS, numeric_value
from ml_audit.csv_io import normalized
from ml_features import compute_payment_features, serialize_derived_metrics
from ml_features.sources import CsvSource, load_directory, profile_status
from ml_ranking.validation import validate_contract


SCHEMA_VERSION = "1.0.0"
ASSEMBLER_VERSION = "backend-profile-1.0.0"
DEFAULT_TTL_HOURS = 168

COMPANY_FIELDS = (
    "SYNTHETIC_U",
    "ORG_TYPE",
    "OKFS_NAME",
    "COMPANY_AGE_YEARS_GRADATION",
    "ECO_MARK_LABEL",
    "MAINOKVED_CODE",
    "MAINOKVED_NAME",
    "NEGATIVEMARK",
    "AMOUNT_WORKERS_FNS_GRADATION",
    "AMOUNT_WORKERS_SPARK_GRADATION",
    "CL_TYPE",
    "CL_PRODUCT_RKO_MARK",
    "CL_PRODUCT_GAR_MARK",
    "CL_PRODUCT_LOA_MARK",
    "CL_PRODUCT_LEA_MARK",
    "CL_PRODUCT_DEP_MARK",
    "CL_PRODUCT_ZPP_MARK",
    "FTS_FLAG",
    "FTS_COUNTRY_NAME",
    "FACTORING_MARK",
    "REVENUE_COMPANY",
    "GROUP_ID",
    "CITY",
    "REGION",
)

BANK_FLAGS = frozenset(
    field for field in COMPANY_FIELDS if field.startswith("CL_PRODUCT_")
) | {"FACTORING_MARK"}

OKVED_RE = re.compile(r"[0-9]{2}(?:\.[0-9]{1,2}){0,2}\Z")

FIELD_CATEGORIES = {
    "SYNTHETIC_U": "identity",
    "ORG_TYPE": "identity",
    "CL_TYPE": "identity",
    "OKFS_NAME": "ownership",
    "MAINOKVED_CODE": "industry",
    "MAINOKVED_NAME": "industry",
    "COMPANY_AGE_YEARS_GRADATION": "scale",
    "ECO_MARK_LABEL": "scale",
    "AMOUNT_WORKERS_FNS_GRADATION": "scale",
    "AMOUNT_WORKERS_SPARK_GRADATION": "scale",
    "REVENUE_COMPANY": "scale",
    "CL_PRODUCT_RKO_MARK": "bank_products",
    "CL_PRODUCT_GAR_MARK": "bank_products",
    "CL_PRODUCT_LOA_MARK": "bank_products",
    "CL_PRODUCT_LEA_MARK": "bank_products",
    "CL_PRODUCT_DEP_MARK": "bank_products",
    "CL_PRODUCT_ZPP_MARK": "bank_products",
    "FACTORING_MARK": "bank_products",
    "FTS_FLAG": "foreign_trade_flag",
    "FTS_COUNTRY_NAME": "foreign_trade_flag",
    "GROUP_ID": "group",
    "CITY": "geography",
    "REGION": "geography",
}

FIELD_TITLES = {
    "SYNTHETIC_U": "Идентификатор компании",
    "ORG_TYPE": "Тип организации",
    "OKFS_NAME": "Форма собственности",
    "COMPANY_AGE_YEARS_GRADATION": "Возрастной диапазон",
    "ECO_MARK_LABEL": "Стадия развития",
    "MAINOKVED_CODE": "Основной ОКВЭД",
    "MAINOKVED_NAME": "Название основного ОКВЭД",
    "AMOUNT_WORKERS_FNS_GRADATION": "Диапазон сотрудников ФНС",
    "AMOUNT_WORKERS_SPARK_GRADATION": "Диапазон сотрудников СПАРК",
    "CL_TYPE": "Статус клиента",
    "CL_PRODUCT_RKO_MARK": "РКО в банке",
    "CL_PRODUCT_GAR_MARK": "Гарантии в банке",
    "CL_PRODUCT_LOA_MARK": "Кредиты в банке",
    "CL_PRODUCT_LEA_MARK": "Лизинг в банке",
    "CL_PRODUCT_DEP_MARK": "Депозиты в банке",
    "CL_PRODUCT_ZPP_MARK": "Зарплатный проект в банке",
    "FTS_FLAG": "Признак ФТС",
    "FTS_COUNTRY_NAME": "Страна из данных ФТС",
    "FACTORING_MARK": "Факторинг в банке",
    "REVENUE_COMPANY": "Наблюдаемая выручка",
    "GROUP_ID": "Идентификатор группы",
    "CITY": "Город регистрации",
    "REGION": "Регион регистрации",
}

STATIC_CONSTRAINTS = (
    {
        "id": "IC-BANK-FLAG-SCOPE",
        "text": (
            "Значение 0 у CL_PRODUCT_* означает отсутствие продукта только "
            "в этом банке; не выводить отсутствие долга, лизинга или гарантий."
        ),
        "origin": "static_rule",
    },
    {
        "id": "IC-ABSENCE-IS-UNKNOWN",
        "text": "Пропущенное значение означает неизвестность, а не отсутствие признака.",
        "origin": "static_rule",
    },
    {
        "id": "IC-NO-INVENTED-ENTITIES",
        "text": (
            "Не называть поставщиков, покупателей, долги, ставки и суммы, "
            "которых нет в evidence."
        ),
        "origin": "static_rule",
    },
    {
        "id": "IC-PAYMENTS-NOT-SUPPLY",
        "text": (
            "Платёжная связь не доказывает поставку, операционную зависимость "
            "или назначение платежа."
        ),
        "origin": "semantics",
    },
)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_id(prefix: str, company_id: str, key: Any) -> str:
    return f"{prefix}-{_canonical_hash([company_id, key])[:8]}"


def _as_of(value: str | date | None) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise TypeError("as_of_date должен быть ISO-датой или date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("as_of_date должен иметь формат YYYY-MM-DD")
    return value


def _typed_value(field: str, raw: str | None) -> Any:
    value = normalized(raw)
    if value is None:
        return None
    if field in FLAG_COLUMNS or field == "REVENUE_COMPANY":
        number, state = numeric_value(value)
        if state != "number":
            return value.strip()
        if number == number.to_integral_value():
            return int(number)
        return float(number)
    return value.strip()


def _field_state(field: str, raw: str | None, *, conflicting: bool) -> dict:
    if conflicting:
        return {"state": "suspect", "scope": None}
    value = normalized(raw)
    if value is None:
        return {"state": "missing", "scope": None}
    if raw is not None and raw != raw.strip():
        return {"state": "suspect", "scope": None}
    if field in FLAG_COLUMNS:
        number, state = numeric_value(value)
        if state != "number" or number not in (Decimal(0), Decimal(1)):
            return {"state": "suspect", "scope": None}
        if number == 0:
            scope = "fts_base" if field == "FTS_FLAG" else "bank"
            return {"state": "confirmed_absent_in_scope", "scope": scope}
    if field == "REVENUE_COMPANY":
        number, state = numeric_value(value)
        if state != "number" or number < 0:
            return {"state": "suspect", "scope": None}
        if number == 1000:
            return {"state": "sentinel", "scope": None}
    if field == "MAINOKVED_CODE" and not OKVED_RE.fullmatch(value.strip()):
        return {"state": "suspect", "scope": None}
    return {"state": "value", "scope": None}


def _customer_status(row: dict[str, str | None], states: dict[str, dict]) -> str:
    if states["CL_TYPE"]["state"] != "value":
        return "unknown"
    token = str(_typed_value("CL_TYPE", row.get("CL_TYPE"))).casefold()
    if "потенц" in token or token in {"potential", "prospect"}:
        return "potential"
    if "действ" in token or token in {"existing", "current", "client", "клиент"}:
        return "existing"
    return "unknown"


def _fact_caveats(field: str, value: Any) -> list[str]:
    if field in BANK_FLAGS and value == 0:
        return ["Отсутствие продукта подтверждено только в пределах данных этого банка."]
    if field == "FTS_FLAG" and value == 0:
        return ["Отсутствие признака подтверждено только в пределах базы ФТС."]
    if field == "REVENUE_COMPANY":
        return ["Период и единицы выручки не подтверждены этим профилем."]
    if field in {"CITY", "REGION"}:
        return ["Это география регистрации, а не производства или продаж."]
    if field == "GROUP_ID":
        return ["Общий GROUP_ID сам по себе не доказывает контроль или зависимость."]
    return []


def _facts(
    company_id: str,
    row: dict[str, str | None],
    states: dict[str, dict],
) -> list[dict]:
    facts = []
    for field in COMPANY_FIELDS:
        if field == "NEGATIVEMARK" or field not in FIELD_CATEGORIES:
            continue
        if states[field]["state"] not in {"value", "confirmed_absent_in_scope"}:
            continue
        value = _typed_value(field, row.get(field))
        if value is None:
            continue
        title = FIELD_TITLES[field]
        statement = f"{title}: {value}."
        reliability = "medium" if field in {
            "MAINOKVED_CODE", "MAINOKVED_NAME", "REVENUE_COMPANY"
        } else "high"
        facts.append({
            "id": _stable_id("F", company_id, [field, value]),
            "epistemic_status": "FACT",
            "category": FIELD_CATEGORIES[field],
            "statement": statement[:500],
            "evidence": [{
                "source": "df_Uid",
                "ref": field,
                "value": value,
                "role": "supports",
            }],
            "data_reliability": reliability,
            "caveats": _fact_caveats(field, value),
        })
    return facts


def _conflicts(company_id: str, directory: Any) -> list[dict]:
    results = []
    for field, raw_values in sorted(directory.conflicts.get(company_id, {}).items()):
        values = sorted(
            (_typed_value(field, value) for value in raw_values),
            key=lambda item: (item is not None, str(item)),
        )
        evidence = [{
            "source": "df_Uid",
            "ref": field,
            "value": value,
            "role": "limits",
        } for value in values[:8]]
        results.append({
            "id": _stable_id("C", company_id, [field, values]),
            "rule_id": "CONF-DUPLICATE-COMPANY-ROW",
            "kind": "hard_contradiction",
            "severity": "critical",
            "fields": evidence,
            "description": (
                f"Для {company_id} поле {field} имеет {len(values)} "
                "различающихся значения; ни одно не выбрано автоматически."
            ),
            "resolution": "excluded_from_analysis",
            "plausible_explanations": [
                "Повторные записи относятся к разным срезам или содержат ошибку выгрузки."
            ],
        })
    if profile_status(directory, company_id) == "technical_ambiguous" and not results:
        results.append({
            "id": _stable_id("C", company_id, "technical_ambiguity"),
            "rule_id": "CONF-TECHNICAL-AMBIGUITY",
            "kind": "anomaly",
            "severity": "critical",
            "fields": [{
                "source": "df_Uid",
                "ref": "SYNTHETIC_U",
                "value": company_id,
                "role": "limits",
            }],
            "description": "Идентификатор помечен как технически неоднозначный.",
            "resolution": "excluded_from_analysis",
            "plausible_explanations": [],
        })
    return results


def _missing_data(
    company_id: str,
    states: dict[str, dict],
    *,
    company_state: str,
) -> list[dict]:
    impact = {
        "SYNTHETIC_U": "blocking",
        "MAINOKVED_CODE": "blocking",
        "REVENUE_COMPANY": "major",
        "CL_TYPE": "major",
    }
    items = []
    for field in COMPANY_FIELDS:
        state = states[field]["state"]
        if state not in {"missing", "sentinel", "suspect", "uninformative"}:
            continue
        if state == "missing":
            kind = "absent" if company_state == "missing" else "null"
        elif state == "sentinel":
            kind = "sentinel"
        else:
            kind = "uninformative"
        items.append({
            "id": _stable_id("M", company_id, [field, kind]),
            "feature": field,
            "kind": kind,
            "impact": impact.get(field, "minor"),
            "limits_conclusions_about": [],
            "request_suggestion": f"Уточнить корректное значение поля {field}.",
        })
    items.append({
        "id": _stable_id("M", company_id, ["PAYMENT_PERIOD", "semantics_unknown"]),
        "feature": "PAYMENT_PERIOD",
        "kind": "semantics_unknown",
        "impact": "major",
        "limits_conclusions_about": ["payment_dynamics", "annualized_amounts"],
        "request_suggestion": "Уточнить год и дату окончания платёжного периода.",
    })
    items.append({
        "id": _stable_id("M", company_id, ["NEGATIVEMARK", "semantics_unknown"]),
        "feature": "NEGATIVEMARK",
        "kind": "semantics_unknown",
        "impact": "none",
        "limits_conclusions_about": ["ownership"],
        "request_suggestion": None,
    })
    return items


def _quality(
    company_id: str,
    directory: Any,
    row: dict[str, str | None],
    states: dict[str, dict],
    payment_features: Any,
) -> dict:
    status = profile_status(directory, company_id)
    code = _typed_value("MAINOKVED_CODE", row.get("MAINOKVED_CODE"))
    code_conflict = "MAINOKVED_CODE" in directory.conflicts.get(company_id, {})
    if code_conflict:
        industry_status = "ambiguous"
        okved = None
    elif code is None:
        industry_status = "missing"
        okved = None
    elif isinstance(code, str) and OKVED_RE.fullmatch(code):
        industry_status = "code_only_unverified"
        okved = code
    else:
        industry_status = "invalid"
        okved = None

    signals = {
        "scale": any(states[field]["state"] == "value" for field in (
            "REVENUE_COMPANY", "AMOUNT_WORKERS_FNS_GRADATION",
            "AMOUNT_WORKERS_SPARK_GRADATION",
        )),
        "payments": bool(payment_features.diagnostics["cohorts"]["outgoing"]["record_count"]),
        "bank_products": any(
            states[field]["state"] in {"value", "confirmed_absent_in_scope"}
            for field in BANK_FLAGS
        ),
        "foreign_trade": any(
            states[field]["state"] in {"value", "confirmed_absent_in_scope"}
            for field in ("FTS_FLAG", "FTS_COUNTRY_NAME")
        ),
        "group_structure": states["GROUP_ID"]["state"] == "value",
    }

    if status != "unambiguous":
        usability = "unusable"
    elif industry_status == "code_only_unverified" and any(signals.values()):
        usability = "full"
    elif industry_status == "code_only_unverified":
        usability = "sector_only"
    elif any(signals.values()):
        usability = "limited"
    else:
        usability = "unusable"

    known = sum(
        state["state"] in {"value", "confirmed_absent_in_scope"}
        for state in states.values()
    )
    completeness = round(known / len(states), 6) if states else 0.0
    consistency = 1.0 if status == "unambiguous" else (
        0.5 if status == "missing" else 0.0
    )
    return {
        "usability": usability,
        "industry_identification": {
            "status": industry_status,
            "okved_code_used": okved,
        },
        "signal_groups": signals,
        "revenue_semantics_status": "unknown",
        "payments_semantics_status": "unknown",
        "target_segment_applicability": "unknown",
        "completeness_score": completeness,
        "consistency_score": consistency,
        "field_states": states,
        "notes": [
            f"company_directory_status={status}",
            f"company_directory_record_count={directory.counts[company_id]}",
        ],
    }


def _hooks(
    quality: dict,
    row: dict[str, str | None],
    conflicts: list[dict],
) -> dict:
    okved = quality["industry_identification"]["okved_code_used"]
    region = (
        _typed_value("REGION", row.get("REGION"))
        if quality["field_states"]["REGION"]["state"] == "value" else None
    )
    fts_flag = _typed_value("FTS_FLAG", row.get("FTS_FLAG"))
    country = (
        _typed_value("FTS_COUNTRY_NAME", row.get("FTS_COUNTRY_NAME"))
        if quality["field_states"]["FTS_COUNTRY_NAME"]["state"] == "value"
        and fts_flag == 1 else None
    )
    constraints = [dict(item) for item in STATIC_CONSTRAINTS]
    if conflicts:
        constraints.append({
            "id": "IC-CONFLICT-NO-SELECTION",
            "text": (
                "Поля из критического конфликта исключены из выводов; "
                "не выбирать одну из строк компании автоматически."
            ),
            "origin": "conflict",
        })
    return {
        "matching_profile": {
            "okved_codes": [okved] if okved else [],
            "regions": [region] if region else [],
            "countries": [country] if country else [],
            "factor_classes_weighted": {},
            "keywords": [],
            "negative_keywords": [],
        },
        "relevance_priors": [],
        "interpretation_constraints": constraints,
    }


def _evidence_is_verified(
    facts: list[dict],
    metrics: list[dict],
    row: dict[str, str | None],
    states: dict[str, dict],
    company_id: str,
) -> bool:
    for fact in facts:
        for evidence in fact["evidence"]:
            field = evidence["ref"]
            if field not in states or states[field]["state"] not in {
                "value", "confirmed_absent_in_scope"
            }:
                return False
            if _typed_value(field, row.get(field)) != evidence["value"]:
                return False
    for metric in metrics:
        for evidence in metric["inputs"]:
            if evidence["source"] == "payments":
                if evidence["ref"] != "PAYER" or evidence["value"] != company_id:
                    return False
            elif evidence["source"] == "df_Uid":
                field = evidence["ref"]
                if field not in states or states[field]["state"] != "value":
                    return False
                if _typed_value(field, row.get(field)) != evidence["value"]:
                    return False
            else:
                return False
    return True


def build_company_profile(
    company_id: str,
    company_csv: str | Path,
    payment_csv: str | Path,
    *,
    as_of_date: str | date | None = None,
    assume_additive_rows: bool = True,
    encoding: str = "auto",
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> dict:
    """Build and validate one immutable CompanyBusinessProfile snapshot.

    ``assume_additive_rows=True`` is an explicit integration assumption.  The
    ML serializer records it and its caveats in every affected metric.
    """
    if not isinstance(company_id, str) or normalized(company_id) is None:
        raise ValueError("company_id должен быть непустой строкой")
    if type(ttl_hours) is not int or ttl_hours < 1:
        raise ValueError("ttl_hours должен быть положительным целым числом")
    snapshot_date = _as_of(as_of_date)

    source = CsvSource.open(company_csv, ("SYNTHETIC_U", "GROUP_ID"), encoding)
    directory = load_directory(source)
    status = profile_status(directory, company_id)
    stored_row = directory.records.get(company_id, {})
    row = {field: stored_row.get(field) for field in COMPANY_FIELDS}
    conflicting_fields = set(directory.conflicts.get(company_id, {}))
    states = {
        field: _field_state(
            field,
            row.get(field),
            conflicting=field in conflicting_fields,
        )
        for field in COMPANY_FIELDS
    }
    if status == "missing":
        states = {
            field: {"state": "missing", "scope": None}
            for field in COMPANY_FIELDS
        }

    payment_features = compute_payment_features(
        company_id,
        company_csv,
        payment_csv,
        assume_additive_rows=assume_additive_rows,
        encoding=encoding,
    )
    derived_metrics = serialize_derived_metrics(payment_features)
    data_conflicts = _conflicts(company_id, directory)
    facts = _facts(company_id, row, states)
    quality = _quality(
        company_id, directory, row, states, payment_features
    )
    hooks = _hooks(quality, row, data_conflicts)
    pipeline_mode = (
        "no_inference" if quality["usability"] == "unusable"
        else "kb_fallback"
    )
    degradation_reasons = (
        ["unusable_input"] if pipeline_mode == "no_inference" else ["kb_miss"]
    )
    input_hash = _canonical_hash({
        "assembler_version": ASSEMBLER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "ttl_hours": ttl_hours,
        "company_id": company_id,
        "as_of_date": snapshot_date,
        "assume_additive_rows": assume_additive_rows,
        "sources": {
            name: item["sha256"]
            for name, item in sorted(payment_features.sources.items())
        },
    })
    profile_id = f"profile-{input_hash[:16]}"
    verified = _evidence_is_verified(
        facts, derived_metrics, row, states, company_id
    )
    if not verified:
        raise ValueError("Внутренняя проверка evidence профиля не пройдена")

    profile = {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "company_id": company_id,
            "group_id": (
                payment_features.company_group_id
                if status == "unambiguous" else None
            ),
            "as_of_date": snapshot_date,
            "profile_id": profile_id,
            "input_hash": input_hash,
            "kb_version": None,
            "prompt_version": None,
            "pipeline_mode": pipeline_mode,
            "degradation_reasons": degradation_reasons,
            "models_used": [],
            "customer_status": _customer_status(row, states),
            "ttl_hours": ttl_hours,
        },
        "input_quality": quality,
        "facts": facts,
        "derived_metrics": derived_metrics,
        "hypotheses": [],
        "sensitivities": [],
        "data_conflicts": data_conflicts,
        "missing_data": _missing_data(
            company_id, states, company_state=status
        ),
        "downstream_hooks": hooks,
        "provenance": {
            "trace_id": f"trace-{input_hash[:16]}",
            "validation": {
                "schema_valid": True,
                "evidence_refs_verified": True,
                "dropped_statements": [],
                "retries": 0,
            },
            "kb_entries_used": [],
            "local_id_map": {},
        },
    }
    validate_contract(profile, "profile")
    return profile


assemble_company_profile = build_company_profile


def build_ml_view(profile: dict) -> dict:
    validate_contract(profile, "profile")
    view = {
        "schema_version": SCHEMA_VERSION,
        "company_id": profile["meta"]["company_id"],
        "profile_id": profile["meta"]["profile_id"],
        "as_of_date": profile["meta"]["as_of_date"],
        "usability": profile["input_quality"]["usability"],
        "weight_semantics": "ordinal_prior_not_probability",
        "hooks": profile["downstream_hooks"],
        "numeric_features": [{
            "derived_id": metric["id"],
            "name": metric["name"],
            "value": metric["value"]
            if type(metric["value"]) in (int, float) else None,
            "coverage": metric["coverage"],
            "data_reliability": metric["data_reliability"],
        } for metric in profile["derived_metrics"]],
    }
    validate_contract(view, "view")
    return view


def build_interpreter_view(profile: dict) -> dict:
    validate_contract(profile, "profile")
    statements = [{
        "id": fact["id"],
        "epistemic_status": "FACT",
        "text": fact["statement"],
        "data_reliability": fact["data_reliability"],
        "overall_confidence": None,
        "uncertainty_sources": [],
        "evidence": fact["evidence"],
    } for fact in profile["facts"]]
    statements.extend({
        "id": metric["id"],
        "epistemic_status": "DERIVED",
        "text": f"{metric['name']} = {metric['value']}",
        "data_reliability": metric["data_reliability"],
        "overall_confidence": None,
        "uncertainty_sources": [],
        "evidence": metric["inputs"],
    } for metric in profile["derived_metrics"])
    statements.extend({
        "id": item["id"],
        "epistemic_status": "HYPOTHESIS",
        "text": item["statement"],
        "data_reliability": item["data_reliability"],
        "overall_confidence": item["overall_confidence"],
        "uncertainty_sources": item["uncertainty_sources"],
        "evidence": item["evidence"],
    } for item in profile["hypotheses"])
    statements.extend({
        "id": item["id"],
        "epistemic_status": "HYPOTHESIS",
        "text": item["transmission_channel"],
        "data_reliability": item["data_reliability"],
        "overall_confidence": item["overall_confidence"],
        "uncertainty_sources": item["uncertainty_sources"],
        "evidence": item["evidence"],
    } for item in profile["sensitivities"])
    return {
        "schema_version": SCHEMA_VERSION,
        "company_id": profile["meta"]["company_id"],
        "profile_id": profile["meta"]["profile_id"],
        "usability": profile["input_quality"]["usability"],
        "statements": statements,
        "constraints": profile["downstream_hooks"]["interpretation_constraints"],
    }


def _ui_evidence(items: list[dict]) -> list[dict]:
    return [{
        "source": item["source"],
        "ref": item["ref"],
        "label": item["ref"],
        "value_text": "null" if item["value"] is None else str(item["value"]),
        "role": item["role"],
    } for item in items]


def _ui_item(
    identifier: str,
    label: str,
    title: str,
    text: str,
    confidence: str | None,
    uncertainty: str | None,
    evidence: list[dict],
) -> dict:
    return {
        "id": identifier,
        "label": label,
        "title": title[:120],
        "text": text[:400],
        "confidence": confidence,
        "uncertainty_note": uncertainty[:300] if uncertainty else None,
        "evidence": _ui_evidence(evidence),
    }


def build_ui_view(profile: dict) -> dict:
    validate_contract(profile, "profile")
    sections = [
        {
            "kind": "facts",
            "items": [_ui_item(
                item["id"], "Факт из данных", item["statement"],
                item["statement"], None,
                "; ".join(item["caveats"]) or None, item["evidence"],
            ) for item in profile["facts"]],
        },
        {
            "kind": "derived",
            "items": [_ui_item(
                item["id"], "Расчёт", item["name"],
                f"{item['name']} = {item['value']}", None,
                "; ".join(item["caveats"]) or None, item["inputs"],
            ) for item in profile["derived_metrics"]],
        },
        {
            "kind": "hypotheses",
            "items": [_ui_item(
                item["id"], "Гипотеза AI", item["statement"],
                item["statement"], item["overall_confidence"],
                "; ".join(item["uncertainty_sources"]), item["evidence"],
            ) for item in profile["hypotheses"]],
        },
        {
            "kind": "sensitivities",
            "items": [_ui_item(
                item["id"], "Гипотеза AI", item["factor"],
                item["transmission_channel"], item["overall_confidence"],
                "; ".join(item["uncertainty_sources"]), item["evidence"],
            ) for item in profile["sensitivities"]],
        },
        {
            "kind": "conflicts",
            "items": [_ui_item(
                item["id"], "Противоречие в данных", item["rule_id"],
                item["description"], None, None, item["fields"],
            ) for item in profile["data_conflicts"]],
        },
        {
            "kind": "missing",
            "items": [_ui_item(
                item["id"], "Нет данных", item["feature"], item["kind"],
                None, item["request_suggestion"], [],
            ) for item in profile["missing_data"]],
        },
    ]
    usability = profile["input_quality"]["usability"]
    return {
        "schema_version": SCHEMA_VERSION,
        "company_id": profile["meta"]["company_id"],
        "profile_id": profile["meta"]["profile_id"],
        "as_of_date": profile["meta"]["as_of_date"],
        "banner": {
            "usability": usability,
            "text": None if usability == "full" else (
                "Профиль построен по неполным или неоднозначным данным."
            ),
        },
        "sections": sections,
    }


def build_profile_views(profile: dict) -> dict[str, dict]:
    return {
        "ml_view": build_ml_view(profile),
        "interpreter_view": build_interpreter_view(profile),
        "ui_view": build_ui_view(profile),
    }


project_profile_views = build_profile_views


def build_profile_bundle(*args: Any, **kwargs: Any) -> dict[str, dict]:
    profile = build_company_profile(*args, **kwargs)
    return {"profile": profile, **build_profile_views(profile)}


__all__ = [
    "assemble_company_profile",
    "build_company_profile",
    "build_interpreter_view",
    "build_ml_view",
    "build_profile_bundle",
    "build_profile_views",
    "build_ui_view",
    "project_profile_views",
]
