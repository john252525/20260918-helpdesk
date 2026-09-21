from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Contact, Conversation, User
from ..schemas import ContactOut, ContactUpdate
from ..security import effective_workspace, get_current_user

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.get("", response_model=list[ContactOut], summary="Список клиентов")
def list_contacts(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    workspace_id: int | None = Query(None, description="Только суперадмин"),
    q: str | None = Query(None),
    channel_type: str | None = Query(None),
    limit: int = Query(100, le=500),
    offset: int = 0,
):
    scope = effective_workspace(user, workspace_id)
    stmt = select(Contact)
    if scope is not None:
        stmt = stmt.where(Contact.workspace_id == scope)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Contact.name.ilike(like), Contact.email.ilike(like),
                Contact.phone.ilike(like), Contact.external_id.ilike(like))
        )
    if channel_type:
        stmt = stmt.where(Contact.channel_type == channel_type)
    stmt = stmt.order_by(Contact.created_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))


@router.get("/{contact_id}", response_model=ContactOut, summary="Клиент по id")
def get_contact(
    contact_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    c = db.get(Contact, contact_id)
    if c is None or (user.role != "superadmin" and c.workspace_id != user.workspace_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    return c


@router.patch("/{contact_id}", response_model=ContactOut, summary="Изменить клиента")
def update_contact(
    contact_id: int,
    payload: ContactUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    c = db.get(Contact, contact_id)
    if c is None or (user.role != "superadmin" and c.workspace_id != user.workspace_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return c


@router.get("/{contact_id}/conversations", summary="Диалоги клиента")
def contact_conversations(
    contact_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    c = db.get(Contact, contact_id)
    if c is None or (user.role != "superadmin" and c.workspace_id != user.workspace_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    convs = list(db.scalars(
        select(Conversation).where(Conversation.contact_id == contact_id)
        .order_by(Conversation.last_message_at.desc().nullslast())
    ))
    return [{"id": cv.id, "status": cv.status, "subject": cv.subject,
             "channel_id": cv.channel_id, "last_message_at": cv.last_message_at,
             "first_response_seconds": cv.first_response_seconds} for cv in convs]
