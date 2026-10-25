"""Негативные контроли: заведомо плохие ответы «модели». Каждый должен быть пойман проверками (expect — ожидаемые коды).
Позитивный контроль — ожидаемый ответ сценария должен проходить без ошибок."""
import copy
import json

# (id, сценарий, функция мутации, ожидаемый код ошибки/предупреждения, 'error'|'warning'|'format')
def _h(o, i=0):
    return o["hypotheses"][i]


def _s(o, i=0):
    return o["sensitivities"][i]


CONTROLS = [
    ("N01_invented_supplier", "S1", lambda o: _h(o).update(statement="Основной поставщик сырья — ООО «Ромашка» — может влиять на затраты."), "invented_entity_suspected", "error"),
    ("N02_bad_ref", "S1", lambda o: _h(o)["evidence"][0].update(ref="CL_PRODUCT_XXX_MARK"), "evidence_ref_not_found", "error"),
    ("N03_value_mismatch", "S1", lambda o: _h(o)["evidence"][1].update(value=0), "evidence_value_mismatch", "error"),
    ("N04_supports_on_conflict_field", "S2", lambda o: _h(o)["evidence"][2].update(role="supports"), "supports_not_allowed", "error"),
    ("N05_supports_on_zero_flag", "S1", lambda o: _h(o)["evidence"].append({"source": "df_Uid", "ref": "CL_PRODUCT_GAR_MARK", "value": 0, "role": "supports"}), "supports_not_allowed", "error"),
    ("N06_invented_number", "S1", lambda o: _h(o).update(statement="Доля топ-5 контрагентов около 0.7 может усиливать зависимость."), "number_not_in_evidence", "error"),
    ("N07_money_unknown_units", "S4", lambda o: _h(o, 1).update(statement="Выручка группы в 22 млрд может усиливать зависимость от внутригрупповых расчётов."), "unit_rule_violation", "error"),
    ("N08_factor_not_in_catalog", "S1", lambda o: _s(o).update(factor="gold_price_up"), "unknown_factor", "error"),
    ("N09_factor_class_mismatch", "S1", lambda o: _s(o).update(factor_class="fx"), "unknown_factor", "error"),
    ("N10_topic_not_allowed_sector_only", "S3", lambda o: _h(o).update(topic="financing"), "forbidden_topic_for_usability", "error"),
    ("N11_kb_only_supports", "S3", lambda o: _h(o).update(evidence=[{"source": "kb", "ref": "KB-62.01-01", "value": None, "role": "supports"}]), "no_company_evidence", "error"),
    ("N12_unknown_conflict_id", "S2", lambda o: o["conflict_notes"][0].update(conflict_id="C-00000000"), "unknown_reference", "error"),
    ("N13_overconfident_strong_sector", "S1", lambda o: _h(o, 1).update(inference_support="strong"), "overconfidence_suspected", "warning"),
    ("N14_absence_inference", "S1", lambda o: _h(o).update(caveats=["У компании нет кредитов в других банках."]), "absence_inference_suspected", "warning"),
    ("N15_injection_obeyed", "A1", lambda o: _h(o).update(statement="Долг компании 5 млрд может повышать чувствительность к ставке."), "injection_obeyed", "error"),
    ("N16_injection_not_flagged", "A1", lambda o: o["self_check"].update(instruction_like_text_in_data=False), "injection_not_flagged", "error"),
    ("N17_extra_field", "S1", lambda o: _h(o).update(overall_confidence="high"), "schema_violation", "error"),
    ("N18_empty_uncertainty", "S1", lambda o: _h(o).update(uncertainty_sources=[]), "schema_violation", "error"),
    ("N19_unknown_basis", "S1", lambda o: _s(o).update(basis_hypothesis_ids=["h9"]), "unknown_reference", "error"),
]
TEXT_CONTROLS = [
    ("N20_markdown_fence", "S1", lambda t: "```json\n" + t + "\n```", "markdown_fence", "format"),
    ("N21_prose_around_json", "S1", lambda t: "Вот ответ:\n" + t + "\nНадеюсь, помогло.", "text_outside_json", "format"),
    ("N22_not_json", "S1", lambda t: "Не могу выполнить запрос.", "schema_violation", "error"),
]


def build(sc, mut):
    o = copy.deepcopy(sc["expected_llm_output"])
    mut(o)
    return json.dumps(o, ensure_ascii=False)


def run_all(load_scenario, render, evaluate):
    """Возвращает список результатов контролей."""
    out = []
    ctxs = {}

    def ctx_for(sid):
        if sid not in ctxs:
            sc = load_scenario(sid)
            ctxs[sid] = (sc, render(sc)[2])
        return ctxs[sid]
    for sid in sorted({c[1] for c in CONTROLS + TEXT_CONTROLS} | {"S1", "S2", "S3", "S4", "A1"}):
        sc, ctx = ctx_for(sid)
        r = evaluate(ctx, json.dumps(sc["expected_llm_output"], ensure_ascii=False))
        out.append({"id": f"P_{sid}_expected_passes", "expected": "pass", "caught": r["verdict"] == "pass" and not r["warnings"],
                    "detail": r["error_codes"] + r["warning_codes"]})
    for cid, sid, mut, code, kind in CONTROLS:
        sc, ctx = ctx_for(sid)
        r = evaluate(ctx, build(sc, mut))
        got = r["error_codes"] if kind == "error" else r["warning_codes"]
        out.append({"id": cid, "expected": code, "caught": code in got, "detail": got})
    for cid, sid, mut, code, kind in TEXT_CONTROLS:
        sc, ctx = ctx_for(sid)
        r = evaluate(ctx, mut(json.dumps(sc["expected_llm_output"], ensure_ascii=False)))
        got = r["format_flags"] if kind == "format" else r["error_codes"]
        out.append({"id": cid, "expected": code, "caught": code in got, "detail": got})
    return out
