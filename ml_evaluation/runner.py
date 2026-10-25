import hashlib
from pathlib import Path
from statistics import mean

from ml_ranking import NewsEvent, rank_news
from ml_ranking.serialization import serialize_ranking

from .baseline import industry_only_rank
from .dataset import (
    SPLITS, analysis_time, canonical_sha256, resolve_judgment,
    validate_dataset,
)
from .evidence import audit_ranked_evidence
from .metrics import evaluate_ranking, validate_options

ROOT = Path(__file__).resolve().parent.parent
ALGORITHMS = ("personalized", "industry_only")
METRICS = ("precision_at_k", "recall_at_k", "ndcg_at_k")


def aggregate_metrics(queries: list[dict], algorithm: str) -> dict:
    result = {"query_count": len(queries)}
    for metric in METRICS:
        values = [query["metrics"][algorithm][metric] for query in queries
                  if query["metrics"][algorithm][metric] is not None]
        result[metric] = {
            "macro_mean": mean(values) if values else None,
            "applicable_query_count": len(values),
        }
    return result


def implementation_manifest() -> dict:
    paths = sorted(
        list((ROOT / "ml_ranking").glob("*.py"))
        + list((ROOT / "ml_evaluation").glob("*.py"))
        + list((ROOT / "schemas").glob("*.json"))
    )
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest() for path in paths
    }


def evaluate_query(
    query: dict, context: dict, event_records: dict, annotations: list,
    k: int, relevant_threshold: int,
) -> dict:
    pool_ids = sorted(query["candidate_event_ids"])
    pool = tuple(NewsEvent.from_dict(event_records[key]["event"])
                 for key in pool_ids)
    predictions = serialize_ranking(rank_news(
        context["ml_view"], pool, company_profile=context["company_profile"],
        analytic_focus=query["analytic_focus"], top_k=k,
        as_of=analysis_time(query["as_of"]),
    ))
    rankings = {
        "personalized": [item["event_id"] for item in predictions],
        "industry_only": industry_only_rank(
            context["ml_view"], pool, top_k=k
        ),
    }
    judgments = {
        key: resolve_judgment(annotations, query["query_id"], key)
        for key in pool_ids
    }
    grades = {key: item["grade"] for key, item in judgments.items()}
    metrics = {
        name: evaluate_ranking(ids, grades, k, relevant_threshold)
        for name, ids in rankings.items()
    }
    examples = {}
    for name, ids in rankings.items():
        eligible = metrics[name]["precision_at_k"] is not None
        examples[name] = {
            "false_positives": [
                key for key in ids if eligible
                and grades[key] < relevant_threshold
            ],
            "false_negatives": [
                key for key in pool_ids if eligible
                and grades[key] >= relevant_threshold and key not in ids
            ],
        }
    missing_evidence = []
    for key, judgment in judgments.items():
        for label in judgment["annotations"]:
            if label["relevance_grade"] in (1, 2) and not (
                label["supporting_evidence_refs"]
            ):
                missing_evidence.append({
                    "event_id": key, "reviewer_id": label["reviewer_id"],
                    "reason": "positive_label_without_evidence",
                })
    for prediction in predictions:
        for problem in audit_ranked_evidence(
            prediction, event_records[prediction["event_id"]]["event"], context
        ):
            missing_evidence.append({
                "event_id": prediction["event_id"],
                "reason": problem, "algorithm": "personalized",
            })
    positions = {
        name: {key: rank for rank, key in enumerate(ids, 1)}
        for name, ids in rankings.items()
    }
    disagreements = [
        {"event_id": key, **{
            name: positions[name].get(key) for name in ALGORITHMS
        }} for key in sorted(set().union(*rankings.values()))
        if positions["personalized"].get(key)
        != positions["industry_only"].get(key)
    ]
    return {
        "query_id": query["query_id"], "company_id": query["company_id"],
        "context_id": query["context_id"],
        "profile_version": context["profile_version"],
        "analytic_focus": query["analytic_focus"], "as_of": query["as_of"],
        "candidate_event_ids": pool_ids,
        "candidate_pool_sha256": canonical_sha256([
            [key, event_records[key]["sha256"]] for key in pool_ids
        ]),
        "judgments": judgments, "metrics": metrics, "rankings": rankings,
        "personalized_explanations": predictions, "examples": examples,
        "ranking_disagreements": disagreements,
        "missing_evidence": missing_evidence,
    }


def evaluate_dataset(
    dataset: dict, *, split: str = "development", k: int = 5,
    relevant_threshold: int = 1,
) -> dict:
    validate_options(k, relevant_threshold)
    if split not in SPLITS:
        raise ValueError("Нужно выбрать development или held_out")
    dataset = validate_dataset(dataset)
    contexts = {item["context_id"]: item for item in dataset["contexts"]}
    events = {item["event"]["event_id"]: item for item in dataset["events"]}
    queries = [evaluate_query(
        query, contexts[query["context_id"]], events, dataset["annotations"],
        k, relevant_threshold,
    ) for query in sorted(
        dataset["queries"], key=lambda item: item["query_id"]
    )
        if query["split"] == split]
    companies = sorted({query["company_id"] for query in queries})
    event_ids = sorted({key for query in queries
                        for key in query["candidate_event_ids"]})
    pairs = {(query["company_id"], key) for query in queries
             for key in query["candidate_event_ids"]}
    eligible = [query for query in queries
                if query["metrics"]["personalized"]["precision_at_k"]
                is not None]
    evaluated_pairs = {(query["company_id"], key) for query in eligible
                       for key in query["candidate_event_ids"]}
    resolutions = [item for query in queries
                   for item in query["judgments"].values()]
    total = len(resolutions)
    resolved = sum(item["grade"] is not None for item in resolutions)
    label_distribution = {
        str(grade): sum(item["grade"] == grade for item in resolutions)
        for grade in (0, 1, 2)
    }
    unresolved = [
        {"query_id": query["query_id"], "event_id": key,
         "reason": item["status"]}
        for query in queries for key, item in query["judgments"].items()
        if item["grade"] is None
    ]
    missing_evidence = [
        {"event_id": key, "reason": "article_content_or_quotes_missing"}
        for key in event_ids if not events[key]["event"].get("content")
        or not events[key]["event"].get("evidence")
    ] + [
        {"query_id": query["query_id"], **item}
        for query in queries for item in query["missing_evidence"]
    ]
    synthetic = dataset["kind"] == "synthetic_demo"
    status = "synthetic_demonstration" if synthetic else (
        "awaiting_independent_annotation" if not eligible
        else "incomplete_annotation" if unresolved else (
            "descriptive_evaluation"
        )
    )
    return {
        "report_version": "ml3a-1", "evaluation_status": status,
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["dataset_version"],
        "dataset_status": dataset["status"], "kind": dataset["kind"],
        "dataset_sha256": canonical_sha256(dataset), "split": split,
        "labeling_provenance": dataset["labeling_provenance"],
        "configuration": {
            "k": k, "relevant_threshold": relevant_threshold,
            "precision_denominator": "k_including_unfilled_positions",
            "recall_denominator": "all_relevant_events_in_labeled_pool",
            "gain": "2**grade-1", "discount": "log2(position+1)",
            "incomplete_query_policy": "all_metrics_not_applicable",
            "no_relevant_policy": "recall_null_ndcg_null_if_idcg_zero",
            "aggregation": "macro_mean_of_applicable_queries",
        },
        "implementation_sha256": implementation_manifest(),
        "counts": {
            "companies": len(companies), "queries": len(queries),
            "unique_events": len(event_ids),
            "underlying_event_groups": len({
                events[key]["event_group_id"] for key in event_ids
            }),
            "company_event_pairs": len(pairs),
            "company_event_context_pairs": total,
            "evaluated_company_event_pairs": len(evaluated_pairs),
            "evaluated_company_event_context_pairs": sum(
                len(query["candidate_event_ids"]) for query in eligible
            ),
        },
        "coverage": {
            "resolved_judgments": resolved, "required_judgments": total,
            "judgment_fraction": resolved / total if total else None,
            "eligible_queries": len(eligible), "total_queries": len(queries),
            "query_fraction": (
                len(eligible) / len(queries) if queries else None
            ),
        },
        "label_distribution": label_distribution,
        "annotation_status_counts": {
            status: sum(label["annotation_status"] == status
                        for query in queries
                        for item in query["judgments"].values()
                        for label in item["annotations"])
            for status in ("pending", "reviewed", "adjudicated")
        },
        "aggregate": {
            name: aggregate_metrics(queries, name) for name in ALGORITHMS
        },
        "per_company": {
            company: {name: aggregate_metrics([
                query for query in queries if query["company_id"] == company
            ], name) for name in ALGORITHMS} for company in companies
        },
        "queries": queries, "unresolved_judgments": unresolved,
        "missing_evidence": missing_evidence,
        "event_manifest": {
            key: {field: events[key][field]
                  for field in ("sha256", "event_group_id", "split")}
            for key in event_ids
        },
        "profile_manifest": {
            key: {field: contexts[key][field] for field in (
                "company_id", "profile_version", "ml_view_sha256",
                "profile_sha256", "provenance",
            )} for key in sorted({query["context_id"] for query in queries})
        },
        "limitations": [
            "Синтетические ожидания проверяют инфраструктуру; "
            "это не качество модели на реальных новостях."
            if synthetic else "Метрики описывают только предоставленный пул.",
            "Размер выборки и зависимые события не позволяют автоматически "
            "утверждать статистическую обобщаемость или превосходство.",
            "Пропущенные, спорные и лишённые доказательств положительные "
            "оценки не заменяются нулями; весь запрос получает N/A.",
            "Проверяется существование ссылок и сохранность цитат; "
            "истинность источника и смысл доказательства проверяет человек.",
            "Группы связанных статей и независимость рецензентов задаются "
            "командой; хеширование не доказывает семантическую независимость.",
            "ML-2A использует ограниченные правила и доверяет классификации "
            "нормализатора; его веса и правила не изменены.",
        ],
    }
