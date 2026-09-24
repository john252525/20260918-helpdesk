"""WhatsApp providers.

`whatsapp` is one channel type with several vendors behind it. The vendor is
picked by `config.provider`; unknown values fall back to the custom form, so
channels created before providers existed keep working unchanged.
"""
from __future__ import annotations

from typing import Optional

from .custom import CustomProvider
from .touchapi import TouchApiProvider
from .base import WhatsAppProvider

# registration order defines the dropdown order in the UI
PROVIDERS: dict[str, type[WhatsAppProvider]] = {
    TouchApiProvider.key: TouchApiProvider,
    CustomProvider.key: CustomProvider,
}

DEFAULT_PROVIDER = CustomProvider.key


def get_provider(channel=None, config: Optional[dict] = None) -> WhatsAppProvider:
    """Instantiate the provider named in the config.

    Missing or unknown `provider` yields the custom implementation: that is
    exactly the behaviour channels had before providers were introduced.
    """
    cfg = config if config is not None else (getattr(channel, "config", None) or {})
    key = str((cfg or {}).get("provider") or DEFAULT_PROVIDER)
    cls = PROVIDERS.get(key, CustomProvider)
    return cls(channel, cfg)


def provider_catalog() -> list[dict]:
    """Provider list for the UI: the backend owns the dropdown."""
    return [cls(channel=None, config={}).catalog() for cls in PROVIDERS.values()]


__all__ = ["WhatsAppProvider", "PROVIDERS", "get_provider", "provider_catalog"]
