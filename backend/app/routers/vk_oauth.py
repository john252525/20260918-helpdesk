"""VK OAuth connection flow: start, callback, group picker, connect.

The callback is reached by the browser, not by fetch(), so it cannot carry an
`Authorization` header. It trusts the signed `state` instead and, once the
token has been exchanged, hands the admin a short-lived ticket. Selecting a
community is a normal authenticated call again.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import ChannelOut, VKOAuthConnect
from ..security import get_current_user, require_admin
from ..services import vk_oauth
from ..services.vk_connect import save_vk_channel, unique_channel_name

log = logging.getLogger("vk_oauth")

router = APIRouter(prefix="/channels/vk/oauth", tags=["channels"])


def _frontend_redirect(**params) -> RedirectResponse:
    base = vk_oauth.frontend_url()
    query = urlencode({k: v for k, v in params.items() if v is not None})
    url = base + ("&" if "?" in base else "?") + query if query else base
    return RedirectResponse(url=url, status_code=303)


@router.get("/start", summary="Начать подключение группы через VK OAuth")
def start(user: User = Depends(require_admin)):
    """Return the VK authorize URL. The browser navigates there next.

    `group_ids` is intentionally omitted: without it VK grants access to all
    communities the user administers, which is exactly what the picker needs.
    """
    if not vk_oauth.settings.vk_app_id:
        raise HTTPException(status_code=500, detail="VK app id не настроен")
    if not vk_oauth.settings.vk_client_secret:
        raise HTTPException(status_code=500, detail="VK client_secret не настроен на сервере")

    state = vk_oauth.make_state(user.id, user.workspace_id)
    return {
        "authorize_url": vk_oauth.build_authorize_url(state),
        "redirect_uri": vk_oauth.redirect_uri(),
    }


@router.get("/callback", include_in_schema=False)
def callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
):
    """VK sends the browser here after the admin approves (or refuses)."""
    if error:
        # access_denied is the ordinary "user pressed cancel" path
        reason = error_description or ("Вы отменили подключение" if error == "access_denied" else error)
        return _frontend_redirect(vk_oauth="error", reason=reason)

    try:
        payload = vk_oauth.consume_state(state)
    except ValueError as exc:
        return _frontend_redirect(vk_oauth="error", reason=str(exc))

    if not code:
        return _frontend_redirect(vk_oauth="error", reason="VK не вернул код авторизации")

    try:
        token_payload = vk_oauth.exchange_code(code)
        access_token = token_payload["access_token"]
        groups = vk_oauth.get_admin_groups(access_token)
    except RuntimeError as exc:
        log.warning("VK OAuth callback failed: %s", exc)
        return _frontend_redirect(vk_oauth="error", reason=str(exc))

    if not groups:
        return _frontend_redirect(
            vk_oauth="error",
            reason="Среди ваших сообществ нет групп, где вы администратор",
        )

    user_id = int(payload["sub"])
    workspace_id = payload.get("wid")
    ticket = vk_oauth.create_ticket(user_id, workspace_id, access_token, groups)
    return _frontend_redirect(vk_oauth="ok", ticket=ticket)


@router.get("/groups", summary="Сообщества, доступные после авторизации")
def groups(
    ticket: str = Query(...),
    user: User = Depends(require_admin),
):
    payload = _load_ticket(ticket, user)
    return {
        "groups": payload["groups"],
        "count": len(payload["groups"]),
    }


@router.post("/connect", response_model=ChannelOut, status_code=201,
             summary="Подключить выбранную группу")
def connect(
    body: VKOAuthConnect,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    payload = _load_ticket(body.ticket, user)

    known = {g["id"]: g for g in payload["groups"]}
    group = known.get(str(body.group_id))
    if group is None:
        raise HTTPException(status_code=400,
                            detail="Эта группа не найдена среди доступных. Начните подключение заново.")

    access_token = payload["access_token"]

    # OAuth yields a *user* token; Long Poll usually needs a *community* one.
    # Report that instead of creating a channel that will silently stay mute.
    rights = vk_oauth.check_group_rights(access_token, group["id"])
    if not rights.get("ok"):
        if rights.get("needs_community_token"):
            raise HTTPException(
                status_code=409,
                detail="Вход выполнен, но для приёма сообщений VK требует ключ группы. "
                       "Вставьте ключ этой группы в форму ручного подключения ниже — "
                       "ID группы уже известен: " + group["id"],
            )
        raise HTTPException(status_code=502, detail=rights.get("detail") or "VK недоступен")

    workspace_id = user.workspace_id
    if user.role == "superadmin":
        workspace_id = body.workspace_id or payload.get("workspace_id")
        if workspace_id is None:
            raise HTTPException(status_code=400,
                                detail="Суперадмин должен указать workspace_id")

    name = body.name or group.get("name") or f"Группа VK {group['id']}"
    name = unique_channel_name(db, workspace_id, name)

    config = {
        "group_id": group["id"],
        "access_token": access_token,
        "screen_name": group.get("screen_name") or "",
        "oauth": True,
    }
    ch = save_vk_channel(db, workspace_id=workspace_id, name=name, config=config)
    vk_oauth.drop_ticket(body.ticket)
    return ch


def _load_ticket(ticket: str, user: User) -> dict:
    try:
        return vk_oauth.get_ticket(ticket, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
