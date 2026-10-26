# Release verification

Проверено 26 октября 2026. Поставка — backend MVP с синтетическим демо и адаптерами для реальных источников.

- Python 3.14 / Windows: **316 тестов успешно** (`unittest discover`).
- `pip check`: **No broken requirements found**.
- Docker Linux image: **успешно собран** из Dockerfile с закреплёнными Python dependencies и Poppler/Tesseract rus+eng.
- Docker Compose: API поднялся и получил **healthy**. Из-за ограничения Windows на порт 8080 использован `ALFA_PORT=18080`.
- Реальный HTTP внутри контейнера: полный smoke **PASS**, включая список компаний, профиль, обзор, разбор, повторный разбор, вопрос, feedback и одинаковый 404 для чужих ресурсов.
- Реальная PDF rasterization + OCR в контейнере: **PASS** на временном синтетическом документе; фрагменты не получают автоматическую отметку verified.
- OpenAPI экспортирован в `docs/openapi.json`.
- ZIP содержит SHA-256 manifest исходных файлов и проверяется CRC при сборке.

В тестах LLM использовались подставные ответы и ошибки, без платных вызовов. Реальные ключи организаторов и приватные банковские CSV/PDF в архив не входят. Качество аналитики на реальной выборке не следует из успешных программных тестов.

Для повторения: команды находятся в `START_HERE.md`, `docs/BACKEND.md`, `scripts/smoke_backend.py` и `scripts/verify_ocr.py`.
