"""VK community authorization (OAuth Authorization Code Flow).

Why this shape. The application (`vk_app_id`) is a VK ID app whose only
community-related scope is the community Access token flow: it requires
`group_ids` up front and returns a *community* token (see ADR-022). It cannot
list the user's communities -- that needs the `groups` scope, which VK grants
only on a support request. So the admin points at one community (link or id),
and everything else is server side.

The application secret lives only here. The token obtained in the callback
never reaches the browser: it is written straight into the channel config
through the same `save_vk_channel` path the manual form uses, so nothing
downstream can tell the two flows apart.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
import jwt

from ..config import settings

log = logging.getLogger("vk_oauth")

VK_OAUTH_AUTHORIZE = "https://oauth.vk.com/authorize"
VK_OAUTH_ACCESS_TOKEN = "https://oauth.vk.com/access_token"
VK_API = "https://api.vk.com/method"

# community tokens allow exactly these scopes; offline is NOT among them
SCOPE = "manage,messages"
STATE_TTL_SECONDS = 600

_lock = threading.Lock()
_used_jti: dict[str, float] = {}

# vk.com/club123, m.vk.com/public123, vk.ru/event123, bare /123 ...
_LINK_RE = re.compile(r"(?:https?://)?(?:m\.)?(?:vk\.com|vk\.ru|vkontakte\.ru)/([^/?#\s]+)", re.I)
_PREFIXED_RE = re.compile(r"^(?:club|public|event)(\d+)$", re.I)
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def redirect_uri() -> str:
    """Callback URI; must match the app settings on VK byte for byte."""
    configured = (settings.vk_oauth_redirect_uri or "").strip()
    if configured:
        return configured
    return settings.public_base_url.rstrip("/") + "/api/v1/channels/vk/oauth/callback"


def frontend_url() -> str:
    return (settings.vk_oauth_frontend_url or "/app/").strip()


def build_authorize_url(state: str, group_id: str) -> str:
    params = {
        "client_id": settings.vk_app_id,
        "redirect_uri": redirect_uri(),
        "scope": SCOPE,
        "group_ids": group_id,          # mandatory for the community flow
        "response_type": "code",
        "state": state,
        "v": settings.vk_api_version,
    }
    return VK_OAUTH_AUTHORIZE + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# CSRF state
# ---------------------------------------------------------------------------

def make_state(user_id: int, workspace_id: Optional[int], group_id: str,
               channel_name: Optional[str] = None) -> str:
    """Signed, single-use state.

    Carries the target community so the callback does not have to trust any
    unsigned query parameter. The channel name is kept too, so a name typed
    in the form survives the round trip through VK.
    """
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "wid": workspace_id,
        "gid": str(group_id),
        "name": channel_name or None,
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
# Group link / id parsing
# ---------------------------------------------------------------------------

def parse_group_input(raw: str) -> dict:
    """Turn whatever the admin typed into a group id, or a name to resolve.

    Accepts `club123`, `public123`, `123`, `-123`, full links with either a
    numeric path or a human-readable screen name.
    """
    s = (raw or "").strip()
    if not s:
        return {"ok": False, "detail": "Укажите ссылку на группу"}
    if s.startswith("-"):
        s = s[1:]
    m = _LINK_RE.search(s)
    if m:
        s = m.group(1)
    if s.isdigit():
        return {"ok": True, "group_id": s}
    mp = _PREFIXED_RE.match(s)
    if mp:
        return {"ok": True, "group_id": mp.group(1)}
    if _NAME_RE.match(s):
        return {"ok": False, "needs_resolve": True, "screen_name": s}
    return {"ok": False, "detail": "Не похоже на ссылку или ID группы"}


def resolve_screen_name(screen_name: str) -> dict:
    """Map a readable community name to its numeric id.

    `groups.getById` accepts a screen name directly and works with the
    application service token -- unlike `utils.resolveScreenName`, which VK
    blocks for service tokens (error 1051). Without a service token only
    numeric links work, and the caller says so plainly.
    """
    token = (settings.vk_service_token or "").strip()
    if not token:
        return {"ok": False,
                "detail": "Для ссылок с коротким именем нужен сервисный ключ приложения. "
                          "Вставьте ссылку вида vk.com/club123456789 или числовой ID."}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{VK_API}/groups.getById",
                data={"group_id": screen_name, "access_token": token,
                      "v": settings.vk_api_version, "fields": "screen_name"},
            )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Нет связи с VK: {exc}"}

    if "error" in data:
        err = data["error"]
        code = err.get("error_code")
        if code == 100:
            return {"ok": False, "detail": "Сообщество с таким адресом не найдено. "
                                           "Проверьте ссылку или вставьте ID группы."}
        return {"ok": False, "detail": f"VK {code}: {err.get('error_msg')}"}
    groups = ((data.get("response") or {}).get("groups")) or []
    if not groups:
        return {"ok": False, "detail": "Сообщество с таким адресом не найдено"}
    return {"ok": True, "group_id": str(groups[0].get("id"))}


def resolve_group(raw: str) -> dict:
    """Full resolution: numeric kept as is, short name via VK."""
    parsed = parse_group_input(raw)
    if parsed.get("ok"):
        return parsed
    if parsed.get("needs_resolve"):
        return resolve_screen_name(parsed["screen_name"])
    return parsed


# ---------------------------------------------------------------------------
# VK calls
# ---------------------------------------------------------------------------

def exchange_code(code: str) -> dict:
    """Swap the authorization code for community token(s). Server side only."""
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
    if not isinstance(payload, dict) or "error" in payload or "error_description" in payload:
        desc = (payload or {}).get("error_description") or (payload or {}).get("error") or "код отклонён"
        raise RuntimeError(f"VK отклонил обмен кода: {desc}")
    return payload


def parse_community_tokens(payload: dict) -> list[dict]:
    """Pull `{group_id, access_token}` pairs out of the exchange response.

    VK returns a `groups` array; older shapes used `access_token_<id>` keys,
    so both are accepted.
    """
    out: list[dict] = []
    for item in payload.get("groups") or []:
        gid = str(item.get("group_id") or "").lstrip("-")
        tok = item.get("access_token") or ""
        if gid and tok:
            out.append({"group_id": gid, "access_token": tok})
    if out:
        return out
    for key, value in payload.items():
        m = re.match(r"access_token_-?(\d+)$", key)
        if m and value:
            out.append({"group_id": m.group(1), "access_token": value})
    return out


def fetch_group_info(group_id: str, access_token: str) -> dict:
    """Readable name and address; purely cosmetic, never fatal."""
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{VK_API}/groups.getById",
                data={"group_id": group_id, "access_token": access_token,
                      "v": settings.vk_api_version, "fields": "photo_100,screen_name"},
            )
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    groups = ((data.get("response") or {}).get("groups")) or []
    if not groups:
        return {}
    g = groups[0]
    return {"name": g.get("name") or "", "screen_name": g.get("screen_name") or ""}
