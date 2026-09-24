"""Touch-API WhatsApp provider.

Contract (verified against the live vendor API and its public collection):
  * base url         https://cloud.controller.touch-api.com/api
    (alt: https://controller.touch-api.com/api)
  * auth             body carries `token` (API key) + `login` (account), plus
                     `source` ("whatsapp"). No Authorization header.
  * outgoing         POST /sendMessage. Prefer `msg={thread, text}` -- the vendor
                     addresses chats by thread (`...@lid`), and a bare number in
                     `msg_to` fails for LID contacts with "Chat not found".
                     Delivery result lives in `results[].status`, not the top level.
                     -> {status, results:[{result:{item, thread, timestamp}}]}
  * account          POST /getInfoByToken {source, token, skipDetails}
                     -> {clients:[{login, activated, state, webhookUrls, ...}]}
                     POST /addAccount {source, token, login, webhookUrl?}
  * authorization    POST /setState {setState:true}; QR via GET /screenshot or
                     POST /getQr; phone-code via /enablePhoneAuth + /getAuthCode
  * hooks            POST /addWebhook | /deleteWebhook {source, token, login, webhookUrl}

Inbound webhook shape (from the vendor's public collection):
  { "from", "to", "time", "text", "source", "thread", "item",
    "outgoing", "replyTo", "content": [...], "hook_type":
      "message" | "message_status" | "add_message_reaction" }
Status types inside `content`: server, delivered, has_seen, pending, errored.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ...config import settings
from ...models import Channel, Contact, Conversation
from .base import WhatsAppProvider
from ..base import SendResult

log = logging.getLogger("whatsapp.touchapi")

DEFAULT_BASE = "https://cloud.controller.touch-api.com/api"

# vendor status -> our Message.status
_STATUS_MAP = {
    "server": "sent",
    "delivered": "delivered",
    "has_seen": "read",
    "read": "read",
    "pending": "pending",
    "errored": "failed",
    "error": "failed",
}


class TouchApiProvider(WhatsAppProvider):
    key = "touch-api"
    title = "Touch-API"
    supports_discovery = True

    def __init__(self, channel: Channel = None, config: dict = None):
        super().__init__(channel, config)
        self.base_url = (self.config.get("base_url")
                         or settings.whatsapp_base_url or DEFAULT_BASE).rstrip("/")
        self.token = self.config.get("token") or settings.whatsapp_token or ""
        self.login = str(self.config.get("login") or "")
        self.source = self.config.get("source") or "whatsapp"

    # ------------------------------------------------------------------ http
    # account lifecycle calls make the vendor spin a session up or down and
    # can take much longer than a plain read; give them room instead of
    # surfacing a timeout as a 500
    # getInfo is measured at 15s+ for some accounts, and it is polled while
    # the admin scans the QR, so it belongs here too
    SLOW_METHODS = {"setState", "addAccount", "deleteAccount", "forceStop",
                    "clearSession", "getNewProxy", "screenshot", "getInfo"}

    def _call(self, method: str, timeout: int = 30, **params) -> dict:
        """POST a vendor method and always return a JSON-shaped dict.

        Network failures become `{"status": "error", ...}` so callers can
        report a readable message; a vendor outage must not turn into an
        unhandled exception.
        """
        payload = {"source": self.source, "token": self.token, **params}
        if method in self.SLOW_METHODS:
            timeout = max(timeout, 90)
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(f"{self.base_url}/{method}", json=payload)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error",
                    "error": {"message": f"Провайдер не ответил ({method}): {exc}"}}
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"status": "error",
                    "error": {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"}}

    @staticmethod
    def _error_text(data: dict) -> str:
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err or data.get("status") or "ошибка провайдера")

    # -------------------------------------------------------------- outbound
    def send(self, conversation: Conversation, contact: Contact, body: str,
             attachments: Optional[list[Any]] = None) -> SendResult:
        if not self.token or not self.login:
            return SendResult(ok=False, status="failed",
                              error="Touch-API: не заданы token/login")

        # Address by the chat thread the vendor gave us (`...@lid`, `...@c.us`).
        # A bare number in `msg_to` yields "Chat not found or created" for LID
        # contacts, which is most of them nowadays.
        thread = str(((contact.meta or {}).get("thread") or "")).strip()
        try:
            if thread:
                data = self._call("sendMessage", login=self.login,
                                  msg={"thread": thread, "text": body})
            else:
                data = self._call("sendMessage", login=self.login,
                                  msg_to=_digits(contact.phone or contact.external_id),
                                  msg_text=body)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, status="failed", error=str(exc))

        # The vendor answers HTTP 200 with `status: "ok"` even when delivery
        # failed: the real verdict is per-item, inside `results[]`. Checking
        # only the top-level status reports a failed send as delivered.
        if data.get("status") == "error":
            return SendResult(ok=False, status="failed",
                              error=f"Touch-API: {self._error_text(data)}", raw=data)

        results = data.get("results") or []
        first = results[0] if results and isinstance(results[0], dict) else {}
        if first.get("status") == "error" or first.get("error"):
            reason = first.get("error") or "доставка не удалась"
            return SendResult(ok=False, status="failed",
                              error=f"Touch-API: {reason}", raw=data)

        result = first.get("result") or {}
        external_id = result.get("item") or result.get("thread")
        if not external_id:
            return SendResult(ok=False, status="failed",
                              error="Touch-API: провайдер не вернул id сообщения", raw=data)
        return SendResult(ok=True, status="sent",
                          external_id=str(external_id), raw=data)

    # --------------------------------------------------------------- inbound
    def normalize_inbound(self, raw: Any) -> list[dict]:
        """Map one Touch-API webhook into ingest rows.

        A single webhook is one event: a message, a status update or a
        reaction. Status events are returned separately so the router can
        update an existing Message instead of creating a new one.
        """
        if not isinstance(raw, dict):
            return None
        hook_type = raw.get("hook_type") or ""
        outgoing = bool(raw.get("outgoing"))

        if hook_type == "message_status":
            statuses = [c.get("type") for c in (raw.get("content") or [])
                        if isinstance(c, dict) and c.get("type")]
            mapped = next((_STATUS_MAP[s] for s in statuses if s in _STATUS_MAP), None)
            if not mapped or not raw.get("item"):
                return []
            return [{
                "status_update": True,
                "message_id": str(raw.get("item")),
                "status": mapped,
                "meta": {"raw": raw},
            }]

        if hook_type == "add_message_reaction":
            # recognised, but deliberately ignored: nothing to ingest
            return []

        if hook_type and hook_type != "message":
            # a hook kind we do not model yet: recognised, intentionally skipped
            return []

        # hook_type is empty or absent -> payload shape is unknown to us
        if not hook_type:
            return None

        # `outgoing` marks messages the account sent. Some are our own replies
        # echoed back (skip those -- they are already stored), others were
        # written from another device and must appear in the thread. The
        # router tells the two apart by the stored message id.
        peer = _peer_from(raw)
        if not peer:
            return []

        body = raw.get("text") or ""
        attachments = _content_to_attachments(raw.get("content"))
        if not body and not attachments:
            body = _content_placeholder(raw.get("content"))

        created_at = _to_datetime(raw.get("time"))
        return [{
            "external_id": peer,
            "body": body,
            "name": raw.get("pushname") or raw.get("name"),
            "phone": peer,
            "message_id": str(raw["item"]) if raw.get("item") else None,
            "created_at": created_at,
            "attachments": attachments,
            "outbound": outgoing,
            "meta": {"raw": raw, "thread": raw.get("thread")},
        }]

    # -------------------------------------------------------- lifecycle api
    # Thin public wrappers so routers never reach into `_call` directly.
    def add_account(self, login: str) -> dict:
        return self._call("addAccount", login=login)

    def delete_account(self, login: str) -> dict:
        return self._call("deleteAccount", login=login)

    def account_info(self, login: str = "") -> dict:
        return self._call("getInfo", login=login or self.login)

    def start_account(self, login: str = "") -> dict:
        return self._call("setState", login=login or self.login, setState=True)

    def stop_account(self, login: str = "") -> dict:
        return self._call("setState", login=login or self.login, setState=False)

    def add_webhook(self, url: str, login: str = "") -> dict:
        return self._call("addWebhook", login=login or self.login, webhookUrl=url)

    def remove_webhook(self, url: str, login: str = "") -> dict:
        return self._call("deleteWebhook", login=login or self.login, webhookUrl=url)

    def qr_string(self, login: str = "") -> dict:
        return self._call("getQr", login=login or self.login)

    def force_stop(self, login: str = "") -> dict:
        return self._call("forceStop", login=login or self.login)

    def get_new_proxy(self, login: str = "") -> dict:
        return self._call("getNewProxy", login=login or self.login)

    def clear_session(self, login: str = "") -> dict:
        return self._call("clearSession", login=login or self.login)

    def status_map(self) -> dict:
        """Statuses for every account of this token in one call.

        A per-account `getInfo` costs ~30s at this vendor, while
        `getInfoByToken(skipDetails=False)` returns steps for the whole token
        in a few seconds -- the difference between a usable channel list and
        a frozen one.
        """
        data = self._call("getInfoByToken", skipDetails=False)
        if data.get("status") == "error":
            return {"ok": False, "detail": self._error_text(data)}
        out: dict = {}
        for c in data.get("clients") or []:
            step = c.get("step") or {}
            out[str(c.get("login"))] = {
                "state": bool(c.get("state")),
                "activated": bool(c.get("activated")),
                "step": step.get("value") if isinstance(step, dict) else None,
                "step_message": step.get("message") if isinstance(step, dict) else None,
            }
        return {"ok": True, "accounts": out}

    @staticmethod
    def error_text(data: dict) -> str:
        """Readable message from a vendor error payload."""
        return TouchApiProvider._error_text(data)

    # ------------------------------------------------------------- discovery
    def discover(self) -> dict:
        """List the accounts available for this token."""
        data = self._call("getInfoByToken", skipDetails=True)
        if data.get("status") == "error":
            return {"ok": False, "detail": self._error_text(data)}
        accounts = []
        for c in data.get("clients") or []:
            accounts.append({
                "login": c.get("login"),
                "activated": bool(c.get("activated")),
                "state": bool(c.get("state")),
                "title": c.get("login"),
            })
        return {"ok": True, "accounts": accounts,
                "summary": data.get("summary") or {}}

    # ---------------------------------------------------------------- catalog
    def catalog(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "supports_discovery": True,
            "fields": [
                {"name": "token", "label": "API-токен Touch-API",
                 "placeholder": "ваш токен из личного кабинета", "required": True},
            ],
        }


# ---------------------------------------------------------------- helpers
def _peer_from(raw: dict) -> str:
    """The contact id behind a message event.

    The chat `thread` is authoritative and identical for both directions
    (`105205425295594@lid`); `from`/`to` swap depending on who wrote. Falls
    back to those fields when no thread is present.
    """
    thread = str(raw.get("thread") or "")
    if thread:
        return _digits(thread.split("@")[0])
    return _digits(raw.get("from") or raw.get("to") or "")


def _digits(value: Any) -> str:
    """Touch-API wants E.164 without symbols; keep digits only."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _to_datetime(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        num = float(ts)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    # heuristics: seconds vs milliseconds vs microseconds
    if num > 10**14:
        num /= 1_000_000
    elif num > 10**11:
        num /= 1000
    return datetime.fromtimestamp(num, tz=timezone.utc)


def _content_to_attachments(content: Any) -> list[dict]:
    out = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type")
        if ctype in (None, "", "server", "delivered", "has_seen", "pending", "errored"):
            continue
        out.append({"type": ctype, "url": c.get("src"), "filename": c.get("filename")})
    return out


def _content_placeholder(content: Any) -> str:
    for c in content or []:
        if isinstance(c, dict) and c.get("type"):
            return f"[{c['type']}]"
    return ""
