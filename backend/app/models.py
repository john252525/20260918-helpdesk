from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Workspace(Base, TimestampMixin):
    """A tenant: one company / project with its own people and channels."""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(32), default="default", nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # NULL workspace_id == platform-level superadmin (sees every tenant)
    workspace_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workspaces.id"), index=True, nullable=True
    )
    role: Mapped[str] = mapped_column(String(16), default="agent", nullable=False)
    # role: superadmin | admin | agent

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # legacy
    avatar_color: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)


class Channel(Base, TimestampMixin):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workspaces.id"), index=True, nullable=True
    )
    type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)  # whatsapp | vk | email
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # unguessable token in the webhook URL: /webhooks/whatsapp/{id}/{secret}
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)

    __table_args__ = (UniqueConstraint("workspace_id", "type", "name", name="uq_channel_ws_type_name"),)


class Contact(Base, TimestampMixin):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workspaces.id"), index=True, nullable=True
    )
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    channel_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "channel_type", "external_id",
                         name="uq_contact_ws_channel_external"),
    )

    @property
    def display_name(self) -> str:
        return self.name or self.email or self.phone or self.external_id


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workspaces.id"), index=True, nullable=True
    )
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True, nullable=False)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="open", index=True, nullable=False)
    # open | pending | resolved | closed
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    # low | normal | high | urgent
    assignee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    last_inbound_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_inbound_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    reopen_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # derived, cached for fast list rendering
    first_response_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_response_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    channel: Mapped[Channel] = relationship(lazy="joined")
    contact: Mapped[Contact] = relationship(lazy="joined")
    assignee: Mapped[Optional[User]] = relationship(lazy="joined")

    __table_args__ = (
        Index("ix_conv_status_lastmsg", "status", "last_message_at"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True, nullable=False)

    direction: Mapped[str] = mapped_column(String(16), index=True, nullable=False)  # inbound | outbound
    author_type: Mapped[str] = mapped_column(String(16), nullable=False)  # contact | agent | system
    author_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    author_contact_id: Mapped[Optional[int]] = mapped_column(ForeignKey("contacts.id"), nullable=True)

    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attachments: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    external_id: Mapped[Optional[str]] = mapped_column(String(255), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="sent", nullable=False)
    # queued | sent | delivered | read | failed | received
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    author_user: Mapped[Optional[User]] = relationship(lazy="joined")

    __table_args__ = (
        Index("ix_msg_conv_created", "conversation_id", "created_at"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("conversations.id"), index=True, nullable=True
    )
    type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    actor_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)


class WebhookLog(Base):
    __tablename__ = "webhook_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    channel_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
