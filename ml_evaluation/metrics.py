from math import log2
from typing import Mapping, Sequence


def validate_options(k: int, relevant_threshold: int) -> None:
    if type(k) is not int or k < 1:
        raise ValueError("k должен быть положительным целым числом")
    if type(relevant_threshold) is not int or relevant_threshold not in (1, 2):
        raise ValueError("Порог релевантности должен быть 1 или 2")


def validate_grade(grade: int) -> None:
    if type(grade) is not int or grade not in (0, 1, 2):
        raise ValueError("Оценка релевантности должна быть 0, 1 или 2")


def dcg(grades: Sequence[int]) -> float:
    for grade in grades:
        validate_grade(grade)
    return sum(
        (2 ** grade - 1) / log2(position + 2)
        for position, grade in enumerate(grades)
    )


def ideal_ranking(judgments: Mapping[str, int]) -> list[str]:
    for grade in judgments.values():
        validate_grade(grade)
    return sorted(judgments, key=lambda key: (-judgments[key], key))


def evaluate_ranking(
    ranking: Sequence[str], judgments: Mapping[str, int | None],
    k: int = 5, relevant_threshold: int = 1,
) -> dict:
    validate_options(k, relevant_threshold)
    if len(ranking) != len(set(ranking)):
        raise ValueError("Повторные события в ранжировании")
    if any(event_id not in judgments for event_id in ranking):
        raise ValueError("Результат вне размечаемого пула кандидатов")
    for grade in judgments.values():
        if grade is not None:
            validate_grade(grade)
    selected = list(ranking[:k])
    result = {
        "precision_at_k": None, "recall_at_k": None,
        "ndcg_at_k": None, "dcg_at_k": None, "idcg_at_k": None,
        "candidate_count": len(judgments),
        "judged_count": sum(value is not None for value in judgments.values()),
        "returned_count": len(selected), "true_positives": None,
        "relevant_count": None, "not_applicable_reason": None,
    }
    if not judgments:
        result["not_applicable_reason"] = "empty_candidate_pool"
        return result
    if any(value is None for value in judgments.values()):
        result["not_applicable_reason"] = "incomplete_judgments"
        return result
    relevant = {
        key for key, grade in judgments.items() if grade >= relevant_threshold
    }
    positives = len(set(selected) & relevant)
    actual_dcg = dcg([judgments[key] for key in selected])
    ideal_dcg = dcg([
        judgments[key] for key in ideal_ranking(judgments)[:k]
    ])
    result.update({
        "precision_at_k": positives / k,
        "recall_at_k": positives / len(relevant) if relevant else None,
        "ndcg_at_k": actual_dcg / ideal_dcg if ideal_dcg else None,
        "dcg_at_k": actual_dcg, "idcg_at_k": ideal_dcg,
        "true_positives": positives, "relevant_count": len(relevant),
        "not_applicable_reason": (
            "no_binary_relevant_events" if not relevant else None
        ),
    })
    return result


def precision_at_k(
    ranking: Sequence[str], judgments: Mapping[str, int | None],
    k: int = 5, relevant_threshold: int = 1,
) -> float | None:
    return evaluate_ranking(
        ranking, judgments, k, relevant_threshold
    )["precision_at_k"]


def recall_at_k(
    ranking: Sequence[str], judgments: Mapping[str, int | None],
    k: int = 5, relevant_threshold: int = 1,
) -> float | None:
    return evaluate_ranking(
        ranking, judgments, k, relevant_threshold
    )["recall_at_k"]


def ndcg_at_k(
    ranking: Sequence[str], judgments: Mapping[str, int | None], k: int = 5,
) -> float | None:
    return evaluate_ranking(ranking, judgments, k)["ndcg_at_k"]
