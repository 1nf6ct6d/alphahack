import copy
from datetime import date, datetime

from .models import NewsEvent, RelevanceResult
from .semantics import (
    REGULATORY_TYPES, RELIEF_DIRECTIONS,
    verify_regulatory_event,
)
from .validation import CompanyContext, OKVED_PATTERN, article_support

INDUSTRY_RULES = {
    "regulation_change": (
        "industry_regulation", "operational",
        "Применимое требование может потребовать изменения процессов "
        "и подготовки обязательной отчётности.",
        "Нужно подтвердить применимость требования к деятельности компании.",
    ),
    "tax_change": (
        "tax_fiscal", "financial",
        "Применимый обязательный платёж может изменить финансовые затраты "
        "и планирование расчётов компании.",
        "Нужно подтвердить применимость платежа и условия его расчёта.",
    ),
    "input_cost_change": (
        "commodity_price", "operational",
        "Изменение цены производственного ресурса может повлиять на затраты, "
        "если компания использует этот ресурс.",
        "Использование ресурса, структура затрат "
        "и условия закупок неизвестны.",
    ),
    "demand_change": (
        "industry_demand", "strategic",
        "Изменение спроса в заявленной отрасли может повлиять на заказы, "
        "если компания работает на затронутом рынке.",
        "Фактические рынки сбыта и состав заказчиков нужно уточнить.",
    ),
}


def statement_reference(context: CompanyContext, statement: dict) -> dict:
    source = statement.get("evidence", statement.get("inputs", []))
    return {
        "kind": "company_statement",
        "statement_id": statement["id"],
        "company_id": context.view["company_id"],
        "profile_id": context.view["profile_id"],
        "epistemic_status": statement["epistemic_status"],
        "data_reliability": statement["data_reliability"],
        "inputs": copy.deepcopy(source),
        "caveats": list(statement.get("caveats", [])),
        "verified_in_profile": True,
    }


def match_event(
    context: CompanyContext, event: NewsEvent,
    as_of: datetime | None, urgency_horizon_days: int,
) -> RelevanceResult:
    view = context.view
    references = [{
        "kind": "article_quote", "event_id": event.event_id,
        "evidence_id": evidence.evidence_id, "quote": evidence.quote,
        "supports": list(evidence.supports), "source_id": event.source_id,
        "source_url": event.source_url,
    } for evidence in event.evidence]
    conditions = []
    limitations = [
        "Размер финансового эффекта и вероятность не оцениваются.",
        "Цитаты проверены по переданному тексту; подлинность источника "
        "и корректность внешней классификации отдельно не подтверждаются.",
        "Приоры и ключевые слова не использованы как доказательства.",
    ]
    if event.synthetic:
        limitations.append(
            "Новость синтетическая и используется для проверки правил."
        )
    if event.published_at is None:
        limitations.append(
            "Дата публикации неизвестна; преимущества по свежести нет."
        )
    relevance = "none"
    explanation = "Нет поддержанного механизма связи события с компанией."
    mechanism = None
    specificity = "none"
    domain = None
    basis = None
    mandatory_scope = False
    regulatory = (
        verify_regulatory_event(event)
        if event.event_type in REGULATORY_TYPES else None
    )
    supported = (
        view["usability"] != "unusable"
        and bool(event.content and event.content.strip())
        and article_support(event, "event_type")
        and article_support(event, "factor_classes")
    )
    if not supported:
        explanation = (
            "Недостаточно текста и подтверждённых полей события "
            "либо данные компании непригодны для сопоставления."
        )
    elif event.event_type in INDUSTRY_RULES:
        factor, domain, mechanism_text, condition = (
            INDUSTRY_RULES[event.event_type]
        )
        codes = sorted(
            code for code in view["hooks"]["matching_profile"]["okved_codes"]
            if OKVED_PATTERN.fullmatch(code)
            and any(code.startswith(scope)
                    and article_support(event, "okved_codes", scope)
                    for scope in event.okved_codes)
        )
        if factor in event.factor_classes and codes:
            code = codes[0]
            basis = context.industry_fact(code)
            mechanism = mechanism_text
            relevance = "conditional"
            specificity = "sector_condition"
            conditions.append(condition)
            explanation = (
                f"В MLView указан ОКВЭД {code}; событие содержит "
                "явную отраслевую область и подходящий канал воздействия."
            )
            if basis is not None:
                references.append(statement_reference(context, basis))
                limitations.extend(basis["caveats"])
            else:
                references.append({
                    "kind": "ml_view_field",
                    "path": "hooks.matching_profile.okved_codes",
                    "value": code, "verified_in_profile": False,
                })
                conditions.append(
                    "ОКВЭД и применимость к конкретной компании "
                    "нужно подтвердить по надёжному профилю."
                )
            if regulatory is not None:
                regulatory = verify_regulatory_event(event, code)
                limitations.extend(regulatory.limitations)
                specificity = (
                    "industry_obligation" if regulatory.source_verified and (
                        regulatory.obligation_verified
                        or regulatory.direction in RELIEF_DIRECTIONS
                    ) else "sector_condition"
                )
                if regulatory.direction in RELIEF_DIRECTIONS:
                    conditions[0] = (
                        "Нужно подтвердить применимость снижения нагрузки "
                        "к деятельности компании."
                    )
                    mechanism = (
                        "Освобождение от указанной обязанности может "
                        "снизить регуляторную нагрузку и затраты "
                        "на её исполнение."
                        if regulatory.direction == "exemption" else
                        "Отмена указанной обязанности может снизить "
                        "регуляторную нагрузку и затраты на её исполнение."
                    )
                elif regulatory.direction == "tightened":
                    mechanism = (
                        "Усиление требований может потребовать изменения "
                        "процессов и дополнительных действий по соблюдению."
                    )
                elif regulatory.direction == "unknown":
                    mechanism = (
                        "Отраслевое регулирование может затронуть процессы "
                        "или затраты; направление изменения "
                        "и применимость нужно проверить."
                    )
                    conditions.append(
                        "Нельзя утверждать введение обязанности, её отмену "
                        "или освобождение до проверки смысла фрагментов."
                    )
                mandatory_scope = (
                    regulatory.obligation_verified
                    and regulatory.scope_verified
                    and not regulatory.metadata_conflict
                )
            source_confirmed = regulatory is not None and (
                regulatory.source_verified and regulatory.scope_verified
                and (regulatory.direction in RELIEF_DIRECTIONS
                     or regulatory.obligation_verified)
            )
            if source_confirmed and basis is not None and not event.geography:
                relevance = "grounded"
                conditions = [
                    "Фактическое снижение затрат и индивидуальные "
                    "обстоятельства не установлены."
                    if regulatory.direction in RELIEF_DIRECTIONS else
                    "Величина эффекта, фактическое исполнение требования "
                    "и индивидуальные обстоятельства не установлены."
                ]
                direction_text = {
                    "introduced": "введение обязанности",
                    "required": "обязательное требование",
                    "tightened": "усиление требований",
                    "exemption": "освобождение от обязанности",
                    "removed": "отмену обязанности",
                }[regulatory.direction]
                explanation = (
                    f"ОКВЭД {code} подтверждён FACT профиля; связный "
                    f"фрагмент статьи подтверждает {direction_text} "
                    "для всей указанной отраслевой области."
                )
            elif regulatory is not None:
                conditions.append(
                    "Направление нормы, охват отрасли и данные компании "
                    "не подтверждены одновременно."
                )
            if regulatory is not None and regulatory.metadata_conflict:
                explanation += (
                    " Нормализованные флаги противоречат источнику; "
                    "они не подтверждают обязательность или срочность."
                )
                conditions.append(
                    "Нужно проверить и исправить противоречивую "
                    "нормализацию; исходные флаги сохранены."
                )
    elif event.event_type == "monetary_policy_change":
        basis = context.loan_fact()
        if "monetary_policy" in event.factor_classes and basis is not None:
            relevance = "conditional"
            specificity = "company_condition"
            domain = "financial"
            references.append(statement_reference(context, basis))
            limitations.extend(basis["caveats"])
            explanation = (
                "В FACT профиля есть банковский кредитный продукт; "
                "событие касается условий денежно-кредитной политики."
            )
            mechanism = (
                "Изменение ставки может повлиять на стоимость нового "
                "или пересматриваемого финансирования."
            )
            conditions.extend([
                "Условия действующих договоров, тип ставки и планы "
                "рефинансирования неизвестны.",
                "Продуктовый флаг не доказывает изменение процентов "
                "по действующему договору или потребность в новом кредите.",
            ])
    elif event.event_type == "counterparty_disruption":
        if set(event.factor_classes) & {"counterparty_credit", "logistics"}:
            for identifier in sorted(event.entities):
                if not article_support(event, "entities", identifier):
                    continue
                link = context.payment_link(identifier)
                if link is None:
                    continue
                relevance = "conditional"
                specificity = "observed_payment_link"
                domain = "operational"
                references.append(statement_reference(context, link))
                limitations.extend(link["caveats"])
                explanation = (
                    "DERIVED профиля содержит платёжную связь "
                    f"с упомянутым участником {identifier}."
                )
                mechanism = (
                    "Нарушение деятельности участника может затронуть "
                    "связанные расчёты, если связь сохраняется и существенна."
                )
                conditions.extend([
                    "Актуальность, назначение "
                    "и значимость расчётов неизвестны.",
                    "Платёжная связь не доказывает отношения поставки.",
                ])
                break
    if event.geography and relevance != "none":
        conditions.append(
            "Географическая применимость не подтверждена; "
            "место регистрации не доказывает операционное присутствие."
        )
    urgent = False
    days = None
    verified_deadline = (
        event.deadline is not None
        and (
            regulatory.deadline_verified and not regulatory.metadata_conflict
            if regulatory is not None else
            article_support(event, "deadline", event.deadline)
        )
    )
    if verified_deadline and as_of is not None:
        days = (date.fromisoformat(event.deadline) - as_of.date()).days
        urgent = (
            relevance == "grounded" and mandatory_scope
            and basis["data_reliability"] == "high"
            and 0 <= days <= urgency_horizon_days
        )
    elif event.deadline is not None and relevance != "none":
        if regulatory is not None:
            limitations.append(
                "Момент анализа неизвестен; близость срока не определена."
                if as_of is None else
                "Переданная дата не использована для срочности: проверка "
                "связи с обязанностью, области применения или "
                "согласованности флагов не пройдена."
            )
        else:
            limitations.append(
                "Близость срока не подтверждена: нет момента анализа "
                "либо явной даты в цитате."
            )
    priority = "high" if urgent else (
        "normal" if relevance == "grounded" else "low"
    )
    if relevance == "none":
        basis = None
        mechanism = None
        conditions = []
        specificity = "none"
        domain = None
    return RelevanceResult(
        event, view["company_id"], view["profile_id"], relevance, priority,
        explanation, mechanism, tuple(conditions), tuple(references),
        tuple(limitations),
        tuple(copy.deepcopy(view["hooks"]["interpretation_constraints"])),
        {
            "mechanism_specificity": specificity,
            "impact_domain": domain,
            "company_basis_verified": basis is not None,
            "urgent_deadline_verified": urgent,
            "verified_deadline": event.deadline if verified_deadline else None,
            "days_until_deadline": days,
            "publication_date_known": event.published_at is not None,
            "weight_semantics": view["weight_semantics"],
            "priors_used_as_evidence": False,
            **({"regulatory_semantics": {
                **regulatory.to_dict(),
                "normalized_claims": {
                    "mandatory": event.mandatory,
                    "applies_to_all_in_scope": event.applies_to_all_in_scope,
                    "deadline": event.deadline,
                },
            }} if regulatory is not None else {}),
        },
    )
