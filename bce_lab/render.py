"""Сборка System/User Prompt из сценария. Не вызывает LLM."""
import json
import re
from pathlib import Path

from . import schema_bundle

ROOT = Path(__file__).resolve().parent.parent
PROMPT_VERSION = "bce.hypo.v3"
SYSTEM_PATH = ROOT / "prompts" / "system_prompt.bce.hypo.v3.txt"
TEMPLATE_PATH = ROOT / "prompts" / "user_template.bce.hypo.v3.txt"
TEMPLATE_VARS = {"prompt_version", "as_of_date", "customer_status", "usability", "revenue_semantics_status",
                 "payments_semantics_status", "allowed_topics", "company_data_json", "evidence_index_json", "kb_version",
                 "sector_kb_entries_json", "factor_catalog_json", "llm_output_schema_json", "validator_errors_or_empty"}
AS_OF_DATE = "2026-10-20"
SECTOR_ONLY_TOPICS_SRC = "schemas/company_business_profile.v1.schema.json"


def jdump(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def load_scenario(sid):
    return load_json(ROOT / "bce_lab" / "scenarios" / f"{sid}.json")


def scenario_ids():
    return sorted(p.stem for p in (ROOT / "bce_lab" / "scenarios").glob("*.json"))


def load_catalog():
    return load_json(ROOT / "bce_lab" / "data" / "factor_catalog.json")


def load_kb():
    return load_json(ROOT / "bce_lab" / "data" / "kb_stub.json")


def allowed_topics(usability):
    """Единый источник: перечень тем из common-схемы; для sector_only — из if/then схемы профиля."""
    all_topics = load_json(ROOT / "schemas/common.v1.schema.json")["$defs"]["Topic"]["enum"]
    if usability != "sector_only":
        return list(all_topics)
    prof = load_json(ROOT / SECTOR_ONLY_TOPICS_SRC)
    for blk in prof["allOf"]:
        iq = blk["if"]["properties"].get("input_quality")
        if iq and iq["properties"]["usability"].get("const") == "sector_only":
            return list(blk["then"]["properties"]["hypotheses"]["items"]["properties"]["topic"]["enum"])
    raise RuntimeError("Не найден блок sector_only в схеме профиля")


def blocked_refs(sc):
    """Поля из конфликтов, которые нельзя использовать как supports: severity=critical или resolution != preferred_primary_field."""
    out = {}
    for c in sc["data_conflicts"]:
        if c["severity"] == "critical" or c["resolution"] != "preferred_primary_field":
            for f in c["fields"]:
                out[(f["source"], f["ref"])] = f"conflict:{c['rule_id']}"
    return out


def build_evidence_index(sc):
    """Возвращает список записей {source, ref, value, supports_allowed} и словарь причин блокировки."""
    blocked = blocked_refs(sc)
    states = sc["field_states"]
    deps = {d["id"]: d for d in sc["derived_metrics"]}
    missing_feats = {m["feature"] for m in sc["missing_data"]}
    sem_unknown = sc["revenue_semantics_status"] == "unknown" or sc["payments_semantics_status"] == "unknown"
    index, reasons = [], {}
    for cand in sc["evidence_candidates"]:
        src, ref, val = cand["source"], cand["ref"], cand["value"]
        if src == "kb" and ref not in sc["kb_ids"]:
            continue
        if val is None and not (src == "kb" or ref in missing_feats):
            continue
        allowed, why = True, None
        if (src, ref) in blocked:
            allowed, why = False, blocked[(src, ref)]
        elif src == "df_Uid":
            st = states.get(ref, {"state": "value"})["state"]
            if st != "value":
                allowed, why = False, f"field_state:{st}"
        elif src == "derived":
            d = deps.get(ref)
            if d is None:
                raise ValueError(f"derived {ref} без описания метрики")
            if "unit_dependency" not in d or d["unit_dependency"] not in ("unit_free", "requires_confirmed_unit"):
                raise ValueError(f"У метрики {ref} нет корректного unit_dependency")
            if d["unit_dependency"] == "requires_confirmed_unit" and sem_unknown:
                allowed, why = False, "unit_unknown"
        if isinstance(val, str) and len(val) > 200:
            val = val[:200]
        index.append({"source": src, "ref": ref, "value": val, "supports_allowed": allowed})
        if why:
            reasons[(src, ref)] = why
    return index, reasons


def check_unit_dependency(sc):
    bad = [d["id"] for d in sc["derived_metrics"] if d.get("unit_dependency") not in ("unit_free", "requires_confirmed_unit")]
    if bad:
        raise ValueError(f"Нет unit_dependency у метрик: {bad}")


def build_company_data(sc):
    nontrivial = {k: v for k, v in sc["field_states"].items() if v["state"] != "value"}
    return {"company_id": sc["company_id"], "industry_identification": sc["industry_identification"],
            "signal_groups": sc["signal_groups"], "target_segment_applicability": sc["target_segment_applicability"],
            "facts": sc["facts"], "derived_metrics": sc["derived_metrics"], "data_conflicts": sc["data_conflicts"],
            "missing_data": sc["missing_data"], "field_states_nonvalue": nontrivial}


def build_kb_entries(sc):
    kb = load_kb()["entries"]
    out = []
    for kid in sc["kb_ids"]:
        e = dict(kb[kid])
        if kid in sc.get("kb_overrides", {}):
            e["summary"] = sc["kb_overrides"][kid]
        out.append({"id": kid, **e})
    return out


def render_template(tmpl, values):
    found = set(re.findall(r"\{\{([a-z_]+)\}\}", tmpl))
    if found != TEMPLATE_VARS:
        raise ValueError(f"Набор плейсхолдеров шаблона не совпадает: {found ^ TEMPLATE_VARS}")
    missing = found - set(values)
    if missing:
        raise ValueError(f"Нет значений для: {missing}")
    out = tmpl
    for k in found:
        out = out.replace("{{" + k + "}}", str(values[k]))
    if re.search(r"\{\{[a-z_]+\}\}", out):
        raise ValueError("В итоговом промпте остались необработанные плейсхолдеры")
    return out


def render(sc, schema_mode="prompt", retry_feedback=""):
    """schema_mode: 'prompt' — схема встроена в промпт; 'api' — схема передаётся через response_format."""
    check_unit_dependency(sc)
    index, reasons = build_evidence_index(sc)
    catalog = load_catalog()
    schema = schema_bundle.bundle_full()
    schema_text = schema_bundle.compact(schema) if schema_mode == "prompt" else "(схема передана через параметры API)"
    kb_entries = build_kb_entries(sc)
    values = {
        "prompt_version": PROMPT_VERSION, "as_of_date": AS_OF_DATE, "customer_status": sc["customer_status"],
        "usability": sc["usability"], "revenue_semantics_status": sc["revenue_semantics_status"],
        "payments_semantics_status": sc["payments_semantics_status"],
        "allowed_topics": ", ".join(allowed_topics(sc["usability"])),
        "company_data_json": jdump(build_company_data(sc)), "evidence_index_json": jdump(index),
        "kb_version": load_kb()["version"], "sector_kb_entries_json": jdump(kb_entries),
        "factor_catalog_json": jdump(catalog["factors"]), "llm_output_schema_json": schema_text,
        "validator_errors_or_empty": retry_feedback,
    }
    system = SYSTEM_PATH.read_text(encoding="utf-8").rstrip("\n")
    user = render_template(TEMPLATE_PATH.read_text(encoding="utf-8"), values)
    ctx = {"scenario": sc, "evidence_index": index, "blocked_reasons": reasons, "catalog": {f["slug"]: f["factor_class"] for f in catalog["factors"]},
           "allowed_topics": allowed_topics(sc["usability"]), "kb_ids": sc["kb_ids"],
           "conflict_ids": {c["id"] for c in sc["data_conflicts"]}, "system_prompt": system,
           "kb_texts": " ".join(e["summary"] for e in kb_entries)}
    return system, user, ctx
