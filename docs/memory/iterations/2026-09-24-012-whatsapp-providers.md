# Итерация 012 — мультипровайдерный канал WhatsApp (Touch-API)

Дата: 2026-09-24

## Цель

Подключить канал WhatsApp как полноценный двусторонний канал (приём и отправка),
заложив сразу поддержку разных вендоров.

## Что сделано

- `backend/app/channels/whatsapp/` — новый пакет вместо одиночного адаптера:
  `base.py` (интерфейс `WhatsAppProvider`), `touchapi.py` (вендор Touch-API),
  `custom.py` (прежняя generic-форма, переехала из `whatsapp/custom.py`), `__init__.py`
  (реестр + фолбэк на `custom`).
- `backend/app/channels/dispatch.py` — выбор провайдера по `config.provider`.
- `backend/app/routers/whatsapp_connect.py` — визард: `providers`, `discover`,
  `connect`, `auth`, `auth-status`, `qr-image`.
- `backend/app/services/whatsapp_connect.py` — общий путь сохранения канала,
  генерация логина, регистрация вебхука.
- `backend/app/routers/webhooks.py` — приём через провайдерский
  `normalize_inbound`, обработка статусов доставки.
- `backend/app/schemas.py` — схемы визарда.
- `frontend/app.js`, `frontend/styles.css` — дропдаун провайдеров, поиск
  аккаунтов, модалка QR.

## Контракт Touch-API (проверено на живом токене)

- база `https://cloud.controller.touch-api.com/api`, авторизация `token`+`login`+`source` в теле;
- `POST /sendMessage` -> `results[].result.item` (ID сообщения);
- `POST /getInfoByToken` -> список аккаунтов; `POST /addAccount`, `/deleteAccount`;
- авторизация: `setState`, `getQr`, `screenshot`, `enablePhoneAuth`+`getAuthCode`;
- вебхуки: `addWebhook`/`deleteWebhook`; формат события — `hook_type`
  (`message`, `message_status`, `add_message_reaction`).

## Проверено

- Провайдеры, discovery (21 аккаунт), битый токен — `400`, пустой — `422`.
- Создание канала с новым аккаунтом — `201`, вебхук зарегистрирован.
- QR-прокси, `auth-status`; `setState` у вендора долгий — таймауты обработаны
  (было необработанное исключение, стало понятное сообщение).
- Приём на реальных payload вендора: сообщение создаётся, повтор идемпотентен,
  статус обновляет сообщение, реакция игнорируется.
- Найден и исправлен баг: пустой список от провайдера трактовался как «не
  распознал» и реакция попадала в переписку. Контракт уточнён (`None` против `[]`).

## Заметки / дальше

- Нужна сквозная проверка на живом номере (QR реальным телефоном).
- Нужен внешний `PUBLIC_BASE_URL`, иначе вендор не достучится до вебхука.
- Мониторинг живости сессии (`getInfo`) пока не сделан.
