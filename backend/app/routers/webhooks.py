import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Channel, Conversation, Message, User, WebhookLog
from ..security import get_current_user
from ..services.ingest import ingest_inbound
from ..channels import whatsapp as whatsapp_providers

log = logging.getLogger("webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _resolve_channel(db: Session, channel_id: int, channel_type: str,
                     provided_secret: str | None) -> Channel:
    """Fetch a channel only if the caller knows its webhook secret.

    The secret lives in the URL path, which makes it unguessable: channel ids
    alone are sequential and trivially enumerable across tenants.
    """
    channel = db.get(Channel, channel_id)
    if channel is None or channel.type != channel_type:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.webhook_secret and provided_secret != channel.webhook_secret:
        # 404, not 403: do not confirm that the channel exists
        raise HTTPException(status_code=404, detail="Channel not found")
    if not channel.enabled:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


def _log(db: Session, channel_type: str, channel_id: int | None, headers: dict, payload: Any,
         processed: bool = False, error: str | None = None) -> WebhookLog:
    entry = WebhookLog(
        channel_type=channel_type,
        channel_id=channel_id,
        headers={k.lower(): v for k, v in headers.items()},
        payload=payload if isinstance(payload, dict) else {"raw": str(payload)},
        processed=processed,
        error=error,
    )
    db.add(entry)
    db.flush()
    return entry


# --------------------------------------------------------------------------
# WhatsApp
# --------------------------------------------------------------------------
@router.post("/whatsapp/{channel_id}/{secret}",
             summary="Входящий вебхук WhatsApp (номерной провайдер)")
async def whatsapp_webhook(channel_id: int, secret: str, request: Request,
                           db: Session = Depends(get_db)):
    raw: Any
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        raw = {"raw": (await request.body()).decode("utf-8", "replace")}

    channel = _resolve_channel(db, channel_id, "whatsapp", secret)
    entry = _log(db, "whatsapp", channel_id, dict(request.headers), raw)

    # optional extra header check, on top of the URL secret
    extra = (channel.config or {}).get("webhook_secret")
    if extra:
        provided = request.headers.get("x-webhook-secret") or request.query_params.get("secret")
        if provided != extra:
            entry.error = "invalid webhook secret header"
            db.commit()
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        # the vendor of a `whatsapp` channel is chosen by config.provider;
        # unknown/missing means the generic form, so old channels keep working
        provider = whatsapp_providers.get_provider(channel)
        items = provider.normalize_inbound(raw)
        if items is None:
            # the provider did not recognise the payload; the tolerant generic
            # normalizer may still make sense of it. An empty list, by
            # contrast, means the event was understood and skipped on purpose.
            items = _normalize_whatsapp(raw)

        for item in items:
            if item.get("status_update"):
                _apply_message_status(db, channel, item)
                continue
            conv, msg, is_new = ingest_inbound(
                db,
                channel=channel,
                external_id=item["external_id"],
                body=item["body"],
                contact_name=item.get("name"),
                contact_phone=item.get("phone"),
                message_external_id=item.get("message_id"),
                created_at=item.get("created_at"),
                attachments=item.get("attachments") or [],
                meta=item.get("meta") or {},
            )
            log.info("WhatsApp inbound conv=%s msg=%s new=%s", conv.id, msg.id, is_new)
        entry.processed = True
        db.commit()
    except Exception as exc:  # noqa: BLE001
        entry.error = str(exc)
        db.commit()
        log.exception("WhatsApp webhook processing failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return {"ok": True, "processed": entry.processed}


def _apply_message_status(db: Session, channel: Channel, item: dict) -> None:
    """Update an existing message from a delivery-status webhook.

    Providers report progress against the message id they returned on send.
    Only an advance is applied: never downgrade `read` back to `sent`.
    """
    ext = item.get("message_id")
    if not ext:
        return
    msg = db.scalar(
        select(Message).where(Message.external_id == str(ext)).order_by(Message.id.desc())
    )
    if msg is None:
        return
    conversation = db.get(Conversation, msg.conversation_id)
    if conversation is None or conversation.channel_id != channel.id:
        return
    rank = {"queued": 0, "pending": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 3}
    current = rank.get(msg.status, 0)
    incoming = item.get("status") or ""
    if rank.get(incoming, -1) > current:
        msg.status = incoming
    if incoming == "failed":
        msg.status = "failed"
    db.flush()


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _normalize_whatsapp(raw: Any) -> list[dict]:
    """Tolerant normalizer: understands common provider shapes + a generic flat shape.

    Generic shape (always works):
      {"from": "79990001122", "text": "hi", "name": "Ivan", "id": "msg-1", "timestamp": 1712345678}
    Batch shape: {"messages": [ ...same... ]}
    """
    items: list[dict] = []
    candidates: list[dict] = []

    if isinstance(raw, dict):
        for key in ("messages", "data", "items", "result"):
            v = raw.get(key)
            if isinstance(v, list):
                candidates.extend([x for x in v if isinstance(x, dict)])
            elif isinstance(v, dict):
                candidates.append(v)
        if not candidates:
            candidates.append(raw)
    elif isinstance(raw, list):
        candidates = [x for x in raw if isinstance(x, dict)]

    for c in candidates:
        inner = c.get("message") if isinstance(c.get("message"), dict) else c
        chat = c.get("chat") if isinstance(c.get("chat"), dict) else {}
        sender = c.get("from") if isinstance(c.get("from"), dict) else {}
        contact = c.get("contact") if isinstance(c.get("contact"), dict) else {}

        external_id = _pick(
            c, "from", "phone", "msisdn", "sender", "chatId", "chat_id", "wa_id", "user_id", "external_id"
        )
        if isinstance(external_id, dict):
            external_id = _pick(external_id, "phone", "id", "number")
        if not external_id:
            external_id = _pick(chat, "id", "phone")
        if not external_id:
            external_id = _pick(sender, "id", "phone")
        if not external_id:
            continue

        body = _pick(inner, "text", "body", "message", "content", "caption", default="")
        if isinstance(body, dict):
            body = _pick(body, "body", "text", default="")

        name = _pick(c, "name", "senderName", "pushname", "username") or _pick(contact, "name") \
            or _pick(sender, "name")
        phone = _pick(c, "phone", "msisdn", "wa_id") or str(external_id)
        message_id = _pick(c, "id", "message_id", "messageId", "msgId") or _pick(inner, "id")
        ts = _pick(c, "timestamp", "date", "time", "created_at")
        created_at = None
        if isinstance(ts, (int, float)):
            created_at = datetime.fromtimestamp(ts if ts < 10**12 else ts / 1000, tz=timezone.utc)
        elif isinstance(ts, str):
            try:
                created_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                created_at = None

        items.append({
            "external_id": str(external_id),
            "body": str(body or ""),
            "name": name,
            "phone": str(phone),
            "message_id": str(message_id) if message_id else None,
            "created_at": created_at,
            "attachments": c.get("attachments") or [],
            "meta": {"raw": c},
        })
    return items


# --------------------------------------------------------------------------
# VK Callback API
# --------------------------------------------------------------------------
@router.post("/vk/{channel_id}/{secret}",
             summary="Входящий вебхук VK Callback API (подтверждение + message_new)")
async def vk_webhook(channel_id: int, secret: str, request: Request,
                     db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}

    channel = _resolve_channel(db, channel_id, "vk", secret)

    entry = _log(db, "vk", channel_id, dict(request.headers), payload)
    cfg = channel.config or {}

    expected_secret = cfg.get("secret") or settings.vk_secret
    if expected_secret and payload.get("secret") != expected_secret:
        entry.error = "invalid secret"
        db.commit()
        raise HTTPException(status_code=403, detail="Invalid secret")

    vk_type = payload.get("type")

    if vk_type == "confirmation":
        entry.processed = True
        db.commit()
        code = cfg.get("confirmation_code") or settings.vk_confirmation_code or "ok"
        return PlainTextResponse(code)

    if vk_type == "message_new":
        try:
            msg = (payload.get("object") or {}).get("message") or {}
            peer_id = str(msg.get("peer_id") or msg.get("from_id") or "")
            body = msg.get("text") or ""
            if peer_id and body:
                author = None
                from_id = msg.get("from_id")
                token = cfg.get("access_token") or settings.vk_access_token
                if from_id and int(from_id) > 0 and token:
                    try:
                        import httpx
                        r = httpx.get(
                            "https://api.vk.com/method/users.get",
                            params={"user_ids": from_id, "access_token": token,
                                    "v": settings.vk_api_version},
                            timeout=15,
                        )
                        res = r.json().get("response") or []
                        if res:
                            author = f"{res[0].get('first_name','')} {res[0].get('last_name','')}".strip()
                    except Exception:  # noqa: BLE001
                        pass
                ingest_inbound(
                    db,
                    channel=channel,
                    external_id=peer_id,
                    body=body,
                    contact_name=author,
                    message_external_id=str(msg.get("id") or "") or None,
                    created_at=datetime.fromtimestamp(msg["date"], tz=timezone.utc) if msg.get("date") else None,
                    meta={"vk_peer_id": peer_id, "vk_from_id": from_id},
                )
            entry.processed = True
            db.commit()
        except Exception as exc:  # noqa: BLE001
            entry.error = str(exc)
            db.commit()
            raise HTTPException(status_code=500, detail=str(exc))
        return PlainTextResponse("ok")

    entry.processed = True
    db.commit()
    return PlainTextResponse("ok")


@router.get("/vk/{channel_id}/{secret}",
            summary="Проверка/подтверждение VK Callback (возвращает confirmation code)")
def vk_webhook_probe(channel_id: int, secret: str, db: Session = Depends(get_db)):
    channel = _resolve_channel(db, channel_id, "vk", secret)
    cfg = channel.config or {}
    return {"confirmation_code": cfg.get("confirmation_code") or settings.vk_confirmation_code or ""}


# --------------------------------------------------------------------------
# Email (webhook bridge for MTA hooks) + generic test injector
# --------------------------------------------------------------------------
@router.post("/email/{channel_id}/{secret}",
             summary="Входящий вебхук для email-моста (MTA hook)")
async def email_webhook(channel_id: int, secret: str, request: Request,
                        db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}

    channel = _resolve_channel(db, channel_id, "email", secret)

    entry = _log(db, "email", channel_id, dict(request.headers), payload)
    try:
        sender = payload.get("from") or payload.get("sender") or ""
        name = payload.get("from_name") or (sender.split("@")[0] if sender else None)
        ingest_inbound(
            db,
            channel=channel,
            external_id=sender,
            body=payload.get("text") or payload.get("body") or "",
            contact_name=name,
            contact_email=sender,
            subject=payload.get("subject"),
            message_external_id=payload.get("message_id"),
            created_at=None,
            meta={"raw": payload},
        )
        entry.processed = True
        db.commit()
    except Exception as exc:  # noqa: BLE001
        entry.error = str(exc)
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


# --------------------------------------------------------------------------
# Debug helper: raw webhook log
# --------------------------------------------------------------------------
@router.get("/logs", summary="Журнал входящих вебхуков")
def webhook_logs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    channel_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    """Raw webhook traffic, for diagnosing a channel that stays silent.

    Scoped to the caller: a company admin sees only their channels' traffic,
    a superadmin sees everything. Payloads contain client messages, so this
    must never be readable without a token.
    """
    stmt = select(WebhookLog).order_by(WebhookLog.id.desc())

    if user.role != "superadmin":
        own = select(Channel.id).where(Channel.workspace_id == user.workspace_id)
        stmt = stmt.where(WebhookLog.channel_id.in_(own))

    if channel_id is not None:
        stmt = stmt.where(WebhookLog.channel_id == channel_id)

    rows = db.scalars(stmt.limit(limit)).all()
    return [
        {"id": r.id, "channel_type": r.channel_type, "channel_id": r.channel_id,
         "processed": r.processed, "error": r.error, "payload": r.payload,
         "created_at": r.created_at}
        for r in rows
    ]
