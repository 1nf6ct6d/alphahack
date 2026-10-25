"""Планирование, dry-run и (после явного разрешения) живой прогон. Живые вызовы платные: нужны два флага и лимит."""
import datetime as dt
import itertools
import json
import os
import re
import sys
from pathlib import Path

from . import checks, client, controls, render, schema_bundle

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "bce_lab" / "lab.config.json"
TARIFFS_PATH = ROOT / "bce_lab" / "tariffs.json"
PLAN_PATH = ROOT / "experiments" / "plan.json"


def slug(s):
    return re.sub(r"[^A-Za-z0-9.]+", "_", s)


def load_plan():
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def expand(stages=None, models=None, scenarios=None, repeats=None):
    """Список запросов (model, scenario, repeat, stage). S5 и прочие usability=unusable пропускаются по замыслу."""
    plan = load_plan()
    reqs, skipped = [], []
    for st in plan["stages"]:
        if stages and st["id"] not in stages:
            continue
        if models and st["model"] not in models:
            continue
        for sid in st["scenarios"]:
            if scenarios and sid not in scenarios:
                continue
            sc = render.load_scenario(sid)
            if not sc["llm_call"]:
                skipped.append((st["model"], sid, "unusable"))
                continue
            for k in range(1, (repeats or st["repeats"]) + 1):
                reqs.append({"stage": st["id"], "model": st["model"], "scenario": sid, "repeat": k})
    return reqs, skipped


def load_config():
    if not CONFIG_PATH.exists():
        return None
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_tariffs():
    if TARIFFS_PATH.exists():
        return json.loads(TARIFFS_PATH.read_text(encoding="utf-8"))
    return None


def cost(tariffs, model, usage):
    """Стоимость только при подтверждённых тарифах и известных токенах; иначе None."""
    if not tariffs or not usage:
        return None
    t = tariffs.get("models", {}).get(model)
    if not t or t.get("confirmed") is not True:
        return None
    if None in (t.get("input_per_1m_tokens"), t.get("output_per_1m_tokens"), usage.get("input_tokens"), usage.get("output_tokens")):
        return None
    return round(usage["input_tokens"] / 1e6 * t["input_per_1m_tokens"] + usage["output_tokens"] / 1e6 * t["output_per_1m_tokens"], 6)


def write_json(path, obj, secrets=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(client.scrub_obj(obj, list(secrets)), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rough_tokens(*texts):
    return sum(len(t) for t in texts) // 3  # грубая оценка длины, НЕ биллинг


def jaccard(a, b):
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def stability(outputs):
    """Среднее попарное Жаккара по множествам (topic) гипотез и (factor) чувствительностей."""
    if len(outputs) < 2:
        return None
    res = {}
    for name, fn in (("topics", lambda o: {h["topic"] for h in o["hypotheses"]}), ("factors", lambda o: {s["factor"] for s in o["sensitivities"]})):
        sets = [fn(o) for o in outputs]
        pairs = list(itertools.combinations(sets, 2))
        res[name] = round(sum(jaccard(a, b) for a, b in pairs) / len(pairs), 3)
    return res


def dry_run(out_dir, stages=None, models=None, scenarios=None, repeats=None, schema_mode="prompt"):
    reqs, skipped = expand(stages, models, scenarios, repeats)
    key_state = "задан" if os.environ.get("BCE_LLM_API_KEY") else "не задан"
    summary = {"mode": "dry-run", "api_calls_made": 0, "api_key_env": f"BCE_LLM_API_KEY: {key_state} (значение не используется и не выводится)",
               "schema_mode": schema_mode, "planned_requests": len(reqs), "skipped_by_design": skipped,
               "token_note": "оценка длины = символы/3, грубая, не тарифицируется", "scenarios": {}}
    seen = set()
    for r in reqs:
        key = (r["model"], r["scenario"])
        if key in seen:
            continue
        seen.add(key)
        sc = render.load_scenario(r["scenario"])
        s, u, ctx = render.render(sc, schema_mode=schema_mode)
        write_json(out_dir / "requests" / f"{slug(r['model'])}__{r['scenario']}.json",
                   {"model": r["model"], "scenario": r["scenario"], "system": s, "user": u, "schema_mode": schema_mode})
        ev = ctx["evidence_index"]
        summary["scenarios"][f"{r['model']} / {r['scenario']}"] = {
            "system_chars": len(s), "user_chars": len(u), "rough_input_tokens": rough_tokens(s, u),
            "evidence_entries": len(ev), "supports_blocked": sorted(e["ref"] for e in ev if not e["supports_allowed"]),
            "requests_planned": sum(1 for q in reqs if (q["model"], q["scenario"]) == key)}
    ctl = controls.run_all(render.load_scenario, render.render, checks.evaluate)
    summary["local_controls"] = ctl
    summary["local_controls_passed"] = f"{sum(1 for c in ctl if c['caught'])}/{len(ctl)}"
    write_json(out_dir / "summary.json", summary)
    return summary


def live_run(out_dir, cfg, stages, models, scenarios, repeats, schema_mode_override, max_requests):
    tariffs = load_tariffs()
    reqs, skipped = expand(stages, models, scenarios, repeats)
    if len(reqs) > max_requests:
        raise SystemExit(f"Запланировано {len(reqs)} запросов > --max-requests {max_requests}. Уменьшите выборку или поднимите лимит осознанно.")
    key = client.get_api_key(cfg["api_key_env"])
    if not key or not cfg.get("base_url"):
        raise SystemExit("Нужны base_url в lab.config.json и ключ в переменной окружения (api_key_env).")
    cl = client.LlmClient(cfg["base_url"], key)
    secrets = [key]
    full, basic = schema_bundle.bundle_full(), schema_bundle.bundle_basic()
    results = []
    for n, r in enumerate(reqs, 1):
        mcfg = cfg["models"].get(r["model"])
        if r["model"] not in client.ALLOWED_MODELS or not mcfg or not mcfg.get("api_model_id"):
            raise SystemExit(f"Модель {r['model']}: не из разрешённого списка или не задан api_model_id.")
        mode = mcfg.get("structured_output", "json_object")
        schema_mode = schema_mode_override or ("api" if mode.startswith("json_schema") else "prompt")
        sc = render.load_scenario(r["scenario"])
        s, u, ctx = render.render(sc, schema_mode=schema_mode)
        resp = cl.chat(mcfg["api_model_id"], s, u, cfg.get("temperature", 0.2), cfg.get("max_tokens", 3000),
                       client.response_format(mode, full, basic))
        rec = {**r, "structured_output": mode, "schema_mode": schema_mode, "response": resp}
        if resp["ok"]:
            ev = checks.evaluate(ctx, resp["text"])
            rec["check"] = {k: v for k, v in ev.items() if k != "output"}
            rec["output"] = ev.get("output")
            rec["cost"] = cost(tariffs, r["model"], resp["usage"])
        write_json(out_dir / "responses" / f"{slug(r['model'])}__{r['scenario']}__{r['repeat']}.json", rec, secrets)
        results.append(rec)
        print(f"[{n}/{len(reqs)}] {r['model']} {r['scenario']}#{r['repeat']}: " + (rec["check"]["verdict"] if resp["ok"] else f"ошибка API {resp['status']}"))
    summary = summarize(results)
    write_json(out_dir / "summary.json", summary, secrets)
    (out_dir / "summary.md").write_text(summary_md(summary), encoding="utf-8")
    return summary


def summarize(results):
    groups = {}
    for r in results:
        groups.setdefault((r["model"], r["scenario"]), []).append(r)
    rows = []
    for (m, sid), rs in sorted(groups.items()):
        ok = [x for x in rs if x["response"]["ok"]]
        verdicts = [x["check"]["verdict"] for x in ok]
        codes, warns = {}, {}
        for x in ok:
            for c in x["check"]["error_codes"]:
                codes[c] = codes.get(c, 0) + 1
            for c in x["check"]["warning_codes"]:
                warns[c] = warns.get(c, 0) + 1
        outs = [x["output"] for x in ok if x.get("output")]
        tin = [x["response"]["usage"]["input_tokens"] for x in ok if x["response"]["usage"]["input_tokens"] is not None]
        tout = [x["response"]["usage"]["output_tokens"] for x in ok if x["response"]["usage"]["output_tokens"] is not None]
        costs = [x["cost"] for x in ok if x.get("cost") is not None]
        rows.append({"model": m, "scenario": sid, "runs": len(rs), "api_errors": len(rs) - len(ok),
                     "pass": verdicts.count("pass"), "partially_usable": verdicts.count("partially_usable"), "fail": verdicts.count("fail"),
                     "error_codes": codes, "warning_codes": warns, "stability": stability(outs),
                     "input_tokens_total": sum(tin) if tin else None, "output_tokens_total": sum(tout) if tout else None,
                     "cost_total_rub": round(sum(costs), 6) if costs and len(costs) == len(ok) else None})
    return {"mode": "live", "rows": rows}


def summary_md(summary):
    lines = ["# Сводка прогона", "", "| модель | сценарий | запусков | pass | partially | fail | ошибки API | коды ошибок | стабильность | токены in/out | стоимость |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in summary["rows"]:
        lines.append(f"| {r['model']} | {r['scenario']} | {r['runs']} | {r['pass']} | {r['partially_usable']} | {r['fail']} | {r['api_errors']} | "
                     f"{r['error_codes'] or '—'} | {r['stability'] or '—'} | {r['input_tokens_total']}/{r['output_tokens_total']} | "
                     f"{r['cost_total_rub'] if r['cost_total_rub'] is not None else 'n/a (нет подтверждённых тарифов)'} |")
    return "\n".join(lines) + "\n"


def new_run_dir(prefix):
    d = ROOT / "lab_results" / f"{prefix}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    return d
