import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import Base, SessionLocal, engine
from .routers import (
    auth,
    channels,
    contacts,
    conversations,
    metrics,
    users,
    webhooks,
    workspaces,
)
from .seed import seed
from .services import pollers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO, which would leak access tokens and
# other secrets that travel as query parameters. Keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("app")

_background: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()

    if settings.imap_host or _any_email_channel():
        p = pollers.EmailPoller(interval=settings.imap_poll_seconds)
        p.start()
        _background.append(p)

    # one supervisor keeps a long-poll thread per enabled VK channel, so every
    # tenant's group is served without a restart when channels change
    sup = pollers.PollerSupervisor(interval=30)
    sup.start()
    _background.append(sup)

    log.info("%s API ready", settings.app_name)
    yield
    for t in _background:
        try:
            t.stop()
        except Exception:  # noqa: BLE001
            pass


def _any_email_channel() -> bool:
    from .models import Channel
    from sqlalchemy import select
    db = SessionLocal()
    try:
        ch = db.scalar(select(Channel).where(Channel.type == "email"))
        return ch is not None and bool((ch.config or {}).get("imap_host"))
    finally:
        db.close()


app = FastAPI(
    title=f"{settings.app_name} API",
    version="1.0.0",
    description=(
        "API-first helpdesk: единый инбокс для WhatsApp / VK / Email.\n\n"
        "**Авторизация**: `POST /api/v1/auth/login` -> JWT -> кнопка *Authorize*.\n\n"
        "**Входящие**: провайдеры шлют вебхуки на `/api/v1/webhooks/{type}/{channel_id}`.\n\n"
        "**Исходящие**: `POST /api/v1/conversations/{id}/messages`."
    ),
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_prefix = "/api/v1"
for module in (auth, users, channels, contacts, conversations, metrics, webhooks, workspaces):
    app.include_router(module.router, prefix=api_prefix)


@app.get("/api/v1/health", tags=["system"], summary="Healthcheck")
def health():
    return {"status": "ok", "app": settings.app_name, "version": "1.0.0"}


# ---- static frontend (standalone client) ----
import os

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend")


@app.get("/", include_in_schema=False)
def index():
    idx = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return JSONResponse({"detail": "Frontend not found", "docs": "/api/docs"})


@app.get("/app", include_in_schema=False)
def app_redirect():
    """`/app` (no slash) must not 404 -- the SPA is served from `/app/`."""
    return RedirectResponse(url="/app/", status_code=307)


if os.path.isdir(FRONTEND_DIR):
    # Primary mount: /app/... (assets referenced explicitly by index.html)
    app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    # Fallback mount at the root so relative asset URLs (styles.css, app.js)
    # keep working for clients that were pointed at the bare host:port.
    # Registered last, so every /api/* route above still takes precedence.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend-root")
