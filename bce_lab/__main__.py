import argparse
import json
import sys

from . import export_scenarios, runner, schema_bundle


def csv(v):
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bce_lab", description="Стенд проверки промптов BCE (без Backend/ML)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export-scenarios", help="пересоздать bce_lab/scenarios/*.json из tests/scenarios.py")
    sub.add_parser("bundle-schema", help="создать schemas/generated/*.json")
    sub.add_parser("selftest", help="локальные проверки стенда (без API)")
    for name in ("dry-run", "run"):
        p = sub.add_parser(name)
        p.add_argument("--stages", type=csv)
        p.add_argument("--models", type=csv)
        p.add_argument("--scenarios", type=csv)
        p.add_argument("--repeats", type=int)
        p.add_argument("--schema-mode", choices=["prompt", "api"], default=None)
        if name == "run":
            p.add_argument("--live", action="store_true", help="разрешить обращения к API")
            p.add_argument("--i-confirm-paid-calls", action="store_true", help="подтверждение платных вызовов (требует отдельного разрешения владельца)")
            p.add_argument("--max-requests", type=int, required=True, help="жёсткий лимит числа запросов")
    a = ap.parse_args(argv)
    if a.cmd == "export-scenarios":
        print(export_scenarios.export())
    elif a.cmd == "bundle-schema":
        for k, v in schema_bundle.write_all().items():
            print("записан", v)
    elif a.cmd == "selftest":
        from . import selftest
        return selftest.main()
    elif a.cmd == "dry-run":
        d = runner.new_run_dir("dryrun")
        s = runner.dry_run(d, a.stages, a.models, a.scenarios, a.repeats, a.schema_mode or "prompt")
        print(f"Dry-run: API-вызовов 0. Запросов в плане: {s['planned_requests']}; пропущено по замыслу: {s['skipped_by_design']}")
        print(s["api_key_env"])
        for k, v in s["scenarios"].items():
            print(f"  {k}: ~{v['rough_input_tokens']} ток. (грубо), evidence={v['evidence_entries']}, supports заблокированы: {len(v['supports_blocked'])}")
        bad = [c for c in s["local_controls"] if not c["caught"]]
        print(f"Локальные контроли: {s['local_controls_passed']}")
        for c in bad:
            print("  НЕ ПОЙМАНО:", c["id"], c["expected"], c["detail"])
        print("Результаты:", d)
        return 1 if bad else 0
    elif a.cmd == "run":
        if not (a.live and a.i_confirm_paid_calls):
            print("Отказ: живой прогон платный. Нужны оба флага --live и --i-confirm-paid-calls (только после отдельного разрешения). Для проверки без API используйте dry-run.")
            return 2
        cfg = runner.load_config()
        if not cfg:
            print("Нет bce_lab/lab.config.json (см. lab.config.example.json).")
            return 2
        d = runner.new_run_dir("live")
        runner.live_run(d, cfg, a.stages, a.models, a.scenarios, a.repeats, a.schema_mode, a.max_requests)
        print("Результаты:", d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
