"""Shared save path and vendor calls for WhatsApp channels.

Mirrors `vk_connect`: both the generic form and the Touch-API wizard end up
here, so nothing downstream cares which vendor or which screen created the
channel. Vendor-specific calls live in the provider modules under
`channels/whatsapp`; this file only orchestrates them.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Channel
from ..channels import whatsapp as whatsapp_providers

log = logging.getLogger("channels")


def generate_login() -> str:
    """A fresh account id for the wizard.

    Touch-API accepts an arbitrary string as `login` (its own demo accounts are
    `cc24`, `test_megaplan`, ...), so we mint a short readable one instead of
    asking the admin to invent something.
    """
    return "hd" + secrets.token_hex(4)


def save_whatsapp_channel(
    db: Session,
    *,
    workspace_id: int,
    name: str,
    config: dict,
    enabled: bool = True,
) -> Channel:
    """Create a WhatsApp channel and return the saved row.

    The webhook secret is issued here so the wizard can register our URL with
    the vendor right after; the name collision guard matches every other channel.
    """
    config = dict(config or {})

    clash = db.scalar(
        select(Channel).where(
            Channel.workspace_id == workspace_id,
            Channel.type == "whatsapp",
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
        type="whatsapp",
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
    return ch


def unique_channel_name(db: Session, workspace_id: int, base: str) -> str:
    """`base`, or `base (2)`... when taken. Used by the wizard, where a
    duplicate label must not abort the whole connection."""
    base = (base or "WhatsApp").strip()[:200] or "WhatsApp"
    taken = {
        n for (n,) in db.execute(
            select(Channel.name).where(
                Channel.workspace_id == workspace_id, Channel.type == "whatsapp"
            )
        ).all()
    }
    if base not in taken:
        return base
    i = 2
    while f"{base} ({i})" in taken:
        i += 1
    return f"{base} ({i})"


def webhook_url_for(channel: Channel, base: str) -> str:
    base = (base or "").rstrip("/")
    return f"{base}/api/v1/webhooks/whatsapp/{channel.id}/{channel.webhook_secret}"


def provider_for(token: str, source: str = "whatsapp", base_url: str = ""):
    """Instantiate the Touch-API provider for a one-off call.

    Used before a channel exists (discovery, account creation).
    """
    cfg = {"provider": "touch-api", "token": token, "login": "", "source": source}
    if base_url:
        cfg["base_url"] = base_url
    return whatsapp_providers.get_provider(channel=None, config=cfg)
