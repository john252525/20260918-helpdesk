# Helpdesk — API-first, единый инбокс

Простой helpdesk: все обращения клиентов из **WhatsApp (номерной)**, **сообщений группы VK** и **Email** собираются в одно окно. Можно отвечать прямо в переписке; видно, кто и когда ответил; считаются классические метрики техподдержки.

Фронтенд — **обособленный статический SPA**, который общается только с REST API. Бэкенд ничего не знает о фронтенде: любой другой клиент (мобильный, бот, скрипт) подключается так же.

## Быстрый старт

```bash
cd /opt/20260918-helpdesk

# 1) зависимости (уже установлены в .venv)
./.venv/bin/pip install -r backend/requirements.txt

# 2) демо-данные (необязательно, идемпотентно)
./.venv/bin/python -m backend.app.demo

# 3) запуск
./run.sh
```

* Фронтенд: http://localhost:8091/
* Swagger: http://localhost:8091/api/docs
* ReDoc: http://localhost:8091/api/redoc
* OpenAPI: http://localhost:8091/api/openapi.json
* Healthcheck: http://localhost:8091/api/v1/health

Порт по умолчанию — `8091` (переопределяется переменными `HOST` и `PORT`).

Доступ по умолчанию:

| Пользователь | Пароль |
|---|---|
| `admin@helpdesk.local` | `admin12345` |
| `agent1@helpdesk.local` | `agent12345` |
| `agent2@helpdesk.local` | `agent12345` |

> Пароль администратора и секрет JWT меняются в `backend/.env` (`BOOTSTRAP_ADMIN_PASSWORD`, `SECRET_KEY`).

## Структура

```
/opt/20260918-helpdesk
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI app, CORS, отдача статики
│   │   ├── config.py          # настройки (backend/.env)
│   │   ├── database.py        # SQLAlchemy, SQLite + WAL
│   │   ├── models.py          # User / Channel / Contact / Conversation / Message / Event
│   │   ├── schemas.py         # Pydantic-схемы (= схема Swagger)
│   │   ├── security.py        # bcrypt + JWT
│   │   ├── routers/           # auth, users, channels, contacts, conversations, metrics, webhooks
│   │   ├── services/
│   │   │   ├── ingest.py      # нормализация трафика в диалоги, расчёт FRT
│   │   │   ├── metrics.py     # метрики поддержки
│   │   │   └── pollers.py     # IMAP-поллер (email), VK Long Poll
│   │   ├── channels/          # исходящие адаптеры: whatsapp.py, vk.py, email.py
│   │   ├── seed.py            # bootstrap admin + демо-операторы + каналы
│   │   └── demo.py            # генератор демо-трафика
│   ├── requirements.txt
│   ├── .env                   # рабочие настройки
│   └── .env.example
├── frontend/                  # самостоятельный клиент: index.html + styles.css + app.js
├── deploy/                    # systemd unit + пример nginx
└── run.sh
```
## Как это устроено

```
WhatsApp ─ webhook ─┐
VK        ─ callback┼─► POST /api/v1/webhooks/{type}/{channel_id} ─► ingest ─► Conversation + Message
Email     ─ IMAP   ─┘

Оператор  ─ POST /api/v1/conversations/{id}/messages ─► channel adapter ─► провайдер
```

* **Входящее** сообщение нормализуется в `Contact` (уникален парой `channel_type + external_id`) и `Conversation`. Активный диалог переиспользуется; закрытый — переоткрывается (`reopen_count`).
* **Исходящее** сообщение сохраняется в БД **всегда**, даже если провайдер вернул ошибку: тогда `message.status = "failed"`, а `message.error` содержит причину. История не теряется.
* Каждое сообщение хранит `direction`, `author_type` (`contact` / `agent` / `system`) и `author_user_id` — поэтому видно, **кто именно и когда** ответил.
* В `Conversation` кэшируются `first_response_at`, `first_response_seconds`, `last_response_seconds`, `closed_at` — метрики считаются быстро, без агрегаций на лету.

## Каналы

Все каналы создаются сидом автоматически. Список и URL вебхука:

```bash
TOKEN=$(curl -s -X POST localhost:8091/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@helpdesk.local","password":"admin12345"}' | jq -r .access_token)

curl -s localhost:8091/api/v1/channels -H "Authorization: Bearer $TOKEN" | jq
curl -s localhost:8091/api/v1/channels/1/webhook-url -H "Authorization: Bearer $TOKEN" | jq
```

Креденшелы провайдера лежат в `Channel.config` и переопределяют значения из `.env`:

```bash
curl -s -X PATCH localhost:8091/api/v1/channels/1 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"config":{"base_url":"https://api.provider.io","token":"XXX","send_path":"/sendMessage"}}' | jq
```

### WhatsApp (номерной провайдер)

* **Входящие**: провайдер вызывает `POST /api/v1/webhooks/whatsapp/{channel_id}`.
* **Исходящие**: `POST {config.base_url}{config.send_path}` с заголовком `Authorization: Bearer {config.token}`.

Нормализатор входящих терпим к формату и понимает:

```jsonc
// плоский
{"from":"79990001122","text":"Здравствуйте","name":"Иван","id":"msg-1","timestamp":1712345678}
// батч
{"messages":[{"chatId":"79990001122","message":{"text":"hi"},"id":"msg-2"}]}
// вложенный
{"data":{"from":{"phone":"79990001122"},"message":{"body":"hi"}}}
```

Если у провайдера своя схема — правьте `_normalize_whatsapp()` в `backend/app/routers/webhooks.py` (там же список распознаваемых ключей).

Дополнительно можно задать `config.webhook_secret` — тогда вебхук проверяет заголовок `X-Webhook-Secret` (или `?secret=`).

Шаблон исходящего запроса переопределяется через `config.body_template`:

```json
{
  "phone_field": "to",
  "message_field": "text",
  "body_template": {"to": "{phone}", "text": "{message}", "type": "text"}
}
```

### VK (сообщения группы)

Поддерживаются оба способа:

* **Callback API**: `POST /api/v1/webhooks/vk/{channel_id}` — обрабатывает `confirmation` и `message_new`, проверяет `secret`. `confirmation_code` задаётся через `config.confirmation_code` или `VK_CONFIRMATION_CODE`.
* **Bots Long Poll**: включается автоматически, если заданы `VK_GROUP_ID` и `VK_ACCESS_TOKEN` (см. `backend/app/services/pollers.py`). Удобно, когда нет публичного URL.

Токену группы нужны права на сообщения (`messages`). Исходящие — `messages.send`.

### Email

* **Исходящие**: SMTP (`config.smtp_host`, `smtp_port`, `smtp_user`, `smtp_password`, `from_address`). Тема — `Re: <subject диалога>`, ставится `In-Reply-To`, поэтому почтовые клиенты собирают тред.
* **Входящие**: IMAP-поллер (`config.imap_host`, `imap_port`, `imap_user`, `imap_password`), читает `UNSEEN` в `INBOX` каждые `IMAP_POLL_SECONDS`. Треды склеиваются по `References`/`In-Reply-To`, затем по теме письма.
* Если у вас MTA с вебхуками — есть мост `POST /api/v1/webhooks/email/{channel_id}` с телом `{"from":"a@b.c","subject":"...","text":"...","message_id":"..."}`.
## API

Полная спецификация — в Swagger (`/api/docs`). Авторизация: `POST /api/v1/auth/login` → JWT → кнопка **Authorize**.

### Диалоги

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/v1/conversations` | единый инбокс: `status`, `channel_id`, `assignee_id`, `priority`, `unassigned`, `q`, `page`, `page_size` |
| `POST` | `/api/v1/conversations` | начать диалог первым исходящим |
| `GET` | `/api/v1/conversations/{id}` | диалог |
| `PATCH` | `/api/v1/conversations/{id}` | `status` / `priority` / `assignee_id` / `tags` / `subject` |
| `GET` | `/api/v1/conversations/{id}/messages` | история сообщений |
| `POST` | `/api/v1/conversations/{id}/messages` | **ответить клиенту** |
| `GET` | `/api/v1/conversations/{id}/events` | таймлайн событий |

```bash
# ответить и перевести в «в ожидании»
curl -s -X POST localhost:8091/api/v1/conversations/1/messages \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"body":"Добрый день! Уже смотрим.","mark_status":"pending"}' | jq
```

### Контакты, каналы, пользователи

`GET/PATCH /api/v1/contacts`, `GET /api/v1/contacts/{id}/conversations`, `GET/POST/PATCH /api/v1/channels`, `GET/POST/PATCH /api/v1/users`, `POST /api/v1/auth/login`, `GET /api/v1/auth/me`.

### Метрики

| Метод | Путь | Что отдаёт |
|---|---|---|
| `GET` | `/api/v1/metrics/overview` | сводка: объём, FRT, время решения, SLA, бэклог, входящие/исходящие |
| `GET` | `/api/v1/metrics/agents` | по операторам: назначено, решено, ответов, ср. FRT, ср. время ответа, активные |
| `GET` | `/api/v1/metrics/channels` | по каналам: диалоги, входящие/исходящие, ср. FRT |
| `GET` | `/api/v1/metrics/timeseries` | динамика по дням/часам: создано, решено, входящие, исходящие |

Параметры: `days`, `date_from`, `date_to`, `sla_seconds`, `interval=day|hour`.

**Какие метрики поддержки считаются** (общепринятый набор):

* **FRT — First Response Time**: от первого входящего до первого ответа оператора. Среднее + медиана (медиана устойчива к выбросам) + корзины `≤5м / 5–15м / 15–60м / 1–4ч / >4ч`.
* **ART — Average Response Time**: среднее время ответа оператора на каждое сообщение клиента.
* **Resolution Time / TTR**: от создания диалога до перевода в `resolved`/`closed`.
* **SLA compliance, %**: доля диалогов, где FRT уложился в `sla_seconds`.
* **Backlog**: диалоги в `open` + `pending`; отдельно — без ответственного.
* **Объём**: создано/решено за период, входящих и исходящих, сообщений на диалог.
* **Разрез по операторам**: назначено, решено, ответов, средний FRT/ART, активная нагрузка.
* **Разрез по каналам**: сравнение WhatsApp / VK / Email по объёму и скорости ответа.
* **Повторные открытия** (`reopen_count`) — индикатор некачественного решения.
* **Динамика** по дням — контроль нагрузки и сезонности.

## Фронтенд

`frontend/` — самостоятельный SPA без сборки (vanilla JS), в стиле DeepSeek/Google: белый фон, тонкие границы, аккуратные радиусы, синий акцент `#4d6bfe`.

Разделы: **Инбокс** (список + переписка + ответ), **Метрики**, **Каналы** (URL вебхука, вкл/выкл), **Команда** (операторы).

Он не знает о бэкенде ничего, кроме базового URL API:

```html
<!-- до подключения app.js -->
<script>window.HELPDESK_API_BASE = 'https://api.example.com/api/v1';</script>
```

Файлы можно разложить на любом статик-хостинге (nginx, S3, GitHub Pages) — см. `deploy/nginx.conf.example`. По умолчанию бэкенд отдаёт их на `/` и `/app`.

## Ограничения текущей версии

* Прав и ролей нет — все пользователи с полным доступом (задел есть: `User.is_admin`).
* Вложения хранятся как метаданные, файлового хранилища нет.
* Нет уведомлений (email/push) и SLA-эскалаций.
* Имя автора ответа берётся из JWT — подделать подпись оператора нельзя.

## Проверка работоспособности

```bash
curl -s localhost:8091/api/v1/health

TOKEN=$(curl -s -X POST localhost:8091/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@helpdesk.local","password":"admin12345"}' | jq -r .access_token)

curl -s "localhost:8091/api/v1/metrics/overview?days=90" \
  -H "Authorization: Bearer $TOKEN" | jq
```

## Деплой

```bash
# systemd
cp deploy/helpdesk.service /etc/systemd/system/
systemctl enable --now helpdesk

# или вручную
PORT=8091 ./run.sh
```

## Перезапуск после изменений

```bash
cd /opt/20260918-helpdesk
pkill -f "uvicorn backend.app.main:app" || true
PORT=8091 ./run.sh &
```

## Развёртывание

Сервис работает под systemd, слушает `0.0.0.0:8091`, включён в автозапуск.

Команды управления:

    systemctl status helpdesk     # состояние
    systemctl restart helpdesk    # перезапуск
    systemctl stop helpdesk       # остановка
    tail -f /var/log/helpdesk.log # логи

Юнит-файл: `deploy/helpdesk.service`, рабочая копия — `/etc/systemd/system/helpdesk.service`.

Установка на чистой машине:

    cp deploy/helpdesk.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now helpdesk

Порт открыт в ufw (правило переживает перезагрузку):

    ufw allow 8091/tcp

### Доступ снаружи

| Ресурс | URL |
|---|---|
| Интерфейс | `http://<host>:8091/` |
| Swagger | `http://<host>:8091/api/docs` |
| ReDoc | `http://<host>:8091/api/redoc` |
| OpenAPI | `http://<host>:8091/api/openapi.json` |
| Healthcheck | `http://<host>:8091/api/v1/health` |

Статика раздаётся и по `/app/...`, и от корня (`/styles.css`, `/app.js`), поэтому клиент
с любым из адресов получит рабочий интерфейс. Запрос `/app` без слэша отвечает редиректом
на `/app/`. SPA определяет базовый URL API как `{location.origin}/api/v1`.

## Тестирование боевых каналов

**Никогда не запускайте тесты, пишущие в живой канал.** Отправка уходит
настоящему клиенту, и отозвать её можно не всегда.

Для проверки логики ответа используйте `dry_run`:

    POST /api/v1/conversations/{id}/messages?dry_run=true

Сообщение сохранится в переписке, но провайдеру не уйдёт.
Для сквозной проверки доставки поднимайте канал-заглушку
с недействительными кредами.
