"""Проверки контрактов BCE v0.2. Запуск: .venv/bin/python tests/validate_all.py
Не вызывает LLM и не реализует движок: проверяет схемы, согласованность промпта со схемой и
соответствие вручную составленных ожидаемых профилей пяти сценариев контрактам."""
import copy, json, re, sys
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import contract_rules as CR
import scenarios as SC

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))


def load(p):
    return json.loads((ROOT / p).read_text(encoding="utf-8"))


SCHEMA_FILES = {
    "common": "schemas/common.v1.schema.json",
    "llm": "schemas/llm_hypotheses_output.v1.schema.json",
    "profile": "schemas/company_business_profile.v1.schema.json",
    "views": "schemas/profile_views.v1.schema.json",
}
schemas = {k: load(v) for k, v in SCHEMA_FILES.items()}
registry = Registry().with_resources(
    [(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012)) for s in schemas.values()])


def validator(key, sub=None):
    s = schemas[key] if sub is None else {"$ref": f"{schemas[key]['$id']}#/$defs/{sub}"}
    return Draft202012Validator(s, registry=registry, format_checker=FormatChecker())


def errs(v, inst):
    return sorted(v.iter_errors(inst), key=lambda e: list(e.path))


# ---------------- A. Метасхема и lint ----------------
for k, p in SCHEMA_FILES.items():
    raw = (ROOT / p).read_text(encoding="utf-8")
    try:
        Draft202012Validator.check_schema(schemas[k]); ok = True
    except Exception as e:  # noqa
        ok = False; print(e)
    check(f"A1 {k}: валидна по метасхеме Draft 2020-12", ok)
    check(f"A2 {k}: нет комментариев вне JSON (/* */ или //)", not re.search(r'/\*|\*/|^\s*//', raw, re.M))
    check(f"A3 {k}: $schema = draft/2020-12", schemas[k]["$schema"] == "https://json-schema.org/draft/2020-12/schema")


def walk(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}/{i}")


bad = []
for k, s in schemas.items():
    for path, n in walk(s):
        for comb in ("allOf", "anyOf", "oneOf"):
            if comb in n and isinstance(n[comb], list):
                for i, m in enumerate(n[comb]):
                    if isinstance(m, dict) and "properties" in m and n.get("additionalProperties") is False:
                        bad.append(f"{k}{path}/{comb}/{i}")
check("A4 нет allOf/anyOf/oneOf-членов с properties рядом с additionalProperties:false (баг v0.1)", not bad, str(bad))

# подтверждаем баг v0.1 и отсутствие его в v0.2
v01 = {"$schema": "https://json-schema.org/draft/2020-12/schema",
       "$defs": {"Statement": {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {"id": {"type": "string"}}}},
       "allOf": [{"$ref": "#/$defs/Statement"}, {"properties": {"category": {"type": "string"}}}]}
check("A5 регрессия: конструкция v0.1 действительно отклоняет корректный объект {id, category}",
      not Draft202012Validator(v01).is_valid({"id": "x", "category": "c"}))

# ---------------- B. Корректные и некорректные примеры ----------------
scen = [f() for f in SC.ALL]
valid_dir = ROOT / "tests/fixtures/valid"; invalid_dir = ROOT / "tests/fixtures/invalid"; scen_dir = ROOT / "tests/fixtures/scenarios"
for d in (valid_dir, invalid_dir, scen_dir):
    for f in d.glob("*.json"):
        f.unlink()


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


vp, vl = validator("profile"), validator("llm")
for i, s in enumerate(scen, 1):
    dump(valid_dir / f"profile_s{i}.json", s["profile"])
    check(f"B1 S{i} профиль валиден", vp.is_valid(s["profile"]), "; ".join(e.message[:120] for e in errs(vp, s["profile"])[:3]))
    if s["llm_output"] is not None:
        dump(valid_dir / f"llm_s{i}.json", s["llm_output"])
        check(f"B2 S{i} ответ LLM валиден", vl.is_valid(s["llm_output"]), "; ".join(e.message[:120] for e in errs(vl, s["llm_output"])[:3]))
        check(f"B3 S{i} ответ LLM не содержит кодовых полей (data_reliability/overall_confidence/epistemic_status)",
              not re.search(r'"(data_reliability|overall_confidence|epistemic_status)"', json.dumps(s["llm_output"])))
    dump(scen_dir / f"s{i}_input.json", {k if isinstance(k, str) else k: v for k, v in s["input"].items()})
    dump(scen_dir / f"s{i}_expected_profile.json", s["profile"])
    if s["llm_output"] is not None:
        dump(scen_dir / f"s{i}_expected_llm_output.json", s["llm_output"])

S1, S2, S3, S4, S5 = scen


def mut(base, fn):
    o = copy.deepcopy(base); fn(o); return o


INVALID = [
    ("profile", "i01_extra_prop_in_hypothesis", S1["profile"], lambda o: o["hypotheses"][0].update(foo=1)),
    ("profile", "i02_hypothesis_status_FACT", S1["profile"], lambda o: o["hypotheses"][0].update(epistemic_status="FACT")),
    ("profile", "i03_empty_evidence", S1["profile"], lambda o: o["hypotheses"][0].update(evidence=[])),
    ("profile", "i04_empty_uncertainty", S1["profile"], lambda o: o["hypotheses"][0].update(uncertainty_sources=[])),
    ("profile", "i05_unusable_with_hypotheses", S1["profile"], lambda o: (o["input_quality"].update(usability="unusable"), o["meta"].update(pipeline_mode="no_inference"))),
    ("profile", "i06_no_inference_but_full", S5["profile"], lambda o: o["input_quality"].update(usability="full")),
    ("profile", "i07_sector_only_financing_topic", S3["profile"], lambda o: o["hypotheses"][0].update(topic="financing")),
    ("profile", "i08_llm_mode_without_models", S1["profile"], lambda o: o["meta"].update(models_used=[])),
    ("profile", "i09_bad_hypothesis_id", S1["profile"], lambda o: o["hypotheses"][0].update(id="H-xyz")),
    ("profile", "i10_bad_date", S1["profile"], lambda o: o["meta"].update(as_of_date="2026-13-45")),
    ("profile", "i11_kb_fallback_no_reason", S1["profile"], lambda o: o["meta"].update(pipeline_mode="kb_fallback", degradation_reasons=[], models_used=[])),
    ("profile", "i12_derived_value_object", S1["profile"], lambda o: o["derived_metrics"][0].update(value={"a": 1})),
    ("profile", "i13_unknown_model", S1["profile"], lambda o: o["meta"]["models_used"][0].update(model="Claude")),
    ("profile", "i14_partial_without_dropped", S1["profile"], lambda o: o["meta"].update(pipeline_mode="llm_partially_validated")),
    ("profile", "i15_unknown_field_state", S1["profile"], lambda o: o["input_quality"]["field_states"]["ORG_TYPE"].update(state="maybe")),
    ("llm", "i16_overall_confidence_in_llm", S1["llm_output"], lambda o: o["hypotheses"][0].update(overall_confidence="high")),
    ("llm", "i17_evidence_without_role", S1["llm_output"], lambda o: o["hypotheses"][0]["evidence"][0].pop("role")),
    ("llm", "i18_stable_id_as_basis", S1["llm_output"], lambda o: o["sensitivities"][0].update(basis_hypothesis_ids=["H-00000000"])),
    ("llm", "i19_nine_hypotheses", S1["llm_output"], lambda o: o.update(hypotheses=[copy.deepcopy(o["hypotheses"][0]) for _ in range(9)])),
    ("llm", "i20_bad_factor_slug", S1["llm_output"], lambda o: o["sensitivities"][0].update(factor="Key Rate")),
    ("llm", "i21_empty_uncertainty", S1["llm_output"], lambda o: o["hypotheses"][1].update(uncertainty_sources=[])),
    ("llm", "i22_no_self_check", S1["llm_output"], lambda o: o.pop("self_check")),
]
for kind, name, base, fn in INVALID:
    o = mut(base, fn); dump(invalid_dir / f"{kind}_{name}.json", o)
    check(f"B4 некорректный пример отклонён: {name}", not (vp if kind == "profile" else vl).is_valid(o))

# ---------------- C. Представления ----------------
LABEL = {"FACT": "Факт из данных", "DERIVED": "Расчёт", "HYPOTHESIS": "Гипотеза AI"}


def ml_view(p):
    return {"schema_version": "1.0.0", "company_id": p["meta"]["company_id"], "profile_id": p["meta"]["profile_id"],
            "as_of_date": p["meta"]["as_of_date"], "usability": p["input_quality"]["usability"],
            "weight_semantics": "ordinal_prior_not_probability", "hooks": p["downstream_hooks"],
            "numeric_features": [{"derived_id": d["id"], "name": d["name"], "value": d["value"] if isinstance(d["value"], (int, float)) else None,
                                  "coverage": d["coverage"], "data_reliability": d["data_reliability"]} for d in p["derived_metrics"]]}


def interp_view(p):
    st = [{"id": f["id"], "epistemic_status": "FACT", "text": f["statement"], "data_reliability": f["data_reliability"],
           "overall_confidence": None, "uncertainty_sources": [], "evidence": f["evidence"]} for f in p["facts"]]
    st += [{"id": d["id"], "epistemic_status": "DERIVED", "text": f"{d['name']} = {d['value']}", "data_reliability": d["data_reliability"],
            "overall_confidence": None, "uncertainty_sources": [], "evidence": d["inputs"]} for d in p["derived_metrics"]]
    st += [{"id": h["id"], "epistemic_status": "HYPOTHESIS", "text": h["statement"], "data_reliability": h["data_reliability"],
            "overall_confidence": h["overall_confidence"], "uncertainty_sources": h["uncertainty_sources"], "evidence": h["evidence"]} for h in p["hypotheses"]]
    st += [{"id": s["id"], "epistemic_status": "HYPOTHESIS", "text": s["transmission_channel"], "data_reliability": s["data_reliability"],
            "overall_confidence": s["overall_confidence"], "uncertainty_sources": s["uncertainty_sources"], "evidence": s["evidence"]} for s in p["sensitivities"]]
    return {"schema_version": "1.0.0", "company_id": p["meta"]["company_id"], "profile_id": p["meta"]["profile_id"],
            "usability": p["input_quality"]["usability"], "statements": st,
            "constraints": p["downstream_hooks"]["interpretation_constraints"]}


def ui_view(p):
    def evs(lst):
        return [{"source": e["source"], "ref": e["ref"], "label": e["ref"], "value_text": str(e["value"]), "role": e["role"]} for e in lst]

    def item(i, label, title, text, conf, unc, ev):
        return {"id": i, "label": label, "title": title[:120], "text": text[:400], "confidence": conf, "uncertainty_note": unc, "evidence": evs(ev)}
    sections = [
        {"kind": "facts", "items": [item(f["id"], LABEL["FACT"], f["statement"], f["statement"], None, None, f["evidence"]) for f in p["facts"]]},
        {"kind": "derived", "items": [item(d["id"], LABEL["DERIVED"], d["name"], f"{d['name']} = {d['value']}", None, None, d["inputs"]) for d in p["derived_metrics"]]},
        {"kind": "hypotheses", "items": [item(h["id"], LABEL["HYPOTHESIS"], h["statement"], h["statement"], h["overall_confidence"],
                                              ", ".join(h["uncertainty_sources"]), h["evidence"]) for h in p["hypotheses"]]},
        {"kind": "conflicts", "items": [item(c["id"], "Противоречие в данных", c["rule_id"], c["description"], None, None, c["fields"]) for c in p["data_conflicts"]]},
        {"kind": "missing", "items": [item(m["id"], "Нет данных", m["feature"], m["kind"], None, m["request_suggestion"], []) for m in p["missing_data"]]},
    ]
    return {"schema_version": "1.0.0", "company_id": p["meta"]["company_id"], "profile_id": p["meta"]["profile_id"],
            "as_of_date": p["meta"]["as_of_date"],
            "banner": {"usability": p["input_quality"]["usability"], "text": None if p["input_quality"]["usability"] == "full" else "Профиль построен по неполным данным"},
            "sections": sections}


for i, s in enumerate(scen, 1):
    p = s["profile"]
    for nm, fn, sub in (("MLView", ml_view, "MLView"), ("InterpreterView", interp_view, "InterpreterView"), ("UIView", ui_view, "UIView")):
        v = fn(p)
        dump(scen_dir / f"s{i}_{nm}.json", v)
        vv = validator("views", sub)
        check(f"C1 S{i} {nm} валиден", vv.is_valid(v), "; ".join(e.message[:120] for e in errs(vv, v)[:3]))
bad_ui = ui_view(S1["profile"]); bad_ui["sections"][0]["items"][0]["label"] = "Гипотеза"
check("C2 UIView с неизвестной меткой отклонён", not validator("views", "UIView").is_valid(bad_ui))
bad_ml = ml_view(S1["profile"]); bad_ml["weight_semantics"] = "probability"
check("C3 MLView с weight_semantics=probability отклонён", not validator("views", "MLView").is_valid(bad_ml))

# ---------------- D. Промпт ↔ схема ----------------
prompt = (ROOT / "prompts/system_prompt.bce.hypo.v2.txt").read_text(encoding="utf-8")
tmpl = (ROOT / "prompts/user_template.bce.hypo.v2.txt").read_text(encoding="utf-8")
common = schemas["common"]["$defs"]
llm = schemas["llm"]
all_props = set(); all_enums = set()
for sk in schemas:
    for _, n in walk(schemas[sk]):
        if "properties" in n:
            all_props |= set(n["properties"])
        if "enum" in n:
            all_enums |= {x for x in n["enum"] if isinstance(x, str)}
top_props = set(llm["properties"])
fmt_section = prompt.split("# Формат ответа")[1].split("# Самопроверка")[0]
check("D1 верхние поля ответа в промпте = верхние поля схемы LLM", set(re.findall(r"`([a-z_]+)`", fmt_section)) == top_props)
llm_props = set()
for _, n in walk(llm):
    if "properties" in n:
        llm_props |= set(n["properties"])
check("D2 LLM-схема не содержит кодовых полей data_reliability/overall_confidence/epistemic_status/generated_by/id",
      not ({"data_reliability", "overall_confidence", "epistemic_status", "generated_by", "id"} & llm_props), str(llm_props))
check("D3 промпт явно запрещает LLM выставлять data_reliability и overall_confidence",
      re.search(r"НЕ оцениваешь достоверность.*data_reliability и overall_confidence вычисляет код", prompt, re.S) is not None)
sector_line = [l for l in prompt.splitlines() if l.strip().startswith("14.")][0]
then_topics = None
for blk in schemas["profile"]["allOf"]:
    iq = blk["if"]["properties"].get("input_quality")
    if iq and iq["properties"]["usability"].get("const") == "sector_only":
        then_topics = set(blk["then"]["properties"]["hypotheses"]["items"]["properties"]["topic"]["enum"])
check("D4 темы режима sector_only в промпте = темы в схеме профиля",
      then_topics == {t for t in common["Topic"]["enum"] if t in sector_line}, f"{then_topics}")
ALLOW = {"revenue_semantics_status", "payments_semantics_status", "data_reliability", "overall_confidence", "unit_dependency",
         "unit_free", "company_data", "evidence_index", "sector_kb", "factor_catalog", "allowed_values", "output_schema",
         "data_conflicts", "intragroup_share", "instruction_like_text_in_data", "local_id", "evidence_index"}
tokens = set(re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", prompt))
unknown = sorted(t for t in tokens if t not in all_props and t not in all_enums and t not in ALLOW and not t.startswith("cl_product"))
check("D5 все snake_case-термины промпта известны схеме или явному allow-list (нет дрейфа имён)", not unknown, str(unknown))
for name, enum in (("inference_support", common["InferenceSupport"]["enum"]), ("uncertainty_sources", common["UncertaintySource"]["enum"]),
                   ("direction", common["Direction"]["enum"]), ("strength", common["Strength"]["enum"]),
                   ("evidence.role", common["EvidenceRole"]["enum"]), ("evidence.source", common["EvidenceSource"]["enum"]),
                   ("topic", common["Topic"]["enum"]), ("factor_class", common["FactorClass"]["enum"])):
    check(f"D6 в шаблоне есть плейсхолдер перечисления для {name}", "{{" + name.replace("evidence.", "evidence_").replace("uncertainty_sources", "uncertainty_source") + "_enum}}" in tmpl
          or "{{" + name + "_enum}}" in tmpl)
for e in common["InferenceSupport"]["enum"]:
    check(f"D7 промпт определяет уровень inference_support={e}", f"• {e} —" in prompt)
check("D8 промпт ссылается только на роли supports/limits из схемы", {"supports", "limits"} == set(common["EvidenceRole"]["enum"]) and "role=supports" in prompt and "role=limits" in prompt)
ph = set(re.findall(r"\{\{([a-z_]+)\}\}", tmpl))
RENDER_VARS = {"prompt_version", "as_of_date", "customer_status", "usability", "revenue_semantics_status", "payments_semantics_status",
               "company_data_json", "evidence_index_json", "kb_version", "sector_kb_entries_json", "factor_catalog_json", "topic_enum",
               "factor_class_enum", "uncertainty_source_enum", "inference_support_enum", "direction_enum", "strength_enum",
               "evidence_role_enum", "evidence_source_enum", "llm_output_schema_json", "validator_errors_or_empty"}
check("D9 все плейсхолдеры шаблона перечислены в таблице переменных спецификации", ph <= RENDER_VARS and RENDER_VARS <= ph, str(ph ^ RENDER_VARS))
for kw in ("Не придумывай поставщиков", "долг", "валюту расчётов", "финансовое положение", "Не вычисляй числа"):
    check(f"D10 сохранён запрет: «{kw}»", kw in prompt)

# ---------------- E. Сценарии ↔ контракты ----------------
EXPECT = {"S1 эталон": ("full", "llm_validated"), "S2 противоречия": ("limited", "llm_validated"),
          "S3 разреженный": ("sector_only", "llm_validated"), "S4 группа": ("full", "llm_validated"),
          "S5 шум и инъекция": ("unusable", "no_inference")}
rules = {r["rule_id"]: r for r in load("rules/conflict_rules.v2.json")["rules"]}
company_src = {"df_Uid", "link_individuals", "payments", "derived"}


def all_evidence(s):
    p = s["profile"]
    for f in p["facts"]:
        yield f["id"], f["evidence"]
    for d in p["derived_metrics"]:
        yield d["id"], d["inputs"]
    for h in p["hypotheses"]:
        yield h["id"], h["evidence"]
    for x in p["sensitivities"]:
        yield x["id"], x["evidence"]
    for c in p["data_conflicts"]:
        yield c["id"], c["fields"]


for s in scen:
    nm = s["name"]; p = s["profile"]; idx = s["evidence_index"]
    uc, pm = EXPECT[nm]
    check(f"E1 {nm}: usability={uc}, pipeline_mode={pm}", p["input_quality"]["usability"] == uc and p["meta"]["pipeline_mode"] == pm)
    bad = [(i, e["ref"]) for i, evl in all_evidence(s) for e in evl
           if (e["source"], e["ref"]) not in idx or idx[(e["source"], e["ref"])] != e["value"]]
    check(f"E2 {nm}: все evidence найдены во входе/индексе с совпадающим value", not bad, str(bad[:3]))
    drel = {d["id"]: d["data_reliability"] for d in p["derived_metrics"]}

    def rel(e):
        return drel[e["ref"]] if e["source"] == "derived" else s["ref_reliability"].get(e["ref"], "high")
    ok_conf, ok_ev, ok_num, ok_flag, ok_unit = True, True, True, True, True
    oc = {}
    for h in p["hypotheses"]:
        sup = [e for e in h["evidence"] if e["role"] == "supports" and e["source"] in company_src]
        ok_ev &= bool(sup)
        dr = CR.lmin(*[rel(e) for e in sup]) if sup else "low"
        ok_conf &= (h["data_reliability"] == dr and h["overall_confidence"] == CR.hypothesis_confidence(dr, h["inference_support"], h["uncertainty_sources"]))
        oc[h["id"]] = h["overall_confidence"]
    for x in p["sensitivities"]:
        sup = [e for e in x["evidence"] if e["role"] == "supports" and e["source"] in company_src]
        ok_ev &= bool(sup) and all(b in oc for b in x["basis_hypothesis_ids"])
        dr = CR.lmin(*[rel(e) for e in sup]) if sup else "low"
        ok_conf &= (x["data_reliability"] == dr and x["overall_confidence"] ==
                    CR.sensitivity_confidence([oc[b] for b in x["basis_hypothesis_ids"]], dr, x["uncertainty_sources"]))
        ok_ev &= x["factor"] in s["catalog"]
    for it in p["hypotheses"] + p["sensitivities"]:
        text = it.get("statement", "") + " " + it.get("transmission_channel", "")
        nums = re.findall(r"\d+(?:[.,]\d+)?", text)
        vals = " ".join(str(e["value"]) for e in it["evidence"])
        ok_num &= all(n in vals for n in nums)
        for e in it["evidence"]:
            if e["role"] == "supports" and e["ref"].startswith("CL_PRODUCT_") and e["value"] == 0:
                ok_flag = False
            if e["role"] == "supports" and e["ref"].startswith(("CL_PRODUCT_", "FACTORING")) and p["meta"]["customer_status"] == "potential":
                ok_flag = False
        if it.get("topic") and p["input_quality"]["usability"] == "sector_only":
            ok_ev &= it["topic"] in ("business_model", "demand_side", "regulation_exposure")
    if p["input_quality"]["revenue_semantics_status"] == "unknown" or p["input_quality"]["payments_semantics_status"] == "unknown":
        dumped = json.dumps([p["hypotheses"], p["sensitivities"]], ensure_ascii=False)
        ok_unit &= not re.search(r"млрд|₽|руб|\b\d{9,}\b", dumped)
        reqd = {d["id"] for d in p["derived_metrics"] if d["unit_dependency"] == "requires_confirmed_unit" and d["value"] is not None}
        ok_unit &= not reqd and not any(e["ref"] in reqd for _, evl in all_evidence(s) for e in evl)
    check(f"E3 {nm}: confidence воспроизводится по правилам (data_reliability, inference_support, uncertainty)", ok_conf)
    check(f"E4 {nm}: у каждого утверждения есть supports-evidence из данных компании; factor из каталога; sector_only-темы", ok_ev)
    check(f"E5 {nm}: числа в тексте гипотез есть в evidence (нет вычисленных/выдуманных)", ok_num)
    check(f"E6 {nm}: нулевые флаги банка и флаги потенциального клиента не используются как supports", ok_flag)
    check(f"E7 {nm}: правила единиц (нет денежных сумм/порогов при неизвестной семантике)", ok_unit)
    rids = {c["rule_id"] for c in p["data_conflicts"]}
    check(f"E8 {nm}: rule_id конфликтов есть в реестре v0.2", rids <= set(rules), str(rids - set(rules)))
    mentioned = {e["ref"] for c in p["data_conflicts"] for e in c["fields"]}
    check(f"E9 {nm}: РКО и порог выручки 15 млрд не используются как основание конфликта", not ({"CL_PRODUCT_RKO_MARK", "REVENUE_COMPANY"} & mentioned))
    if s["llm_output"]:
        cids = {c["id"] for c in p["data_conflicts"]}
        lids = {h["local_id"] for h in s["llm_output"]["hypotheses"]} | {x["local_id"] for x in s["llm_output"]["sensitivities"]}
        ok = all(n["conflict_id"] in cids and set(n["affected_local_ids"]) <= lids for n in s["llm_output"]["conflict_notes"])
        ok &= set(p["provenance"]["local_id_map"]) == lids
        ok &= all(set(x["basis_hypothesis_ids"]) <= lids for x in s["llm_output"]["sensitivities"])
        check(f"E10 {nm}: ссылки conflict_notes/local_id согласованы между LLM-ответом и профилем", ok)
    hit = [h for h in p["hypotheses"] if any(e["ref"] in {x["ref"] for c in p["data_conflicts"] if c["severity"] == "critical" for x in c["fields"]}
                                               and e["role"] == "supports" for e in h["evidence"]) and h["overall_confidence"] != "low"]
    check(f"E11 {nm}: нет гипотез с confidence>low, опирающихся на поле из critical-конфликта", not hit)
check("E12 S5: текст-инъекция не попал в профиль", "Игнорируй" not in json.dumps(S5["profile"], ensure_ascii=False) and S5["llm_output"] is None)
check("E13 S5: нет гипотез при unusable", S5["profile"]["hypotheses"] == [] and S5["profile"]["sensitivities"] == [])
check("E14 S2: в реестре конфликтов нет правил, удалённых из v0.1",
      not ({"CONF-REV-THRESHOLD", "CONF-PROD-TYPE", "CONF-AGE-STAGE"} & {c["rule_id"] for c in S2["profile"]["data_conflicts"]}))
check("E15 расчётные веса relevance_priors = strength×confidence (самосогласованность, не калибровка)",
      all(abs(r["weight"] - CR.weight(next(x for x in s["profile"]["sensitivities"] if x["id"] == r["sensitivity_id"])["strength"], r["overall_confidence"])) < 1e-9
          for s in scen for r in s["profile"]["downstream_hooks"]["relevance_priors"]))

# ---------------- Итог ----------------
fails = [r for r in RESULTS if not r[1]]
for n, ok, d in RESULTS:
    print(("PASS " if ok else "FAIL ") + n + (f"  [{d}]" if (d and not ok) else ""))
print(f"\nИтого: {len(RESULTS) - len(fails)} / {len(RESULTS)} пройдено")
sys.exit(1 if fails else 0)
