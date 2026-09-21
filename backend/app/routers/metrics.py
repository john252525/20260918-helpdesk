from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import AgentMetrics, ChannelMetrics, MetricsOverview, Timeseries
from ..security import effective_workspace, get_current_user
from ..services import metrics as metrics_service

router = APIRouter(prefix="/metrics", tags=["metrics"])


def _range(date_from: datetime | None, date_to: datetime | None, days: int):
    now = datetime.now(timezone.utc)
    if date_to is None:
        date_to = now
    if date_from is None:
        date_from = date_to - timedelta(days=days)
    if date_from.tzinfo is None:
        date_from = date_from.replace(tzinfo=timezone.utc)
    if date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=timezone.utc)
    return date_from, date_to


@router.get("/overview", response_model=MetricsOverview, summary="Сводные метрики поддержки")
def overview(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    workspace_id: int | None = Query(None, description="Только суперадмин"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    sla_seconds: int = Query(900, description="Целевое время первого ответа, сек"),
):
    f, t = _range(date_from, date_to, days)
    scope = effective_workspace(user, workspace_id)
    return metrics_service.overview(db, f, t, sla_seconds, scope=scope)


@router.get("/agents", response_model=list[AgentMetrics], summary="Метрики по операторам")
def agents(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    workspace_id: int | None = Query(None, description="Только суперадмин"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    days: int = Query(30, ge=1, le=365),
):
    f, t = _range(date_from, date_to, days)
    scope = effective_workspace(user, workspace_id)
    return metrics_service.by_agent(db, f, t, scope=scope)


@router.get("/channels", response_model=list[ChannelMetrics], summary="Метрики по каналам")
def channels(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    workspace_id: int | None = Query(None, description="Только суперадмин"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    days: int = Query(30, ge=1, le=365),
):
    f, t = _range(date_from, date_to, days)
    scope = effective_workspace(user, workspace_id)
    return metrics_service.by_channel(db, f, t, scope=scope)


@router.get("/timeseries", response_model=Timeseries, summary="Динамика по дням/часам")
def timeseries(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    workspace_id: int | None = Query(None, description="Только суперадмин"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    interval: str = Query("day", pattern="^(day|hour)$"),
):
    f, t = _range(date_from, date_to, days)
    scope = effective_workspace(user, workspace_id)
    return metrics_service.timeseries(db, f, t, interval, scope=scope)
