"""Shared save path for VK channels.

Both connection flows -- the manual token form and VK OAuth -- end up here,
so nothing downstream has to care where the credentials came from (see
ADR-021). Keeping a single implementation also means Long Poll setup, the
duplicate-name guard and the `webhook_secret` issuing happen exactly once.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Channel

log = logging.getLogger("channels")


def setup_vk_long_poll(cfg: dict) -> dict:
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


def save_vk_channel(
    db: Session,
    *,
    workspace_id: int,
    name: str,
    config: dict,
    enabled: bool = True,
) -> Channel:
    """Create a VK channel from either flow and return the saved row.

    The caller supplies only group id + token (plus optional hints); the
    Long Poll switch, the name collision guard and the webhook secret are all
    handled here so both flows behave identically.
    """
    config = dict(config or {})
    long_poll = setup_vk_long_poll(config)
    config["long_poll"] = long_poll

    clash = db.scalar(
        select(Channel).where(
            Channel.workspace_id == workspace_id,
            Channel.type == "vk",
            Channel.name == name,
        )
    )
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Канал с названием «{name}» уже есть в этой компании",
        )

    ch = Channel(
        workspace_id=workspace_id,
        type="vk",
        name=name,
        enabled=enabled,
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
            detail=f"Канал с названием «{name}» уже есть в этой компании",
        )
    db.refresh(ch)

    if not long_poll.get("enabled"):
        # the channel exists, but receiving will not work until VK is fixed;
        # surface it in the log so it is not a silent failure
        log.warning("VK channel %s: Long Poll not enabled: %s", ch.id, long_poll.get("detail"))

    return ch


def unique_channel_name(db: Session, workspace_id: int, base: str) -> str:
    """Return `base`, or `base (2)`, `base (3)`... when the name is taken.

    A community name is not guaranteed unique inside a company, and failing
    the whole connect just because of a label would be hostile. The manual
    flow keeps returning a 409 instead -- that path is unchanged.
    """
    base = (base or "Группа VK").strip()[:200] or "Группа VK"
    taken = {
        n for (n,) in db.execute(
            select(Channel.name).where(Channel.workspace_id == workspace_id, Channel.type == "vk")
        ).all()
    }
    if base not in taken:
        return base
    i = 2
    while f"{base} ({i})" in taken:
        i += 1
    return f"{base} ({i})"
