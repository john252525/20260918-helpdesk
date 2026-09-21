from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User

ALGORITHM = "HS256"
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False



# ---------------------------------------------------------------------------
# Tenancy helpers
# ---------------------------------------------------------------------------

def create_access_token(user: User) -> tuple[str, int]:

    expires_minutes = settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=expires_minutes)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "wid": user.workspace_id,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, expires_minutes * 60


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or not credentials.credentials:
        raise unauthorized
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise unauthorized
    user_id = payload.get("sub")
    if not user_id:
        raise unauthorized
    user = db.get(User, int(user_id))
    if user is None or not user.is_active:
        raise unauthorized
    return user


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    try:
        return get_current_user(credentials, db)
    except HTTPException:
        return None


def effective_workspace(user: User, requested: Optional[int] = None) -> Optional[int]:
    """The workspace a request must be scoped to.

    superadmin    -> whatever was asked for; None means "every tenant"
    everyone else -> always their own workspace, `requested` is ignored

    A non-superadmin without a workspace gets `-1`, which matches no row:
    better an empty result than a leak.
    """
    if user.role == "superadmin":
        return requested
    return user.workspace_id if user.workspace_id is not None else -1


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Workspace admin or superadmin."""
    if user.role not in ("admin", "superadmin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Требуются права администратора",
        )
    return user


def require_superadmin(user: User = Depends(get_current_user)) -> User:
    if user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Требуются права суперадминистратора",
        )
    return user
