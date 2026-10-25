from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from ml_audit.audit import CompanyAudit, numeric_value
from ml_audit.csv_io import normalized

from .payments import (
    PaymentFeatures,
    RecipientPayments,
    compute_payment_features,
)
from .sources import (
    CsvSource,
    confirmed_group,
    load_directory,
    profile_status,
)


@dataclass(frozen=True)
class GroupMember:
    company_id: str
    source_record_count: int
    revenue_raw: str | None
    revenue_status: str
    revenue_amount: Fraction | None


@dataclass(frozen=True)
class GroupFeatures:
    company_id: str
    company_profile_status: str
    group_id_present: bool | None
    confirmed_group_id: str | None
    members: tuple[GroupMember, ...] | None
    revenue: dict[str, Any] | None
    directory_diagnostics: dict[str, Any]
    unresolved_candidates: tuple[dict, ...]
    payments: PaymentFeatures | None
    payment_relationships: dict[str, Any] | None
    payment_mode: str
    source: dict[str, Any]
    limitations: tuple[str, ...]

    @property
    def group_size(self) -> int | None:
        return len(self.members) if self.members is not None else None


def revenue_observation(
    company_id: str, raw_value: str | None, record_count: int
) -> GroupMember:
    number, status = numeric_value(raw_value)
    if status == "number":
        if number == 1000:
            status = "placeholder"
        elif number < 0:
            status = "negative"
        else:
            status = "valid"
    amount = Fraction(number) if status == "valid" else None
    return GroupMember(company_id, record_count, raw_value, status, amount)


def revenue_summary(members: tuple[GroupMember, ...]) -> dict[str, Any]:
    counts = Counter(member.revenue_status for member in members)
    valid = [member.revenue_amount for member in members
             if member.revenue_amount is not None]
    return {
        "year": 2025,
        "currency": "RUB",
        "member_count": len(members),
        "observed_revenue_member_count": len(members) - counts["missing"],
        "valid_revenue_member_count": counts["valid"],
        "missing_revenue_member_count": counts["missing"],
        "placeholder_revenue_member_count": counts["placeholder"],
        "invalid_revenue_member_count": sum(
            counts[status] for status in ("invalid", "non_finite", "negative")
        ),
        "status_counts": dict(sorted(counts.items())),
        "reported_member_revenue_sum": (
            sum(valid, Fraction(0)) if valid else None
        ),
        "valid_revenue_coverage": (
            Fraction(counts["valid"], len(members)) if members else None
        ),
        "complete_for_observed_members": counts["valid"] == len(members),
        "confirmed_consolidated_group_revenue": None,
        "sum_scope": "unique_confirmed_members_with_valid_2025_revenue",
    }


def assignment_status(directory: CompanyAudit, company_id: str) -> str:
    status = profile_status(directory, company_id)
    return "missing_group_id" if status == "unambiguous" else status


def unknown_relationship_reason(
    directory: CompanyAudit, recipient: RecipientPayments
) -> str:
    status = profile_status(directory, recipient.company_id)
    if status == "missing":
        return "recipient_not_in_directory"
    if status == "technical_ambiguous":
        return "technical_ambiguous_recipient"
    if status == "conflicting":
        return "conflicting_recipient_profile"
    if confirmed_group(directory, recipient.company_id) is None:
        return "recipient_group_missing"
    return "target_group_unconfirmed"


def payment_relationships(
    company_id: str, directory: CompanyAudit, payments: PaymentFeatures,
) -> dict[str, Any]:
    target_group = confirmed_group(directory, company_id)
    if payments.company_group_id != target_group:
        raise ValueError("Группа плательщика в платёжном объекте не совпадает")
    seen = set()
    counts: Counter[str] = Counter()
    amounts = {name: Fraction(0) for name in ("same", "other", "unknown")}
    unknown = []
    recipients = {name: [] for name in amounts}
    for recipient in payments.recipients:
        identifier = recipient.company_id
        if identifier in seen:
            raise ValueError("В платёжном объекте повторяется ID получателя")
        seen.add(identifier)
        status = profile_status(directory, identifier)
        group_id = confirmed_group(directory, identifier)
        relation = "unknown"
        if target_group is not None and group_id is not None:
            relation = "same" if target_group == group_id else "other"
        if (recipient.profile_status, recipient.group_id,
            recipient.group_relation) != (status, group_id, relation):
            raise ValueError("Несогласованная классификация получателя")
        recipients[relation].append(identifier)
        if payments.additive:
            if recipient.amount is None:
                raise ValueError(
                    "У получателя отсутствует агрегированная сумма"
                )
            amounts[relation] += recipient.amount
        if relation == "unknown":
            reason = unknown_relationship_reason(directory, recipient)
            counts[reason] += 1
            unknown.append({
                "company_id": identifier,
                "profile_status": status,
                "present_in_directory": identifier in directory.records,
                "reason": reason,
                "amount": recipient.amount,
            })
    external = payments.metrics["external_outgoing_amount"]
    conserved = None
    if payments.additive and external is not None:
        conserved = sum(amounts.values(), Fraction(0)) == external
        if not conserved:
            raise ValueError(
                "Не сохраняется разбиение внешней суммы по группам"
            )
        expected = {
            "confirmed_same_group_amount": amounts["same"],
            "confirmed_other_group_amount": amounts["other"],
            "unknown_group_amount": amounts["unknown"],
            "intragroup_share_lower_bound": None,
            "intragroup_share_upper_bound": None,
            "intragroup_share_out": None,
        }
        if external > 0:
            expected["intragroup_share_lower_bound"] = (
                amounts["same"] / external
            )
            expected["intragroup_share_upper_bound"] = (
                amounts["same"] + amounts["unknown"]
            ) / external
            if amounts["unknown"] == 0:
                expected["intragroup_share_out"] = amounts["same"] / external
        if any(payments.metrics[name] != value
               for name, value in expected.items()):
            raise ValueError(
                "Границы ML-1A не согласованы с суммами получателей"
            )
    return {
        "same_group_recipients": sorted(recipients["same"]),
        "different_group_recipients": sorted(recipients["other"]),
        "unknown_group_recipients": sorted(
            unknown, key=lambda item: item["company_id"]
        ),
        "unknown_group_reason_counts": dict(sorted(counts.items())),
        "unresolved_recipients_in_directory_count": sum(
            item["present_in_directory"] for item in unknown
        ),
        "unresolved_unambiguous_profiles_missing_group_count": (
            counts["recipient_group_missing"]
        ),
        "external_group_partition_conservation_verified": conserved,
        "ml1a_outgoing_valid_amount_conservation_verified": (
            payments.diagnostics["amount_conservation_verified"]
        ),
        "conservation_scope": "valid_external_amounts_only",
        "bounds_consistent_with_ml1a": conserved,
    }


def compute_group_features(
    company_id: str,
    company_csv: str | Path,
    payment_csv: str | Path | None = None,
    *,
    payment_features: PaymentFeatures | None = None,
    assume_additive_rows: bool = False,
    encoding: str = "auto",
) -> GroupFeatures:
    if not isinstance(company_id, str) or normalized(company_id) is None:
        raise ValueError("ID компании должен быть непустой строкой")
    if not isinstance(assume_additive_rows, bool):
        raise TypeError("assume_additive_rows должен быть bool")
    if payment_csv is not None and payment_features is not None:
        raise ValueError("Передайте CSV платежей либо готовые признаки ML-1A")
    mode = "not_requested"
    payments = payment_features
    if payments is not None:
        if not isinstance(payments, PaymentFeatures):
            raise TypeError("payment_features должен быть PaymentFeatures")
        if payments.company_id != company_id:
            raise ValueError("Платёжные признаки относятся к другой компании")
        if assume_additive_rows and not payments.additive:
            raise ValueError(
                "Нельзя включить суммирование без пересчёта ML-1A"
            )
        mode = "reused"
    elif payment_csv is not None:
        payments = compute_payment_features(
            company_id, company_csv, payment_csv,
            assume_additive_rows=assume_additive_rows, encoding=encoding,
        )
        mode = "calculated_from_csv"
    source = CsvSource.open(company_csv, ("SYNTHETIC_U", "GROUP_ID"), encoding)
    if payments is not None and (
        payments.sources["companies"]["sha256"] != source.sha256
    ):
        raise ValueError("Версии справочника ML-1A и ML-1B не совпадают")
    directory = load_directory(source)
    group_id = confirmed_group(directory, company_id)
    members = []
    candidates = []
    unassigned: Counter[str] = Counter()
    for identifier in sorted(directory.records):
        group = confirmed_group(directory, identifier)
        if group is None:
            status = assignment_status(directory, identifier)
            unassigned[status] += 1
            if group_id is not None and group_id in (
                directory.group_values[identifier]
            ):
                candidates.append({
                    "company_id": identifier,
                    "profile_status": profile_status(directory, identifier),
                    "source_record_count": directory.counts[identifier],
                    "observed_group_ids": sorted(
                        group for group in directory.group_values[identifier]
                        if group is not None
                    ),
                    "has_missing_group_observations": (
                        None in directory.group_values[identifier]
                    ),
                    "conflicting_fields": sorted(
                        directory.conflicts.get(identifier, {})
                    ),
                })
        elif group_id is not None and group == group_id:
            members.append(revenue_observation(
                identifier,
                directory.records[identifier].get("REVENUE_COMPANY"),
                directory.counts[identifier],
            ))
    member_tuple = tuple(members) if group_id is not None else None
    revenue = (
        revenue_summary(member_tuple) if member_tuple is not None else None
    )
    diagnostics = {
        "directory_unique_company_count": len(directory.records),
        "directory_record_count": sum(directory.counts.values())
        + directory.missing_identifiers,
        "directory_unassigned_company_count": sum(unassigned.values()),
        "unassigned_company_reason_counts": dict(sorted(unassigned.items())),
        "directory_rows_without_company_id": directory.missing_identifiers,
        "repeated_company_id_count": sum(
            count > 1 for count in directory.counts.values()
        ),
        "repeated_company_id_extra_records": sum(
            count - 1 for count in directory.counts.values()
        ),
        "unresolved_candidate_member_count": (
            len(candidates) if group_id is not None else None
        ),
        "target_source_record_count": directory.counts[company_id],
        "target_conflicting_fields": sorted(
            directory.conflicts.get(company_id, {})
        ),
        "revenue_column_present": "REVENUE_COMPANY" in source.columns,
    }
    relationships = (
        payment_relationships(company_id, directory, payments)
        if payments is not None else None
    )
    limitations = [
        "Состав группы относится только к надёжным ID в данной выгрузке.",
        "Пропуск GROUP_ID не подтверждает независимость компании.",
        "U_428697 и конфликтующие профили не входят в подтверждённый состав.",
        "Сумма выручки юрлиц не является консолидированной выручкой группы.",
        "Внутригрупповые обороты в выручке не исключены; "
        "рыночная доля неизвестна.",
        "GROUP_ID не доказывает контроль собственности, "
        "зависимость или поставку.",
    ]
    if group_id is None:
        limitations.append("Группа целевой компании не подтверждена.")
    if candidates:
        limitations.append(
            "Спорные кандидаты показаны отдельно и не посчитаны."
        )
    if revenue is not None and not revenue["complete_for_observed_members"]:
        limitations.append(
            "Выручка неполна даже для подтверждённых участников."
        )
    if not diagnostics["revenue_column_present"]:
        limitations.append("В CSV отсутствует столбец REVENUE_COMPANY.")
    if payments is None:
        limitations.append(
            "Платежи не переданы; платёжные показатели неизвестны."
        )
    else:
        limitations.extend(payments.limitations)
    present = None
    if company_id in directory.records:
        present = any(group is not None
                      for group in directory.group_values[company_id])
    return GroupFeatures(
        company_id, profile_status(directory, company_id), present, group_id,
        member_tuple, revenue, diagnostics, tuple(candidates), payments,
        relationships, mode, source.manifest(),
        tuple(dict.fromkeys(limitations)),
    )
