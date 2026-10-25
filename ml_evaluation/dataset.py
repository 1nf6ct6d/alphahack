import copy
import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ml_ranking import NewsEvent
from ml_ranking.validation import (
    company_context, parse_timestamp, validate_event,
)

from .evidence import evidence_reference_error

SPLITS = {"development", "held_out"}
FOCUSES = {None, "CFO", "CEO"}
STATUSES = {"pending", "reviewed", "adjudicated"}


def canonical_sha256(value) -> str:
    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def analysis_time(value: str) -> datetime:
    parse_timestamp(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def freeze_context(
    context_id: str, ml_view: dict, company_profile: dict | None,
    provenance: str,
) -> dict:
    company_context(ml_view, company_profile)
    return copy.deepcopy({
        "context_id": context_id, "company_id": ml_view["company_id"],
        "profile_version": ml_view["profile_id"], "ml_view": ml_view,
        "company_profile": company_profile,
        "ml_view_sha256": canonical_sha256(ml_view),
        "profile_sha256": (
            canonical_sha256(company_profile)
            if company_profile is not None else None
        ),
        "provenance": provenance,
    })


def freeze_event(event: NewsEvent, event_group_id: str, split: str) -> dict:
    validate_event(event)
    payload = asdict(event)
    return {
        "event": payload, "sha256": canonical_sha256(payload),
        "event_group_id": event_group_id, "split": split,
    }


def require_fields(value, names: str, location: str) -> None:
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError(f"Неверный набор полей: {location}")


def require_text(value, location: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Нужна непустая строка: {location}")


def require_list(value, location: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"Нужен массив: {location}")


def validate_contexts(records: list) -> dict[str, dict]:
    contexts = {}
    versions = {}
    for record in records:
        require_fields(
            record, "context_id company_id profile_version ml_view "
            "company_profile ml_view_sha256 profile_sha256 provenance",
            "contexts",
        )
        for field in ("context_id", "company_id", "profile_version",
                      "provenance"):
            require_text(record[field], field)
        key = record["context_id"]
        if key in contexts:
            raise ValueError("Повторный context_id")
        company_context(record["ml_view"], record["company_profile"])
        expected = freeze_context(
            key, record["ml_view"], record["company_profile"],
            record["provenance"],
        )
        if record != expected:
            raise ValueError("Контекст, версия профиля или хеш изменены")
        version = (record["company_id"], record["profile_version"])
        hashes = (record["ml_view_sha256"], record["profile_sha256"])
        if version in versions and versions[version] != hashes:
            raise ValueError("Одна версия профиля имеет разные снимки")
        versions[version] = hashes
        contexts[key] = record
    return contexts


def validate_events(records: list, kind: str) -> dict[str, dict]:
    events = {}
    groups = {}
    contents = {}
    for record in records:
        require_fields(record, "event sha256 event_group_id split", "events")
        require_text(record["event_group_id"], "event_group_id")
        if record["split"] not in SPLITS:
            raise ValueError("Неизвестная часть выборки")
        event = NewsEvent.from_dict(record["event"])
        validate_event(event)
        if (kind == "synthetic_demo") != event.synthetic:
            raise ValueError("Тип набора не соответствует synthetic новости")
        if event.event_id in events:
            raise ValueError("Повторный event_id в наборе")
        if record["sha256"] != canonical_sha256(record["event"]):
            raise ValueError("Хеш новости не совпадает со снимком")
        group = record["event_group_id"]
        split = record["split"]
        if group in groups and groups[group] != split:
            raise ValueError("Связанные статьи попали в разные части выборки")
        groups[group] = split
        if event.content and event.content.strip():
            content_hash = canonical_sha256(event.content.strip())
            if content_hash in contents and contents[content_hash] != (
                group, split
            ):
                raise ValueError("Одинаковый текст имеет разные группы/части")
            contents[content_hash] = (group, split)
        events[event.event_id] = record
    return events


def validate_queries(
    records: list, contexts: dict, events: dict,
) -> dict[str, dict]:
    queries = {}
    identities = set()
    pairs = set()
    for query in records:
        require_fields(
            query, "query_id context_id company_id analytic_focus split "
            "as_of candidate_event_ids", "queries",
        )
        for field in ("query_id", "context_id", "company_id"):
            require_text(query[field], field)
        if query["query_id"] in queries:
            raise ValueError("Повторный query_id")
        if query["context_id"] not in contexts:
            raise ValueError("Контекст запроса не найден")
        context = contexts[query["context_id"]]
        if query["company_id"] != context["company_id"]:
            raise ValueError("company_id запроса не совпадает с контекстом")
        if query["analytic_focus"] not in FOCUSES:
            raise ValueError("Неизвестный аналитический фокус")
        if query["split"] not in SPLITS:
            raise ValueError("Неизвестная часть выборки запроса")
        as_of = analysis_time(query["as_of"])
        if context["ml_view"]["as_of_date"] > as_of.date().isoformat():
            raise ValueError("Версия профиля создана после момента оценки")
        identity = (
            query["company_id"], context["profile_version"],
            query["analytic_focus"], query["split"],
        )
        if identity in identities:
            raise ValueError("Повторный контекст оценки компании")
        identities.add(identity)
        require_list(query["candidate_event_ids"], "candidate_event_ids")
        for identifier in query["candidate_event_ids"]:
            require_text(identifier, "candidate_event_id")
            if identifier not in events:
                raise ValueError("Кандидат отсутствует в замороженном наборе")
            pair = identity[:3] + (identifier,)
            if pair in pairs:
                raise ValueError("Повторная пара компания-событие-фокус")
            pairs.add(pair)
            record = events[identifier]
            if record["split"] != query["split"]:
                raise ValueError("Пул запроса пересекает части выборки")
            published = record["event"].get("published_at")
            if published and parse_timestamp(published) > as_of:
                raise ValueError("Новость опубликована после момента оценки")
        queries[query["query_id"]] = query
    return queries


def validate_annotations(
    records: list, queries: dict, contexts: dict, events: dict,
) -> None:
    reviewers = set()
    for label in records:
        require_fields(
            label, "query_id company_id event_id analytic_focus "
            "relevance_grade relevance_reason impact_mechanism "
            "supporting_evidence_refs reviewer_id annotation_status "
            "adjudicates", "annotations",
        )
        for field in ("query_id", "company_id", "event_id", "reviewer_id"):
            require_text(label[field], field)
        query = queries.get(label["query_id"])
        if query is None or label["event_id"] not in (
            query["candidate_event_ids"]
        ):
            raise ValueError("Разметка вне пула запроса")
        if any(label[field] != query[field]
               for field in ("company_id", "analytic_focus")):
            raise ValueError("Компания или фокус разметки не совпадают")
        identity = (label["query_id"], label["event_id"], label["reviewer_id"])
        if identity in reviewers:
            raise ValueError("Повторная оценка пары одним рецензентом")
        reviewers.add(identity)
        status = label["annotation_status"]
        if status not in STATUSES:
            raise ValueError("Неизвестный статус разметки")
        grade = label["relevance_grade"]
        if grade is not None and (
            type(grade) is not int or grade not in (0, 1, 2)
        ):
            raise ValueError("Некорректная оценка релевантности")
        for field in ("relevance_reason", "impact_mechanism"):
            if label[field] is not None:
                require_text(label[field], field)
        if status != "pending":
            if grade is None:
                raise ValueError("Проверенная разметка должна иметь оценку")
            require_text(label["relevance_reason"], "relevance_reason")
            if grade > 0:
                require_text(label["impact_mechanism"], "impact_mechanism")
        require_list(label["supporting_evidence_refs"], "evidence_refs")
        for reference in label["supporting_evidence_refs"]:
            if not isinstance(reference, dict):
                raise ValueError("Ссылка должна быть объектом")
            error = evidence_reference_error(
                reference, events[label["event_id"]]["event"],
                contexts[query["context_id"]],
            )
            if error:
                raise ValueError(error)
        require_list(label["adjudicates"], "adjudicates")
        if len(set(label["adjudicates"])) != len(label["adjudicates"]):
            raise ValueError("Повторные рецензенты в adjudicates")
        if status != "adjudicated" and label["adjudicates"]:
            raise ValueError("Только арбитр указывает adjudicates")
    for query in queries.values():
        for event_id in query["candidate_event_ids"]:
            resolve_judgment(records, query["query_id"], event_id)


def resolve_judgment(
    annotations: list[dict], query_id: str, event_id: str,
) -> dict:
    records = sorted(
        (label for label in annotations if label["query_id"] == query_id
         and label["event_id"] == event_id),
        key=lambda item: item["reviewer_id"],
    )
    reviewed = [item for item in records
                if item["annotation_status"] == "reviewed"]
    adjudicated = [item for item in records
                   if item["annotation_status"] == "adjudicated"]
    if len(adjudicated) > 1:
        raise ValueError("Несколько арбитражных решений для одной пары")
    result = {
        "grade": None, "status": "missing_judgment",
        "reviewer_ids": [item["reviewer_id"] for item in records],
        "annotations": copy.deepcopy(records),
    }
    if adjudicated:
        selected = adjudicated[0]
        expected = {item["reviewer_id"] for item in reviewed}
        if len(expected) < 2 or set(selected["adjudicates"]) != expected:
            raise ValueError("Арбитр должен явно рассмотреть все две+ оценки")
    elif not reviewed:
        return result
    elif len({item["relevance_grade"] for item in reviewed}) != 1:
        result["status"] = "reviewer_conflict"
        return result
    else:
        selected = reviewed[0]
    if selected["relevance_grade"] > 0 and not (
        selected["supporting_evidence_refs"]
    ):
        result["status"] = "missing_evidence"
        return result
    result.update({
        "grade": selected["relevance_grade"],
        "status": selected["annotation_status"],
        "selected_reviewer_id": selected["reviewer_id"],
    })
    return result


def validate_dataset(value: dict) -> dict:
    canonical_sha256(value)
    require_fields(
        value, "format_version dataset_id dataset_version kind status "
        "labeling_provenance contexts events queries annotations", "dataset",
    )
    if value["format_version"] != "ml3a-1":
        raise ValueError("Неизвестная версия формата оценки")
    for field in ("dataset_id", "dataset_version"):
        require_text(value[field], field)
    kind = value["kind"]
    if kind not in {"synthetic_demo", "real"}:
        raise ValueError("Неизвестный тип набора")
    allowed = {"synthetic_demonstration"} if kind == "synthetic_demo" else {
        "awaiting_independent_annotation", "collecting_annotation", "annotated"
    }
    if value["status"] not in allowed:
        raise ValueError("Статус набора не соответствует типу данных")
    provenance = value["labeling_provenance"]
    require_fields(
        provenance, "method independent_of_ranker description", "provenance"
    )
    method = "synthetic_test_definition" if kind == "synthetic_demo" else (
        "independent_human"
    )
    if provenance["method"] != method or (
        provenance["independent_of_ranker"] is not True
    ):
        raise ValueError("Нужна независимая ручная разметка")
    require_text(provenance["description"], "description")
    for field in ("contexts", "events", "queries", "annotations"):
        require_list(value[field], field)
    contexts = validate_contexts(value["contexts"])
    events = validate_events(value["events"], kind)
    queries = validate_queries(value["queries"], contexts, events)
    validate_annotations(value["annotations"], queries, contexts, events)
    return copy.deepcopy(value)


def load_dataset(path: str | Path) -> dict:
    return validate_dataset(json.loads(Path(path).read_text(encoding="utf-8")))
