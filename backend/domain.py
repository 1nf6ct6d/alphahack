"""Pure domain helpers shared by the API and command-line workflows."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_context_id(
    *,
    company_id: str,
    analytic_focus: str,
    access_scope: Iterable[str],
    profile_id: str,
    news_snapshot_hash: str,
    prompt_version: str,
    model_id: str,
    rules_version: str,
    schema_version: str,
) -> str:
    payload = {
        "company_id": company_id,
        "analytic_focus": analytic_focus,
        "access_scope": sorted(set(access_scope)),
        "profile_id": profile_id,
        "news_snapshot_hash": news_snapshot_hash,
        "prompt_version": prompt_version,
        "model_id": model_id,
        "rules_version": rules_version,
        "schema_version": schema_version,
    }
    return f"ctx-{sha256_json(payload)[:24]}"


def evidence_identifier(item: dict) -> str:
    if item.get("kind") == "article_quote" and item.get("evidence_id"):
        return str(item["evidence_id"])
    if item.get("kind") == "company_statement" and item.get("statement_id"):
        return str(item["statement_id"])
    return f"E-{sha256_json(item)[:12]}"


def candidate_from_ranking(
    ranking: dict, *, insight_id: str | None = None
) -> dict:
    evidence_ids = [evidence_identifier(item) for item in ranking["evidence"]]
    source = {
        "source_id": ranking["source_id"],
        "url": ranking["source_url"],
    }
    conditions = list(ranking.get("conditions", []))
    return {
        "event_id": ranking["event_id"],
        "title": ranking["title"],
        "published_at": ranking.get("published_at"),
        "relevance": ranking["relevance"],
        "relevance_reason": {
            "text": ranking["relevance_explanation"],
            "evidence_ids": evidence_ids,
            "conditions": conditions,
        },
        "priority": ranking["priority"],
        "priority_reason": {
            "text": _priority_text(ranking),
            "evidence_ids": evidence_ids,
            "conditions": conditions,
        },
        "source_ids": [ranking["source_id"]],
        "sources": [source],
        "evidence": [
            {"evidence_id": evidence_identifier(item), **item}
            for item in ranking["evidence"]
        ],
        "conditions": conditions,
        "limitations": list(ranking.get("limitations", [])),
        "impact_mechanism": ranking.get("impact_mechanism"),
        "ranking_factors": ranking.get("ranking_factors", {}),
        "insight_id": insight_id,
    }


def _priority_text(ranking: dict) -> str:
    priority = ranking.get("priority", "low")
    factors = ranking.get("ranking_factors", {})
    if factors.get("urgent_deadline_verified"):
        return (
            f"Приоритет {priority}: подтверждён близкий срок "
            f"{factors.get('verified_deadline')}."
        )
    return (
        f"Приоритет {priority}: определён категорией связи и конкретностью "
        "подтверждённого механизма, без числовой вероятности."
    )


MIN_BANK_SHARED_TOKENS = 2
MIN_BANK_SCORE = 0.25


def score_bank_fragments(
    query: str,
    fragments: Iterable[dict],
    *,
    limit: int = 3,
    min_shared_tokens: int = MIN_BANK_SHARED_TOKENS,
    min_score: float = MIN_BANK_SCORE,
) -> list[dict]:
    """Rank verified OCR fragments with deterministic lexical overlap.

    A fragment is returned only when it shares at least ``min_shared_tokens``
    query terms and covers at least ``min_score`` of the query; a single common
    word is not treated as evidence of relevance.  The thresholds are
    heuristics, not a semantic relevance judgement.
    """

    tokens = _tokens(query)
    ranked = []
    for fragment in fragments:
        if not fragment.get("verified"):
            continue
        text_tokens = _tokens(fragment.get("text", ""))
        overlap = tokens & text_tokens
        if len(overlap) < min_shared_tokens:
            continue
        score = len(overlap) / max(1, len(tokens))
        if score < min_score:
            continue
        ranked.append({**fragment, "score": round(score, 6)})
    ranked.sort(
        key=lambda item: (
            -item["score"],
            str(item.get("source_id", "")),
            int(item.get("page_number", 0)),
        )
    )
    return ranked[:limit]


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[0-9A-Za-zА-Яа-яЁё]{3,}", text.lower())
        if token not in {"для", "или", "как", "что", "при", "это", "the"}
    }


def build_deterministic_insight(
    *,
    insight_id: str,
    version: int,
    company_id: str,
    analytic_focus: str,
    context_id: str,
    candidate: dict,
    event: dict,
    bank_corpus_available: bool,
    bank_hits: list[dict],
    generated_at: str | None = None,
) -> dict:
    """Create a schema-shaped, evidence-bound fallback without an LLM."""

    generated_at = generated_at or utc_now()
    evidence = _insight_evidence(candidate, bank_hits)
    evidence_ids = [item["evidence_id"] for item in evidence]
    conditions = list(candidate.get("conditions", []))
    limitations = list(candidate.get("limitations", []))
    status = "ready" if candidate.get("relevance") == "grounded" else "limited"
    if not evidence_ids:
        status = "unavailable"
    limitation_objects = []
    if status == "limited":
        limitation_objects.append({
            "code": "CONDITIONAL_MECHANISM",
            "message": (
                "Механизм зависит от неизвестных условий; вывод следует "
                "использовать как предмет проверки."
            ),
            "affected_fields": ["ai_interpretation", "next_steps"],
        })
    if status == "unavailable":
        limitation_objects.append({
            "code": "NO_VERIFIABLE_EVIDENCE",
            "message": "Недостаточно проверяемых оснований для разбора.",
            "affected_fields": ["ai_interpretation"],
        })

    bank_fields = _bank_position(bank_corpus_available, bank_hits)
    if bank_fields["bank_position"] and bank_hits:
        bank_evidence_ids = [
            item["evidence_id"]
            for item in evidence
            if item.get("source_type") == "bank_report"
        ]
        bank_fields["bank_position"]["evidence_ids"] = bank_evidence_ids

    summary = candidate.get("impact_mechanism") or candidate["relevance_reason"][
        "text"
    ]
    next_step = _role_next_step(analytic_focus, conditions)
    claim_evidence = evidence_ids[:20]
    company_trigger = {
        "text": candidate["relevance_reason"]["text"],
        "evidence_ids": claim_evidence,
        "conditions": conditions,
    }
    sources = _sources(event, bank_hits)
    trace_steps = [
        {
            "step": "load_ranked_candidate",
            "status": "ok",
            "details": {"event_id": event["event_id"]},
        },
        {
            "step": "search_bank_reports",
            "status": (
                "ok" if bank_corpus_available else "unavailable"
            ),
            "details": {"matches": len(bank_hits)},
        },
        {
            "step": "compose_evidence_bound_insight",
            "status": "ok" if status != "unavailable" else "limited",
            "details": {"generator": "deterministic"},
        },
    ]
    return {
        "schema_version": "2.2.0",
        "insight_id": insight_id,
        "insight_version": version,
        "company_id": company_id,
        "analytic_focus": analytic_focus,
        "context_id": context_id,
        "mode": "deterministic_evidence_bound",
        "status": status,
        "generated_at": generated_at,
        "title": event["title"],
        "event": {
            "event_id": event["event_id"],
            "title": event["title"],
            "published_at": event.get("published_at"),
            "source_id": event["source_id"],
            "source_url": event["source_url"],
        },
        "priority": candidate["priority"],
        "priority_reason": candidate["priority_reason"]["text"],
        "company_triggers": [company_trigger],
        **bank_fields,
        "ai_interpretation": {
            "summary": {
                "text": summary,
                "evidence_ids": claim_evidence,
                "conditions": conditions,
            },
            "risks": [],
            "opportunities": [],
        },
        "counterparty_impact": (
            {
                "text": candidate.get("impact_mechanism"),
                "evidence_ids": claim_evidence,
                "conditions": conditions,
            }
            if candidate.get("ranking_factors", {}).get(
                "mechanism_specificity"
            )
            == "counterparty_payment"
            else None
        ),
        "unknowns_and_conditions": conditions,
        "next_steps": [{
            "text": next_step,
            "evidence_ids": claim_evidence,
            "conditions": conditions,
        }],
        "sources": sources,
        "evidence": evidence,
        "limitations": limitation_objects,
        "interpretation_limits": limitations,
        "agent_trace": {
            "run_id": f"run-{sha256_json([insight_id, version])[:16]}",
            "steps": trace_steps,
            "reasoning_exposed": False,
        },
        "error": None,
    }


def _insight_evidence(candidate: dict, bank_hits: list[dict]) -> list[dict]:
    output = []
    seen = set()
    for item in candidate.get("evidence", []):
        evidence_id = str(item.get("evidence_id") or evidence_identifier(item))
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        output.append({
            "evidence_id": evidence_id,
            "source_type": (
                "news" if item.get("kind") == "article_quote" else "profile"
            ),
            "source_id": item.get("source_id") or item.get("profile_id"),
            "locator": item.get("statement_id") or item.get("evidence_id"),
            "observation": item.get("quote") or item.get("kind"),
            "raw": item,
        })
    for hit in bank_hits:
        evidence_id = (
            f"BANK-{sha256_json([hit.get('source_id'), hit.get('page_number'), hit.get('text')])[:12]}"
        )
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        output.append({
            "evidence_id": evidence_id,
            "source_type": "bank_report",
            "source_id": hit.get("source_id"),
            "locator": f"page:{hit.get('page_number')}",
            "observation": hit.get("text"),
            "ocr_quality": hit.get("ocr_quality"),
        })
    return output


def _bank_position(available: bool, hits: list[dict]) -> dict:
    if not available:
        return {
            "bank_retrieval_status": "unavailable",
            "bank_analytics_mode": None,
            "bank_position": None,
        }
    if not hits:
        return {
            "bank_retrieval_status": "completed",
            "bank_analytics_mode": "no_relevant_report",
            "bank_position": {
                "status": "not_found",
                "text": None,
                "evidence_ids": [],
                "reason": "Поиск завершён, релевантный проверенный фрагмент не найден.",
            },
        }
    best = hits[0]
    return {
        "bank_retrieval_status": "completed",
        "bank_analytics_mode": "macro_background",
        "bank_position": {
            "status": "found",
            "text": best["text"],
            "evidence_ids": [],
            "reason": "Найден проверенный фрагмент банковского материала.",
        },
    }


def _sources(event: dict, bank_hits: list[dict]) -> list[dict]:
    result = [{
        "source_id": event["source_id"],
        "type": "news",
        "title": event["title"],
        "url": event["source_url"],
        "date": event.get("published_at"),
    }]
    seen = {event["source_id"]}
    for hit in bank_hits:
        source_id = str(hit["source_id"])
        if source_id in seen:
            continue
        seen.add(source_id)
        result.append({
            "source_id": source_id,
            "type": "bank_report",
            "title": hit.get("title"),
            "url": None,
            "date": hit.get("report_date"),
        })
    return result


def _role_next_step(focus: str, conditions: list[str]) -> str:
    suffix = f" Ключевое условие: {conditions[0]}" if conditions else ""
    if focus == "CFO":
        return (
            "Проверить финансовые предпосылки, договорные условия и "
            "сценарии денежных потоков, связанные с событием." + suffix
        )
    return (
        "Обсудить с ответственным руководителем влияние на непрерывность "
        "работы и приоритеты компании." + suffix
    )


def answer_question(insight: dict, question: str) -> dict:
    """Answer the three accepted demo intents without free-form invention."""

    lowered = question.casefold()
    evidence_ids = [item["evidence_id"] for item in insight.get("evidence", [])]
    sources = insight.get("sources", [])
    conditions = insight.get("unknowns_and_conditions", [])
    if any(word in lowered for word in ("признак", "основан", "доказ")):
        text = (
            "В разборе использованы основания, перечисленные в evidence_ids. "
            "Они ведут к цитатам новости и подтверждённым полям профиля."
        )
    elif any(word in lowered for word in ("услов", "точно", "риск")):
        text = (
            "Влияние условно. " + (
                " ".join(conditions)
                if conditions
                else "Дополнительных условий в сохранённом разборе нет."
            )
        )
    elif any(word in lowered for word in ("уточ", "провер", "дальше")):
        steps = insight.get("next_steps", [])
        text = " ".join(step["text"] for step in steps) or (
            "Следующий проверяемый шаг не сформирован."
        )
    else:
        text = (
            "Детерминированный режим отвечает только по сохранённым "
            "основаниям. Уточните, какие признаки использованы, при каких "
            "условиях возникает влияние или что проверить дальше."
        )
    return {
        "answer": text,
        "evidence_ids": evidence_ids,
        "sources": sources,
        "unknowns_and_conditions": conditions,
    }

