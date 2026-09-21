from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import LoginRequest, PasswordChange, TokenResponse, UserOut
from ..security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse, summary="Получить JWT по email и паролю")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is disabled")
    token, expires_in = create_access_token(user)
    return TokenResponse(access_token=token, expires_in=expires_in, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut, summary="Текущий пользователь")
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/change-password", summary="Сменить свой пароль")
def change_password(
    payload: PasswordChange,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Self-service password change. The current password is required, so a
    stolen token alone is not enough to lock the owner out of the account."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Текущий пароль указан неверно")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="Новый пароль совпадает с текущим")

    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True, "detail": "Пароль изменён"}
