from __future__ import annotations

from ..models import Channel
from .base import ChannelAdapter, SendResult
from .email import EmailAdapter
from .vk import VKAdapter
from . import whatsapp as whatsapp_providers

ADAPTERS = {
    "vk": VKAdapter,
    "email": EmailAdapter,
}


def get_adapter(channel: Channel) -> ChannelAdapter:
    """Pick the outbound implementation for a channel.

    `whatsapp` is a family: the concrete vendor comes from `config.provider`
    (see channels/whatsapp). Every other type maps to one class.
    """
    if channel.type == "whatsapp":
        return whatsapp_providers.get_provider(channel)
    cls = ADAPTERS.get(channel.type)
    if cls is None:
        raise ValueError(f"Unknown channel type: {channel.type}")
    return cls(channel, channel.config or {})


__all__ = ["get_adapter", "ChannelAdapter", "SendResult"]
