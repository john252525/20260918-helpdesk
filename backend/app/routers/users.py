"""Operators.

Everyone sees only their own workspace. Admins manage people inside it;
only a superadmin may touch other tenants.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import UserCreate, UserOut, UserUpdate
from ..security import get_current_user, hash_password, require_admin

router = APIRouter(prefix="/users", tags=["users"])

VALID_ROLES = {"admin", "agent"}
# superadmin may only be granted by an existing superadmin, never by
# a workspace admin -- otherwise one tenant could escalate to platform level
SUPERADMIN_ONLY = "superadmin"


@router.get("", response_model=list[UserOut], summary="Список операторов")
def list_users(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    workspace_id: int | None = Query(None, description="Только для суперадмина"),
):
    stmt = select(User)
    if user.role == "superadmin":
        if workspace_id is not None:
            stmt = stmt.where(User.workspace_id == workspace_id)
    else:
        stmt = stmt.where(User.workspace_id == user.workspace_id)
    return list(db.scalars(stmt.order_by(User.name)))


@router.post("", response_model=UserOut, status_code=201, summary="Создать сотрудника")
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    role = getattr(payload, "role", None) or "agent"

    if role == SUPERADMIN_ONLY:
        # platform-level account: only a superadmin may mint another one
        if actor.role != SUPERADMIN_ONLY:
            raise HTTPException(status_code=403,
                                detail="Только суперадмин может создать суперадмина")
        new_super = User(
            email=payload.email,
            name=payload.name,
            password_hash=hash_password(payload.password),
            is_active=payload.is_active,
            workspace_id=None,
            role=SUPERADMIN_ONLY,
            is_admin=True,
            avatar_color=payload.avatar_color,
        )
        if db.scalar(select(User).where(User.email == payload.email)):
            raise HTTPException(status_code=409, detail="Email уже занят")
        db.add(new_super)
        db.commit()
        db.refresh(new_super)
        return new_super

    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(VALID_ROLES)}")

    # an admin can only create inside their own workspace
    workspace_id = actor.workspace_id
    if actor.role == "superadmin":
        requested = getattr(payload, "workspace_id", None)
        if requested is None:
            raise HTTPException(
                status_code=400,
                detail="Суперадмин должен указать workspace_id",
            )
        workspace_id = requested

    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="Email уже занят")

    new_user = User(
        email=payload.email,
        name=payload.name,
        password_hash=hash_password(payload.password),
        is_active=payload.is_active,
        workspace_id=workspace_id,
        role=role,
        is_admin=(role == "admin"),
        avatar_color=payload.avatar_color,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@router.patch("/{user_id}", response_model=UserOut, summary="Изменить сотрудника")
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # an admin may not touch people from another workspace
    if actor.role != "superadmin" and target.workspace_id != actor.workspace_id:
        raise HTTPException(status_code=403, detail="Нет доступа к этому пользователю")

    data = payload.model_dump(exclude_unset=True)
    if "password" in data and data["password"]:
        target.password_hash = hash_password(data.pop("password"))
    data.pop("password", None)

    role = data.pop("role", None)
    if role is not None:
        if role == SUPERADMIN_ONLY:
            if actor.role != SUPERADMIN_ONLY:
                raise HTTPException(status_code=403,
                                    detail="Только суперадмин может выдать права суперадмина")
            target.role = SUPERADMIN_ONLY
            target.workspace_id = None
            target.is_admin = True
        elif role in VALID_ROLES:
            if actor.role != SUPERADMIN_ONLY and target.role == SUPERADMIN_ONLY:
                raise HTTPException(status_code=403, detail="Нельзя менять суперадмина")
            target.role = role
            target.is_admin = role == "admin"
            if actor.role == "superadmin" and target.workspace_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="Суперадмин не привязан к компании. "
                           "Сначала создайте компанию и добавьте его туда.",
                )
        else:
            raise HTTPException(status_code=400,
                                detail=f"role must be one of {sorted(VALID_ROLES + [SUPERADMIN_ONLY])}")

    for k, v in data.items():
        setattr(target, k, v)

    db.commit()
    db.refresh(target)
    return target


@router.delete("/{user_id}", status_code=204, summary="Удалить сотрудника")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target.id == actor.id:
        raise HTTPException(status_code=400, detail="Нельзя удалить себя")
    if target.role == "superadmin":
        raise HTTPException(status_code=403, detail="Нельзя удалить суперадмина")
    if actor.role != "superadmin" and target.workspace_id != actor.workspace_id:
        raise HTTPException(status_code=403, detail="Нет доступа к этому пользователю")

    # release references so the FK does not block the delete
    from ..models import Conversation, Message
    db.query(Conversation).filter(Conversation.assignee_id == user_id) \
        .update({Conversation.assignee_id: None})
    db.query(Message).filter(Message.author_user_id == user_id) \
        .update({Message.author_user_id: None})
    db.delete(target)
    db.commit()
