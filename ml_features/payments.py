from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from ml_audit.audit import numeric_value
from ml_audit.csv_io import normalized

from .sources import (
    CsvSource,
    confirmed_group,
    load_directory,
    profile_status,
)

AMOUNT_METRICS = frozenset({
    "outgoing_amount", "external_outgoing_amount", "self_payment_amount",
    "unassigned_outgoing_amount", "confirmed_same_group_amount",
    "confirmed_other_group_amount", "unknown_group_amount",
})
RATIO_METRICS = frozenset({
    "top1_share", "top5_share", "hhi_out", "intragroup_share_out",
    "counterparty_coverage", "counterparty_amount_coverage",
    "intragroup_share_lower_bound", "intragroup_share_upper_bound",
    "unknown_group_amount_share",
})


@dataclass
class PaymentCohort:
    additive: bool
    records: int = 0
    valid_amount_records: int = 0
    valid_count_records: int = 0
    amount: Fraction = Fraction(0)
    known_count: int = 0
    amount_issues: Counter[str] = field(default_factory=Counter)
    count_issues: Counter[str] = field(default_factory=Counter)

    def observe(self, raw_amount: str, raw_count: str) -> Fraction | None:
        self.records += 1
        amount, amount_state = numeric_value(raw_amount)
        count, count_state = numeric_value(raw_count)
        if amount_state == "number" and amount < 0:
            amount_state = "negative"
        if count_state == "number":
            if count < 0:
                count_state = "negative"
            elif count != count.to_integral_value():
                count_state = "fractional"
        if count_state != "number":
            self.count_issues[count_state] += 1
        if amount_state != "number":
            self.amount_issues[amount_state] += 1
            return None
        exact_amount = Fraction(amount)
        self.valid_amount_records += 1
        if count_state == "number":
            self.valid_count_records += 1
        if self.additive:
            self.amount += exact_amount
            if count_state == "number":
                self.known_count += int(count)
        return exact_amount

    def value(self) -> Fraction | None:
        if not self.additive:
            return None
        if self.records and not self.valid_amount_records:
            return None
        return self.amount

    def diagnostics(self) -> dict[str, Any]:
        return {
            "record_count": self.records,
            "valid_amount_records": self.valid_amount_records,
            "valid_count_records_on_valid_amounts": self.valid_count_records,
            "amount_issues": dict(sorted(self.amount_issues.items())),
            "count_issues_on_all_records": dict(
                sorted(self.count_issues.items())
            ),
            "known_payment_count_on_valid_amounts": (
                self.known_count if self.additive else None
            ),
        }


@dataclass
class RecipientPayments:
    company_id: str
    profile_status: str
    group_relation: str
    group_id: str | None
    additive: bool
    valid_amount_records: int = 0
    amount: Fraction | None = None

    def __post_init__(self) -> None:
        self.amount = Fraction(0) if self.additive else None

    def observe(self, amount: Fraction) -> None:
        self.valid_amount_records += 1
        if self.amount is not None:
            self.amount += amount


@dataclass(frozen=True)
class PaymentFeatures:
    company_id: str
    metrics: dict[str, int | Fraction | None]
    recipients: tuple[RecipientPayments, ...]
    diagnostics: dict[str, Any]
    sources: dict[str, dict]
    undefined_reasons: dict[str, str]
    limitations: tuple[str, ...]
    amount_row_coverage: Fraction | None
    group_amount_coverage: Fraction | None
    company_group_id: str | None
    additive: bool


def divide(numerator: int | Fraction, denominator: int | Fraction) -> Fraction:
    return Fraction(numerator) / denominator


def compute_payment_features(
    company_id: str,
    company_csv: str | Path,
    payment_csv: str | Path,
    *,
    assume_additive_rows: bool = False,
    encoding: str = "auto",
) -> PaymentFeatures:
    if not isinstance(company_id, str) or normalized(company_id) is None:
        raise ValueError("ID компании должен быть непустой строкой")
    if not isinstance(assume_additive_rows, bool):
        raise TypeError("assume_additive_rows должен быть bool")
    company_source = CsvSource.open(
        company_csv, ("SYNTHETIC_U", "GROUP_ID"), encoding
    )
    payment_source = CsvSource.open(
        payment_csv, ("PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"), encoding
    )
    directory = load_directory(company_source)
    company_group = confirmed_group(directory, company_id)
    cohorts = {
        name: PaymentCohort(assume_additive_rows)
        for name in ("outgoing", "external", "self", "unassigned")
    }
    recipients: dict[str, RecipientPayments] = {}
    signatures: Counter[tuple[str, ...]] = Counter()
    pairs: Counter[str] = Counter()
    scanned_records = 0
    missing_payer_records = 0
    for row in payment_source.rows():
        scanned_records += 1
        payer = normalized(row["PAYER"])
        if payer is None:
            missing_payer_records += 1
        if payer != company_id:
            continue
        signatures[tuple(row.values())] += 1
        beneficiary = normalized(row["BENEFICIARY"])
        if beneficiary is None:
            cohort = cohorts["unassigned"]
        elif beneficiary == company_id:
            cohort = cohorts["self"]
        else:
            cohort = cohorts["external"]
        if beneficiary is not None:
            pairs[beneficiary] += 1
        cohorts["outgoing"].observe(row["PAY_AMT"], row["PAY_CNT"])
        amount = cohort.observe(row["PAY_AMT"], row["PAY_CNT"])
        if cohort is not cohorts["external"] or amount is None:
            continue
        if beneficiary not in recipients:
            group = confirmed_group(directory, beneficiary)
            relation = "unknown"
            if company_group is not None and group is not None:
                relation = "same" if company_group == group else "other"
            recipients[beneficiary] = RecipientPayments(
                beneficiary, profile_status(directory, beneficiary),
                relation, group, assume_additive_rows,
            )
        recipients[beneficiary].observe(amount)
    metrics, reasons, group_coverage = calculate_metrics(
        cohorts, tuple(recipients.values()), assume_additive_rows
    )
    external = cohorts["external"]
    coverage = (
        divide(external.valid_amount_records, external.records)
        if external.records else None
    )
    duplicated = sorted(
        (signature, count) for signature, count in signatures.items()
        if count > 1
    )
    repeated_pairs = sorted(
        (recipient, count) for recipient, count in pairs.items() if count > 1
    )
    diagnostics = {
        "scanned_payment_records": scanned_records,
        "source_records_with_missing_payer": missing_payer_records,
        "company_profile_status": profile_status(directory, company_id),
        "company_directory_record_count": directory.counts[company_id],
        "company_conflicting_fields": sorted(
            directory.conflicts.get(company_id, {})
        ),
        "cohorts": {
            name: cohort.diagnostics() for name, cohort in cohorts.items()
        },
        "exact_duplicate_groups": len(duplicated),
        "exact_duplicate_extra_records": sum(
            count - 1 for _, count in duplicated
        ),
        "exact_duplicate_examples": [
            {"fields": dict(zip(payment_source.columns, signature)),
             "occurrences": count}
            for signature, count in duplicated[:20]
        ],
        "repeated_directed_pair_count": len(repeated_pairs),
        "repeated_pair_extra_records": sum(
            count - 1 for _, count in repeated_pairs
        ),
        "repeated_pair_examples": [
            {"payer": company_id, "beneficiary": recipient,
             "record_count": count}
            for recipient, count in repeated_pairs[:20]
        ],
        "recipient_profile_counts": dict(sorted(Counter(
            recipient.profile_status for recipient in recipients.values()
        ).items())),
        "recipient_group_relation_counts": dict(sorted(Counter(
            recipient.group_relation for recipient in recipients.values()
        ).items())),
        "amount_conservation_verified": (
            cohorts["outgoing"].amount == sum(
                (cohorts[name].amount for name in ("external", "self",
                                                 "unassigned")),
                Fraction(0),
            ) if assume_additive_rows else None
        ),
    }
    limitations = build_limitations(cohorts, diagnostics, assume_additive_rows)
    return PaymentFeatures(
        company_id, metrics,
        tuple(sorted(recipients.values(), key=lambda value: value.company_id)),
        diagnostics,
        {"companies": company_source.manifest(),
         "payments": payment_source.manifest()},
        reasons, limitations, coverage, group_coverage, company_group,
        assume_additive_rows,
    )


def calculate_metrics(
    cohorts: dict[str, PaymentCohort],
    recipients: tuple[RecipientPayments, ...],
    additive: bool,
) -> tuple[dict[str, int | Fraction | None], dict[str, str], Fraction | None]:
    external = cohorts["external"]
    known = [value for value in recipients
             if value.profile_status == "unambiguous"]
    metrics: dict[str, int | Fraction | None] = {
        "outgoing_amount": cohorts["outgoing"].value(),
        "external_outgoing_amount": external.value(),
        "self_payment_amount": cohorts["self"].value(),
        "unassigned_outgoing_amount": cohorts["unassigned"].value(),
        "self_payment_record_count": cohorts["self"].records,
        "external_recipient_count": len(recipients),
        "unambiguous_recipient_count": len(known),
        "external_outgoing_payment_count": None,
        "counterparty_coverage": divide(len(known), len(recipients))
        if recipients else None,
    }
    reasons: dict[str, str] = {}
    if not recipients:
        reasons["counterparty_coverage"] = "no_valid_external_recipients"
    for name in AMOUNT_METRICS | RATIO_METRICS:
        metrics.setdefault(name, None)
    if not additive:
        blocked_metrics = AMOUNT_METRICS | (
            RATIO_METRICS - {"counterparty_coverage"}
        )
        for name in blocked_metrics:
            reasons[name] = "additivity_not_assumed"
        reasons["external_outgoing_payment_count"] = "additivity_not_assumed"
        return metrics, reasons, None
    for name, cohort_name in (
        ("outgoing_amount", "outgoing"),
        ("external_outgoing_amount", "external"),
        ("self_payment_amount", "self"),
        ("unassigned_outgoing_amount", "unassigned"),
    ):
        if metrics[name] is None:
            reasons[name] = "no_valid_amounts"
    if external.value() is None:
        reasons["external_outgoing_payment_count"] = "no_valid_amounts"
    elif external.valid_count_records != external.valid_amount_records:
        reasons["external_outgoing_payment_count"] = "incomplete_pay_cnt"
    else:
        metrics["external_outgoing_payment_count"] = external.known_count
    grouped = {
        relation: sum((recipient.amount for recipient in recipients
                       if recipient.group_relation == relation), Fraction(0))
        for relation in ("same", "other", "unknown")
    }
    for relation, name in (
        ("same", "confirmed_same_group_amount"),
        ("other", "confirmed_other_group_amount"),
        ("unknown", "unknown_group_amount"),
    ):
        metrics[name] = (
            grouped[relation] if external.value() is not None else None
        )
        if metrics[name] is None:
            reasons[name] = "no_valid_amounts"
    denominator = external.value()
    if denominator is None or denominator == 0:
        reason = (
            "no_valid_amounts"
            if denominator is None else "zero_external_amount"
        )
        for name in RATIO_METRICS - {"counterparty_coverage"}:
            reasons[name] = reason
        return metrics, reasons, None
    ranked_amounts = sorted(
        (recipient.amount for recipient in recipients), reverse=True
    )
    metrics["top1_share"] = ranked_amounts[0] / denominator
    metrics["top5_share"] = sum(ranked_amounts[:5], Fraction(0)) / denominator
    metrics["hhi_out"] = sum(
        (amount * amount for amount in ranked_amounts), Fraction(0)
    ) / (denominator * denominator)
    metrics["counterparty_amount_coverage"] = sum(
        (recipient.amount for recipient in known), Fraction(0)
    ) / denominator
    lower_bound = grouped["same"] / denominator
    metrics["intragroup_share_lower_bound"] = lower_bound
    metrics["intragroup_share_upper_bound"] = (
        grouped["same"] + grouped["unknown"]
    ) / denominator
    metrics["unknown_group_amount_share"] = grouped["unknown"] / denominator
    group_coverage = (grouped["same"] + grouped["other"]) / denominator
    if grouped["unknown"] == 0:
        metrics["intragroup_share_out"] = lower_bound
    else:
        reasons["intragroup_share_out"] = "unknown_group_membership"
    return metrics, reasons, group_coverage


def build_limitations(
    cohorts: dict[str, PaymentCohort],
    diagnostics: dict[str, Any],
    additive: bool,
) -> tuple[str, ...]:
    limitations = [
        "PAY_AMT в RUB. Начало периода — 1 января; год и конец неизвестны.",
        "Платёжные связи не доказывают отношения поставки или цели платежа.",
        "GROUP_ID не доказывает контроль собственности или зависимость.",
        "Отсутствующие и неоднозначные профили не подтверждают другую группу.",
        "Показатели относятся к пригодным суммам, а не ко всем операциям.",
    ]
    limitations.append(
        "Суммы условны: принято допущение аддитивных неперекрывающихся строк."
        if additive else
        "Суммирование отключено: аддитивность строк явно не допущена."
    )
    if diagnostics["repeated_directed_pair_count"]:
        limitations.append(
            "Повторные пары сохранены; аддитивность по CSV не подтверждается."
        )
    if diagnostics["exact_duplicate_extra_records"]:
        limitations.append(
            "Точные дубликаты сохранены; условные суммы могут быть завышены."
        )
    if cohorts["outgoing"].amount_issues:
        limitations.append(
            "Непригодные суммы исключены; "
            "состав получателей может быть неполон."
        )
    if cohorts["external"].valid_count_records != (
        cohorts["external"].valid_amount_records
    ):
        limitations.append(
            "PAY_CNT неполон: показана частичная сумма, "
            "полный итог неизвестен."
        )
    if cohorts["unassigned"].records:
        limitations.append(
            "Без BENEFICIARY внешнее назначение суммы неизвестно; "
            "она отдельна."
        )
    return tuple(limitations)
