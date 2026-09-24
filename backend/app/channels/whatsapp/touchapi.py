"""Touch-API WhatsApp provider.

Contract (verified against the live vendor API and its public collection):
  * base url         https://cloud.controller.touch-api.com/api
    (alt: https://controller.touch-api.com/api)
  * auth             body carries `token` (API key) + `login` (account), plus
                     `source` ("whatsapp"). No Authorization header.
  * outgoing         POST /sendMessage  {source, token, login, msg_to, msg_text}
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
    SLOW_METHODS = {"setState", "addAccount", "deleteAccount", "forceStop",
                    "clearSession", "getNewProxy", "screenshot"}

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
        target = _digits(contact.phone or contact.external_id)
        try:
            data = self._call("sendMessage", login=self.login,
                              msg_to=target, msg_text=body)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, status="failed", error=str(exc))

        if data.get("status") == "error":
            return SendResult(ok=False, status="failed",
                              error=f"Touch-API: {self._error_text(data)}", raw=data)

        external_id = None
        results = data.get("results") or []
        if results and isinstance(results[0], dict):
            result = results[0].get("result") or {}
            external_id = result.get("item") or result.get("thread")
        return SendResult(ok=True, status="sent",
                          external_id=str(external_id) if external_id else None,
                          raw=data)

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

        # incoming vs our own echo: `outgoing` marks messages the account sent.
        # Echoes are skipped -- we already store them when the operator replies.
        if outgoing:
            return []

        peer = _digits(raw.get("from") or raw.get("thread") or "")
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
            "meta": {"raw": raw, "thread": raw.get("thread")},
        }]

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
