import re
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


_EMAIL_RE = re.compile(
    r"^(?![.])"                                  # local part must not start with a dot
    r"(?!.*\.\.)"                               # no consecutive dots anywhere
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}"    # local part
    r"(?<![.])"                                  # local part must not end with a dot
    r"@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"  # domain labels
    r"[A-Za-z]{2,63}$"                           # TLD
)


def _check_email(value: str) -> str:
    """Soft e-mail check.

    Deliberately does NOT use `EmailStr`: that rejects reserved domains such as
    `.local` / `.internal`, which an internal helpdesk must accept. Syntax is
    still validated so obvious garbage is refused.
    """
    v = (value or "").strip()
    if len(v) > 254 or not _EMAIL_RE.match(v):
        raise ValueError("invalid email address")
    return v


# ---------- auth ----------
class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        return _check_email(v)



class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserOut"


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


# ---------- users ----------
class UserBase(BaseModel):
    email: str
    name: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        return _check_email(v)
    is_active: bool = True
    is_admin: bool = False
    avatar_color: Optional[str] = None


class UserCreate(UserBase):
    password: str = Field(min_length=6)
    role: str = "agent"
    workspace_id: Optional[int] = None


class UserUpdate(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=6)
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    role: Optional[str] = None
    avatar_color: Optional[str] = None


class UserOut(ORMModel):
    id: int
    email: str
    name: str
    is_active: bool
    is_admin: bool
    role: str = "agent"
    workspace_id: Optional[int] = None
    avatar_color: Optional[str] = None
    created_at: datetime


# ---------- workspaces ----------
class WorkspaceOut(ORMModel):
    id: int
    slug: str
    name: str
    is_active: bool
    plan: str
    created_at: datetime


class WorkspaceCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=255)
    admin_email: str
    admin_name: Optional[str] = None
    admin_password: Optional[str] = Field(default=None, min_length=8)


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


# ---------- channels ----------
class ChannelOut(ORMModel):
    id: int
    workspace_id: Optional[int] = None
    type: str
    name: str
    enabled: bool
    config: dict
    webhook_secret: Optional[str] = None
    created_at: datetime


class ChannelCreate(BaseModel):
    type: str
    name: str
    enabled: bool = True
    config: dict = {}


class ChannelConnect(BaseModel):
    """Payload for self-service channel connection.

    For VK the admin supplies exactly two things: the group id and a token.
    Everything else (Long Poll, polling, webhook secrets) is set up by us.
    """
    type: str
    name: Optional[str] = None
    config: dict = {}


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    config: Optional[dict] = None


class VKOAuthConnect(BaseModel):
    """Pick one of the communities found after VK OAuth.

    `ticket` is the short-lived handle for the token obtained in the callback;
    the token itself never travels to the browser. `workspace_id` is only
    meaningful for a superadmin, who has no company of their own.
    """
    ticket: str
    group_id: str
    name: Optional[str] = None
    workspace_id: Optional[int] = None


class ChannelCreate(BaseModel):
    type: str
    name: str
    enabled: bool = True
    config: dict = {}


# ---------- contacts ----------
class ContactOut(ORMModel):
    id: int
    name: Optional[str]
    channel_type: str
    external_id: str
    email: Optional[str]
    phone: Optional[str]
    avatar_url: Optional[str]
    meta: dict
    created_at: datetime


class ContactUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


# ---------- messages ----------
class MessageOut(ORMModel):
    id: int
    conversation_id: int
    direction: str
    author_type: str
    author_user_id: Optional[int]
    author_contact_id: Optional[int]
    author_name: Optional[str] = None
    body: str
    attachments: List[Any]
    external_id: Optional[str]
    status: str
    error: Optional[str]
    created_at: datetime
    meta: dict


class MessageCreate(BaseModel):
    body: str = Field(min_length=1)
    attachments: List[Any] = []
    mark_status: Optional[str] = "resolved"


class MessageSentOut(MessageOut):
    """Response for POST /conversations/{id}/messages.

    Extends MessageOut with delivery feedback: the API never silently drops a
    requested status change.
    """
    status_applied: bool = True
    notice: Optional[str] = None


# ---------- conversations ----------
class ConversationOut(ORMModel):
    id: int
    channel_id: int
    contact_id: int
    subject: Optional[str]
    status: str
    priority: str
    assignee_id: Optional[int]
    tags: List[Any]
    meta: dict
    created_at: datetime
    updated_at: datetime
    last_message_at: Optional[datetime]
    last_inbound_at: Optional[datetime]
    last_outbound_at: Optional[datetime]
    first_inbound_at: Optional[datetime]
    first_response_at: Optional[datetime]
    closed_at: Optional[datetime]
    reopen_count: int
    first_response_seconds: Optional[int]
    last_response_seconds: Optional[int]

    channel: Optional[ChannelOut] = None
    contact: Optional[ContactOut] = None
    assignee: Optional[UserOut] = None

    last_message_preview: Optional[str] = None
    last_message_direction: Optional[str] = None
    messages_count: Optional[int] = None


class ConversationUpdate(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee_id: Optional[int] = None
    subject: Optional[str] = None
    tags: Optional[List[str]] = None


class ConversationCreate(BaseModel):
    channel_id: int
    contact_id: Optional[int] = None
    contact_external_id: Optional[str] = None
    contact_name: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None


class PaginatedConversations(BaseModel):
    items: List[ConversationOut]
    total: int
    page: int
    page_size: int


class PaginatedMessages(BaseModel):
    items: List[MessageOut]
    total: int


# ---------- metrics ----------
class MetricsOverview(BaseModel):
    range_from: datetime
    range_to: datetime
    conversations_total: int
    conversations_created: int
    conversations_resolved: int
    conversations_open: int
    conversations_pending: int
    conversations_unassigned: int
    backlog: int

    messages_inbound: int
    messages_outbound: int

    avg_first_response_seconds: Optional[float]
    median_first_response_seconds: Optional[float]
    avg_resolution_seconds: Optional[float]
    median_resolution_seconds: Optional[float]
    avg_messages_per_conversation: Optional[float]

    sla_target_seconds: int
    sla_met: int
    sla_total: int
    sla_compliance_pct: Optional[float]

    frt_buckets: dict


class AgentMetrics(BaseModel):
    user_id: Optional[int]
    name: str
    email: Optional[str]
    conversations_assigned: int
    conversations_resolved: int
    messages_outbound: int
    avg_first_response_seconds: Optional[float]
    avg_response_seconds: Optional[float]
    active_conversations: int


class ChannelMetrics(BaseModel):
    channel_id: int
    channel_type: str
    channel_name: str
    conversations: int
    messages_inbound: int
    messages_outbound: int
    avg_first_response_seconds: Optional[float]


class TimePoint(BaseModel):
    date: str
    created: int = 0
    resolved: int = 0
    inbound: int = 0
    outbound: int = 0


class Timeseries(BaseModel):
    interval: str
    points: List[TimePoint]


TokenResponse.model_rebuild()
