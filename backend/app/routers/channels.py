import secrets
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Channel, Conversation, Event, Message, User, WebhookLog
from ..schemas import ChannelCreate, ChannelOut, ChannelUpdate
from ..security import get_current_user, require_admin

router = APIRouter(prefix="/channels", tags=["channels"])

KNOWN_TYPES = {"whatsapp", "vk", "email"}


@router.get("", response_model=list[ChannelOut], summary="Список каналов")
def list_channels(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(Channel)
    if user.role != "superadmin":
        stmt = stmt.where(Channel.workspace_id == user.workspace_id)
    return list(db.scalars(stmt.order_by(Channel.id)))


def _setup_vk_long_poll(cfg: dict) -> dict:
    """Enable Long Poll reception for a VK group.

    VK requires this to be switched on before `groups.getLongPollServer` will
    return a server. It is done here so a freshly connected channel starts
    receiving immediately, without the operator visiting VK settings.

    Never raises: connecting a channel must not fail because VK is unhappy.
    """
    group_id = str(cfg.get("group_id") or "").strip()
    token = str(cfg.get("access_token") or "").strip()
    if not group_id or not token:
        return {"enabled": False, "detail": "нет group_id или токена"}

    import httpx
    try:
        with httpx.Client(timeout=20) as client:
            # enabled=1 is a separate parameter from the event flags; without
            # it VK accepts the call but leaves Long Poll off
            r = client.post(
                "https://api.vk.com/method/groups.setLongPollSettings",
                data={"group_id": group_id, "access_token": token,
                      "v": settings.vk_api_version, "enabled": 1, "message_new": 1},
            )
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": False, "detail": f"нет связи с VK: {exc}"}

    if "error" in data:
        err = data["error"]
        code = err.get("error_code")
        hint = ""
        if code in (15, 27):
            hint = (" Токену не хватает прав: при создании ключа отметьте "
                    "«Управление сообществом» (manage) и «Сообщения сообщества» (messages), "
                    "либо включите Long Poll вручную: Управление → Работа с API → Long Poll API.")
        return {"enabled": False,
                "detail": f"VK ответил ошибкой {code}: {err.get('error_msg')}.{hint}"}

    return {"enabled": True, "detail": "Long Poll включён"}


@router.post("", response_model=ChannelOut, status_code=201, summary="Создать канал")
def create_channel(
    payload: ChannelCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    if payload.type not in KNOWN_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown channel type. Allowed: {sorted(KNOWN_TYPES)}")

    workspace_id = user.workspace_id
    if user.role == "superadmin":
        workspace_id = getattr(payload, "workspace_id", None)
        if workspace_id is None:
            raise HTTPException(status_code=400,
                                detail="Суперадмин должен указать workspace_id")

    config = dict(payload.config or {})
    long_poll = None
    if payload.type == "vk":
        long_poll = _setup_vk_long_poll(config)
        config["long_poll"] = long_poll

    # a channel name must be unique inside its workspace; a plain commit() would
    # surface as an opaque 500, so check first and answer 409
    clash = db.scalar(
        select(Channel).where(
            Channel.workspace_id == workspace_id,
            Channel.type == payload.type,
            Channel.name == payload.name,
        )
    )
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Канал с названием «{payload.name}» уже есть в этой компании",
        )

    ch = Channel(
        workspace_id=workspace_id,
        type=payload.type,
        name=payload.name,
        enabled=payload.enabled,
        config=config,
        webhook_secret=secrets.token_urlsafe(24),
    )
    db.add(ch)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Канал с названием «{payload.name}» уже есть в этой компании",
        )
    db.refresh(ch)

    if long_poll and not long_poll.get("enabled"):
        # the channel exists, but receiving will not work until VK is fixed;
        # surface it in the log so it is not a silent failure
        import logging
        logging.getLogger("channels").warning(
            "VK channel %s: Long Poll not enabled: %s", ch.id, long_poll.get("detail"))

    return ch


@router.post("/validate", summary="Проверить креды канала до сохранения")
def validate_channel(
    payload: ChannelCreate,
    _: User = Depends(require_admin),
):
    """Checks a channel configuration without saving anything.

    For VK: calls groups.getById with the supplied token. This is the single
    most useful pre-flight check -- a wrong token or group id is the most
    common reason a freshly connected channel stays silent.
    """
    ctype = payload.type
    cfg = payload.config or {}

    if ctype not in KNOWN_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"Unknown channel type. Allowed: {sorted(KNOWN_TYPES)}")

    if ctype == "vk":
        group_id = str(cfg.get("group_id") or "").strip()
        token = str(cfg.get("access_token") or "").strip()
        if not group_id or not token:
            return {"ok": False, "detail": "Нужны group_id и access_token"}

        import httpx

        def vk_call(method: str, **params) -> dict:
            with httpx.Client(timeout=15) as client:
                r = client.post(
                    f"https://api.vk.com/method/{method}",
                    data={**params, "access_token": token, "v": settings.vk_api_version},
                )
            return r.json()

        # `groups.getById` is a PUBLIC method: any valid token can read any
        # group, so it proves nothing about ownership. A token belonging to a
        # different community would pass that check and the channel would
        # stay silent forever. Use methods that require rights on the group.
        try:
            settings_resp = vk_call("groups.getLongPollSettings", group_id=group_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"Нет связи с VK: {exc}"}

        if "error" in settings_resp:
            err = settings_resp["error"]
            code = err.get("error_code")
            if code in (15, 27):
                # 15 = "Access denied", 27 = "group auth failed": both mean the
                # token exists but has no rights on this particular group.
                return {"ok": False,
                        "detail": "Токен не даёт прав на эту группу. Проверьте три вещи: "
                                  "1) ключ создан в настройках ИМЕННО этой группы; "
                                  "2) при создании отмечены «Сообщения сообщества» (messages) "
                                  "и «Управление сообществом» (manage); "
                                  "3) в группе включены Сообщения сообщества. "
                                  "Создать ключ заново: Управление → Работа с API → Создать ключ."}
            if code == 5:
                return {"ok": False, "detail": "VK отклонил токен: недействительный или отозван."
                                                " Создайте новый в настройках группы."}
            if code == 100:
                # Long Poll is simply not switched on yet -- the token is fine
                pass
            else:
                return {"ok": False,
                        "detail": f"VK ответил ошибкой {code}: {err.get('error_msg')}"}
        else:
            lp = settings_resp.get("response") or {}
            if not lp.get("is_enabled"):
                return {"ok": True,
                        "detail": "Токен подходит. Long Poll будет включён автоматически "
                                  "при подключении канала.",
                        "long_poll_enabled": False}
            events = lp.get("events", {})
            if not events.get("message_new"):
                return {"ok": True,
                        "detail": "Токен подходит. Событие «новое сообщение» будет включено "
                                  "автоматически при подключении.",
                        "long_poll_enabled": True}
            return {"ok": True, "detail": "Токен действителен, приём сообщений настроен.",
                    "long_poll_enabled": True}

        # Long Poll is off: confirm the token still has rights, then report
        try:
            conf = vk_call("groups.getCallbackConfirmationCode", group_id=group_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"Нет связи с VK: {exc}"}

        if "error" in conf:
            err = conf["error"]
            return {"ok": False,
                    "detail": f"Токен не имеет прав на эту группу "
                              f"({err.get('error_code')}: {err.get('error_msg')})"}

        # readable name, purely informational
        try:
            info = vk_call("groups.getById", group_id=group_id)
            g = ((info.get("response") or {}).get("groups") or [{}])[0]
        except Exception:  # noqa: BLE001
            g = {}

        return {
            "ok": True,
            "detail": f"Токен подходит для группы «{g.get('name')}». "
                      f"Long Poll будет включён автоматически при подключении.",
            "group_name": g.get("name"),
            "screen_name": g.get("screen_name"),
        }

    if ctype == "whatsapp":
        missing = [k for k in ("base_url", "token") if not cfg.get(k)]
        if missing:
            return {"ok": False, "detail": f"Не хватает полей: {', '.join(missing)}"}
        return {"ok": True,
                "detail": "Креды заполнены. Провайдер проверится при первом исходящем."}

    if ctype == "email":
        missing = [k for k in ("smtp_host", "smtp_user") if not cfg.get(k)]
        if missing:
            return {"ok": False, "detail": f"Не хватает полей: {', '.join(missing)}"}
        return {"ok": True, "detail": "Почтовые настройки заполнены."}

    return {"ok": False, "detail": "Неизвестный тип канала"}



@router.get("/{channel_id}", response_model=ChannelOut, summary="Канал по id")
def get_channel(
    channel_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ch = db.get(Channel, channel_id)
    if ch is None or (user.role != "superadmin" and ch.workspace_id != user.workspace_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    return ch


@router.patch("/{channel_id}", response_model=ChannelOut, summary="Обновить канал (config/enabled/name)")
def update_channel(
    channel_id: int,
    payload: ChannelUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    ch = db.get(Channel, channel_id)
    if ch is None or (user.role != "superadmin" and ch.workspace_id != user.workspace_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    data = payload.model_dump(exclude_unset=True)
    if "config" in data and data["config"] is not None:
        ch.config = {**(ch.config or {}), **data.pop("config")}
    for k, v in data.items():
        setattr(ch, k, v)
    db.commit()
    db.refresh(ch)
    return ch


@router.get("/{channel_id}/webhook-url", summary="URL вебхука для провайдера")
def webhook_url(
    channel_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ch = db.get(Channel, channel_id)
    if ch is None or (user.role != "superadmin" and ch.workspace_id != user.workspace_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    base = settings.public_base_url.rstrip("/")
    if ch.webhook_secret:
        url = f"{base}/api/v1/webhooks/{ch.type}/{ch.id}/{ch.webhook_secret}"
    else:
        # legacy channel created before secrets existed: issue one now
        ch.webhook_secret = secrets.token_urlsafe(24)
        db.commit()
        url = f"{base}/api/v1/webhooks/{ch.type}/{ch.id}/{ch.webhook_secret}"
    return {
        "channel_id": ch.id,
        "type": ch.type,
        "url": url,
    }


@router.delete("/{channel_id}", status_code=204, summary="Удалить канал")
def delete_channel(
    channel_id: int,
    force: bool = Query(False, description="Удалить вместе с историей переписки"),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Removes a channel.

    Refuses while the channel still holds conversations: deleting a client's
    history by accident is worse than a second click. Pass `force=true` to
    remove the conversations with it, or disable the channel instead.
    """
    ch = db.get(Channel, channel_id)
    if ch is None or (user.role != "superadmin" and ch.workspace_id != user.workspace_id):
        raise HTTPException(status_code=404, detail="Канал не найден")

    conv_ids = [r[0] for r in db.execute(
        select(Conversation.id).where(Conversation.channel_id == channel_id)
    ).all()]

    if conv_ids and not force:
        raise HTTPException(
            status_code=409,
            detail=f"В канале {len(conv_ids)} диалогов. Отключите канал, если он больше "
                   f"не нужен, или подтвердите удаление вместе с историей.",
        )

    if conv_ids:
        db.execute(delete(Event).where(Event.conversation_id.in_(conv_ids)))
        db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))

    db.execute(delete(WebhookLog).where(WebhookLog.channel_id == channel_id))
    db.delete(ch)
    db.commit()
    return None
