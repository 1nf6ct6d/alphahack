import copy
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .models import ArticleEvidence, NewsEvent
from .semantics import REGULATORY_TYPES, verify_regulatory_event

ROOT = Path(__file__).resolve().parent.parent
EVENT_TYPES = frozenset({
    "regulation_change", "tax_change", "input_cost_change", "demand_change",
    "monetary_policy_change", "counterparty_disruption", "other",
})
SUPPORTED_FIELDS = frozenset({
    "event_type", "factor_classes", "okved_codes", "entities", "deadline",
    "mandatory", "applies_to_all_in_scope",
})
OKVED_PATTERN = re.compile(r"[0-9]{2}(?:\.[0-9]{1,2}){0,2}\Z")


@lru_cache(maxsize=1)
def schema_validators() -> tuple[dict, dict]:
    schemas = {}
    for name, filename in (
        ("common", "common.v1.schema.json"),
        ("profile", "company_business_profile.v1.schema.json"),
        ("views", "profile_views.v1.schema.json"),
    ):
        schemas[name] = json.loads(
            (ROOT / "schemas" / filename).read_text(encoding="utf-8")
        )
    registry = Registry().with_resources([
        (schema["$id"], Resource.from_contents(
            schema, default_specification=DRAFT202012
        )) for schema in schemas.values()
    ])
    validators = {
        "view": Draft202012Validator({
            "$ref": schemas["views"]["$id"] + "#/$defs/MLView"
        }, registry=registry, format_checker=FormatChecker()),
        "profile": Draft202012Validator(
            schemas["profile"], registry=registry,
            format_checker=FormatChecker(),
        ),
    }
    return validators, schemas["common"]["$defs"]


def validate_contract(value: dict, name: str) -> None:
    json.dumps(value, allow_nan=False)
    validator = schema_validators()[0][name]
    error = next(validator.iter_errors(value), None)
    if error is not None:
        location = "/".join(str(part) for part in error.path) or "<root>"
        raise ValueError(
            f"Нарушен контракт {name}, поле {location}: {error.message}"
        )


def same_scalar(left, right) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


@dataclass(frozen=True)
class CompanyContext:
    view: dict
    profile: dict | None

    @property
    def verified(self) -> bool:
        if self.profile is None:
            return False
        validation = self.profile["provenance"]["validation"]
        return (
            validation["schema_valid"] and validation["evidence_refs_verified"]
        )

    def fact(self, field: str, value) -> dict | None:
        if not self.verified:
            return None
        states = self.profile["input_quality"]["field_states"]
        if states.get(field, {}).get("state") != "value":
            return None
        for conflict in self.profile["data_conflicts"]:
            if conflict["severity"] == "critical" or (
                conflict["resolution"] != "preferred_primary_field"
            ):
                if any(item["source"] == "df_Uid" and item["ref"] == field
                       for item in conflict["fields"]):
                    return None
        matches = []
        for fact in self.profile["facts"]:
            for evidence in fact["evidence"]:
                if evidence["source"] != "df_Uid" or evidence["ref"] != field:
                    continue
                if evidence["role"] != "supports":
                    continue
                if not same_scalar(evidence["value"], value):
                    return None
                if fact["data_reliability"] != "low":
                    matches.append(fact)
        return min(matches, key=lambda item: item["id"]) if matches else None

    def industry_fact(self, code: str) -> dict | None:
        if self.profile is None:
            return None
        industry = self.profile["input_quality"]["industry_identification"]
        if industry["status"] != "confirmed" or (
            industry["okved_code_used"] != code
        ):
            return None
        return self.fact("MAINOKVED_CODE", code)

    def loan_fact(self) -> dict | None:
        if self.profile is None or self.view["usability"] == "sector_only":
            return None
        if self.profile["meta"]["customer_status"] != "existing":
            return None
        return self.fact("CL_PRODUCT_LOA_MARK", 1)

    def payment_link(self, identifier: str) -> dict | None:
        if not self.verified:
            return None
        visible = {
            item["derived_id"] for item in self.view["numeric_features"]
        }
        metrics = sorted(
            self.profile["derived_metrics"], key=lambda item: item["id"]
        )
        for metric in metrics:
            if metric["id"] not in visible or (
                metric["data_reliability"] == "low"
            ):
                continue
            if metric["name"] not in {
                "top1_counterparty_share_out", "top5_counterparty_share_out",
                "hhi_out", "counterparty_count_out",
            }:
                continue
            if type(metric["value"]) not in (int, float) or (
                metric["value"] <= 0
            ):
                continue
            if not metric["coverage"]:
                continue
            if metric["unit_dependency"] == "requires_confirmed_unit" and (
                self.profile["input_quality"]["payments_semantics_status"]
                != "confirmed"
            ):
                continue
            if not any(
                item["source"] == "payments" and item["role"] == "supports"
                for item in metric["inputs"]
            ):
                continue
            if any(item["key"] == identifier and item["share"] > 0
                   for item in metric.get("breakdown", [])):
                return metric
        return None


def company_context(view: dict, profile: dict | None) -> CompanyContext:
    validate_contract(view, "view")
    view = copy.deepcopy(view)
    if profile is None:
        return CompanyContext(view, None)
    validate_contract(profile, "profile")
    profile = copy.deepcopy(profile)
    for key in ("company_id", "profile_id", "as_of_date"):
        if view[key] != profile["meta"][key]:
            raise ValueError(f"MLView и профиль расходятся по {key}")
    if view["usability"] != profile["input_quality"]["usability"]:
        raise ValueError("MLView и профиль имеют разную пригодность данных")
    if view["hooks"] != profile["downstream_hooks"]:
        raise ValueError("MLView и профиль имеют разные hooks")
    statements = [item for section in (
        "facts", "derived_metrics", "hypotheses", "sensitivities"
    ) for item in profile[section]]
    ids = [item["id"] for item in statements]
    if len(ids) != len(set(ids)):
        raise ValueError("В профиле повторяются ID утверждений")
    derived = {item["id"]: item for item in profile["derived_metrics"]}
    numeric_ids = []
    for feature in view["numeric_features"]:
        numeric_ids.append(feature["derived_id"])
        metric = derived.get(feature["derived_id"])
        if metric is None:
            raise ValueError("Признак MLView не найден в профиле")
        expected = metric["value"]
        if type(expected) not in (int, float):
            expected = None
        if not same_scalar(feature["value"], expected) or any(
            feature[key] != metric[key]
            for key in ("name", "coverage", "data_reliability")
        ):
            raise ValueError("Числовой признак MLView противоречит профилю")
    if len(numeric_ids) != len(set(numeric_ids)):
        raise ValueError("Повторяющиеся числовые признаки MLView")
    for metric in derived.values():
        keys = [item["key"] for item in metric.get("breakdown", [])]
        if len(keys) != len(set(keys)):
            raise ValueError("Повторяющиеся ключи breakdown")
    return CompanyContext(view, profile)


def parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Дата публикации должна быть ISO-строкой или null")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Для даты и времени нужен часовой пояс")
    return parsed.astimezone(timezone.utc)


def validate_event(event: NewsEvent) -> None:
    if not isinstance(event, NewsEvent):
        raise TypeError("Новость должна быть NewsEvent")
    for name in ("event_id", "title", "source_id", "source_url", "event_type"):
        value = getattr(event, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Нужно непустое поле новости {name}")
    url = urlparse(event.source_url)
    if url.scheme not in ("http", "https") or not url.netloc:
        raise ValueError("source_url должен быть HTTP(S)-адресом")
    if event.event_type not in EVENT_TYPES:
        raise ValueError("Неизвестный тип события")
    if event.content is not None and not isinstance(event.content, str):
        raise TypeError("content должен быть строкой или null")
    if event.published_at is not None:
        parse_timestamp(event.published_at)
    if event.deadline is not None:
        if date.fromisoformat(event.deadline).isoformat() != event.deadline:
            raise ValueError("deadline должен иметь формат YYYY-MM-DD")
    for name in ("mandatory", "applies_to_all_in_scope"):
        value = getattr(event, name)
        if value is not None and type(value) is not bool:
            raise TypeError(f"{name} должен быть bool или null")
    if type(event.synthetic) is not bool:
        raise TypeError("synthetic должен быть bool")
    for name in ("factor_classes", "okved_codes", "geography", "entities"):
        values = getattr(event, name)
        if not isinstance(values, tuple) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(f"{name} должен быть кортежем непустых строк")
    allowed = schema_validators()[1]["FactorClass"]["enum"]
    if not set(event.factor_classes) <= set(allowed):
        raise ValueError("Класс фактора отсутствует в существующей схеме")
    if any(not OKVED_PATTERN.fullmatch(code) for code in event.okved_codes):
        raise ValueError("Неверный формат ОКВЭД события")
    if not isinstance(event.evidence, tuple):
        raise TypeError("evidence должен быть кортежем ArticleEvidence")
    ids = []
    for evidence in event.evidence:
        if not isinstance(evidence, ArticleEvidence):
            raise TypeError("Цитата должна быть ArticleEvidence")
        if any(not isinstance(value, str) or not value.strip()
               for value in (evidence.evidence_id, evidence.quote)):
            raise ValueError("У цитаты нужны ID и текст")
        if event.content is None or evidence.quote not in event.content:
            raise ValueError("Цитата не найдена в переданном тексте статьи")
        if not isinstance(evidence.supports, tuple) or not evidence.supports:
            raise ValueError("supports должен быть непустым кортежем полей")
        if not set(evidence.supports) <= SUPPORTED_FIELDS:
            raise ValueError("Неизвестное или пустое основание цитаты")
        ids.append(evidence.evidence_id)
    if len(ids) != len(set(ids)):
        raise ValueError("Повторяющиеся ID цитат внутри события")


def article_support(event: NewsEvent, field: str, value=None) -> bool:
    if event.event_type in REGULATORY_TYPES and field in {
        "mandatory", "applies_to_all_in_scope", "deadline"
    }:
        verified = verify_regulatory_event(event)
        if verified.metadata_conflict:
            return False
        if field == "mandatory":
            return event.mandatory is True and verified.obligation_verified
        if field == "applies_to_all_in_scope":
            return event.applies_to_all_in_scope is True and (
                verified.scope_verified
            )
        return value == event.deadline and verified.deadline_verified
    for evidence in event.evidence:
        if field not in evidence.supports:
            continue
        quote = evidence.quote
        lower = quote.lower()
        if field in ("okved_codes", "entities"):
            boundary = r"[0-9.]" if field == "okved_codes" else r"\w"
            if not re.search(
                rf"(?<!{boundary}){re.escape(value)}(?!{boundary})", quote
            ):
                continue
        if field == "mandatory" and (
            not re.search(r"обяз|должн", lower)
            or re.search(r"не\s+(?:обяз|должн)", lower)
        ):
            continue
        if field == "applies_to_all_in_scope" and (
            not re.search(r"\bвсе\b|\bкажд", lower)
            or re.search(r"не\s+(?:все|кажд)", lower)
        ):
            continue
        if field == "deadline":
            deadline = date.fromisoformat(value)
            if not any(text in quote for text in (
                deadline.isoformat(), deadline.strftime("%d.%m.%Y")
            )):
                continue
        return True
    return False
