# Backend integration and operations

## Состав

FastAPI обслуживает HTTP, SQLite WAL хранит состояние, `ml_features` рассчитывает признаки, `ml_ranking` ранжирует события. Backend собирает профиль и три представления, сохраняет evidence и неизменяемые снимки новостей, отделяет CFO/CEO и доступ компаний. Миграции применяются автоматически при старте; контрольные суммы защищают уже применённые миграции от незаметного изменения.

Повтор создания insight в одном контексте возвращает сохранённый результат. Lease в SQLite защищает от параллельных повторных генераций. Контекст включает компанию, набор доступных компаний пользователя, профиль, новости, банковский корпус, версии модели/промпта/правил/схемы и час расчёта. Исторический insight сохраняет прежние источники и условия. Обновление события не меняет содержимое старого снимка.

## HTTP

Пользовательские маршруты требуют bearer-сессию:

- `GET /companies` → `{"items":[{"id":"U_5639",...}]}`.
- `GET /companies/{id}` → профиль и views.
- `GET /companies/{id}/overview?analytic_focus=CFO` → сохранённый обзор либо `not_prepared`.
- `POST /companies/{id}/overview/refresh` → `{"analytic_focus":"CFO"}`.
- `POST /companies/{id}/insights` → `{"analytic_focus":"CFO","event_id":"..."}`.
- `GET /insights/{id}` → сохранённая версия разбора.
- `POST /insights/{id}/questions` → `{"question":"Какие признаки использованы?","conversation_id":null}`.
- `PUT /insights/{id}/feedback` → `{"rating":"useful","reason":"relevant","comment":null}`.

Второй вопрос передаёт `conversation_id` предыдущего ответа. Диалог закреплён за пользователем, insight и его версией. Feedback сохраняется отдельно для пользователя и версии, повторный PUT обновляет запись с историей.

`rating`: `useful` или `not_useful`. `reason`: `relevant`, `irrelevant`, `factual_error`, `outdated`, `unclear`, `other`.

`GET /meta/capabilities` показывает включённый генератор. При выключенном LLM разбор строится детерминированно по ML evidence, а вопросы поддерживают три намерения: основания, условия влияния и следующий проверяемый шаг.

Ошибки содержат `code`, `message`, `request_id`. Некорректный JSON/параметры — 400, неверный bearer — 401, отсутствующий или чужой ресурс — одинаковый 404, незавершённая подготовка/параллельная генерация — 409, бюджет — 429, внешний сбой — 503. Пустой результат ранжирования имеет состояние `empty`; ошибка зависимости не подменяется пустым результатом. Тестовый `as_of` доступен только при `ALFA_ENV=test`.

Admin-маршруты требуют `X-Admin-Token`, заданный через `ALFA_ADMIN_TOKEN`:

- `POST /admin/events` — проверенное нормализованное событие.
- `GET /admin/events/{id}` — исходный сохранённый текст и разметка.
- `POST /admin/events/{id}/normalize` — нормализация через настроенный LLM с бюджетом.
- `POST /admin/news/url` — `{"url":"https://allowed-host/article","source_id":"SOURCE"}`.
- `POST /admin/news/feed` — `{"url":"https://allowed-host/feed","source_id":"SOURCE","fetch_articles":true,"max_items":25}`.
- `POST /admin/bank-reports` — проверенные OCR-фрагменты.
- `GET /admin/budget` — резерв, списание и остаток, в копейках.

Полные request-схемы и ограничения доступны в OpenAPI. Администратор имеет возможность импортировать общедоступный новостной корпус; этот credential хранится только на доверенной стороне.

## Реальные CSV и пользователи

Ниже PowerShell, native Python. Для Docker используйте `docker compose exec api python -m backend.cli ...`; файлы должны быть доступны внутри контейнера, например через read-only bind mount.

Сначала создайте отдельную БД и постоянный pepper до выпуска сессий:

```powershell
$env:ALFA_DEMO_SEED = "0"
$env:ALFA_DATABASE_PATH = "data/private/live.sqlite3"
$env:ALFA_AUTH_TOKEN_PEPPER = "ЗАМЕНИТЕ_ПОСТОЯННЫМ_СЛУЧАЙНЫМ_СЕКРЕТОМ_ОТ_24_БАЙТ"
.\.venv_backend\Scripts\python.exe -m backend.cli migrate
```

Положите исходные файлы в `data/private/input/`:

- `df_Uid_data_hack.csv`
- `df_Uid_payment_hack.csv`
- `df_Fid_Uid_link_hack.csv`

Импорт сохраняет raw cells, нормализованные значения, номер записи/строки, hash файла и журнал ошибок. Повтор файла идемпотентен. Платежи-дубликаты сохраняются; строки неверной ширины остаются в staging с ошибкой. Person links хранятся для происхождения данных; они не дают пользователю доступ к компании автоматически.

```powershell
.\.venv_backend\Scripts\python.exe -m backend.cli import-datasets --data-dir data/private/input
.\.venv_backend\Scripts\python.exe -m backend.cli import-profile --company-id U_5639 --companies data/private/input/df_Uid_data_hack.csv --payments data/private/input/df_Uid_payment_hack.csv --as-of-date 2026-10-26
.\.venv_backend\Scripts\python.exe -m backend.cli import-profile --company-id U_123123 --companies data/private/input/df_Uid_data_hack.csv --payments data/private/input/df_Uid_payment_hack.csv --as-of-date 2026-10-26
.\.venv_backend\Scripts\python.exe -m backend.cli create-user --user-id TEST_A --company-id U_5639 --hours 24 --show-token
.\.venv_backend\Scripts\python.exe -m backend.cli create-user --user-id TEST_B --company-id U_123123 --hours 24 --show-token
```

Дата должна соответствовать срезу данных; в примере дата хакатона. Выданный токен выводится один раз. Для передачи заранее созданного секрета можно использовать `--token-env NAME_OF_SECRET_VARIABLE`.

Сборка профиля вызывает существующие ML-функции; неизвестные значения и конфликты сохраняются. Профиль не получает вымышленные гипотезы. Аддитивность строк платежей по умолчанию является явным допущением в meta/метриках; `--rows-are-not-additive` отключает его. Исходный CSV остаётся источником сборки профиля: staging служит аудитом, а не альтернативной реализацией ML-математики.

Seed работает только с демо-профилями: при попытке запуска поверх реальных данных он завершится с сообщением поставить `ALFA_DEMO_SEED=0`. Не меняйте pepper после выдачи токенов без плановой перевыдачи сессий.

## LLM

Настройте разрешённый организаторами OpenAI-compatible endpoint `POST /chat/completions`. Выбор конкретного model ID должен соответствовать вашему доступу; его нельзя угадать из репозитория. Python CLI читает переменные процесса. Compose дополнительно читает `.env`, созданный из `config/backend.env.example`.

```powershell
$env:ALFA_LLM_ENABLED = "1"
$env:ALFA_LLM_BASE_URL = "https://ВАШ_ПРОВАЙДЕР/v1"
$env:ALFA_LLM_API_KEY = "ВАШ_КЛЮЧ"
$env:ALFA_LLM_MODEL = "РАЗРЕШЕННЫЙ_API_MODEL_ID"
$env:ALFA_LLM_MAX_CALL_KOPECKS = "ПОДТВЕРЖДЕННАЯ_ВЕРХНЯЯ_СТОИМОСТЬ_ОДНОГО_ВЫЗОВА_В_КОПЕЙКАХ"
```

Значение стоимости — положительное целое число, вычисленное по подтверждённому тарифу для максимального запроса: до 192 KiB входа и 1800 output tokens, включая тарифицируемые reasoning tokens. Placeholder из примера намеренно не проходит проверку конфигурации. Перезапустите сервер после настройки.

Перед вызовом SQLite атомарно резервирует максимум. В журнал консервативно списывается эта верхняя оценка, в том числе при timeout: провайдер мог обработать запрос. Повторы автоматически не выполняются. Внутренний лимит — 450 рублей в день по Москве, внешний ориентир — 500 рублей. Эти суммы контролируют только вызовы через данный backend, а не лабораторные/чужие обращения с тем же API-ключом.

Генератор может менять только текст интерпретации и следующие шаги. ID, priority, источники, bank_position и evidence принадлежат backend; неизвестные ссылки отклоняются, исходные условия сохраняются. Источники и вопросы передаются как недоверенные данные. Проверка ссылок не доказывает смысловую правильность каждой фразы: реальную аналитику следует оценить на размеченных новостях команды.

Исходные BCE v3 промпты и лаборатория сохранены. Они не являются готовым продуктовым сервисом с экспертной KB. Профильный режим по умолчанию — `kb_fallback`; новый runtime narrative/normalization gateway использует отдельно версионированные промпты backend.

## Новости

```powershell
$env:ALFA_ALLOWED_SOURCE_HOSTS = "www.cbr.ru,cbr.ru,ВАШ_НОВОСТНОЙ_ХОСТ"
.\.venv_backend\Scripts\python.exe -m backend.cli import-feed "https://РАЗРЕШЕННЫЙ_ХОСТ/feed" --source-id SOURCE --fetch-articles --max-items 10
.\.venv_backend\Scripts\python.exe -m backend.cli import-url "https://РАЗРЕШЕННЫЙ_ХОСТ/article" --source-id SOURCE
.\.venv_backend\Scripts\python.exe -m backend.cli normalize-event NEWS_ID_ИЗ_ОТВЕТА
```

Allowlist проверяется по точному hostname на каждом redirect; соединение привязано к проверенному публичному IP. Приватные адреса, userinfo, нестандартные порты и HTTPS downgrade запрещены. Получение ограничено размером, временем и количеством redirect. Парсер не исполняет JavaScript; paywall/JS-only страницы могут остаться metadata-only.

Fetch сохраняет текст с консервативным `event_type=other`. Для ранжирования нужна нормализация: команда `normalize-event` при включённом LLM либо импорт проверенной разметки через `import-event event.json` / `POST /admin/events`. Цитаты должны дословно присутствовать в content. При отсутствии полного текста подробный разбор не строится. После импорта/нормализации вызовите POST overview/refresh. Периодический сбор не запускается скрыто: команды можно вызывать из согласованного scheduler команды.

## Банковские PDF и OCR

Docker-образ содержит Poppler и Tesseract rus+eng. Для native Windows установите `pdfinfo`, `pdftotext`, `pdftoppm`, `tesseract` в PATH либо задайте `ALFA_PDFINFO_BIN`, `ALFA_PDFTOTEXT_BIN`, `ALFA_PDFTOPPM_BIN`, `ALFA_TESSERACT_BIN` абсолютными путями.

```powershell
.\.venv_backend\Scripts\python.exe -m backend.cli import-bank-pdf data/private/report.pdf --source-id BANK_REPORT --title "Банковская аналитика" --report-date 2026-10-26 --review-json data/private/report.review.json
```

Проверьте текст и номера страниц в JSON, исправьте OCR-ошибки, выставьте `verified: true` только для вручную проверенных фрагментов, затем:

```powershell
.\.venv_backend\Scripts\python.exe -m backend.cli import-bank-report data/private/report.review.json
```

Сначала пробуется текстовый слой, сканы проходят OCR. Сохраняются hash PDF, имя, страница, текст, quality и verified. Непроверенный текст не цитируется как позиция банка. Повтор исправленного отчёта заменяет его OCR-ревизию атомарно и отзывает прежние фрагменты. Поиск проверенных фрагментов лексический; отсутствие корпуса и завершённый поиск без совпадений различаются в ответе. Автоматического заключения банка о конкретной компании нет.

## Эксплуатация и границы проверки

SQLite предназначен для одного хоста с локальным persistent volume. Не размещайте рабочую SQLite БД на сетевом/синхронизируемом диске: для регулярного запуска задайте локальный путь вне OneDrive либо используйте Docker volume. Для нескольких хостов потребуется отдельная миграция на серверную БД; простое увеличение числа контейнеров не является поддерживаемой схемой.

Default Compose слушает только 127.0.0.1. Для публичного размещения нужны HTTPS reverse proxy, реальные секреты, allowlist CORS и доступ к admin-маршрутам только с доверенной стороны. `ALFA_ENV=production` требует установленный pepper. Демо-токены и demo-admin предназначены для локальной проверки.

Просмотр логов: `docker compose logs -f api`. Пользовательские ответы снабжены X-Request-ID. Evidence/trace/feedback/usage хранятся в БД; приватная цепочка рассуждений не сохраняется. При резерве после аварийного завершения он остаётся учтённым до ручной сверки с провайдером, что предотвращает незаметное превышение лимита.

Резервную копию активной БД делайте SQLite backup API либо при остановленном приложении; копирование одного `.sqlite3` без WAL у работающего сервера может потерять последние записи.

Проверка: `python -m unittest discover -s tests -p "test_*.py"`, `python scripts/smoke_backend.py`, `python -m pip check`. Экспорт OpenAPI: `python -m backend.cli export-openapi docs/openapi.json`. Сборка ZIP: `python scripts/package_backend.py`; внутри — manifest SHA-256 каждого файла.

В поставку не входят реальные CSV, банковские материалы, API-ключи, экспертная KB и результаты независимой оценки. Они подключаются командами выше. Реальные платные запросы не выполнялись при разработческой проверке; адаптер проверен на контролируемых ответах и ошибках.
