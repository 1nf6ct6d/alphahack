from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True)
class ArticleEvidence:
    evidence_id: str
    quote: str
    supports: tuple[str, ...]


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    title: str
    source_id: str
    source_url: str
    event_type: str
    content: str | None = None
    published_at: str | None = None
    factor_classes: tuple[str, ...] = ()
    okved_codes: tuple[str, ...] = ()
    geography: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    evidence: tuple[ArticleEvidence, ...] = ()
    deadline: str | None = None
    mandatory: bool | None = None
    applies_to_all_in_scope: bool | None = None
    synthetic: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NewsEvent":
        if not isinstance(value, dict):
            raise ValueError("Новость должна быть JSON-объектом")
        unknown = set(value) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"Неизвестные поля новости: {sorted(unknown)}")
        data = dict(value)
        for name in ("factor_classes", "okved_codes", "geography", "entities"):
            if name in data:
                if not isinstance(data[name], (list, tuple)):
                    raise ValueError(f"Поле {name} должно быть массивом")
                data[name] = tuple(data[name])
        evidence = []
        for item in data.get("evidence", []):
            if not isinstance(item, dict) or set(item) != {
                "evidence_id", "quote", "supports"
            }:
                raise ValueError("Неверная структура цитаты новости")
            if not isinstance(item["supports"], (list, tuple)):
                raise ValueError("supports должен быть массивом")
            evidence.append(ArticleEvidence(
                item["evidence_id"], item["quote"], tuple(item["supports"])
            ))
        data["evidence"] = tuple(evidence)
        return cls(**data)


@dataclass(frozen=True)
class RelevanceResult:
    event: NewsEvent
    company_id: str
    profile_id: str
    relevance: str
    priority: str
    explanation: str
    impact_mechanism: str | None
    conditions: tuple[str, ...]
    evidence: tuple[dict, ...]
    limitations: tuple[str, ...]
    interpretation_constraints: tuple[dict, ...]
    ranking_factors: dict[str, Any]

    @property
    def event_id(self) -> str:
        return self.event.event_id
