import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import User
from .security import hash_password

log = logging.getLogger("seed")

AVATAR_COLORS = ["#2563eb", "#7c3aed", "#db2777", "#ea580c", "#16a34a", "#0891b2"]



def seed(db: Session) -> None:
    admin = db.scalar(select(User).where(User.email == settings.bootstrap_admin_email))
    if admin is None:
        if not settings.bootstrap_admin_password:
            # намеренно ничего не создаём: поднимать сервис с известным
            # паролем по умолчанию — плохая идея. Пароль задаётся явно в .env
            log.warning(
                "Bootstrap-админ не создан: задайте BOOTSTRAP_ADMIN_PASSWORD в backend/.env"
            )
        else:
            admin = User(
                email=settings.bootstrap_admin_email,
                name=settings.bootstrap_admin_name,
                password_hash=hash_password(settings.bootstrap_admin_password),
                is_admin=True,
                avatar_color=AVATAR_COLORS[0],
            )
            db.add(admin)
            log.info("Created bootstrap admin: %s", settings.bootstrap_admin_email)

    # No demo operators and no pre-created channels.
    #
    # Both used to be seeded here, which meant every restart resurrected rows
    # that had been deleted on purpose. Channels are now created by the
    # workspace admin (or by the operator through the API); see the README.
    db.commit()
