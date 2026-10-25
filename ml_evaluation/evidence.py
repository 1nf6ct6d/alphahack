def evidence_reference_error(
    reference: dict, event: dict, context: dict,
) -> str | None:
    kind = reference.get("kind")
    if kind == "article_quote":
        if set(reference) != {"kind", "event_id", "evidence_id"}:
            return "Неверные поля ссылки на цитату"
        if reference["event_id"] != event["event_id"]:
            return "Цитата относится к другому событию"
        if not isinstance(reference["evidence_id"], str):
            return "ID цитаты должен быть строкой"
        if not any(item["evidence_id"] == reference["evidence_id"]
                   for item in event.get("evidence", [])):
            return "ID цитаты отсутствует в замороженной новости"
        return None
    if kind == "company_statement":
        if set(reference) != {"kind", "statement_id"}:
            return "Неверные поля ссылки на утверждение"
        profile = context["company_profile"]
        if profile is None:
            return "Полный профиль для проверки ссылки отсутствует"
        if not isinstance(reference["statement_id"], str):
            return "ID утверждения должен быть строкой"
        if reference["statement_id"] not in {
            item["id"] for key in (
                "facts", "derived_metrics", "hypotheses", "sensitivities"
            ) for item in profile[key]
        }:
            return "ID утверждения отсутствует в замороженном профиле"
        return None
    if kind == "ml_view_field":
        if set(reference) != {"kind", "path"}:
            return "Неверные поля ссылки на MLView"
        if not isinstance(reference["path"], str):
            return "Путь MLView должен быть строкой"
        value = context["ml_view"]
        for part in reference["path"].split("."):
            if not isinstance(value, dict) or part not in value:
                return "Путь отсутствует в замороженном MLView"
            value = value[part]
        return None
    return "Неизвестный вид доказательства"


def audit_ranked_evidence(
    result: dict, event: dict, context: dict,
) -> list[str]:
    problems = []
    if any(result.get(key) != event.get(key)
           for key in ("event_id", "source_id", "source_url", "published_at")):
        problems.append("Результат относится к другому событию или источнику")
    view = context["ml_view"]
    if any(result.get(key) != view[key]
           for key in ("company_id", "profile_id")):
        problems.append("Результат относится к другой компании или профилю")
    if not result["evidence"]:
        problems.append("Результат не содержит ссылок на доказательства")
    kinds = {item["kind"] for item in result["evidence"]}
    if "article_quote" not in kinds:
        problems.append("Нет цитаты статьи")
    if result["relevance"] == "grounded" and (
        "company_statement" not in kinds
    ):
        problems.append("У grounded нет ссылки на утверждение профиля")
    for reference in result["evidence"]:
        keys = {
            "article_quote": ("kind", "event_id", "evidence_id"),
            "company_statement": ("kind", "statement_id"),
            "ml_view_field": ("kind", "path"),
        }.get(reference["kind"], ("kind",))
        error = evidence_reference_error(
            {key: reference.get(key) for key in keys}, event, context
        )
        if error:
            problems.append(error)
        elif reference["kind"] == "article_quote":
            quote = next(item for item in event["evidence"]
                         if item["evidence_id"] == reference["evidence_id"])
            supports = reference.get("supports")
            if any(reference.get(key) != event[key]
                   for key in ("source_id", "source_url")) or (
                reference.get("quote") != quote["quote"]
            ) or not isinstance(supports, (list, tuple)) or (
                tuple(supports) != tuple(quote["supports"])
            ) or quote["quote"] not in (event.get("content") or ""):
                problems.append("Источник или текст цитаты изменён")
        elif reference["kind"] == "company_statement":
            profile = context["company_profile"]
            statement = next(item for key in (
                "facts", "derived_metrics", "hypotheses", "sensitivities"
            ) for item in profile[key]
                if item["id"] == reference["statement_id"])
            if any(reference.get(key) != view[key]
                   for key in ("company_id", "profile_id")):
                problems.append("Ссылка ведёт к другой компании или профилю")
            if any(reference.get(key) != statement[key]
                   for key in ("epistemic_status", "data_reliability")):
                problems.append("Статус или надёжность утверждения изменены")
            validation = profile["provenance"]["validation"]
            if reference.get("verified_in_profile") is not True or not (
                validation["schema_valid"]
                and validation["evidence_refs_verified"]
            ):
                problems.append(
                    "Подтверждение утверждения профилем отсутствует"
                )
            if result["relevance"] == "grounded" and (
                statement["epistemic_status"] not in {"FACT", "DERIVED"}
            ):
                problems.append("Grounded не может опираться на гипотезу")
            if reference.get("inputs") != statement.get(
                "evidence", statement.get("inputs", [])
            ) or reference.get("caveats") != statement["caveats"]:
                problems.append("Основания или ограничения профиля изменены")
        elif reference["kind"] == "ml_view_field":
            value = view
            for part in reference["path"].split("."):
                value = value[part]
            matches = reference.get("value") == value
            if reference["path"] == "hooks.matching_profile.okved_codes":
                matches = matches or (
                    isinstance(reference.get("value"), str)
                    and reference["value"] in value
                )
            if not matches or (
                reference.get("verified_in_profile") is not False
            ):
                problems.append("Значение или статус поля MLView изменены")
    return problems
