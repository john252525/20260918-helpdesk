"""Workspace (tenant) management.

Only a superadmin may create or delete tenants. A workspace admin can read
and rename their own. Regular agents see nothing here.
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Channel,
    Contact,
    Conversation,
    Event,
    Message,
    User,
    WebhookLog,
    Workspace,
)
from ..schemas import UserOut, WorkspaceCreate, WorkspaceOut, WorkspaceUpdate
from ..security import get_current_user, hash_password, require_superadmin

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceOut], summary="Список компаний")
def list_workspaces(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role == "superadmin":
        return list(db.scalars(select(Workspace).order_by(Workspace.id)))
    if user.workspace_id is None:
        return []
    ws = db.get(Workspace, user.workspace_id)
    return [ws] if ws else []


@router.post("", response_model=WorkspaceOut, status_code=201,
             summary="Создать компанию (только суперадмин)")
def create_workspace(
    payload: WorkspaceCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
):
    if db.scalar(select(Workspace).where(Workspace.slug == payload.slug)):
        raise HTTPException(status_code=409, detail="Компания с таким кодом уже есть")

    ws = Workspace(slug=payload.slug, name=payload.name, is_active=True)
    db.add(ws)
    db.flush()

    if db.scalar(select(User).where(User.email == payload.admin_email)):
        db.rollback()
        raise HTTPException(status_code=409, detail="Email уже занят")

    password = payload.admin_password or secrets.token_urlsafe(12)
    admin = User(
        email=payload.admin_email,
        name=payload.admin_name or f"Администратор {payload.name}",
        password_hash=hash_password(password),
        workspace_id=ws.id,
        role="admin",
        is_admin=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(ws)

    # the plaintext password is shown exactly once, here
    out = WorkspaceOut.model_validate(ws)
    return out


@router.get("/{workspace_id}", response_model=WorkspaceOut, summary="Компания по id")
def get_workspace(
    workspace_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    if user.role != "superadmin" and user.workspace_id != ws.id:
        raise HTTPException(status_code=403, detail="Нет доступа к этой компании")
    return ws


@router.patch("/{workspace_id}", response_model=WorkspaceOut, summary="Изменить компанию")
def update_workspace(
    workspace_id: int,
    payload: WorkspaceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    if user.role != "superadmin" and user.workspace_id != ws.id:
        raise HTTPException(status_code=403, detail="Нет доступа к этой компании")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(ws, k, v)
    db.commit()
    db.refresh(ws)
    return ws


@router.get("/{workspace_id}/users", response_model=list[UserOut],
            summary="Сотрудники компании")
def workspace_users(
    workspace_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "superadmin" and user.workspace_id != workspace_id:
        raise HTTPException(status_code=403, detail="Нет доступа к этой компании")
    return list(db.scalars(
        select(User).where(User.workspace_id == workspace_id).order_by(User.name)
    ))


@router.delete("/{workspace_id}", status_code=204, summary="Удалить компанию со всеми данными")
def delete_workspace(
    workspace_id: int,
    confirm: str = Query("", description="Введите код компании для подтверждения"),
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
):
    """Removes a tenant and everything inside it.

    Destructive: every conversation, message and contact of the company goes
    with it. The caller must echo the company slug back as `confirm`, so this
    cannot happen from a stray request.
    """
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")

    if confirm != ws.slug:
        raise HTTPException(
            status_code=400,
            detail=f"Для подтверждения передайте confirm={ws.slug}",
        )

    # delete children before parents -- there are no ON DELETE CASCADE rules
    conv_ids = [r[0] for r in db.execute(
        select(Conversation.id).where(Conversation.workspace_id == workspace_id)
    ).all()]
    if conv_ids:
        db.execute(delete(Event).where(Event.conversation_id.in_(conv_ids)))
        db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))

    db.execute(delete(Contact).where(Contact.workspace_id == workspace_id))

    ch_ids = [r[0] for r in db.execute(
        select(Channel.id).where(Channel.workspace_id == workspace_id)
    ).all()]
    if ch_ids:
        db.execute(delete(WebhookLog).where(WebhookLog.channel_id.in_(ch_ids)))
    db.execute(delete(Channel).where(Channel.workspace_id == workspace_id))

    db.execute(delete(User).where(User.workspace_id == workspace_id))
    db.delete(ws)
    db.commit()
    return None
