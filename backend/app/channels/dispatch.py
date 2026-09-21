from __future__ import annotations

from ..models import Channel
from .base import ChannelAdapter, SendResult
from .email import EmailAdapter
from .vk import VKAdapter
from .whatsapp import WhatsAppAdapter

ADAPTERS = {
    "whatsapp": WhatsAppAdapter,
    "vk": VKAdapter,
    "email": EmailAdapter,
}


def get_adapter(channel: Channel) -> ChannelAdapter:
    cls = ADAPTERS.get(channel.type)
    if cls is None:
        raise ValueError(f"Unknown channel type: {channel.type}")
    return cls(channel, channel.config or {})


__all__ = ["get_adapter", "ChannelAdapter", "SendResult"]
