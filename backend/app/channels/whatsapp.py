"""WhatsApp adapter.

Contract (per provider docs):
  * incoming -> provider calls our webhook  POST /api/v1/webhooks/whatsapp/{channel_id}
  * outgoing -> we call  POST {base_url}{send_path}
        headers: Authorization: Bearer {token}
        body:    {"phone": "<msisdn>", "message": "<text>"}   (overridable via config.body_template)

Channel config keys (stored in Channel.config, override env):
  base_url, token, send_path, phone_field, message_field, body_template, extra_headers
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import settings
from ..models import Channel, Contact, Conversation
from .base import ChannelAdapter, SendResult


class WhatsAppAdapter(ChannelAdapter):
    type = "whatsapp"

    def __init__(self, channel: Channel, config: dict):
        super().__init__(channel, config)
        self.base_url = (self.config.get("base_url") or settings.whatsapp_base_url or "").rstrip("/")
        self.token = self.config.get("token") or settings.whatsapp_token or ""
        self.send_path = self.config.get("send_path") or settings.whatsapp_send_path or "/sendMessage"

    def send(self, conversation: Conversation, contact: Contact, body: str,
             attachments: Optional[list[Any]] = None) -> SendResult:
        if not self.base_url:
            return SendResult(ok=False, status="failed", error="whatsapp base_url is not configured")

        phone_field = self.config.get("phone_field", "phone")
        message_field = self.config.get("message_field", "message")
        template = self.config.get("body_template")

        target = contact.phone or contact.external_id
        if template:
            payload = _render(template, {"phone": target, "message": body, "name": contact.name or ""})
        else:
            payload = {phone_field: target, message_field: body}

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(self.config.get("extra_headers") or {})

        url = self.base_url + self.send_path
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(url, json=payload, headers=headers)
            data = _safe_json(resp)
            if resp.status_code >= 400:
                return SendResult(ok=False, status="failed",
                                  error=f"HTTP {resp.status_code}: {resp.text[:500]}", raw=data)
            external_id = None
            for key in ("id", "message_id", "messageId", "msg_id", "result"):
                if isinstance(data, dict) and key in data:
                    v = data[key]
                    external_id = str(v.get("id") if isinstance(v, dict) and "id" in v else v)
                    break
            return SendResult(ok=True, status="sent", external_id=external_id, raw=data if isinstance(data, dict) else {"raw": data})
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, status="failed", error=str(exc))


def _render(template: Any, values: dict) -> dict:
    """Render a payload template.

    template may be:
      * a dict, whose string values may contain {phone}, {message}, {name} placeholders
      * a flat string, treated as the "message" field (provider shape is then minimal)
    """
    if isinstance(template, dict):
        out = {}
        for k, v in template.items():
            if isinstance(v, str):
                try:
                    out[k] = v.format(**values)
                except (KeyError, IndexError, ValueError):
                    out[k] = v
            else:
                out[k] = v
        return out
    return dict(values)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:  # noqa: BLE001
        return {"text": resp.text[:1000]}
