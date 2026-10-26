# Запуск интерфейса

Интерфейс обращается к backend по HTTP и передаёт заголовок `Authorization: Bearer <token>`.

```bash
cd alfa-context-frontend
npm ci
cp .env.example .env.local   # при необходимости измените адрес API
npm run dev                  # http://localhost:5173
```

Для работы с backend из контейнера в `ALFA_CORS_ORIGINS` должен быть указан адрес интерфейса (в `docker-compose.yml` по умолчанию разрешены `http://localhost:5173` и `http://127.0.0.1:5173`).

- Токен сессии вводится в форме на странице при первом ответе 401 и хранится в `sessionStorage` вкладки.
- `VITE_DEMO_MODE=true` включает локальные демонстрационные данные без обращения к backend; в этом режиме показывается соответствующая плашка.
- Сборка для проверки типов: `npm run build`.
