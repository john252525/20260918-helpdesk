"""VK group messages adapter.

Incoming:
  * Callback API  -> POST /api/v1/webhooks/vk/{channel_id}   (confirmation + message_new)
  * Bots Long Poll -> background poller in services/vk_poller.py

Outgoing: VK API  messages.send
  GET/POST https://api.vk.com/method/messages.send
       params: group_id, peer_id, message, random_id, access_token, v=5.199
"""
from __future__ import annotations

import random
from typing import Any, Optional

import httpx

from ..config import settings
from ..models import Channel, Contact, Conversation
from .base import ChannelAdapter, SendResult

VK_API = "https://api.vk.com/method"


class VKAdapter(ChannelAdapter):
    type = "vk"

    def __init__(self, channel: Channel, config: dict):
        super().__init__(channel, config)
        self.token = self.config.get("access_token") or settings.vk_access_token or ""
        self.group_id = str(self.config.get("group_id") or settings.vk_group_id or "")
        self.api_version = str(self.config.get("api_version") or settings.vk_api_version or "5.199")

    def send(self, conversation: Conversation, contact: Contact, body: str,
             attachments: Optional[list[Any]] = None) -> SendResult:
        if not self.token or not self.group_id:
            return SendResult(ok=False, status="failed",
                              error="vk access_token/group_id is not configured")

        params = {
            "peer_id": contact.external_id,
            "message": body,
            "random_id": random.randint(1, 2_000_000_000),
            "access_token": self.token,
            "v": self.api_version,
        }
        if attachments:
            params["attachment"] = ",".join(str(a) for a in attachments)

        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(f"{VK_API}/messages.send", data=params)
            data = resp.json()
            if "error" in data:
                err = data["error"]
                return SendResult(ok=False, status="failed",
                                  error=f"VK {err.get('error_code')}: {err.get('error_msg')}", raw=data)
            return SendResult(ok=True, status="sent",
                              external_id=str(data.get("response")), raw=data)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, status="failed", error=str(exc))


def vk_api_call(method: str, token: str, params: dict) -> dict:
    payload = {**params, "access_token": token, "v": settings.vk_api_version}
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{VK_API}/{method}", data=payload)
    return resp.json()
