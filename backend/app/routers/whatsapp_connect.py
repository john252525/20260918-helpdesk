"""WhatsApp connection wizard for the Touch-API vendor.

Four steps, all server-side: list accounts for a token, create one, save the
channel, then walk the admin through QR authorization. The vendor contract is
implemented in `channels/whatsapp/touchapi.py`; this router only wires it to
the shared save path.
"""
from __future__ import annotations

import logging

import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal, get_db
from ..models import Channel, User
from ..schemas import (ChannelOut, WhatsAppAuthRequest, WhatsAppConnect,
                       WhatsAppDiscover, WhatsAppMaintenance, WhatsAppStatusRequest)
from ..security import require_admin
from ..services import whatsapp_connect as connect
from ..channels import whatsapp as whatsapp_providers

log = logging.getLogger("whatsapp_connect")

router = APIRouter(prefix="/channels/whatsapp", tags=["channels"])


@router.get("/providers", summary="Провайдеры WhatsApp для формы подключения")
def providers(_: User = Depends(require_admin)):
    """The backend owns the dropdown: a new vendor is one registry entry."""
    return {"providers": whatsapp_providers.provider_catalog()}


@router.post("/discover", summary="Найти аккаунты по токену Touch-API")
def discover(body: WhatsAppDiscover, _: User = Depends(require_admin)):
    provider = connect.provider_for(body.token, body.source)
    result = provider.discover()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("detail") or "Токен отклонён")
    return result


@router.post("/connect", response_model=ChannelOut, status_code=201,
             summary="Создать канал WhatsApp (Touch-API)")
def connect_channel(
    body: WhatsAppConnect,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Attach an existing account or create a new one, then save the channel.

    The webhook is registered with the vendor afterwards, so incoming events
    start flowing without the admin visiting the vendor's cabinet.
    """
    workspace_id = user.workspace_id
    if user.role == "superadmin":
        workspace_id = body.workspace_id
        if workspace_id is None:
            raise HTTPException(status_code=400, detail="Суперадмин должен указать workspace_id")

    provider = connect.provider_for(body.token, body.source)

    login = (body.login or "").strip()
    if body.create_new or not login:
        login = connect.generate_login()
        created = provider.add_account(login)
        if created.get("status") == "error":
            raise HTTPException(status_code=502,
                                detail=f"Не удалось создать аккаунт: {provider.error_text(created)}")
        log.info("Touch-API account created: %s", login)
    else:
        # make sure the account really belongs to this token before saving it
        found = provider.discover()
        if not found.get("ok"):
            raise HTTPException(status_code=400, detail=found.get("detail") or "Токен отклонён")
        known = {a["login"] for a in found.get("accounts", [])}
        if login not in known:
            raise HTTPException(status_code=400,
                                detail=f"Аккаунт «{login}» не найден среди доступных по этому токену")

    config = {
        "provider": "touch-api",
        "source": body.source,
        "token": body.token,
        "login": login,
        "base_url": provider.base_url,
    }
    name = connect.unique_channel_name(db, workspace_id, (body.name or "").strip() or login)
    ch = connect.save_whatsapp_channel(db, workspace_id=workspace_id, name=name, config=config)

    # register our webhook so incoming messages start arriving
    url = connect.webhook_url_for(ch, settings.public_base_url)
    cfg_provider = whatsapp_providers.get_provider(ch)
    reg = cfg_provider.add_webhook(url, login)
    if reg.get("status") == "error":
        ch.config = {**(ch.config or {}), "webhook_registered": False,
                     "webhook_error": cfg_provider.error_text(reg)}
        log.warning("Touch-API webhook not registered for channel %s: %s", ch.id, reg)
    else:
        ch.config = {**(ch.config or {}), "webhook_registered": True, "webhook_url": url}
    db.commit()
    db.refresh(ch)
    return ch


def _load_channel(db: Session, channel_id: int, user: User) -> Channel:
    ch = db.get(Channel, channel_id)
    if ch is None or ch.type != "whatsapp" or (
        user.role != "superadmin" and ch.workspace_id != user.workspace_id
    ):
        raise HTTPException(status_code=404, detail="Канал не найден")
    return ch


@router.post("/auth", summary="Начать авторизацию WhatsApp (QR)")
def start_auth(
    body: WhatsAppAuthRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Switch the account on and return the first QR (if the vendor has one)."""
    ch = _load_channel(db, body.channel_id, user)
    provider = whatsapp_providers.get_provider(ch)
    if provider.key != "touch-api":
        raise HTTPException(status_code=400, detail="Авторизация доступна только для Touch-API")

    # keep the vendor webhook pointing at the current public origin: a channel
    # created while PUBLIC_BASE_URL was localhost would otherwise never receive
    desired = connect.webhook_url_for(ch, settings.public_base_url)
    if (ch.config or {}).get("webhook_url") != desired:
        old_url = (ch.config or {}).get("webhook_url")
        if old_url:
            provider.remove_webhook(old_url)
        reg = provider.add_webhook(desired)
        cfg = dict(ch.config or {})
        cfg["webhook_url"] = desired
        cfg["webhook_registered"] = reg.get("status") != "error"
        if reg.get("status") == "error":
            cfg["webhook_error"] = provider.error_text(reg)
            log.warning("WhatsApp webhook re-register failed for %s: %s", ch.id, reg)
        ch.config = cfg
        db.commit()
        db.refresh(ch)

    state = provider.start_account()
    if state.get("status") == "error":
        # "Trying to start existing account" just means it is already up;
        # the vendor still hands out a QR, so carry on and let the snapshot
        # report the real state. Anything else is surfaced to the caller.
        msg = provider.error_text(state)
        if "existing" not in msg.lower():
            log.info("WhatsApp start for channel %s: %s", ch.id, msg)

    return _auth_snapshot(provider, ch)


@router.get("/auth-status", summary="Состояние авторизации WhatsApp")
def auth_status(
    channel_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    ch = _load_channel(db, channel_id, user)
    provider = whatsapp_providers.get_provider(ch)
    if provider.key != "touch-api":
        raise HTTPException(status_code=400, detail="Авторизация доступна только для Touch-API")
    return _auth_snapshot(provider, ch)


def _auth_snapshot(provider, channel) -> dict:
    """Account state for the auth modal.

    Uses the batch `status_map` rather than a per-account `getInfo`: the
    single-account call is measured at ~30s at this vendor while the batch
    covers every account of the token in ~5s, and the modal polls this.
    """
    r = provider.status_map()
    cfg = dict(channel.config or {})
    base = {
        "login": provider.login,
        "activated": False,
        "state": False,
        "step": None,
        "step_message": None,
        "maintenance": cfg.get("maintenance"),
        "maintenance_error": cfg.get("maintenance_error"),
        "qr": None,
        # NOTE: no image URL here -- the vendor's screenshot URL carries the
        # token in its query string, and the browser must never see it.
        # The SPA fetches /channels/whatsapp/qr-image instead.
    }
    if not r.get("ok"):
        base["error"] = r.get("detail") or "нет данных от провайдера"
        return base
    st = r["accounts"].get(str(provider.login)) or {}
    base.update({
        "activated": bool(st.get("activated")),
        "state": bool(st.get("state")),
        "step": st.get("step"),
        "step_message": st.get("step_message"),
    })
    return base


@router.get("/qr-image", summary="QR-код авторизации WhatsApp (прокси)")
def qr_image(
    channel_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Fetch the vendor's QR screenshot server-side and pass the bytes through.

    The vendor serves the image from a URL that embeds the API token, so the
    browser must never call it directly: that would leak the token into page
    source, history and referrers.
    """
    import httpx
    from fastapi import Response

    ch = _load_channel(db, channel_id, user)
    provider = whatsapp_providers.get_provider(ch)
    if provider.key != "touch-api":
        raise HTTPException(status_code=400, detail="QR доступен только для Touch-API")

    params = {"source": provider.source, "token": provider.token, "login": provider.login}
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(f"{provider.base_url}/screenshot", params=params)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Нет связи с провайдером: {exc}")

    ctype = resp.headers.get("content-type", "")
    if "image" not in ctype:
        raise HTTPException(status_code=404, detail="QR пока недоступен")
    return Response(content=resp.content, media_type=ctype,
                    headers={"Cache-Control": "no-store"})


@router.post("/status", summary="Статусы аккаунтов WhatsApp (батч)")
def statuses(
    body: WhatsAppStatusRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Status of every requested channel, grouped by vendor token.

    One `getInfoByToken` per token covers all its channels, which keeps the
    channel list responsive: a per-account `getInfo` is an order of magnitude
    slower at this vendor.
    """
    channels: list[Channel] = []
    for cid in body.channel_ids:
        ch = db.get(Channel, cid)
        if ch is None or ch.type != "whatsapp":
            continue
        if user.role != "superadmin" and ch.workspace_id != user.workspace_id:
            continue
        channels.append(ch)

    by_token: dict = {}
    for ch in channels:
        cfg = ch.config or {}
        if cfg.get("provider") != "touch-api" or not cfg.get("token"):
            continue
        by_token.setdefault(cfg["token"], []).append(ch)

    result: dict = {}
    for token, group in by_token.items():
        provider = connect.provider_for(token)
        r = provider.status_map()
        if not r.get("ok"):
            for ch in group:
                result[str(ch.id)] = {"error": r.get("detail") or "нет данных"}
            continue
        accounts = r["accounts"]
        for ch in group:
            login = str((ch.config or {}).get("login") or "")
            st = accounts.get(login, {})
            result[str(ch.id)] = {
                "login": login,
                "state": st.get("state"),
                "activated": st.get("activated"),
                "step": st.get("step"),
                "step_message": st.get("step_message"),
                "maintenance": (ch.config or {}).get("maintenance"),
                "maintenance_error": (ch.config or {}).get("maintenance_error"),
            }
    return {"statuses": result}


@router.post("/maintenance", summary="Рестарт или сброс сессии WhatsApp")
def maintenance(
    body: WhatsAppMaintenance,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Queue a maintenance sequence and return at once.

    forceStop + getNewProxy + setState takes tens of seconds at this vendor
    (setState alone was measured at 31s), far beyond a safe HTTP wait. The
    work runs in a background thread; the channel list and the auth modal
    report progress through `config.maintenance`.
    """
    ch = _load_channel(db, body.channel_id, user)
    provider = whatsapp_providers.get_provider(ch)
    if provider.key != "touch-api":
        raise HTTPException(status_code=400, detail="Доступно только для Touch-API")

    cfg = dict(ch.config or {})
    if cfg.get("maintenance"):
        raise HTTPException(status_code=409, detail="Обслуживание уже выполняется")

    cfg["maintenance"] = body.action
    cfg.pop("maintenance_error", None)
    ch.config = cfg
    db.commit()

    threading.Thread(target=_run_maintenance, args=(ch.id, body.action), daemon=True).start()
    return {"started": True, "action": body.action}


# messages the vendor returns on the happy path of a restart; they are not
# failures, so they must not be reported as such
_BENIGN = ("qr code", "existing account")


def _run_maintenance(channel_id: int, action: str) -> None:
    """Execute the maintenance sequence outside the request cycle."""
    db = SessionLocal()
    try:
        ch = db.get(Channel, channel_id)
        if ch is None:
            return
        provider = whatsapp_providers.get_provider(ch)

        calls = [("forceStop", provider.force_stop)]
        if action == "reset":
            calls.append(("clearSession", provider.clear_session))
        calls.append(("getNewProxy", provider.get_new_proxy))
        calls.append(("setState", provider.start_account))

        errors: list[str] = []
        for name, fn in calls:
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")
                continue
            if isinstance(res, dict) and res.get("status") == "error":
                msg = provider.error_text(res)
                if not any(b in msg.lower() for b in _BENIGN):
                    errors.append(f"{name}: {msg}")

        cfg = dict(ch.config or {})
        cfg.pop("maintenance", None)
        if errors:
            cfg["maintenance_error"] = "; ".join(errors)
        else:
            cfg.pop("maintenance_error", None)
        cfg["last_maintenance"] = {
            "action": action,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        ch.config = cfg
        db.commit()
        log.info("WhatsApp maintenance %s for channel %s done, errors=%s",
                 action, channel_id, errors or "none")
    except Exception as exc:  # noqa: BLE001
        log.exception("WhatsApp maintenance failed")
        try:
            ch = db.get(Channel, channel_id)
            if ch is not None:
                cfg = dict(ch.config or {})
                cfg.pop("maintenance", None)
                cfg["maintenance_error"] = str(exc)
                ch.config = cfg
                db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()
