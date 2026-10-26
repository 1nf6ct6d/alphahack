# Альфа Контекст — готовый backend MVP

Сначала распакуйте ZIP в отдельную папку и откройте терминал **внутри `alfa-context-backend`**, где находятся Dockerfile и этот файл. В архиве уже есть backend, ML, промпты команды, схемы, тесты и закреплённые зависимости.

## 1. Запуск через Docker — рекомендуемый путь

Установите/запустите Docker Desktop в режиме Linux containers. Затем:

```powershell
docker compose up --build -d
docker compose ps
```

Первая сборка скачивает Python, зависимости и OCR-инструменты. Сервис доступен после перехода в `healthy`:

- Swagger: http://127.0.0.1:8080/docs
- OpenAPI: http://127.0.0.1:8080/openapi.json
- Проверка готовности: http://127.0.0.1:8080/health/ready

Если Windows сообщает `ports are not available` / `access permissions` для 8080, используйте другой порт:

```powershell
$env:ALFA_PORT = "18080"
docker compose up -d --wait
```

Тогда Swagger — http://127.0.0.1:18080/docs, а smoke запускается с `--base-url http://127.0.0.1:18080`.

В Swagger нажмите **Authorize** и вставьте `demo-test-a` — без слова Bearer. В HTTP-запросах заголовок выглядит так: `Authorization: Bearer demo-test-a`.

Демо содержит две изолированные компании: TEST_A → U_5639, TEST_B → U_123123. Токен второго пользователя: `demo-test-b`. Новости и профили демо синтетические и отмечены соответствующим образом.

Остановка сохраняет БД:

```powershell
docker compose down
```

Не добавляйте `-v`, если нужно сохранить данные: эта опция удаляет Docker volume с БД.

## 2. Проверка полного сценария

С установленным Python 3.14 выполните во втором терминале:

```powershell
py -3.14 scripts/smoke_backend.py
```

Проверка проходит путь: компании → профиль → обзор → разбор → вопрос → обратная связь, проверяет повторный разбор и недоступность чужой компании. Последняя строка должна начинаться с `PASS`.

Можно повторить вручную в Swagger:

1. `GET /companies`.
2. `POST /companies/U_5639/overview/refresh` с `{"analytic_focus":"CFO"}`.
3. Взять `event_id` из `items` ответа.
4. `POST /companies/U_5639/insights` с `{"analytic_focus":"CFO","event_id":"взятый event_id"}`.
5. Взять `insight_id`; выполнить GET insight, POST questions и PUT feedback.

## 3. Запуск без Docker на Windows

Нужен Python 3.14. Из корня проекта:

```powershell
py -3.14 -m venv .venv_backend
.\.venv_backend\Scripts\python.exe -m pip install -r requirements-backend.txt
$env:ALFA_DEMO_SEED = "1"
$env:ALFA_ADMIN_TOKEN = "demo-admin"
$env:ALFA_CORS_ORIGINS = "http://localhost:5173,http://localhost:3000"
.\.venv_backend\Scripts\python.exe -m backend.cli serve --host 127.0.0.1 --port 8080
```

Терминал с сервером должен оставаться открытым. Для остановки — Ctrl+C. Можно использовать `scripts/run_backend.ps1 -Demo`. Активация virtualenv не нужна: команды вызывают его Python напрямую.

При ошибке `No module named fastapi` запускайте именно `.venv_backend\Scripts\python.exe`, а не системный Python.

## 4. Что передать frontend-разработчику

Базовый URL: `http://127.0.0.1:8080`. Контракт: `/openapi.json`. Все пользовательские запросы содержат `Authorization: Bearer <token>`. Admin token в frontend не передаётся.

Начальный GET overview может вернуть `state=not_prepared`: нужно вызвать POST refresh. Смена CFO/CEO создаёт отдельный контекст. При `409 operation_in_progress` повторите запрос через несколько секунд; при `429` исчерпан бюджет, при `503` недоступен источник/LLM/хранилище. Подробности и JSON-примеры — [docs/BACKEND.md](docs/BACKEND.md).

## 5. Переход к реальным данным

Для самостоятельного демо дополнительных данных и ключей не нужно. Для работы команды нужны три исходных CSV, разрешённые новостные источники, банковские PDF и — если нужна свободная генерация — доступ к API модели организаторов.

Переход описан пошагово в [docs/BACKEND.md](docs/BACKEND.md): отдельная БД, `ALFA_DEMO_SEED=0`, импорт CSV, сборка профилей, выдача сессий, новости, OCR и настройка LLM. Реальные CSV/ключи не включены в ZIP. Лаборатория BCE и её исходные промпты сохранены; сборка профиля по умолчанию использует проверяемый режим `kb_fallback`, а генерация текстов имеет собственную версию промпта backend.

## 6. Тесты

```powershell
.\.venv_backend\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv_backend\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -p "test_*.py"
.\.venv_backend\Scripts\python.exe -m pip check
```

Обычные тесты не вызывают платную модель. `bce_lab run --live` — отдельная платная лабораторная команда; для запуска backend она не нужна.
