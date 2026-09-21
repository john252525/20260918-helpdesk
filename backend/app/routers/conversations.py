from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from ..channels.base import SendResult
from ..channels.dispatch import get_adapter
from ..database import get_db
from ..models import Channel, Contact, Conversation, Event, Message, User, utcnow
from ..schemas import (
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    MessageCreate,
    MessageOut,
    MessageSentOut,
    PaginatedConversations,
    PaginatedMessages,
)
from ..security import effective_workspace, get_current_user
from ..services.ingest import ingest_inbound, record_outbound

router = APIRouter(prefix="/conversations", tags=["conversations"])

VALID_STATUSES = {"open", "pending", "resolved", "closed"}
VALID_PRIORITIES = {"low", "normal", "high", "urgent"}


def _scoped_conversation(db: Session, conversation_id: int, user: User) -> Conversation:
    """Fetch a conversation the user is allowed to see.

    A conversation from another workspace answers 404, not 403: an outsider
    should not be able to confirm that a given id exists at all.
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if user.role != "superadmin" and conv.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def _serialize(conv: Conversation, last_msg: Message | None = None, count: int | None = None) -> ConversationOut:
    out = ConversationOut.model_validate(conv)
    if last_msg is not None:
        out.last_message_preview = (last_msg.body or "")[:160]
        out.last_message_direction = last_msg.direction
    out.messages_count = count
    return out


def _last_messages(db: Session, conv_ids: list[int]) -> dict[int, Message]:
    """Newest message per conversation.

    Ordered by (created_at, id), NOT by id alone: imported history can be
    written out of chronological order, so a later-inserted row may be an
    older message. Using max(id) would show a stale preview.

    Uses a window function so the query is portable across SQLite and
    PostgreSQL -- neither `.strftime()` nor `.printf()` exists in Postgres.
    """
    if not conv_ids:
        return {}

    ranked = (
        select(
            Message.id.label("mid"),
            func.row_number().over(
                partition_by=Message.conversation_id,
                order_by=(Message.created_at.desc(), Message.id.desc()),
            ).label("rn"),
        )
        .where(Message.conversation_id.in_(conv_ids))
        .subquery()
    )
    rows = db.execute(
        select(Message).join(ranked, Message.id == ranked.c.mid).where(ranked.c.rn == 1)
    ).scalars().all()
    return {m.conversation_id: m for m in rows}


def _message_counts(db: Session, conv_ids: list[int]) -> dict[int, int]:
    if not conv_ids:
        return {}
    rows = db.execute(
        select(Message.conversation_id, func.count())
        .where(Message.conversation_id.in_(conv_ids))
        .group_by(Message.conversation_id)
    ).all()
    return {cid: cnt for cid, cnt in rows}


@router.get("", response_model=PaginatedConversations, summary="Единый инбокс: список диалогов")
def list_conversations(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    workspace_id: int | None = Query(None, description="Только суперадмин: чужая компания"),
    status: str | None = Query(None, description="open|pending|resolved|closed, через запятую"),
    channel_id: int | None = None,
    assignee_id: int | None = None,
    priority: str | None = None,
    q: str | None = Query(None, description="поиск по тексту сообщений и имени клиента"),
    unassigned: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    sort: str = Query("last_message_at", description="last_message_at|created_at|priority"),
):
    scope = effective_workspace(user, workspace_id)
    stmt = select(Conversation)
    if scope is not None:
        stmt = stmt.where(Conversation.workspace_id == scope)
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        stmt = stmt.where(Conversation.status.in_(statuses))
    if channel_id:
        stmt = stmt.where(Conversation.channel_id == channel_id)
    if assignee_id:
        stmt = stmt.where(Conversation.assignee_id == assignee_id)
    if priority:
        stmt = stmt.where(Conversation.priority == priority)
    if unassigned:
        stmt = stmt.where(Conversation.assignee_id.is_(None))
    if q:
        like = f"%{q}%"
        msg_conv_ids = select(Message.conversation_id).where(Message.body.ilike(like))
        contact_stmt = select(Contact.id).where(
            or_(Contact.name.ilike(like), Contact.email.ilike(like),
                Contact.phone.ilike(like), Contact.external_id.ilike(like))
        )
        if scope is not None:
            contact_stmt = contact_stmt.where(Contact.workspace_id == scope)
        contact_ids = contact_stmt
        stmt = stmt.where(
            or_(
                Conversation.id.in_(msg_conv_ids),
                Conversation.contact_id.in_(contact_ids),
                Conversation.subject.ilike(like),
            )
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    order = {
        "created_at": desc(Conversation.created_at),
        "priority": desc(Conversation.priority),
    }.get(sort, desc(Conversation.last_message_at))
    stmt = stmt.order_by(order, desc(Conversation.id)).limit(page_size).offset((page - 1) * page_size)
    convs = list(db.scalars(stmt))
    ids = [c.id for c in convs]
    lasts = _last_messages(db, ids)
    counts = _message_counts(db, ids)
    items = [_serialize(c, lasts.get(c.id), counts.get(c.id, 0)) for c in convs]
    return PaginatedConversations(items=items, total=total, page=page, page_size=page_size)


@router.get("/{conversation_id}", response_model=ConversationOut, summary="Диалог по id")
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = _scoped_conversation(db, conversation_id, user)
    return _serialize(conv, _last_messages(db, [conv.id]).get(conv.id), _message_counts(db, [conv.id]).get(conv.id, 0))


@router.post("", response_model=ConversationOut, status_code=201, summary="Создать диалог (исходящий первым)")
def create_conversation(payload: ConversationCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    channel = db.get(Channel, payload.channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if user.role != "superadmin" and channel.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="Channel not found")

    contact = None
    if payload.contact_id:
        contact = db.get(Contact, payload.contact_id)
    elif payload.contact_external_id:
        contact = db.scalar(
            select(Contact).where(
                Contact.channel_type == channel.type,
                Contact.external_id == payload.contact_external_id,
            )
        )
    if contact is None and not payload.contact_external_id:
        raise HTTPException(status_code=400, detail="contact_id or contact_external_id is required")

    body = payload.body or ""
    conv, _msg, _new = ingest_inbound(
        db,
        channel=channel,
        external_id=payload.contact_external_id or contact.external_id,
        body=body or "(conversation started by operator)",
        contact_name=payload.contact_name,
        subject=payload.subject,
        meta={"initiated_by": user.id},
    )
    if payload.subject:
        conv.subject = payload.subject
    conv.assignee_id = user.id
    db.commit()
    db.refresh(conv)
    return _serialize(conv)


@router.patch("/{conversation_id}", response_model=ConversationOut, summary="Статус / приоритет / ответственный / теги")
def update_conversation(conversation_id: int, payload: ConversationUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    conv = _scoped_conversation(db, conversation_id, user)
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(VALID_STATUSES)}")
    if "priority" in data and data["priority"] not in VALID_PRIORITIES:
        raise HTTPException(status_code=400, detail=f"priority must be one of {sorted(VALID_PRIORITIES)}")
    if "assignee_id" in data and data["assignee_id"] is not None:
        if db.get(User, data["assignee_id"]) is None:
            raise HTTPException(status_code=404, detail="Assignee not found")

    old_status = conv.status
    for k, v in data.items():
        setattr(conv, k, v)

    if "status" in data and data["status"] != old_status:
        if data["status"] in ("resolved", "closed"):
            conv.closed_at = utcnow()
        else:
            conv.closed_at = None
        db.add(Event(conversation_id=conv.id, type="status_changed", actor_user_id=user.id,
                     payload={"from": old_status, "to": data["status"]}))
    db.commit()
    db.refresh(conv)
    return _serialize(conv)


@router.get("/{conversation_id}/messages", response_model=PaginatedMessages, summary="Сообщения диалога")
def list_messages(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(200, ge=1, le=1000),
    before_id: int | None = None,
):
    conv = _scoped_conversation(db, conversation_id, user)

    stmt = select(Message).where(Message.conversation_id == conversation_id)

    if before_id is not None:
        # Paginate on (created_at, id), not id alone: backfilled history is
        # inserted out of order, so a newer id does not imply a newer message.
        ref = db.get(Message, before_id)
        if ref is not None:
            stmt = stmt.where(
                or_(
                    Message.created_at < ref.created_at,
                    and_(Message.created_at == ref.created_at, Message.id < ref.id),
                )
            )
        else:
            stmt = stmt.where(Message.id < before_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    # Take the newest `limit` by time, then flip for rendering.
    msgs = list(
        db.scalars(
            stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        )
    )
    msgs.reverse()
    items = []
    for m in msgs:
        out = MessageOut.model_validate(m)
        if m.author_type == "agent":
            # Imported history has no local user attached, but the message is
            # still an operator reply -- don't label it "System".
            out.author_name = m.author_user.name if m.author_user else "Оператор"
        elif m.author_type == "contact":
            out.author_name = conv.contact.display_name if conv.contact else "Клиент"
        else:
            out.author_name = "Система"
        items.append(out)
    return PaginatedMessages(items=items, total=total)


@router.post("/{conversation_id}/messages", response_model=MessageSentOut, status_code=201, summary="Ответить в диалоге")
def send_message(
    conversation_id: int,
    payload: MessageCreate,
    dry_run: bool = Query(False, description="Записать ответ, не отправляя провайдеру"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = _scoped_conversation(db, conversation_id, user)

    if payload.mark_status is not None and payload.mark_status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"mark_status must be one of {sorted(VALID_STATUSES)} or null",
        )

    if conv.status == "closed":
        conv.status = "open"
        conv.reopen_count = (conv.reopen_count or 0) + 1
        conv.closed_at = None

    if dry_run:
        # Tests and rehearsals use this: the message is stored in the thread,
        # but nothing leaves the server. Never point a test at a live channel.
        result = SendResult(ok=False, status="failed", error="dry_run: not delivered")
    else:
        try:
            result = get_adapter(conv.channel).send(
                conv, conv.contact, payload.body, payload.attachments
            )
        except Exception as exc:  # noqa: BLE001
            result = SendResult(ok=False, status="failed", error=str(exc))

    msg = record_outbound(
        db,
        conversation=conv,
        body=payload.body,
        author=user,
        attachments=payload.attachments,
        status=result.status if result.ok else "failed",
        external_id=result.external_id,
        error=result.error,
        meta={"raw": result.raw} if result.raw else {},
    )

    status_applied = True
    notice = None

    wants_status = bool(payload.mark_status)

    if wants_status and result.ok:
        conv.status = payload.mark_status
        if payload.mark_status in ("resolved", "closed"):
            conv.closed_at = utcnow()
        elif conv.closed_at is not None:
            conv.closed_at = None
        db.add(Event(
            conversation_id=conv.id, type="status_changed", actor_user_id=user.id,
            payload={"to": payload.mark_status, "reason": "applied_on_reply"},
        ))
    elif wants_status and not result.ok:
        # Delivery failed: the client never received the reply, so advancing the
        # conversation state would be misleading. Report the skip explicitly
        # instead of silently ignoring the request.
        status_applied = False
        notice = (
            f"Статус не изменён: сообщение не доставлено через {conv.channel.type} "
            f"({result.error}). Диалог оставлен в «{conv.status}»."
        )
        db.add(Event(
            conversation_id=conv.id, type="status_skipped", actor_user_id=user.id,
            payload={"requested": payload.mark_status, "current": conv.status,
                     "reason": result.error or "delivery failed"},
        ))

    db.commit()
    db.refresh(msg)
    out = MessageSentOut.model_validate(msg)
    out.author_name = user.name
    out.status_applied = status_applied
    out.notice = notice
    return out


@router.get("/{conversation_id}/events", summary="Таймлайн событий диалога")
def conversation_events(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # access gate: raises 404 for a conversation the caller may not see
    _scoped_conversation(db, conversation_id, user)
    events = list(db.scalars(
        select(Event).where(Event.conversation_id == conversation_id).order_by(Event.created_at.asc())
    ))
    return [
        {"id": e.id, "type": e.type, "payload": e.payload,
         "actor_user_id": e.actor_user_id, "created_at": e.created_at}
        for e in events
    ]
