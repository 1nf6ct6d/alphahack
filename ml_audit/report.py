import json
from pathlib import Path
from typing import Any

DATASET_TITLES = {
    "companies": "Справочник компаний",
    "payments": "Платежи",
    "links": "Связи физических лиц и компаний",
}
STATUS_TITLES = {
    "ok": "Обработан",
    "missing": "Файл отсутствует",
    "incomplete": "Обработан с ограничениями",
    "error": "Ошибка чтения",
}
TYPE_TITLES = {
    "string": "строка",
    "integer": "целое число",
    "decimal": "десятичное число",
    "mixed": "смешанные значения",
    "unknown": "нет значений",
}


def cell(value: Any) -> str:
    if value is None:
        return "нет данных"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def percent(value: float | None) -> str:
    return "нет данных" if value is None else f"{value:.2f}%"


def coverage_text(coverage: dict[str, Any]) -> str:
    return (
        f"{coverage['known_count']} из {coverage['total_count']} "
        f"({percent(coverage['percentage'])})"
    )


def company_lines(analysis: dict[str, Any]) -> list[str]:
    identifiers = analysis["identifiers"]
    groups = analysis["groups"]
    lines = [
        f"Уникальных ID компаний: **{identifiers['unique_count']}**. "
        "ID с несколькими строками: "
        f"**{identifiers['repeated_identifier_count']}**. "
        f"Строк без ID компании: {analysis['missing_identifier_records']}.",
        "",
        f"Конфликтующих ID: **{analysis['conflicting_company_count']}**; "
        "строк с такими ID: "
        f"{analysis['records_with_conflicting_company_id']}. "
        "Различающиеся записи не объединены и не выбраны произвольно.",
        "",
    ]
    revenue = analysis["revenue"]
    if revenue is not None:
        lines.extend([
            f"Выручка за 2025 год, RUB: пригодных значений "
            f"**{revenue['usable_count']} "
            f"({percent(revenue['usable_percentage'])})**; "
            f"пропусков {revenue['missing_count']}; "
            "технических значений 1000 — "
            f"**{revenue['technical_placeholder_count']}**. "
            "Знаменатель — все строки, включая дубликаты.",
            "",
        ])
    lines.extend([
        f"Непустых GROUP_ID: **{groups['distinct_non_null_group_ids']}**. "
        "Компаний хотя бы с одним GROUP_ID: "
        f"{groups['companies_with_any_group_id']}; "
        "с однозначной принадлежностью: "
        f"{groups['companies_with_consistent_group_id']}; "
        "с противоречивой принадлежностью: "
        f"{groups['companies_with_conflicting_group_id']}.",
        "",
        "Размер группы — число уникальных компаний с однозначным GROUP_ID. "
        "Конфликтующие принадлежности исключены из расчёта размеров. "
        "Размер 0 означает, что группа наблюдалась, "
        "но однозначных участников нет.",
        "",
        "| Компаний в группе | Число групп |",
        "|---|---|",
    ])
    for size, count in groups["group_size_distribution"].items():
        lines.append(f"| {size} | {count} |")
    if analysis["conflict_examples"]:
        lines.extend([
            "",
            "Примеры конфликтов, максимум 20 компаний и 5 значений поля:",
            "",
            "| ID компании | Строк | Различающиеся поля и значения |",
            "|---|---|---|",
        ])
        for example in analysis["conflict_examples"]:
            values = json.dumps(example["values"], ensure_ascii=False)
            lines.append(
                f"| {cell(example['company_id'])} | {example['record_count']} "
                f"| {cell(values)} |"
            )
    return lines


def payment_lines(analysis: dict[str, Any]) -> list[str]:
    pairs = analysis["repeated_directed_pairs"]
    lines = [
        f"Уникальных плательщиков: **{analysis['payers']['unique_count']}**; "
        f"получателей: **{analysis['beneficiaries']['unique_count']}**.",
        "",
        f"Самоплатежей: **{analysis['self_payment_records']}**. "
        "Строк без одного или обоих участников: "
        f"{analysis['records_missing_payer_or_beneficiary']}.",
        "",
        f"Уникальных направленных пар: {pairs['distinct_records']}; "
        f"повторяющихся пар: **{pairs['repeated_record_groups']}**; "
        f"строк в повторяющихся парах: {pairs['records_in_repeated_groups']}; "
        f"повторов сверх первого: {pairs['extra_occurrences']}.",
        "",
        "Плательщиков вне справочника: "
        f"**{cell(analysis['payers_missing_from_directory'])}**; "
        "строк с ними: "
        f"{cell(analysis['payer_records_missing_from_directory'])}. "
        "Получателей вне справочника: "
        f"**{cell(analysis['beneficiaries_missing_from_directory'])}**; "
        "строк с ними: "
        f"{cell(analysis['beneficiary_records_missing_from_directory'])}.",
        "",
    ]
    coverage = analysis["directory_coverage"]
    if coverage is None:
        lines.append(
            "Покрытие контрагентов не рассчитано: "
            "справочник компаний недоступен."
        )
    else:
        external_unique = coverage["external_outgoing_unique_counterparties"]
        lines.extend([
            "Покрытие справочником:",
            "",
            "- Все уникальные участники платежей: "
            f"{coverage_text(coverage['all_unique_endpoints'])}.",
            "- Уникальные внешние получатели платежей компаний "
            "из справочника: "
            f"{coverage_text(external_unique)}.",
            "- Строки таких внешних платежей с известным получателем: "
            f"{coverage_text(coverage['external_outgoing_records'])}.",
            "",
            "Самоплатежи исключены только из внешнего покрытия. "
            "Неизвестные получатели включены в знаменатель. "
            "Строки с неполной парой ID не входят в это покрытие. "
            "Суммы и PAY_CNT здесь не используются; "
            "точные дубликаты сохранены.",
        ])
    return lines


def link_lines(analysis: dict[str, Any]) -> list[str]:
    individuals = analysis["individual_identifiers"]
    companies = analysis["company_identifiers"]
    return [
        f"Уникальных физических лиц: **{individuals['unique_count']}**; "
        f"связанных компаний: **{companies['unique_count']}**.",
        "",
        "Повторяющихся ID физлиц: "
        f"{individuals['repeated_identifier_count']}; "
        f"компаний: {companies['repeated_identifier_count']}. "
        "Несколько связей одной компании сами по себе не являются ошибкой.",
        "",
        "Связанных компаний вне справочника: "
        f"{cell(analysis['companies_missing_from_directory'])}.",
    ]


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ML-0 — аудит исходных данных",
        "",
        "Статус: " + (
            "все три файла обработаны." if report["status"] == "complete"
            else "неполный аудит; смотрите ограничения и ошибки чтения."
        ),
        "",
        "## Сводка",
        "",
        "| Набор | Статус | Строк | Столбцов "
        "| Точных повторов сверх первого |",
        "|---|---|---|---|---|",
    ]
    for kind, dataset in report["datasets"].items():
        duplicates = dataset.get("exact_duplicates", {}).get(
            "extra_occurrences"
        )
        lines.append(
            f"| {DATASET_TITLES[kind]} | {STATUS_TITLES[dataset['status']]} "
            f"| {cell(dataset['row_count'])} "
            f"| {cell(dataset.get('column_count'))} "
            f"| {cell(duplicates)} |"
        )
    lines.extend([
        "",
        "## Методика и подтверждённая семантика",
        "",
        "Выручка REVENUE_COMPANY — за 2025 год в RUB. Значение 1000, "
        "включая запись 1000.0, — техническая заглушка "
        "и не считается пригодной выручкой. "
        "PAY_AMT — в RUB. Платежи наблюдаются с 1 января; "
        "год и конец периода неизвестны.",
        "",
        "CSV читаются построчно с разделителем `;`. "
        "ID и коды сохраняются строками, "
        "включая ведущие нули и внешние пробелы. Определение числового типа — "
        "только характеристика содержимого, "
        "преобразования исходных полей нет. "
        "Для проверки чисел используется Decimal; допустимы точка или запятая "
        "как десятичный разделитель. "
        "PAY_CNT должен быть целым и неотрицательным.",
        "",
        "Пропуски: пустая или пробельная строка и текстовые маркеры "
        "`null`, `none`, `nan`, `n/a`, `<na>` без учёта регистра. "
        "Значения `0` и `NA` автоматически пропусками не становятся. "
        "Неизвестные значения не заменяются нулями.",
        "",
        "Точный повтор — совпадение всех разобранных полей CSV "
        "до нормализации пропусков. Число повторов сверх первого "
        "отличается от числа всех строк в повторяющихся группах. "
        "Конфликт — различие полей одного ID после нормализации "
        "только маркеров пропуска. "
        "Разная текстовая запись числа также фиксируется как различие. "
        "Ни один дубликат не удаляется, "
        "конфликт не разрешается автоматически.",
        "",
        "Строки с неправильным числом полей учитываются "
        "в общем числе строк и поиске "
        "дубликатов. Недостающие ячейки считаются пропусками, "
        "лишние не входят в статистику столбцов. Анализ связей "
        "и групп использует только строки правильной структуры. "
        "После ошибки чтения результаты относятся лишь "
        "к успешно прочитанному префиксу. "
        "Отсутствие обязательных столбцов "
        "отключает анализ связей этого файла.",
        "",
    ])
    renderers = {
        "companies": company_lines,
        "payments": payment_lines,
        "links": link_lines,
    }
    for kind, dataset in report["datasets"].items():
        lines.extend([f"## {DATASET_TITLES[kind]}", ""])
        if dataset["status"] == "missing":
            lines.extend([
                f"Не найден файл `{dataset['expected_filename']}`.", ""
            ])
            continue
        source = dataset["source"]
        lines.extend([
            f"Источник: `{cell(source['path'])}`; "
            f"байт: {source['size_bytes']}; "
            f"кодировка: {cell(source.get('encoding'))}.",
            "",
            f"SHA-256: `{source['sha256']}`.",
            "",
            "Строк неправильной структуры: "
            f"**{dataset['malformed_row_count']}**.",
            "",
        ])
        if dataset.get("error"):
            lines.extend([f"Ошибка: {cell(dataset['error'])}.", ""])
        if dataset["missing_required_columns"]:
            names = ", ".join(dataset["missing_required_columns"])
            lines.extend([f"Нет обязательных столбцов: {names}.", ""])
        lines.extend([
            "| Столбец | Тип содержимого | Пропуски "
            "| Доля пропусков | Внешние пробелы |",
            "|---|---|---|---|---|",
        ])
        numeric_columns = {}
        for name, column in dataset["columns"].items():
            lines.append(
                f"| {cell(name)} | {TYPE_TITLES[column['inferred_type']]} "
                f"| {column['missing_count']} "
                f"| {percent(column['missing_percentage'])} "
                f"| {column['surrounding_whitespace_count']} |"
            )
            if "numeric_quality" in column:
                numeric_columns[name] = column["numeric_quality"]
        if numeric_columns:
            lines.extend([
                "",
                "Проверка числовых полей. Категории могут пересекаться; "
                "дробность является ошибкой для PAY_CNT, "
                "но допустима для сумм.",
                "",
                "| Поле | Неверный формат | Бесконечные/нечисловые "
                "| Отрицательные | Дробные | Вне домена флага "
                "| Заглушка 1000 | Пригодные |",
                "|---|---|---|---|---|---|---|---|",
            ])
            for name, quality in numeric_columns.items():
                keys = (
                    "invalid_format_count", "non_finite_count",
                    "negative_count",
                    "fractional_count", "invalid_domain_count",
                    "technical_placeholder_count", "usable_count",
                )
                values = " | ".join(str(quality[key]) for key in keys)
                lines.append(f"| {cell(name)} | {values} |")
        duplicates = dataset["exact_duplicates"]
        lines.extend([
            "",
            "Групп точных дубликатов: "
            f"{duplicates['repeated_record_groups']}; "
            f"строк в них: {duplicates['records_in_repeated_groups']}; "
            f"повторов сверх первого: **{duplicates['extra_occurrences']}**.",
            "",
        ])
        if dataset.get("analysis") is not None:
            lines.extend(renderers[kind](dataset["analysis"]))
        lines.append("")
    lines.extend(["## Ограничения интерпретации", ""])
    lines.extend(f"- {text}" for text in report["limitations"])
    return "\n".join(lines) + "\n"


def write_reports(
    report: dict[str, Any], output_dir: str | Path
) -> tuple[Path, Path]:
    output = Path(output_dir)
    json_content = json.dumps(
        report, ensure_ascii=False, indent=2, allow_nan=False
    ) + "\n"
    markdown_content = render_markdown(report)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "ml0_data_audit.json"
    markdown_path = output / "ml0_data_audit.md"
    json_path.write_text(json_content, encoding="utf-8", newline="\n")
    markdown_path.write_text(markdown_content, encoding="utf-8", newline="\n")
    return json_path, markdown_path
