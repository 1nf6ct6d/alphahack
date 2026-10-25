# bce_lab — стенд проверки промптов BCE

Не Backend и не часть продукта: инструмент эксперимента. Только стандартная библиотека + `jsonschema` (см. `requirements-dev.txt`).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt     # один раз
.venv/bin/python -m bce_lab selftest        # локальные проверки (без API), в т. ч. живой путь на mock-сервере 127.0.0.1
.venv/bin/python -m bce_lab dry-run         # план, промпты, локальные контроли (без API)
.venv/bin/python -m bce_lab dry-run --scenarios S2,S4 --models "GLM-5.3-Flash" --schema-mode api
.venv/bin/python -m bce_lab bundle-schema   # schemas/generated/llm_output.{bundled,basic}.schema.json
.venv/bin/python -m bce_lab export-scenarios
```

## Живой прогон (платный; только после отдельного разрешения)
1. `cp bce_lab/lab.config.example.json bce_lab/lab.config.json`, заполнить `base_url`, `api_model_id` моделей и `structured_output`.
2. Ключ: `export BCE_LLM_API_KEY=...` в своей оболочке (имя переменной задаётся `api_key_env`). В файлы и консоль ключ не попадает.
3. По желанию `cp bce_lab/tariffs.example.json bce_lab/tariffs.json` и заполнить **подтверждённые** тарифы (`confirmed: true`).
4. `.venv/bin/python -m bce_lab run --live --i-confirm-paid-calls --stages P0_pilot --max-requests 1`

Режимы structured output (`structured_output` в конфигурации модели): `json_schema` (полная самодостаточная схема), `json_schema_basic` (урезанная, без `$ref` и ограничений длины/паттернов), `json_object` (только «JSON-режим»), `none` (обычная генерация). В режимах `json_object` и `none` схема встраивается в промпт, в `json_schema*` передаётся через API. Полная проверка выполняется локально во всех режимах.

Допущение: клиент рассчитан на OpenAI-совместимый `POST {base_url}/chat/completions`; формат API организаторов в ТЗ не описан.
Данные сценариев — условные синтетические; KB и каталог факторов — тестовые заглушки (`bce_lab/data/`), не экспертные.
