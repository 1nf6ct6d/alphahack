
from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from datetime import date
from urllib.parse import urlsplit
from ml_ranking.models import NewsEvent
from ml_ranking.validation import validate_event


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AnalyticFocus(str, Enum):
    CFO = "CFO"
    CEO = "CEO"


class ErrorBody(StrictModel):
    code: str
    message: str
    request_id: str | None = None
    details: dict | list | None = None


class OverviewRefreshRequest(StrictModel):
    analytic_focus: AnalyticFocus
    as_of: str | None = Field(
        default=None,
        description="ISO-8601 timestamp. If omitted, the server timestamp is used.",
    )


class InsightCreateRequest(StrictModel):
    analytic_focus: AnalyticFocus
    event_id: NonEmpty


class QuestionRequest(StrictModel):
    question: str = Field(min_length=1, max_length=2_000)
    conversation_id: str | None = Field(default=None, max_length=80)


class FeedbackRequest(StrictModel):
    rating: Literal["useful", "not_useful"]
    reason: Literal[
        "relevant",
        "irrelevant",
        "factual_error",
        "outdated",
        "unclear",
        "other",
    ]
    comment: str | None = Field(default=None, max_length=1_000)


class ArticleEvidenceIn(StrictModel):
    evidence_id: NonEmpty
    quote: str = Field(min_length=1, max_length=20_000)
    supports: list[NonEmpty] = Field(min_length=1, max_length=20)


class NormalizedEventIn(StrictModel):
    event_id: NonEmpty
    title: str = Field(min_length=1, max_length=500)
    source_id: NonEmpty
    source_url: str = Field(min_length=1, max_length=2_000)
    event_type: Literal[
        "regulation_change",
        "tax_change",
        "input_cost_change",
        "demand_change",
        "monetary_policy_change",
        "counterparty_disruption",
        "other",
    ]
    content: str | None = Field(default=None, max_length=500_000)
    published_at: str | None = None
    factor_classes: list[str] = Field(default_factory=list, max_length=30)
    okved_codes: list[str] = Field(default_factory=list, max_length=100)
    geography: list[str] = Field(default_factory=list, max_length=100)
    entities: list[str] = Field(default_factory=list, max_length=500)
    evidence: list[ArticleEvidenceIn] = Field(default_factory=list, max_length=100)
    deadline: str | None = None
    mandatory: bool | None = None
    applies_to_all_in_scope: bool | None = None
    synthetic: bool = False
    extraction_status: Literal["full_text", "metadata_only", "error"] = (
        "full_text"
    )

    @model_validator(mode="after")
    def validate_ml_contract(self):
        parsed = urlsplit(self.source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("source_url must be an HTTP(S) URL without credentials")
        if self.extraction_status == "full_text" and not (self.content or "").strip():
            raise ValueError("full_text requires a non-empty extracted article")
        validate_event(NewsEvent.from_dict(self.ml_payload()))
        return self

    def ml_payload(self) -> dict:
        payload = self.model_dump(exclude={"extraction_status"})
        if self.extraction_status != "full_text":
            payload["content"] = None
            payload["evidence"] = []
        return payload


class BankFragmentIn(StrictModel):
    page_number: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=100_000)
    ocr_quality: float = Field(ge=0, le=1)
    verified: bool = False


class BankReportIn(StrictModel):
    source_id: NonEmpty
    title: str = Field(min_length=1, max_length=500)
    report_date: str | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_filename: str = Field(min_length=1, max_length=500)
    fragments: list[BankFragmentIn] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_report_date(self):
        if self.report_date is not None:
            if date.fromisoformat(self.report_date).isoformat() != self.report_date:
                raise ValueError("report_date must use YYYY-MM-DD")
        return self


class RssImportRequest(StrictModel):
    url: str = Field(min_length=1, max_length=2_000)
    source_id: NonEmpty
    fetch_articles: bool = False
    max_items: int = Field(default=25, ge=1, le=100)


class UrlImportRequest(StrictModel):
    url: str = Field(min_length=1, max_length=2_000)
    source_id: NonEmpty

