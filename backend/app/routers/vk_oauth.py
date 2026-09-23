"""VK community connection flow: start and callback.

Only two endpoints. The admin points at one community (link or id); VK returns
a community Access token straight to the callback, which writes the channel
through the same `save_vk_channel` path as the manual form. There is no group
picker: VK only lists communities to apps holding the `groups` scope, which it
grants on request (see ADR-022).

The callback is reached by the browser, not by fetch(), so it carries no
`Authorization` header. It trusts the signed `state` instead.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import VKOAuthStart
from ..security import require_admin
from ..services import vk_oauth
from ..services.vk_connect import save_vk_channel, unique_channel_name

log = logging.getLogger("vk_oauth")

router = APIRouter(prefix="/channels/vk/oauth", tags=["channels"])


def _frontend_redirect(**params) -> RedirectResponse:
    base = vk_oauth.frontend_url()
    query = urlencode({k: v for k, v in params.items() if v is not None})
    if query:
        base = base + ("&" if "?" in base else "?") + query
    return RedirectResponse(url=base, status_code=303)


@router.post("/start", summary="Начать подключение группы через VK OAuth")
def start(
    body: VKOAuthStart,
    user: User = Depends(require_admin),
):
    """Resolve the community and return the VK authorize URL.

    The group id is baked into the signed `state`, so the callback does not
    have to guess which community was intended.
    """
    if not vk_oauth.settings.vk_app_id:
        raise HTTPException(status_code=500, detail="VK app id не настроен")
    if not vk_oauth.settings.vk_client_secret:
        raise HTTPException(status_code=500, detail="VK client_secret не настроен на сервере")

    resolved = vk_oauth.resolve_group(body.group)
    if not resolved.get("ok"):
        raise HTTPException(status_code=400, detail=resolved.get("detail") or "Не удалось определить группу")
    group_id = resolved["group_id"]

    workspace_id = user.workspace_id
    if user.role == "superadmin":
        workspace_id = body.workspace_id
        if workspace_id is None:
            raise HTTPException(status_code=400, detail="Суперадмин должен указать workspace_id")

    state = vk_oauth.make_state(
        user_id=user.id,
        workspace_id=workspace_id,
        group_id=group_id,
        channel_name=(body.name or "").strip() or None,
    )
    return {
        "authorize_url": vk_oauth.build_authorize_url(state, group_id),
        "group_id": group_id,
        "redirect_uri": vk_oauth.redirect_uri(),
    }


@router.get("/callback", include_in_schema=False)
def callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
):
    """VK sends the browser here once the admin approves (or refuses)."""
    if error:
        reason = error_description or ("Вы отменили подключение" if error == "access_denied" else error)
        return _frontend_redirect(vk_oauth="error", reason=reason)

    try:
        payload = vk_oauth.consume_state(state)
    except ValueError as exc:
        return _frontend_redirect(vk_oauth="error", reason=str(exc))

    if not code:
        return _frontend_redirect(vk_oauth="error", reason="VK не вернул код авторизации")

    group_id = str(payload.get("gid") or "")
    workspace_id = payload.get("wid")
    user_id = int(payload["sub"])

    if not group_id or workspace_id is None:
        return _frontend_redirect(vk_oauth="error", reason="В state потеряны данные группы")

    try:
        token_payload = vk_oauth.exchange_code(code)
    except RuntimeError as exc:
        log.warning("VK OAuth callback failed: %s", exc)
        return _frontend_redirect(vk_oauth="error", reason=str(exc))

    tokens = vk_oauth.parse_community_tokens(token_payload)
    token = next((t["access_token"] for t in tokens if t["group_id"] == group_id), None)
    if token is None:
        return _frontend_redirect(
            vk_oauth="error",
            reason="VK не выдал токен для этой группы. Проверьте, что вы её администратор.",
        )

    info = vk_oauth.fetch_group_info(group_id, token)

    # persist through the shared path, so the manual and OAuth flows converge
    from ..database import SessionLocal
    db: Session = SessionLocal()
    try:
        desired = payload.get("name") or info.get("name") or f"Группа VK {group_id}"
        name = unique_channel_name(db, workspace_id, desired)
        config = {
            "group_id": group_id,
            "access_token": token,
            "screen_name": info.get("screen_name") or "",
            "oauth": True,
        }
        ch = save_vk_channel(db, workspace_id=workspace_id, name=name, config=config)
        channel_id = ch.id
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return _frontend_redirect(vk_oauth="error", reason=detail)
    except Exception as exc:  # noqa: BLE001
        log.warning("VK OAuth channel save failed: %s", exc)
        return _frontend_redirect(vk_oauth="error", reason="Не удалось сохранить канал")
    finally:
        db.close()

    log.info("VK channel %s connected via OAuth for workspace %s", channel_id, workspace_id)
    return _frontend_redirect(vk_oauth="ok", channel_id=channel_id)
