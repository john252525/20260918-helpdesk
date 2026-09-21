"""Email adapter: SMTP for outbound, IMAP poller for inbound."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Optional

from ..config import settings
from ..models import Channel, Contact, Conversation
from .base import ChannelAdapter, SendResult


class EmailAdapter(ChannelAdapter):
    type = "email"

    def __init__(self, channel: Channel, config: dict):
        super().__init__(channel, config)
        self.smtp_host = self.config.get("smtp_host") or settings.smtp_host or ""
        self.smtp_port = int(self.config.get("smtp_port") or settings.smtp_port or 465)
        self.smtp_user = self.config.get("smtp_user") or settings.smtp_user or ""
        self.smtp_password = self.config.get("smtp_password") or settings.smtp_password or ""
        self.use_ssl = bool(self.config.get("smtp_use_ssl", settings.smtp_use_ssl))
        self.from_address = self.config.get("from_address") or self.smtp_user
        self.from_name = self.config.get("from_name") or self.channel.name

    def send(self, conversation: Conversation, contact: Contact, body: str,
             attachments: Optional[list[Any]] = None) -> SendResult:
        if not self.smtp_host or not self.from_address:
            return SendResult(ok=False, status="failed",
                              error="email smtp_host/from_address is not configured")

        to_addr = contact.email or contact.external_id
        if not to_addr or "@" not in str(to_addr):
            return SendResult(ok=False, status="failed", error="contact has no valid email")

        msg = EmailMessage()
        msg["From"] = formataddr((self.from_name, self.from_address))
        msg["To"] = to_addr
        msg["Subject"] = _subject(conversation)
        msg_id = make_msgid(domain=(self.from_address.split("@")[-1] or "localhost"))
        msg["Message-ID"] = msg_id
        if conversation.meta and conversation.meta.get("email_message_id"):
            msg["In-Reply-To"] = conversation.meta["email_message_id"]
            msg["References"] = conversation.meta["email_message_id"]
        msg.set_content(body)

        try:
            context = ssl.create_default_context()
            if self.use_ssl:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=30) as server:
                    if self.smtp_user:
                        server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                    server.starttls(context=context)
                    if self.smtp_user:
                        server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
            return SendResult(ok=True, status="sent", external_id=msg_id, raw={"message_id": msg_id})
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, status="failed", error=str(exc))


def _subject(conversation: Conversation) -> str:
    base = conversation.subject or "Support"
    if base.lower().startswith("re:"):
        return base
    return f"Re: {base}"
