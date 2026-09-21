"""Populate the helpdesk with realistic demo traffic (safe to re-run).

Run:  ./.venv/bin/python -m backend.app.demo
"""
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .database import Base, SessionLocal, engine
from .models import Channel, Contact, Conversation, Message, User
from .seed import seed
from .services.ingest import ingest_inbound, record_outbound

random.seed(20260918)

CLIENTS = [
    ("whatsapp", "79990001122", "Игорь Волков"),
    ("whatsapp", "79995553311", "Мария Крылова"),
    ("vk", "1001", "Дмитрий Соколов"),
    ("vk", "1002", "Елена Никитина"),
    ("email", "petrov@example.com", "Алексей Петров"),
    ("email", "sidorova@example.com", "Ольга Сидорова"),
    ("email", "kuznetsov@example.com", "Николай Кузнецов"),
    ("whatsapp", "79997770044", "Светлана Миронова"),
]

DIALOGS = [
    ["Здравствуйте! Не могу войти в личный кабинет, пишет неверный пароль.",
     "Добрый день! Сейчас проверю ваш аккаунт, одну минуту.",
     "Спасибо, жду.",
     "Сбросил пароль и отправил новый на привязанную почту. Проверьте, пожалуйста.",
     "Всё получилось, спасибо!"],
    ["Подскажите, как оформить возврат товара?",
     "Добрый день! Возврат оформляется в разделе «Мои заказы» в течение 14 дней.",
     "А если заказ уже в доставке?",
     "Тогда дождитесь получения и оформите возврат из личного кабинета — курьер заберёт бесплатно."],
    ["Когда придёт мой заказ №48123?",
     "Проверяю статус... Заказ передан в доставку, ожидайте сегодня до 20:00.",
     "Отлично, спасибо!"],
    ["Приложение вылетает при открытии раздела «Платежи». iPhone 15, iOS 18.",
     "Спасибо за детали! Передали разработчикам. Попробуйте переустановить приложение — часто помогает.",
     "Переустановил, проблема осталась.",
     "Понял, эскалировал в команду мобильной разработки, вернёмся с ответом в течение дня."],
    ["Не приходит код подтверждения по SMS.",
     "Добрый день! Проверьте, пожалуйста, не включён ли блокировщик спама. Код отправлен повторно.",
     "Пришёл, спасибо!"],
    ["Хочу изменить адрес доставки в заказе.",
     "К сожалению, заказ уже собран на складе. Можно изменить через службу доставки после передачи.",
     "Хорошо, спасибо за информацию."],
]

UNANSWERED = [
    ("whatsapp", "79991112233", "Андрей Лебедев", "Есть ли у вас скидка при заказе от 100 000 рублей?"),
    ("vk", "1003", "Татьяна Орлова", "Здравствуйте, нужна помощь с настройкой интеграции API."),
    ("email", "morozov@example.com", "Сергей Морозов", "Прошу выставить счёт на оплату обучения для сотрудников."),
]


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed(db)
        channels = {c.type: c for c in db.scalars(select(Channel))}
        agents = list(db.scalars(select(User)))
        operator = next((a for a in agents if not a.is_admin), agents[0])

        now = datetime.now(timezone.utc)
        created = 0

        for idx, (ctype, ext_id, name) in enumerate(CLIENTS):
            channel = channels.get(ctype)
            if channel is None:
                continue
            contact = db.scalar(
                select(Contact).where(Contact.channel_type == ctype, Contact.external_id == ext_id)
            )
            if contact and db.scalar(select(Conversation).where(Conversation.contact_id == contact.id)):
                continue

            script = DIALOGS[idx % len(DIALOGS)]
            t = now - timedelta(days=random.randint(0, 20), hours=random.randint(0, 20),
                                minutes=random.randint(0, 59))
            conv = None
            for i, text in enumerate(script):
                t = t + timedelta(minutes=random.randint(3, 90))
                if i % 2 == 0:
                    conv, _m, _n = ingest_inbound(
                        db, channel=channel, external_id=ext_id, body=text,
                        contact_name=name,
                        contact_email=ext_id if ctype == "email" else None,
                        contact_phone=ext_id if ctype == "whatsapp" else None,
                        message_external_id=f"demo-{ctype}-{ext_id}-{i}",
                        created_at=t, meta={"demo": True},
                    )
                else:
                    if conv is None:
                        continue
                    record_outbound(db, conversation=conv, body=text, author=operator, created_at=t)
                    if i >= len(script) - 1:
                        conv.status = "resolved"
                        conv.closed_at = t + timedelta(minutes=random.randint(5, 60))
                db.flush()

            if conv is not None and conv.status == "open" and random.random() < 0.5:
                conv.status = random.choice(["pending", "resolved"])
                if conv.status == "resolved":
                    conv.closed_at = t
            created += 1

        for ctype, ext_id, name, text in UNANSWERED:
            channel = channels.get(ctype)
            if channel is None:
                continue
            contact = db.scalar(
                select(Contact).where(Contact.channel_type == ctype, Contact.external_id == ext_id)
            )
            if contact and db.scalar(select(Conversation).where(Conversation.contact_id == contact.id)):
                continue
            ingest_inbound(
                db, channel=channel, external_id=ext_id, body=text, contact_name=name,
                contact_email=ext_id if ctype == "email" else None,
                contact_phone=ext_id if ctype == "whatsapp" else None,
                message_external_id=f"demo-un-{ctype}-{ext_id}",
                created_at=now - timedelta(hours=random.randint(1, 30)),
                meta={"demo": True},
            )
            created += 1

        db.commit()

        n_conv = len(list(db.scalars(select(Conversation))))
        n_msg = len(list(db.scalars(select(Message))))
        print(f"Demo data ready: {n_conv} conversations, {n_msg} messages (new conversations: {created}).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
