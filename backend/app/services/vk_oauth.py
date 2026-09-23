"""VK OAuth (Authorization Code Flow) for connecting a community.

The application secret is used **only** on the server, and the access token
obtained in the callback never reaches the browser: it is kept in a
short-lived in-memory ticket until the admin picks a community.

Community token caveat. OAuth issues a *user* access token. Sending and
receiving messages on behalf of a community normally requires a *community*
token, which VK does not hand out over OAuth. `check_group_rights` reports
whether the user token is enough; when it is not, the caller offers the
manual token form instead. The token still authenticates the user, so the
fallback is one field short of done.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt

from ..config import settings

log = logging.getLogger("vk_oauth")

VK_OAUTH_AUTHORIZE = "https://oauth.vk.com/authorize"
VK_OAUTH_ACCESS_TOKEN = "https://oauth.vk.com/access_token"
VK_API = "https://api.vk.com/method"

# manage -- administer the community, messages -- send/receive, offline --
# a long-lived token (otherwise VK expires it in a day)
SCOPE = "manage,messages,offline"
STATE_TTL_SECONDS = 600
TICKET_TTL_SECONDS = 600

# Process-local stores. The service runs a single uvicorn worker, so a plain
# dict is enough. A restart only forces the admin to repeat the login.
_lock = threading.Lock()
_used_jti: dict[str, float] = {}
_tickets: dict[str, dict] = {}


def redirect_uri() -> str:
    """The callback URI registered on dev.vk.com.

    Must match byte for byte, otherwise VK rejects the exchange.
    """
    configured = (settings.vk_oauth_redirect_uri or "").strip()
    if configured:
        return configured
    return settings.public_base_url.rstrip("/") + "/api/v1/channels/vk/oauth/callback"


def frontend_url() -> str:
    return (settings.vk_oauth_frontend_url or "/app/").strip()


def build_authorize_url(state: str, group_ids: Optional[str] = None) -> str:
    params = {
        "client_id": settings.vk_app_id,
        "redirect_uri": redirect_uri(),
        "scope": SCOPE,
        "response_type": "code",
        "state": state,
        "v": settings.vk_api_version,
    }
    if group_ids:
        params["group_ids"] = group_ids
    return VK_OAUTH_AUTHORIZE + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# CSRF state
# ---------------------------------------------------------------------------

def make_state(user_id: int, workspace_id: Optional[int]) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "wid": workspace_id,
        "jti": secrets.token_urlsafe(16),
        "iat": now,
        "exp": now + STATE_TTL_SECONDS,
        "scope": "vk_oauth",
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def consume_state(state: Optional[str]) -> dict:
    """Verify the CSRF token and burn it so it cannot be replayed."""
    if not state:
        raise ValueError("Отсутствует параметр state")
    try:
        payload = jwt.decode(state, settings.secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise ValueError("Ссылка авторизации устарела, начните заново")
    except jwt.PyJWTError:
        raise ValueError("Некорректный state")
    if payload.get("scope") != "vk_oauth":
        raise ValueError("Некорректный state")

    jti = payload.get("jti") or ""
    exp = float(payload.get("exp") or (time.time() + STATE_TTL_SECONDS))
    now = time.time()
    with _lock:
        for k, deadline in list(_used_jti.items()):
            if deadline < now:
                _used_jti.pop(k, None)
        if jti in _used_jti:
            raise ValueError("Ссылка уже использована, начните заново")
        _used_jti[jti] = exp
    return payload


# ---------------------------------------------------------------------------
# VK calls
# ---------------------------------------------------------------------------

def exchange_code(code: str) -> dict:
    """Swap the authorization code for a user access token.

    Server side only: the application secret never leaves this function.
    """
    if not settings.vk_client_secret:
        raise RuntimeError("VK client_secret не настроен на сервере")
    data = {
        "client_id": settings.vk_app_id,
        "client_secret": settings.vk_client_secret,
        "redirect_uri": redirect_uri(),
        "code": code,
    }
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(VK_OAUTH_ACCESS_TOKEN, data=data)
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Нет связи с VK: {exc}")
    if not isinstance(payload, dict) or "access_token" not in payload:
        desc = (payload or {}).get("error_description") or (payload or {}).get("error") or "code отклонён"
        raise RuntimeError(f"VK отклонил обмен кода: {desc}")
    return payload


def get_admin_groups(access_token: str) -> list[dict]:
    """Communities where the user is an admin.

    `filter=admin` does the filtering on the VK side, and `extended=1` brings
    names back, so the SPA can render a picker without extra calls.
    """
    params = {
        "access_token": access_token,
        "v": settings.vk_api_version,
        "filter": "admin",
        "extended": 1,
        "fields": "photo_100,members_count",
    }
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(f"{VK_API}/groups.get", data=params)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Нет связи с VK: {exc}")
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"VK {err.get('error_code')}: {err.get('error_msg')}")
    items = ((data.get("response") or {}).get("items")) or []
    return [
        {
            "id": str(g.get("id")),
            "name": g.get("name") or "",
            "screen_name": g.get("screen_name") or "",
            "photo": g.get("photo_100") or "",
        }
        for g in items
    ]


def check_group_rights(access_token: str, group_id: str) -> dict:
    """Can this token run Long Poll for the group?

    A user token frequently cannot: most group methods want a community
    token. That is a soft result (`needs_community_token`), not an error --
    the caller offers the manual token form as the fallback.
    """
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                f"{VK_API}/groups.getLongPollSettings",
                data={"group_id": group_id, "access_token": access_token,
                      "v": settings.vk_api_version},
            )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Нет связи с VK: {exc}"}

    if "error" in data:
        err = data["error"]
        code = err.get("error_code")
        if code in (15, 27):
            return {"ok": False, "needs_community_token": True,
                    "detail": "Для приёма сообщений VK требует ключ группы"}
        if code == 100:
            # Parameters are off -- usually means Long Poll was never enabled.
            return {"ok": True, "detail": "Long Poll будет включён при подключении"}
        return {"ok": False, "detail": f"VK {code}: {err.get('error_msg')}"}

    lp = data.get("response") or {}
    if not lp.get("is_enabled"):
        return {"ok": True, "detail": "Long Poll будет включён при подключении"}
    if not (lp.get("events") or {}).get("message_new"):
        return {"ok": True, "detail": "Событие «новое сообщение» включится при подключении"}
    return {"ok": True, "detail": "Приём сообщений настроен"}


# ---------------------------------------------------------------------------
# Short-lived tickets: hold the token between "authorized" and "pick a group"
# ---------------------------------------------------------------------------

def create_ticket(user_id: int, workspace_id: Optional[int], access_token: str,
                  groups: list[dict]) -> str:
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for k, v in list(_tickets.items()):
            if v["exp"] < now:
                _tickets.pop(k, None)
        _tickets[ticket] = {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "access_token": access_token,
            "groups": groups,
            "exp": now + TICKET_TTL_SECONDS,
        }
    return ticket


def get_ticket(ticket: Optional[str], user_id: int) -> dict:
    if not ticket:
        raise ValueError("Не указан ticket авторизации")
    with _lock:
        payload = _tickets.get(ticket)
    if payload is None or payload["exp"] < time.time():
        raise ValueError("Сессия авторизации истекла, начните заново")
    if payload.get("user_id") != user_id:
        raise ValueError("Ticket принадлежит другому пользователю")
    return payload


def drop_ticket(ticket: Optional[str]) -> None:
    if not ticket:
        return
    with _lock:
        _tickets.pop(ticket, None)
