from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..models import Channel, Conversation, Message, User
from ..schemas import AgentMetrics, ChannelMetrics, MetricsOverview, TimePoint, Timeseries


def _dialect(db: Session) -> str:
    return db.get_bind().dialect.name


def _duration_seconds(db: Session, start_col, end_col):
    """Seconds between two timestamps, as a SQL expression.

    SQLite has `julianday()`, Postgres has `EXTRACT(EPOCH FROM ...)`.
    """
    if _dialect(db) == "postgresql":
        return func.extract("epoch", end_col - start_col)
    return (func.julianday(end_col) - func.julianday(start_col)) * 86400.0


def _bucket_expr(db: Session, col, interval: str):
    """Truncate a timestamp to a day or hour, as a SQL expression."""
    if _dialect(db) == "postgresql":
        pattern = "YYYY-MM-DD" if interval == "day" else "YYYY-MM-DD HH24:00"
        return func.to_char(col, pattern)
    pattern = "%Y-%m-%d" if interval == "day" else "%Y-%m-%d %H:00"
    return func.strftime(pattern, col)


def _percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    vals = sorted(values)
    k = (len(vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return float(vals[f])
    return float(vals[f] + (vals[c] - vals[f]) * (k - f))


def overview(db: Session, dt_from: datetime, dt_to: datetime, sla_seconds: int = 900,
             scope: int | None = None) -> MetricsOverview:
    conv_in_range = select(Conversation).where(
        Conversation.created_at >= dt_from, Conversation.created_at <= dt_to
    )
    if scope is not None:
        conv_in_range = conv_in_range.where(Conversation.workspace_id == scope)
    total = db.scalar(select(func.count()).select_from(conv_in_range.subquery())) or 0

    created = total
    resolved_q = select(Conversation).where(
        Conversation.closed_at.is_not(None),
        Conversation.closed_at >= dt_from,
        Conversation.closed_at <= dt_to,
    )
    if scope is not None:
        resolved_q = resolved_q.where(Conversation.workspace_id == scope)
    resolved = db.scalar(select(func.count()).select_from(resolved_q.subquery())) or 0

    status_q = select(Conversation.status, func.count()).group_by(Conversation.status)
    if scope is not None:
        status_q = status_q.where(Conversation.workspace_id == scope)
    status_counts = dict(db.execute(status_q).all())
    unassigned_q = select(func.count()).where(
        Conversation.status.in_(("open", "pending")),
        Conversation.assignee_id.is_(None),
    )
    if scope is not None:
        unassigned_q = unassigned_q.where(Conversation.workspace_id == scope)
    unassigned = db.scalar(unassigned_q) or 0

    inbound_q = select(func.count()).select_from(Message).where(
        Message.direction == "inbound",
        Message.created_at >= dt_from, Message.created_at <= dt_to,
    )
    outbound_q = select(func.count()).select_from(Message).where(
        Message.direction == "outbound",
        Message.created_at >= dt_from, Message.created_at <= dt_to,
    )
    if scope is not None:
        # messages belong to a workspace through their conversation
        inbound_q = inbound_q.join(Conversation, Message.conversation_id == Conversation.id) \
            .where(Conversation.workspace_id == scope)
        outbound_q = outbound_q.join(Conversation, Message.conversation_id == Conversation.id) \
            .where(Conversation.workspace_id == scope)
    inbound = db.scalar(inbound_q) or 0
    outbound = db.scalar(outbound_q) or 0

    frt_q = select(Conversation.first_response_seconds).where(
        Conversation.first_response_seconds.is_not(None),
        Conversation.created_at >= dt_from, Conversation.created_at <= dt_to,
    )
    if scope is not None:
        frt_q = frt_q.where(Conversation.workspace_id == scope)
    frt_rows = db.scalars(frt_q).all()
    frts = [float(x) for x in frt_rows if x is not None]

    res_q = select(_duration_seconds(db, Conversation.created_at, Conversation.closed_at)).where(
        Conversation.closed_at.is_not(None),
        Conversation.closed_at >= dt_from, Conversation.closed_at <= dt_to,
    )
    if scope is not None:
        res_q = res_q.where(Conversation.workspace_id == scope)
    res_rows = db.scalars(res_q).all()
    resolutions = [float(x) for x in res_rows if x is not None]

    msgs_per_conv = (inbound + outbound) / total if total else None

    sla_met = sum(1 for x in frts if x <= sla_seconds)
    sla_total = len(frts)

    buckets = {
        "<=5m": sum(1 for x in frts if x <= 300),
        "5-15m": sum(1 for x in frts if 300 < x <= 900),
        "15-60m": sum(1 for x in frts if 900 < x <= 3600),
        "1-4h": sum(1 for x in frts if 3600 < x <= 14400),
        ">4h": sum(1 for x in frts if x > 14400),
    }

    total_q = select(func.count()).select_from(Conversation)
    if scope is not None:
        total_q = total_q.where(Conversation.workspace_id == scope)
    return MetricsOverview(
        range_from=dt_from,
        range_to=dt_to,
        conversations_total=db.scalar(total_q) or 0,
        conversations_created=created,
        conversations_resolved=resolved,
        conversations_open=status_counts.get("open", 0),
        conversations_pending=status_counts.get("pending", 0),
        conversations_unassigned=unassigned,
        backlog=status_counts.get("open", 0) + status_counts.get("pending", 0),
        messages_inbound=inbound,
        messages_outbound=outbound,
        avg_first_response_seconds=(sum(frts) / len(frts)) if frts else None,
        median_first_response_seconds=_percentile(frts, 0.5),
        avg_resolution_seconds=(sum(resolutions) / len(resolutions)) if resolutions else None,
        median_resolution_seconds=_percentile(resolutions, 0.5),
        avg_messages_per_conversation=msgs_per_conv,
        sla_target_seconds=sla_seconds,
        sla_met=sla_met,
        sla_total=sla_total,
        sla_compliance_pct=(100.0 * sla_met / sla_total) if sla_total else None,
        frt_buckets=buckets,
    )


def by_agent(db: Session, dt_from: datetime, dt_to: datetime,
             scope: int | None = None) -> list[AgentMetrics]:
    user_q = select(User).order_by(User.name)
    if scope is not None:
        user_q = user_q.where(User.workspace_id == scope)
    users = list(db.scalars(user_q))
    result: list[AgentMetrics] = []

    for user in users:
        assigned = db.scalar(
            select(func.count()).select_from(Conversation).where(
                Conversation.assignee_id == user.id,
                Conversation.created_at >= dt_from, Conversation.created_at <= dt_to,
            )
        ) or 0
        resolved = db.scalar(
            select(func.count()).select_from(Conversation).where(
                Conversation.assignee_id == user.id,
                Conversation.closed_at.is_not(None),
                Conversation.closed_at >= dt_from, Conversation.closed_at <= dt_to,
            )
        ) or 0
        outbound = db.scalar(
            select(func.count()).select_from(Message).where(
                Message.author_user_id == user.id,
                Message.direction == "outbound",
                Message.created_at >= dt_from, Message.created_at <= dt_to,
            )
        ) or 0
        active = db.scalar(
            select(func.count()).select_from(Conversation).where(
                Conversation.assignee_id == user.id,
                Conversation.status.in_(("open", "pending")),
            )
        ) or 0
        frts = [
            float(x) for x in db.scalars(
                select(Conversation.first_response_seconds).where(
                    Conversation.assignee_id == user.id,
                    Conversation.first_response_seconds.is_not(None),
                    Conversation.created_at >= dt_from, Conversation.created_at <= dt_to,
                )
            ).all() if x is not None
        ]
        resp = [
            float(x) for x in db.scalars(
                select(Conversation.last_response_seconds).where(
                    Conversation.assignee_id == user.id,
                    Conversation.last_response_seconds.is_not(None),
                    Conversation.created_at >= dt_from, Conversation.created_at <= dt_to,
                )
            ).all() if x is not None
        ]
        result.append(
            AgentMetrics(
                user_id=user.id,
                name=user.name,
                email=user.email,
                conversations_assigned=assigned,
                conversations_resolved=resolved,
                messages_outbound=outbound,
                avg_first_response_seconds=(sum(frts) / len(frts)) if frts else None,
                avg_response_seconds=(sum(resp) / len(resp)) if resp else None,
                active_conversations=active,
            )
        )
    return result


def by_channel(db: Session, dt_from: datetime, dt_to: datetime,
               scope: int | None = None) -> list[ChannelMetrics]:
    out: list[ChannelMetrics] = []
    ch_q = select(Channel).order_by(Channel.id)
    if scope is not None:
        ch_q = ch_q.where(Channel.workspace_id == scope)
    channels = list(db.scalars(ch_q))
    for ch in channels:
        convs = db.scalar(
            select(func.count()).select_from(Conversation).where(
                Conversation.channel_id == ch.id,
                Conversation.created_at >= dt_from, Conversation.created_at <= dt_to,
            )
        ) or 0
        inb = db.scalar(
            select(func.count()).select_from(Message).join(
                Conversation, Message.conversation_id == Conversation.id
            ).where(
                Conversation.channel_id == ch.id,
                Message.direction == "inbound",
                Message.created_at >= dt_from, Message.created_at <= dt_to,
            )
        ) or 0
        outb = db.scalar(
            select(func.count()).select_from(Message).join(
                Conversation, Message.conversation_id == Conversation.id
            ).where(
                Conversation.channel_id == ch.id,
                Message.direction == "outbound",
                Message.created_at >= dt_from, Message.created_at <= dt_to,
            )
        ) or 0
        frts = [
            float(x) for x in db.scalars(
                select(Conversation.first_response_seconds).where(
                    Conversation.channel_id == ch.id,
                    Conversation.first_response_seconds.is_not(None),
                    Conversation.created_at >= dt_from, Conversation.created_at <= dt_to,
                )
            ).all() if x is not None
        ]
        out.append(
            ChannelMetrics(
                channel_id=ch.id,
                channel_type=ch.type,
                channel_name=ch.name,
                conversations=convs,
                messages_inbound=inb,
                messages_outbound=outb,
                avg_first_response_seconds=(sum(frts) / len(frts)) if frts else None,
            )
        )
    return out


def timeseries(db: Session, dt_from: datetime, dt_to: datetime, interval: str = "day",
               scope: int | None = None) -> Timeseries:
    created_bucket = _bucket_expr(db, Conversation.created_at, interval)
    closed_bucket = _bucket_expr(db, Conversation.closed_at, interval)
    msg_bucket = _bucket_expr(db, Message.created_at, interval)

    created_q = select(created_bucket, func.count()).where(
        Conversation.created_at >= dt_from, Conversation.created_at <= dt_to)
    resolved_q = select(closed_bucket, func.count()).where(
        Conversation.closed_at.is_not(None),
        Conversation.closed_at >= dt_from, Conversation.closed_at <= dt_to)
    msg_q = select(msg_bucket, Message.direction, func.count()).where(
        Message.created_at >= dt_from, Message.created_at <= dt_to)

    if scope is not None:
        created_q = created_q.where(Conversation.workspace_id == scope)
        resolved_q = resolved_q.where(Conversation.workspace_id == scope)
        msg_q = msg_q.join(Conversation, Message.conversation_id == Conversation.id) \
            .where(Conversation.workspace_id == scope)

    created_rows = db.execute(created_q.group_by(created_bucket)).all()
    resolved_rows = db.execute(resolved_q.group_by(closed_bucket)).all()
    msg_rows = db.execute(msg_q.group_by(msg_bucket, Message.direction)).all()

    buckets: dict[str, TimePoint] = {}

    def bucket(key: str) -> TimePoint:
        if key not in buckets:
            buckets[key] = TimePoint(date=key)
        return buckets[key]

    for key, cnt in created_rows:
        if key:
            bucket(key).created = cnt
    for key, cnt in resolved_rows:
        if key:
            bucket(key).resolved = cnt
    for key, direction, cnt in msg_rows:
        if not key:
            continue
        if direction == "inbound":
            bucket(key).inbound = cnt
        else:
            bucket(key).outbound = cnt

    points = [buckets[k] for k in sorted(buckets.keys())]
    return Timeseries(interval=interval, points=points)
