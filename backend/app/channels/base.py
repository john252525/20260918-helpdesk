from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import Channel, Contact, Conversation


@dataclass
class SendResult:
    ok: bool
    external_id: Optional[str] = None
    status: str = "sent"
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)


class ChannelAdapter:
    """Base class for outbound delivery. Inbound is handled by routers/webhooks."""

    type: str = "base"

    def __init__(self, channel: Channel, config: dict):
        self.channel = channel
        self.config = config or {}

    def send(self, conversation: Conversation, contact: Contact, body: str,
             attachments: Optional[list[Any]] = None) -> SendResult:
        raise NotImplementedError
