"""Channel-agnostic normalization of inbound/outbound traffic into conversations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..models import Channel, Contact, Conversation, Event, Message, User, utcnow


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_or_create_channel(
    db: Session, channel_type: str, name: str, config: Optional[dict] = None
) -> Channel:
    ch = db.scalar(
        select(Channel).where(Channel.type == channel_type, Channel.name == name)
    )
    if ch is None:
        ch = Channel(type=channel_type, name=name, config=config or {}, enabled=True)
        db.add(ch)
        db.flush()
    elif config:
        merged = {**(ch.config or {}), **config}
        ch.config = merged
    return ch


def get_or_create_contact(
    db: Session,
    channel_type: str,
    external_id: str,
    workspace_id: Optional[int] = None,
    *,
    name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    avatar_url: Optional[str] = None,
    meta: Optional[dict] = None,
) -> Contact:
    contact = db.scalar(
        select(Contact).where(
            Contact.workspace_id == workspace_id,
            Contact.channel_type == channel_type,
            Contact.external_id == external_id,
        )
    )
    if contact is None:
        contact = Contact(
            workspace_id=workspace_id,
            channel_type=channel_type,
            external_id=external_id,
            name=name,
            email=email,
            phone=phone,
            avatar_url=avatar_url,
            meta=meta or {},
        )
        db.add(contact)
        db.flush()
    else:
        changed = False
        if name and not contact.name:
            contact.name = name
            changed = True
        if email and not contact.email:
            contact.email = email
            changed = True
        if phone and not contact.phone:
            contact.phone = phone
            changed = True
        if meta:
            contact.meta = {**(contact.meta or {}), **meta}
            changed = True
        if changed:
            db.flush()
    return contact


def find_active_conversation(db: Session, channel_id: int, contact_id: int) -> Optional[Conversation]:
    return db.scalar(
        select(Conversation)
        .where(
            Conversation.channel_id == channel_id,
            Conversation.contact_id == contact_id,
            Conversation.status.in_(("open", "pending", "resolved")),
        )
        .order_by(desc(Conversation.last_message_at), desc(Conversation.id))
        .limit(1)
    )


def _touch_conversation(conv: Conversation, message: Message) -> None:
    ts = _aware(message.created_at) or utcnow()
    if message.direction == "inbound":
        conv.last_inbound_at = ts
        if conv.first_inbound_at is None:
            conv.first_inbound_at = ts
        if conv.status == "resolved":
            conv.status = "open"
            conv.reopen_count = (conv.reopen_count or 0) + 1
            conv.closed_at = None
        if conv.status == "closed":
            conv.status = "open"
            conv.reopen_count = (conv.reopen_count or 0) + 1
            conv.closed_at = None
    else:
        conv.last_outbound_at = ts
        if conv.first_inbound_at and conv.first_response_at is None:
            conv.first_response_at = ts
            conv.first_response_seconds = int((ts - _aware(conv.first_inbound_at)).total_seconds())
        # last response: time since the last inbound that preceded this outbound
        prev_inbound = conv.last_inbound_at
        if prev_inbound:
            conv.last_response_seconds = int((ts - _aware(prev_inbound)).total_seconds())

    prev_last = _aware(conv.last_message_at)
    if prev_last is None or ts >= prev_last:
        conv.last_message_at = ts
    conv.updated_at = utcnow()


def ingest_inbound(
    db: Session,
    *,
    channel: Channel,
    external_id: str,
    body: str,
    contact_name: Optional[str] = None,
    contact_email: Optional[str] = None,
    contact_phone: Optional[str] = None,
    subject: Optional[str] = None,
    message_external_id: Optional[str] = None,
    attachments: Optional[list] = None,
    created_at: Optional[datetime] = None,
    meta: Optional[dict] = None,
    priority: str = "normal",
    conversation: Optional[Conversation] = None,
) -> tuple[Conversation, Message, bool]:
    """Returns (conversation, message, is_new_conversation). Idempotent by external_id."""
    if message_external_id:
        existing = db.scalar(
            select(Message).where(
                Message.external_id == message_external_id,
                Message.direction == "inbound",
            )
        )
        if existing is not None:
            conv = db.get(Conversation, existing.conversation_id)
            return conv, existing, False

    contact = get_or_create_contact(
        db,
        channel.type,
        external_id,
        workspace_id=channel.workspace_id,
        name=contact_name,
        email=contact_email,
        phone=contact_phone,
        meta=meta,
    )

    ts = _aware(created_at) or utcnow()

    conv = conversation or find_active_conversation(db, channel.id, contact.id)
    is_new = conv is None
    if conv is None:
        conv = Conversation(
            workspace_id=channel.workspace_id,
            channel_id=channel.id,
            contact_id=contact.id,
            subject=subject or (contact_name or contact.display_name),
            status="open",
            priority=priority,
            tags=[],
            meta={},
            # a conversation starts when its first message arrived, not when the
            # row happened to be written (webhooks may deliver backdated traffic)
            created_at=ts,
        )
        db.add(conv)
        db.flush()
        db.add(Event(conversation_id=conv.id, type="conversation_created", payload={}))
    elif subject and not conv.subject:
        conv.subject = subject

    msg = Message(
        conversation_id=conv.id,
        direction="inbound",
        author_type="contact",
        author_contact_id=contact.id,
        body=body or "",
        attachments=attachments or [],
        external_id=message_external_id,
        status="received",
        created_at=ts,
        ingested_at=utcnow(),
        meta=meta or {},
    )
    db.add(msg)
    _touch_conversation(conv, msg)
    db.flush()
    db.add(
        Event(
            conversation_id=conv.id,
            type="message_inbound",
            payload={"message_id": msg.id, "channel": channel.type},
        )
    )
    return conv, msg, is_new


def record_outbound(
    db: Session,
    *,
    conversation: Conversation,
    body: str,
    author: Optional[User],
    attachments: Optional[list] = None,
    status: str = "sent",
    external_id: Optional[str] = None,
    error: Optional[str] = None,
    meta: Optional[dict] = None,
    created_at: Optional[datetime] = None,
    author_type: Optional[str] = None,
) -> Message:
    ts = _aware(created_at) or utcnow()
    msg = Message(
        conversation_id=conversation.id,
        direction="outbound",
        author_type=author_type or ("agent" if author else "system"),
        author_user_id=author.id if author else None,
        body=body,
        attachments=attachments or [],
        external_id=external_id,
        status=status,
        error=error,
        created_at=ts,
        ingested_at=utcnow(),
        meta=meta or {},
    )
    db.add(msg)
    if conversation.assignee_id is None and author is not None:
        conversation.assignee_id = author.id
    _touch_conversation(conversation, msg)
    db.flush()
    db.add(
        Event(
            conversation_id=conversation.id,
            type="message_outbound",
            actor_user_id=author.id if author else None,
            payload={"message_id": msg.id, "status": status},
        )
    )
    return msg


def recompute_conversation_metrics(db: Session, conversation: Conversation) -> None:
    """Full recompute of FRT/last-response for a conversation."""
    msgs = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    )
    first_inbound = None
    first_response = None
    last_inbound = None
    last_response_seconds = None
    for m in msgs:
        ts = _aware(m.created_at)
        if m.direction == "inbound":
            last_inbound = ts
            if first_inbound is None:
                first_inbound = ts
        elif m.direction == "outbound":
            if first_inbound is not None and first_response is None:
                first_response = ts
            if last_inbound is not None:
                last_response_seconds = int((ts - last_inbound).total_seconds())
    conversation.first_inbound_at = first_inbound
    conversation.first_response_at = first_response
    conversation.first_response_seconds = (
        int((first_response - first_inbound).total_seconds())
        if first_response and first_inbound
        else None
    )
    conversation.last_response_seconds = last_response_seconds
    if msgs:
        conversation.last_message_at = _aware(msgs[-1].created_at)
    db.flush()
