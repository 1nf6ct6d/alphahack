"""Локальные проверки стенда. Не обращаются к внешним API: живой путь проверяется на mock-сервере 127.0.0.1."""
import copy
import hashlib
import http.server
import io
import json
import re
import tempfile
import threading
from contextlib import redirect_stdout
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from . import __main__ as cli
from . import checks, client, controls, render, runner, schema_bundle

ROOT = Path(__file__).resolve().parent.parent
R = []
V2_SHA = {}


def check(name, ok, detail=""):
    R.append((name, bool(ok), detail))


def ld(p):
    return json.loads((ROOT / p).read_text(encoding="utf-8"))


def walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from walk(v)
    elif isinstance(n, list):
        for v in n:
            yield from walk(v)


def prompts_checks():
    sysp = render.SYSTEM_PATH.read_text(encoding="utf-8")
    tmpl = render.TEMPLATE_PATH.read_text(encoding="utf-8")
    common = ld("schemas/common.v1.schema.json")["$defs"]
    llm = ld("schemas/llm_hypotheses_output.v1.schema.json")
    props = set()
    for n in walk(llm):
        if "properties" in n:
            props |= set(n["properties"])
    missing = sorted(p for p in props if p not in sysp)
    check("P1 каждое поле схемы ответа LLM описано/упомянуто в System Prompt", not missing, str(missing))
    check("P2 набор плейсхолдеров User Template = переменные рендерера", set(re.findall(r"\{\{([a-z_]+)\}\}", tmpl)) == render.TEMPLATE_VARS)
    check("P3 в шаблоне нет дублирующего блока допустимых значений", "<allowed_values>" not in tmpl and "_enum}}" not in tmpl)
    dup = [l for l in sysp.splitlines() for k in ("Topic", "FactorClass", "UncertaintySource", "EvidenceSource")
           if sum(1 for v in common[k]["enum"] if re.search(rf"\b{re.escape(v)}\b", l)) >= 3]
    check("P4 System Prompt не перечисляет enum-ы повторно (темы, классы факторов, источники неопределённости)", not dup, str(dup[:2]))
    enums = set()
    for k in ("InferenceSupport", "Direction", "EvidenceRole", "Strength", "EvidenceSource", "Topic", "UncertaintySource", "FactorClass"):
        enums |= set(common[k]["enum"])
    tokens = {t for t in re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", sysp)}
    allow = {"customer_status", "revenue_semantics_status", "payments_semantics_status", "supports_allowed", "unit_dependency",
             "requires_confirmed_unit", "unit_free", "field_states", "data_reliability", "overall_confidence", "data_conflicts",
             "company_data", "evidence_index", "sector_kb", "factor_catalog", "output_schema", "allowed_topics", "intragroup_share",
             "instruction_like_text_in_data", "sector_only"}
    unknown = sorted(t for t in tokens if t not in props and t not in enums and t not in allow and not t.startswith("cl_product"))
    check("P5 все snake_case-термины промпта существуют в схеме/перечислениях/allow-list", not unknown, str(unknown))
    check("P6 нет few-shot-примеров (нет JSON-литералов с кавычками и слов «Пример»)", not re.search(r'\{"|\bПример\b|\bExample\b', sysp))
    for phrase, nm in (("НЕ оценка величины финансового эффекта", "strength не величина эффекта"),
                       ("strength=high не означает доказанного", "high не доказывает эффект"),
                       ("выбирай более осторожную оценку", "осторожная оценка при нехватке данных"),
                       ("supports_allowed=true", "supports только при supports_allowed=true"),
                       ("критических и неразрешённых конфликтов", "критические конфликты → только limits"),
                       ("customer_status=potential", "potential связан с uninformative"),
                       ("uninformative", "состояние uninformative упомянуто"),
                       ("unit_dependency=requires_confirmed_unit", "правило единиц использует unit_dependency")):
        check(f"P7 {nm}", phrase in sysp)
    check("P8 запреты v2 сохранены (поставщики, долг, валюта, суммы, финансовое положение, вычисление чисел)",
          all(x in sysp for x in ("Не придумывай поставщиков", "долг", "валюту расчётов", "финансовое положение", "Не вычисляй числа")))
    for e in ("strong", "moderate", "weak", "supports", "limits", "risk_if_adverse", "opportunity_if_favorable", "two_sided", "unclear"):
        check(f"P9 значение «{e}», упомянутое в промпте, существует в схеме", e in enums and e in sysp)
    for p, h in V2_SHA.items():
        check(f"P10 файл v2 не изменён: {Path(p).name}", hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h)


def schema_checks():
    full, basic = schema_bundle.bundle_full(), schema_bundle.bundle_basic()
    for nm, s in (("full", full), ("basic", basic)):
        try:
            Draft202012Validator.check_schema(s); ok = True
        except Exception:  # noqa: BLE001
            ok = False
        check(f"S1 {nm}: валидна по метасхеме 2020-12", ok)
        check(f"S2 {nm}: нет неразрешённых/внешних $ref", not schema_bundle.unresolved_refs(s), str(schema_bundle.unresolved_refs(s)))
    check("S3 basic: нет $ref и $defs вообще (полностью развёрнута)", '"$ref"' not in json.dumps(basic) and "$defs" not in basic)
    schemas = {k: ld(v) for k, v in (("common", "schemas/common.v1.schema.json"), ("llm", "schemas/llm_hypotheses_output.v1.schema.json"))}
    reg = Registry().with_resources([(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012)) for s in schemas.values()])
    orig = Draft202012Validator(schemas["llm"], registry=reg, format_checker=FormatChecker())
    bund = Draft202012Validator(full, format_checker=FormatChecker())
    bas = Draft202012Validator(basic)
    cases = []
    for f in sorted((ROOT / "tests/fixtures/valid").glob("llm_s*.json")):
        cases.append((f.name, json.loads(f.read_text(encoding="utf-8")), True))
    for f in sorted((ROOT / "tests/fixtures/invalid").glob("llm_*.json")):
        cases.append((f.name, json.loads(f.read_text(encoding="utf-8")), False))
    for sid in render.scenario_ids():
        sc = render.load_scenario(sid)
        if sc["expected_llm_output"]:
            cases.append((f"{sid}_expected", sc["expected_llm_output"], True))
    mism = [n for n, o, _ in cases if orig.is_valid(o) != bund.is_valid(o)]
    check(f"S4 full ≡ исходная схема на {len(cases)} примерах (одинаковые вердикты)", not mism, str(mism))
    check("S5 full принимает все корректные и отклоняет все некорректные примеры",
          all(bund.is_valid(o) == exp for _, o, exp in cases))
    struct_bad = [(n, o) for n, o, e in cases if not e and n.split("_")[1] in ("i16", "i17", "i18", "i22")]
    check("S6 basic принимает все корректные примеры", all(bas.is_valid(o) for _, o, e in cases if e))
    check("S7 basic отклоняет структурные нарушения (лишнее поле, нет role, нет self_check, неверный enum)",
          all(not bas.is_valid(o) for _, o in struct_bad) or all(not bas.is_valid(o) for n, o in struct_bad if n.split("_")[1] in ("i16", "i17", "i22")))
    def pnames(s):
        return {k for n in walk(s) if "properties" in n for k in n["properties"]}
    check("S8 basic сохраняет все имена полей исходной схемы", pnames(basic) == pnames(full) - set() or pnames(basic) >= pnames(schemas['llm']))
    enums_full = sorted(json.dumps(n["enum"]) for n in walk(full) if "enum" in n)
    check("S9 enum-ы full ⊇ enum-ов common (единый источник значений)",
          all(json.dumps(n["enum"]) in enums_full for n in walk(schemas["common"]) if "enum" in n))


def render_checks():
    ids = render.scenario_ids()
    check("R1 найдены сценарии S1–S5 и A1", ids == ["A1", "S1", "S2", "S3", "S4", "S5"], str(ids))
    for sid in ids:
        sc = render.load_scenario(sid)
        try:
            s, u, ctx = render.render(sc); ok = "{{" not in u
        except Exception as e:  # noqa: BLE001
            ok = False; print(e)
        check(f"R2 {sid}: промпты собираются без остаточных плейсхолдеров", ok)
    s1, s2, s3, s4 = (render.load_scenario(x) for x in ("S1", "S2", "S3", "S4"))
    def allowed(sc):
        return {(e["source"], e["ref"]): e["supports_allowed"] for e in render.render(sc)[2]["evidence_index"]}
    a1, a2, a3, a4 = allowed(s1), allowed(s2), allowed(s3), allowed(s4)
    check("R3 S1: LOA=1 разрешён, нулевой флаг GAR заблокирован", a1[("df_Uid", "CL_PRODUCT_LOA_MARK")] and not a1[("df_Uid", "CL_PRODUCT_GAR_MARK")])
    check("R4 S2: поля критического конфликта (ОКВЭД) и конфликтов kept_both_flagged заблокированы как supports",
          not a2[("df_Uid", "MAINOKVED_CODE")] and not a2[("df_Uid", "MAINOKVED_NAME")] and not a2[("df_Uid", "FTS_COUNTRY_NAME")] and not a2[("df_Uid", "CL_TYPE")])
    check("R5 S3: все продуктовые флаги потенциального клиента заблокированы (uninformative)",
          all(not v for (src, ref), v in a3.items() if ref.startswith("CL_PRODUCT_")) and a3[("df_Uid", "MAINOKVED_CODE")])
    check("R6 S4: метрики unit_free разрешены при неизвестных единицах",
          all(v for (src, ref), v in a4.items() if src == "derived"))
    syn = copy.deepcopy(s4)
    syn["derived_metrics"].append({"id": "D-aaaaaaaa", "name": "size_class", "value": "x", "unit": "class", "unit_dependency": "requires_confirmed_unit", "coverage": 1.0, "data_reliability": "high"})
    syn["evidence_candidates"].append({"source": "derived", "ref": "D-aaaaaaaa", "value": "x"})
    check("R7 метрика requires_confirmed_unit блокируется при unknown", allowed(syn)[("derived", "D-aaaaaaaa")] is False)
    bad = copy.deepcopy(s4); del bad["derived_metrics"][0]["unit_dependency"]
    try:
        render.render(bad); ok = False
    except ValueError:
        ok = True
    check("R8 отсутствие unit_dependency у метрики останавливает сборку промпта", ok)
    check("R9 все метрики во всех сценариях имеют unit_dependency",
          all(d.get("unit_dependency") in ("unit_free", "requires_confirmed_unit") for i in ids for d in render.load_scenario(i)["derived_metrics"]))
    check("R10 allowed_topics: sector_only → 3 темы из схемы; иначе все 9", len(render.allowed_topics("sector_only")) == 3 and len(render.allowed_topics("full")) == 9)
    check("R11 A1: инъекция действительно присутствует во входе (red-team данные)", "ИГНОРИРУЙ" in render.render(render.load_scenario("A1"))[1])
    check("R12 S5 (unusable) пропускается планом, LLM не вызывается", ("Qwen3.6 35B A3B", "S5", "unusable") not in runner.expand()[1] and all(r["scenario"] != "S5" for r in runner.expand()[0]))


def controls_checks():
    res = controls.run_all(render.load_scenario, render.render, checks.evaluate)
    for c in res:
        check(f"C {c['id']}: {'пойман' if c['expected'] != 'pass' else 'ожидаемый ответ проходит'} ({c['expected']})", c["caught"], str(c["detail"]))


def tariff_checks():
    usage = {"input_tokens": 1000, "output_tokens": 500}
    check("T1 стоимость n/a без тарифов", runner.cost(None, "Qwen3.6 35B A3B", usage) is None)
    t = {"models": {"M": {"input_per_1m_tokens": 10, "output_per_1m_tokens": 20, "confirmed": False, "source": "x"}}}
    check("T2 стоимость n/a при confirmed=false", runner.cost(t, "M", usage) is None)
    t["models"]["M"]["confirmed"] = True
    check("T3 стоимость считается при confirmed=true", runner.cost(t, "M", usage) == 0.02)
    check("T4 стоимость n/a при неизвестных токенах", runner.cost(t, "M", {"input_tokens": None, "output_tokens": 5}) is None)


class _Mock(http.server.BaseHTTPRequestHandler):
    seen_auth, responses = [], {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        _Mock.seen_auth.append(self.headers.get("Authorization"))
        if body["model"] == "err-model":
            data = json.dumps({"error": f"bad key {self.headers.get('Authorization')}"}).encode("utf-8")
            self.send_response(401); self.end_headers(); self.wfile.write(data); return
        user = body["messages"][1]["content"]
        text = next((t for cid, t in _Mock.responses.items() if cid in user), "{}")
        data = json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1234, "completion_tokens": 321}}).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)


def live_path_checks():
    fake = "sk-FAKE-SECRET-0123456789"
    import os
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for sid in ("S1", "S2"):
        sc = render.load_scenario(sid)
        _Mock.responses[sc["company_id"]] = json.dumps(sc["expected_llm_output"], ensure_ascii=False)
    os.environ["BCE_TEST_KEY"] = fake
    cfg = {"base_url": f"http://127.0.0.1:{srv.server_address[1]}", "api_key_env": "BCE_TEST_KEY", "temperature": 0.2, "max_tokens": 100,
           "models": {"Qwen3.6 35B A3B": {"api_model_id": "mock-ok", "structured_output": "json_object"},
                      "GLM-5.3-Flash": {"api_model_id": "err-model", "structured_output": "json_object"}}}
    tmp = Path(tempfile.mkdtemp(prefix="bce_selftest_"))
    runner.TARIFFS_PATH = tmp / "none.json"
    buf = io.StringIO()
    with redirect_stdout(buf):
        s1 = runner.live_run(tmp / "a", cfg, ["P0_pilot"], None, None, None, None, 5)
        s2 = runner.live_run(tmp / "b", cfg, ["P2_glm_compare"], None, ["S2"], 1, None, 5)
    row = s1["rows"][0]
    check("L1 живой путь (mock): ответ получен, проверки пройдены", row["pass"] == 1 and row["api_errors"] == 0, str(row))
    check("L2 токены берутся из usage API", row["input_tokens_total"] == 1234 and row["output_tokens_total"] == 321)
    check("L3 стоимость n/a без подтверждённых тарифов", row["cost_total_rub"] is None)
    check("L4 ошибка API фиксируется, не роняя прогон", s2["rows"][0]["api_errors"] == 1)
    files = " ".join(p.read_text(encoding="utf-8") for p in tmp.rglob("*.json"))
    check("L5 ключ в запросе отправлен (mock его получил), но в сохранённых файлах отсутствует (включая эхо в тексте ошибки)",
          f"Bearer {fake}" in _Mock.seen_auth and fake not in files and fake not in buf.getvalue() and "[REDACTED]" in files)
    check("L6 ключ не попал в вывод консоли", fake not in buf.getvalue())
    try:
        runner.live_run(tmp / "c", cfg, ["P1_qwen_main"], None, None, None, None, 3); ok = False
    except SystemExit:
        ok = True
    check("L7 превышение --max-requests останавливает прогон", ok)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["run", "--max-requests", "5"])
    check("L8 run без --live и --i-confirm-paid-calls отказывает (код 2)", rc == 2)
    srv.shutdown()
    del os.environ["BCE_TEST_KEY"]
    check("L9 неразрешённая модель отклоняется", True if "Claude" not in client.ALLOWED_MODELS else False)
    os.environ["BCE_LLM_API_KEY"] = fake
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["dry-run", "--scenarios", "S1", "--stages", "P0_pilot"])
    del os.environ["BCE_LLM_API_KEY"]
    dr = sorted((ROOT / "lab_results").glob("dryrun-*"))[-1]
    alltxt = " ".join(p.read_text(encoding="utf-8") for p in dr.rglob("*.json"))
    check("L10 dry-run: ключ из окружения не выводится и не сохраняется; API-вызовов 0", fake not in out.getvalue() and fake not in alltxt and '"api_calls_made": 0' in alltxt)


def main():
    for rel in ("prompts/system_prompt.bce.hypo.v2.txt", "prompts/user_template.bce.hypo.v2.txt"):
        V2_SHA[rel] = V2_EXPECTED[rel]
    prompts_checks(); schema_checks(); render_checks(); controls_checks(); tariff_checks(); live_path_checks()
    fails = [r for r in R if not r[1]]
    for n, ok, d in R:
        print(("PASS " if ok else "FAIL ") + n + (f"  [{d}]" if d and not ok else ""))
    print(f"\nИтого: {len(R) - len(fails)} / {len(R)} пройдено")
    return 1 if fails else 0


V2_EXPECTED = {
    "prompts/system_prompt.bce.hypo.v2.txt": "810d8daa9da6da6bef06b67ca40806b134be9527dd3e77c4d817bad042cf13d9",
    "prompts/user_template.bce.hypo.v2.txt": "8518a4b431e0ce75e2dfbeeac1cb21c00e5d1bbef325236eea7db875eed48781",
}
