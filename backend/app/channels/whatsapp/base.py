"""Common interface for WhatsApp providers.

Providers differ by vendor: Touch-API speaks its own HTTP contract, while the
"custom" one is the generic URL+token+template form. The channel type stays
`whatsapp`; `config.provider` selects the implementation, so neither the
dispatch layer nor the webhook router needs to know the vendor.
"""
from __future__ import annotations

from typing import Any, Optional

from ..base import SendResult


class WhatsAppProvider:
    """One vendor behind the `whatsapp` channel type."""

    key: str = "base"
    title: str = "Провайдер"
    supports_discovery: bool = False

    def __init__(self, channel: Any = None, config: Optional[dict] = None):
        self.channel = channel
        self.config = config or {}

    # ---- outbound ----
    def send(self, conversation: Any, contact: Any, body: str,
             attachments: Optional[list[Any]] = None) -> SendResult:
        raise NotImplementedError

    # ---- inbound ----
    def normalize_inbound(self, raw: Any) -> Optional[list[dict]]:
        """Turn a provider webhook payload into ingest-ready items.

        Return value carries meaning:
          * `None` -- the payload is not recognised; the caller may fall back
            to the tolerant generic normalizer.
          * a list (possibly empty) -- the payload *was* recognised, so an
            empty list means "this event is intentionally ignored" (our own
            echo, a reaction). The caller must not fall back in that case.

        Each item is either a message (keys `external_id`, `body`, ...) or a
        status update (`status_update`: True, `message_id`, `status`).
        """
        return None

    # ---- connection (optional, discovery-capable vendors only) ----
    def catalog(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "supports_discovery": self.supports_discovery,
        }
