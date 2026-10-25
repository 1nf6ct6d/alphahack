import re
from dataclasses import asdict, dataclass
from datetime import date

from .models import NewsEvent

REGULATORY_TYPES = {"regulation_change", "tax_change"}
DUTY_DIRECTIONS = {"introduced", "tightened", "required"}
RELIEF_DIRECTIONS = {"removed", "exemption"}
CLAUSE_SEPARATOR = re.compile(r"[!?;\n]|(?<!\d)\.|\.(?!\d)")
UNCERTAIN = re.compile(
    r"\b(?:проект\w*|законопроект\w*|предлага\w*|предлож\w*|планиру\w*|"
    r"обсужда\w*|возможно|вероятно|предположительно|ожида\w*|если|"
    r"могут|может|могли|эксперт\w*|счита\w*|заявил\w*|сказал\w*|"
    r"призвал\w*|мнен\w*|ранее|прежде)\b|\bпри условии\b"
)
UNADOPTED = re.compile(
    r"\b(?:закон|норма|постановление|требование|проект)\b"
    r"[^.!?\n]{0,60}\bне\s+(?:принят\w*|утвержден\w*|вступил\w*|действу\w*)"
)
UNIVERSAL = re.compile(
    r"\b(?:все|всех|всем|кажд\w*)\s+"
    r"(?:организаци\w*|компани\w*|предприяти\w*|производител\w*|"
    r"участник\w*|налогоплательщик\w*)\b"
)
LIMITED_SCOPE = re.compile(
    r"\b(?:кроме|некотор\w*|отдельн\w*|только)\b|\bза исключением\b"
)
ACTION = (
    r"\b(?:подать|подавать|представить|представлять|предоставить|"
    r"предоставлять|учитывать|уплатить|уплачивать|платить|оплатить|"
    r"оплачивать|направить|направлять|вести|соблюдать|передать|передавать|"
    r"получить|обеспечить|проводить|осуществлять)\b"
)
AFFIRMATIVE_DUTY = re.compile(
    r"\b(?:обязаны|обязан|обязана|обязано|должны|должен|должна|должно)\b"
    r".{0,160}?" + ACTION
)
EXEMPTION = re.compile(
    r"\b(?:освобождены|освобожден|освобождена|освобождено|"
    r"освобождаются|освобождается)\s+от\s+"
    r"(?:обязанност\w*|уплаты\b|представления\b|подачи\b|"
    r"обязательн\w*\s+(?:отчетност\w*|платеж\w*|сбор\w*))"
)
DUTY_OBJECT = (
    r"(?:обязанност\w*|требовани\w*|"
    r"обязательн\w*\s+(?:отчетност\w*|платеж\w*|сбор\w*))"
)
REMOVAL = re.compile(
    r"\b(?:отменена|отменено|отменены|отменяется|отменяются)\b"
    r".{0,160}?\b" + DUTY_OBJECT + r"\b|\b" + DUTY_OBJECT
    + r"\b.{0,160}?\b(?:отменена|отменено|отменены|отменяется|отменяются)\b"
)
INTRODUCTION = re.compile(
    r"\b(?:введена|вводится|установлена|устанавливается)\s+"
    r"(?:новая\s+)?обязанность\b.{0,160}?" + ACTION
)
TIGHTENING = re.compile(
    r"\b(?:ужесточены|ужесточаются|усилены|усиливаются)\s+"
    r"(?:обязательн\w*\s+)?требования\b|\bтребования\b.{0,100}?"
    r"\b(?:ужесточены|ужесточаются|усилены|усиливаются)\b"
)


@dataclass(frozen=True)
class RegulatoryVerification:
    direction: str = "unknown"
    evidence_status: str = "unverified"
    source_verified: bool = False
    scope_verified: bool = False
    obligation_verified: bool = False
    deadline_verified: bool = False
    mandatory_metadata_status: str = "unverified"
    scope_metadata_status: str = "unverified"
    supporting_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @property
    def metadata_conflict(self) -> bool:
        return "contradicted" in (
            self.mandatory_metadata_status, self.scope_metadata_status
        )

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "limitations": list(self.limitations),
        }


def normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def clause_direction(clause: str) -> str:
    text = normalize(clause)
    checked = re.sub(r"\bне позднее\b", "до", text)
    if UNCERTAIN.search(text) or re.search(r"\b(?:не|нет|нельзя)\b", checked):
        return "unknown"
    directions = set()
    if EXEMPTION.search(text):
        directions.add("exemption")
    if REMOVAL.search(text):
        directions.add("removed")
    if TIGHTENING.search(text):
        directions.add("tightened")
    if INTRODUCTION.search(text):
        directions.add("introduced")
    if AFFIRMATIVE_DUTY.search(text) and not directions:
        directions.add("required")
    return next(iter(directions)) if len(directions) == 1 else "unknown"


def contains_scope(clause: str, scopes: tuple[str, ...]) -> bool:
    return any(re.search(
        rf"(?<![0-9.]){re.escape(code)}(?![0-9.])", clause
    ) for code in scopes)


def potentially_same_scope(
    clause: str, scopes: tuple[str, ...], company_code: str | None,
) -> bool:
    mentioned = re.findall(
        r"\bоквэд\s+([0-9]{2}(?:\.[0-9]{1,2}){0,2})(?![0-9.])",
        normalize(clause),
    )
    targets = (company_code,) if company_code is not None else scopes
    return not mentioned or any(
        target.startswith(code) or code.startswith(target)
        for target in targets for code in mentioned
    )


def uncertain_context(event: NewsEvent, quote: str) -> bool:
    if any(mark in quote for mark in ('"', "«", "»", "“", "”", "?")):
        return True
    content = event.content or ""
    if UNADOPTED.search(normalize(content)):
        return True
    start = 0
    while (position := content.find(quote, start)) >= 0:
        prefix = content[:position]
        if prefix.count("«") != prefix.count("»") or prefix.count('"') % 2:
            return True
        paragraph_start = content.rfind("\n\n", 0, position) + 2
        if paragraph_start == 1:
            paragraph_start = 0
        paragraph_end = content.find("\n\n", position + len(quote))
        paragraph = content[paragraph_start:(
            paragraph_end if paragraph_end >= 0 else len(content)
        )]
        if UNCERTAIN.search(normalize(paragraph)):
            return True
        start = position + len(quote)
    return False


def deadline_in_clause(event: NewsEvent, clause: str) -> bool:
    if event.deadline is None:
        return False
    deadline = date.fromisoformat(event.deadline)
    return any(re.search(
        r"\b(?:до|не позднее)\s+" + re.escape(text) + r"(?![0-9])",
        normalize(clause),
    ) for text in (deadline.isoformat(), deadline.strftime("%d.%m.%Y")))


def verify_regulatory_event(
    event: NewsEvent, company_code: str | None = None,
) -> RegulatoryVerification:
    if event.event_type not in REGULATORY_TYPES:
        return RegulatoryVerification(evidence_status="not_applicable")
    scopes = tuple(code for code in event.okved_codes
                   if company_code is None or company_code.startswith(code))
    witnesses = []
    uncertain = False
    ids = set()
    for evidence in event.evidence:
        if not event.content or evidence.quote not in event.content:
            continue
        if not {"event_type", "factor_classes", "okved_codes"} <= set(
            evidence.supports
        ):
            continue
        for clause in CLAUSE_SEPARATOR.split(evidence.quote):
            if not contains_scope(clause, scopes):
                continue
            ids.add(evidence.evidence_id)
            direction = clause_direction(clause)
            if direction == "unknown" or uncertain_context(
                event, evidence.quote
            ):
                uncertain = True
                continue
            universal = bool(UNIVERSAL.search(normalize(clause))) and not (
                LIMITED_SCOPE.search(normalize(clause))
            )
            witnesses.append({
                "direction": direction,
                "universal": universal and "applies_to_all_in_scope"
                in evidence.supports,
                "duty": direction in DUTY_DIRECTIONS
                and "mandatory" in evidence.supports,
                "deadline": "deadline" in evidence.supports
                and deadline_in_clause(event, clause),
            })
    directions = {item["direction"] for item in witnesses}
    body_directions = {
        clause_direction(clause)
        for clause in CLAUSE_SEPARATOR.split(event.content or "")
        if potentially_same_scope(clause, scopes, company_code)
    } - {"unknown"}
    conflict = bool(
        (directions & DUTY_DIRECTIONS and body_directions & RELIEF_DIRECTIONS)
        or (directions & RELIEF_DIRECTIONS
            and body_directions & DUTY_DIRECTIONS)
    )
    if "required" in directions and len(directions) > 1:
        directions.remove("required")
    limits = [
        "Проверены узкие текстовые конструкции; это не полное понимание "
        "русского языка и не юридическая экспертиза."
    ]
    if not witnesses or len(directions) != 1 or uncertain or conflict:
        limits.append(
            "Направление нормы не подтверждено: нет достаточной связной "
            "цитаты, есть неопределённость или противоречие фрагментов."
        )
        return RegulatoryVerification(
            evidence_status="uncertain" if ids else "unverified",
            supporting_evidence_ids=tuple(sorted(ids)),
            limitations=tuple(limits),
        )
    direction = next(iter(directions))
    obligation = direction in DUTY_DIRECTIONS and any(
        item["duty"] for item in witnesses
    )
    scope = any(item["universal"] for item in witnesses)
    mandatory_status = "missing" if event.mandatory is None else (
        "supported" if event.mandatory == (direction in DUTY_DIRECTIONS)
        else "contradicted"
    )
    scope_status = "missing" if event.applies_to_all_in_scope is None else (
        "supported" if event.applies_to_all_in_scope is True and scope
        else "contradicted" if event.applies_to_all_in_scope is False and scope
        else "unverified"
    )
    if "contradicted" in (mandatory_status, scope_status):
        limits.append(
            "Нормализованные флаги противоречат источнику; смысл берётся "
            "из подтверждённого фрагмента, срочность не подтверждается."
        )
    return RegulatoryVerification(
        direction, "verified", True, scope, obligation,
        obligation and any(item["duty"] and item["universal"]
                           and item["deadline"] for item in witnesses),
        mandatory_status, scope_status, tuple(sorted(ids)), tuple(limits),
    )
