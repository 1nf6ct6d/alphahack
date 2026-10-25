import csv
import json
import re
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .csv_io import (
    DATASET_FILES,
    IDENTIFIER_COLUMNS,
    MISSING_TOKENS,
    detect_encoding,
    discover_datasets,
    file_hash,
    missing,
    normalized,
)

REQUIRED_COLUMNS = {
    "companies": ("SYNTHETIC_U", "GROUP_ID", "REVENUE_COMPANY"),
    "payments": ("PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"),
    "links": ("SYNTHETIC_F", "SYNTHETIC_U", "CITY_NAME", "RC_NAME"),
}
FLAG_COLUMNS = frozenset({
    "CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK",
    "CL_PRODUCT_LEA_MARK", "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK",
    "FACTORING_MARK", "FTS_FLAG",
})
NUMERIC_COLUMNS = FLAG_COLUMNS | {"REVENUE_COMPANY", "PAY_AMT", "PAY_CNT"}
NUMBER_PATTERN = re.compile(
    r"[+-]?(?:[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?\Z"
)
EXAMPLE_LIMIT = 20


def percentage(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 6) if denominator else None


def numeric_value(value: str | None) -> tuple[Decimal | None, str]:
    if missing(value):
        return None, "missing"
    text = value.strip().replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None, "invalid"
    if not number.is_finite():
        return None, "non_finite"
    if NUMBER_PATTERN.fullmatch(text) is None:
        return None, "invalid"
    return number, "number"


@dataclass
class ColumnAudit:
    name: str
    missing_count: int = 0
    numeric_count: int = 0
    fractional_count: int = 0
    text_count: int = 0
    whitespace_count: int = 0
    issues: Counter = field(default_factory=Counter)

    def observe(self, value: str | None) -> None:
        if missing(value):
            self.missing_count += 1
            return
        if value != value.strip():
            self.whitespace_count += 1
        if self.name in IDENTIFIER_COLUMNS:
            return
        number, state = numeric_value(value)
        if state != "number":
            self.text_count += 1
            self.issues[state] += 1
            return
        self.numeric_count += 1
        fractional = number != number.to_integral_value()
        self.fractional_count += int(fractional)
        negative = number < 0
        invalid_domain = self.name in FLAG_COLUMNS and number not in (0, 1)
        placeholder = self.name == "REVENUE_COMPANY" and number == 1000
        self.issues["negative"] += int(negative)
        self.issues["invalid_domain"] += int(invalid_domain)
        self.issues["placeholder"] += int(placeholder)
        usable = not (
            negative or invalid_domain or placeholder
            or self.name == "PAY_CNT" and fractional
        )
        self.issues["usable"] += int(usable)

    def report(self, row_count: int) -> dict[str, Any]:
        if self.name in IDENTIFIER_COLUMNS:
            inferred = "string"
        elif not (self.numeric_count or self.text_count):
            inferred = "unknown"
        elif self.text_count:
            inferred = "mixed" if self.numeric_count else "string"
        else:
            inferred = "decimal" if self.fractional_count else "integer"
        result = {
            "inferred_type": inferred,
            "missing_count": self.missing_count,
            "missing_percentage": percentage(self.missing_count, row_count),
            "surrounding_whitespace_count": self.whitespace_count,
        }
        if self.name in NUMERIC_COLUMNS:
            result["numeric_quality"] = {
                "missing_count": self.missing_count,
                "invalid_format_count": self.issues["invalid"],
                "non_finite_count": self.issues["non_finite"],
                "negative_count": self.issues["negative"],
                "fractional_count": self.fractional_count,
                "invalid_domain_count": self.issues["invalid_domain"],
                "technical_placeholder_count": self.issues["placeholder"],
                "usable_count": self.issues["usable"],
            }
        return result


class CountingStore:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA cache_size = -8192")
        self.connection.execute("PRAGMA temp_store = FILE")
        self.connection.execute(
            "CREATE TABLE counts (kind TEXT, value TEXT, occurrences INTEGER, "
            "PRIMARY KEY (kind, value)) WITHOUT ROWID"
        )

    def add(self, kind: str, values: list[str] | tuple[str, ...]) -> None:
        signature = json.dumps(
            values, ensure_ascii=False, separators=(",", ":")
        )
        self.connection.execute(
            "INSERT INTO counts VALUES (?, ?, 1) "
            "ON CONFLICT(kind, value) DO UPDATE "
            "SET occurrences = occurrences + 1",
            (kind, signature),
        )

    def summary(self, kind: str) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(occurrences - 1), 0), "
            "COALESCE(SUM(CASE WHEN occurrences > 1 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN occurrences > 1 "
            "THEN occurrences ELSE 0 END), 0) FROM counts WHERE kind = ?",
            (kind,),
        ).fetchone()
        return dict(zip((
            "distinct_records", "extra_occurrences", "repeated_record_groups",
            "records_in_repeated_groups",
        ), row))

    def close(self) -> None:
        self.connection.close()


def identifier_summary(counts: Counter) -> dict[str, Any]:
    repeated = sorted(key for key, count in counts.items() if count > 1)
    return {
        "unique_count": len(counts),
        "repeated_identifier_count": len(repeated),
        "extra_occurrences": sum(count - 1 for count in counts.values()),
        "records_in_repeated_identifiers": sum(
            counts[key] for key in repeated
        ),
        "examples": repeated[:EXAMPLE_LIMIT],
        "examples_truncated": len(repeated) > EXAMPLE_LIMIT,
    }


class CompanyAudit:
    def __init__(self) -> None:
        self.available = False
        self.records: dict[str, dict[str, str | None]] = {}
        self.counts: Counter = Counter()
        self.conflicts: dict[str, dict[str, set]] = {}
        self.group_values: dict[str, set[str | None]] = {}
        self.observed_groups: set[str] = set()
        self.rows_with_group = 0
        self.missing_identifiers = 0

    def observe(self, row: dict[str, str]) -> None:
        company_id = normalized(row.get("SYNTHETIC_U"))
        group_id = normalized(row.get("GROUP_ID"))
        self.rows_with_group += int(group_id is not None)
        if group_id is not None:
            self.observed_groups.add(group_id)
        if company_id is None:
            self.missing_identifiers += 1
            return
        self.counts[company_id] += 1
        self.group_values.setdefault(company_id, set()).add(group_id)
        values = {key: normalized(value) for key, value in row.items()}
        if company_id not in self.records:
            self.records[company_id] = values
            return
        previous = self.records[company_id]
        for key, value in values.items():
            if value != previous[key]:
                fields = self.conflicts.setdefault(company_id, {})
                fields.setdefault(key, {previous[key]}).add(value)

    def report(
        self, columns: dict[str, Any], row_count: int
    ) -> dict[str, Any]:
        examples = []
        for company_id in sorted(self.conflicts)[:EXAMPLE_LIMIT]:
            fields = self.conflicts[company_id]
            examples.append({
                "company_id": company_id,
                "record_count": self.counts[company_id],
                "differing_columns": sorted(fields),
                "values": {
                    key: sorted(
                        values,
                        key=lambda value: (value is not None, value or ""),
                    )[:5]
                    for key, values in sorted(fields.items())
                },
                "value_counts": {
                    key: len(values) for key, values in sorted(fields.items())
                },
            })
        observed_groups = self.observed_groups
        sizes: Counter = Counter({group: 0 for group in observed_groups})
        ambiguous = []
        companies_with_group = 0
        consistent_companies = 0
        for company_id, groups in self.group_values.items():
            companies_with_group += int(
                any(group is not None for group in groups)
            )
            if len(groups) > 1:
                ambiguous.append(company_id)
            elif None not in groups:
                sizes[next(iter(groups))] += 1
                consistent_companies += 1
        distribution = Counter(sizes.values())
        revenue = columns.get("REVENUE_COMPANY", {}).get("numeric_quality")
        return {
            "identifiers": identifier_summary(self.counts),
            "missing_identifier_records": self.missing_identifiers,
            "conflicting_company_count": len(self.conflicts),
            "records_with_conflicting_company_id": sum(
                self.counts[key] for key in self.conflicts
            ),
            "conflict_examples": examples,
            "conflict_examples_truncated": len(self.conflicts) > EXAMPLE_LIMIT,
            "revenue": None if revenue is None else {
                **revenue,
                "basis": "all_records_without_deduplication",
                "usable_percentage": percentage(
                    revenue["usable_count"], row_count
                ),
                "year": 2025,
                "currency": "RUB",
            },
            "groups": {
                "distinct_non_null_group_ids": len(observed_groups),
                "company_records_with_group_id": self.rows_with_group,
                "companies_with_any_group_id": companies_with_group,
                "companies_with_consistent_group_id": consistent_companies,
                "companies_with_conflicting_group_id": len(ambiguous),
                "conflicting_group_company_examples": (
                    sorted(ambiguous)[:EXAMPLE_LIMIT]
                ),
                "group_size_basis": (
                    "unique_companies_with_unambiguous_group_membership"
                ),
                "group_size_distribution": {
                    str(size): count
                    for size, count in sorted(distribution.items())
                },
                "groups_without_unambiguous_members": distribution[0],
            },
        }


class PaymentAudit:
    def __init__(self, directory: CompanyAudit, store: CountingStore) -> None:
        self.directory = directory
        self.store = store
        self.payers: Counter = Counter()
        self.beneficiaries: Counter = Counter()
        self.self_payments = 0
        self.rows_missing_endpoint = 0
        self.external_records = 0
        self.covered_external_records = 0
        self.external_counterparties: set[str] = set()

    def observe(self, row: dict[str, str]) -> None:
        payer = normalized(row.get("PAYER"))
        beneficiary = normalized(row.get("BENEFICIARY"))
        if payer is not None:
            self.payers[payer] += 1
        if beneficiary is not None:
            self.beneficiaries[beneficiary] += 1
        if payer is None or beneficiary is None:
            self.rows_missing_endpoint += 1
            return
        self.store.add("payment_pairs", (payer, beneficiary))
        if payer == beneficiary:
            self.self_payments += 1
            return
        if self.directory.available and payer in self.directory.records:
            self.external_records += 1
            self.external_counterparties.add(beneficiary)
            self.covered_external_records += int(
                beneficiary in self.directory.records
            )

    def report(self) -> dict[str, Any]:
        directory = self.directory.records
        available = self.directory.available
        endpoints = self.payers.keys() | self.beneficiaries.keys()
        known = (
            sum(key in directory for key in endpoints) if available else None
        )
        missing_payers = (
            sorted(self.payers.keys() - directory.keys()) if available else []
        )
        missing_beneficiaries = (
            sorted(self.beneficiaries.keys() - directory.keys())
            if available else []
        )
        known_counterparties = sum(
            key in directory for key in self.external_counterparties
        )
        return {
            "payers": identifier_summary(self.payers),
            "beneficiaries": identifier_summary(self.beneficiaries),
            "self_payment_records": self.self_payments,
            "records_missing_payer_or_beneficiary": self.rows_missing_endpoint,
            "repeated_directed_pairs": self.store.summary("payment_pairs"),
            "company_directory_available": available,
            "payers_missing_from_directory": (
                len(missing_payers) if available else None
            ),
            "payer_records_missing_from_directory": (
                sum(self.payers[key] for key in missing_payers)
                if available else None
            ),
            "missing_payer_examples": missing_payers[:EXAMPLE_LIMIT],
            "beneficiaries_missing_from_directory": (
                len(missing_beneficiaries) if available else None
            ),
            "beneficiary_records_missing_from_directory": (
                sum(self.beneficiaries[key] for key in missing_beneficiaries)
                if available else None
            ),
            "missing_beneficiary_examples": (
                missing_beneficiaries[:EXAMPLE_LIMIT]
            ),
            "directory_coverage": None if not available else {
                "all_unique_endpoints": {
                    "known_count": known,
                    "total_count": len(endpoints),
                    "percentage": percentage(known, len(endpoints)),
                },
                "external_outgoing_unique_counterparties": {
                    "known_count": known_counterparties,
                    "total_count": len(self.external_counterparties),
                    "percentage": percentage(
                        known_counterparties, len(self.external_counterparties)
                    ),
                },
                "external_outgoing_records": {
                    "known_count": self.covered_external_records,
                    "total_count": self.external_records,
                    "percentage": percentage(
                        self.covered_external_records, self.external_records
                    ),
                },
            },
        }


class LinkAudit:
    def __init__(self, directory: CompanyAudit) -> None:
        self.directory = directory
        self.individuals: Counter = Counter()
        self.companies: Counter = Counter()

    def observe(self, row: dict[str, str]) -> None:
        individual = normalized(row.get("SYNTHETIC_F"))
        company = normalized(row.get("SYNTHETIC_U"))
        if individual is not None:
            self.individuals[individual] += 1
        if company is not None:
            self.companies[company] += 1

    def report(self) -> dict[str, Any]:
        unknown = sorted(self.companies.keys() - self.directory.records.keys())
        return {
            "individual_identifiers": identifier_summary(self.individuals),
            "company_identifiers": identifier_summary(self.companies),
            "companies_missing_from_directory": (
                len(unknown) if self.directory.available else None
            ),
            "missing_company_examples": (
                unknown[:EXAMPLE_LIMIT] if self.directory.available else []
            ),
            "repeated_company_ids_are_not_automatically_errors": True,
        }


def audit_dataset(
    path: Path,
    kind: str,
    observer: CompanyAudit | PaymentAudit | LinkAudit,
    store: CountingStore,
    encoding: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ok",
        "source": {
            "path": path.as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": file_hash(path),
        },
        "delimiter": ";",
        "storage_type": "string",
        "row_count": 0,
        "structurally_valid_row_count": 0,
        "malformed_row_count": 0,
        "malformed_row_examples": [],
        "missing_required_columns": [],
        "columns": {},
    }
    statistics: dict[str, ColumnAudit] = {}
    try:
        selected_encoding = detect_encoding(path, encoding)
        result["source"]["encoding"] = selected_encoding
        with path.open("r", encoding=selected_encoding, newline="") as stream:
            reader = csv.reader(stream, delimiter=";", strict=True)
            header = next(reader, None)
            if not header or any(not name.strip() for name in header):
                raise ValueError("Пустой файл или пустое имя столбца")
            if len(set(header)) != len(header):
                raise ValueError("Повторяющиеся имена столбцов")
            result["column_count"] = len(header)
            result["column_names"] = header
            result["missing_required_columns"] = sorted(
                set(REQUIRED_COLUMNS[kind]) - set(header)
            )
            statistics = {name: ColumnAudit(name) for name in header}
            for row in reader:
                result["row_count"] += 1
                store.add(kind + "_records", row)
                for index, column in enumerate(statistics.values()):
                    column.observe(row[index] if index < len(row) else None)
                if len(row) != len(header):
                    result["malformed_row_count"] += 1
                    if len(result["malformed_row_examples"]) < EXAMPLE_LIMIT:
                        result["malformed_row_examples"].append({
                            "record_number": result["row_count"],
                            "physical_end_line": reader.line_num,
                            "actual_field_count": len(row),
                            "expected_field_count": len(header),
                        })
                    continue
                result["structurally_valid_row_count"] += 1
                if not result["missing_required_columns"]:
                    observer.observe(dict(zip(header, row)))
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        result["status"] = "error"
        result["error"] = str(error)
    result["columns"] = {
        name: column.report(result["row_count"])
        for name, column in statistics.items()
    }
    result["exact_duplicates"] = store.summary(kind + "_records")
    if result["status"] == "ok" and (
        result["malformed_row_count"] or result["missing_required_columns"]
    ):
        result["status"] = "incomplete"
    result["analysis_scope"] = (
        "structurally_valid_records_with_required_columns"
    )
    if result["missing_required_columns"]:
        result["analysis"] = None
    elif isinstance(observer, CompanyAudit):
        result["analysis"] = observer.report(
            result["columns"], result["row_count"]
        )
        observer.available = result["status"] in ("ok", "incomplete")
    else:
        result["analysis"] = observer.report()
    return result


def audit_data(data_dir: str | Path, encoding: str = "auto") -> dict[str, Any]:
    paths = discover_datasets(Path(data_dir))
    directory = CompanyAudit()
    datasets = {}
    with tempfile.TemporaryDirectory(prefix="ml0_audit_") as temporary:
        store = CountingStore(Path(temporary) / "counts.sqlite3")
        try:
            observers = {
                "companies": directory,
                "payments": PaymentAudit(directory, store),
                "links": LinkAudit(directory),
            }
            for kind, path in paths.items():
                if path is None:
                    datasets[kind] = {
                        "status": "missing",
                        "expected_filename": DATASET_FILES[kind],
                        "row_count": None,
                        "analysis": None,
                    }
                else:
                    datasets[kind] = audit_dataset(
                        path, kind, observers[kind], store, encoding
                    )
        finally:
            store.close()
    limitations = [
        "Дубликаты и конфликтующие записи сохранены; "
        "финансовые суммы не агрегируются.",
        "Повтор пары плательщик–получатель сам по себе не доказывает "
        "дублирование платежа.",
        "Год и дата окончания наблюдений платежей неизвестны; "
        "начало — 1 января.",
        "Платёжная связь не доказывает отношения поставщика и покупателя.",
        "GROUP_ID не доказывает контроль собственности "
        "или экономическую зависимость.",
        "Принадлежность к корпоративному сегменту нельзя определять "
        "только по выручке юрлица.",
        "Покрытие связей считается по идентификаторам, независимо "
        "от качества сумм; неизвестные контрагенты остаются в знаменателе.",
        "Отсутствие строки в справочнике означает отсутствие данных "
        "в этой выгрузке.",
        "Словари категорий и смысл NEGATIVEMARK "
        "не подтверждаются этим аудитом.",
    ]
    for kind, dataset in datasets.items():
        if dataset["status"] != "ok":
            limitations.append(
                f"Набор {kind}: статус {dataset['status']}; "
                "результаты могут быть неполными."
            )
        if dataset.get("source", {}).get("encoding") == "cp1251":
            limitations.append(
                f"Набор {kind}: использован резервный вариант кодировки "
                "CP1251; проверьте отображение текста."
            )
    return {
        "report_version": "1.0.0",
        "status": "complete" if all(
            dataset["status"] == "ok" for dataset in datasets.values()
        ) else "incomplete",
        "semantics": {
            "revenue": {
                "column": "REVENUE_COMPANY", "year": 2025,
                "currency": "RUB", "technical_placeholder": "1000",
            },
            "payments": {
                "currency": "RUB", "start_month_day": "01-01",
                "year": None, "end_date": None,
                "row_grain": "not_confirmed",
            },
        },
        "methodology": {
            "missing_tokens_case_insensitive": sorted(MISSING_TOKENS),
            "identifiers_preserved_as_strings": True,
            "surrounding_identifier_whitespace_preserved": True,
            "duplicate_definition": (
                "identical_parsed_csv_fields_before_normalization"
            ),
            "conflict_definition": (
                "same_company_id_with_different_fields_"
                "after_missing_token_normalization"
            ),
            "duplicate_records_removed": False,
            "company_conflicts_resolved": False,
            "percentages_range": [0, 100],
            "empty_denominator_result": None,
            "example_limit": EXAMPLE_LIMIT,
        },
        "datasets": datasets,
        "limitations": limitations,
    }
