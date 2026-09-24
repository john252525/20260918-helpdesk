"""Background pollers: IMAP for email, Bots Long Poll for VK groups."""
from __future__ import annotations

import email
import imaplib
import logging
import re
import threading
import time
from datetime import timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

import httpx
from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import Channel, Conversation
from .ingest import get_or_create_channel, ingest_inbound
from .vk_connect import pick_vk_token

log = logging.getLogger("pollers")

_HTML_TAG = re.compile(r"<[^>]+>")


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:  # noqa: BLE001
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    return _HTML_TAG.sub(" ", html)
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:  # noqa: BLE001
        return str(msg.get_payload())


class PollerSupervisor(threading.Thread):
    """Keeps one VK long-poll thread per enabled channel.

    Channels can be added or disabled at runtime, so this wakes up every
    ``interval`` seconds and reconciles the running threads with the database.
    """

    daemon = True

    def __init__(self, interval: int = 30):
        super().__init__(name="poller-supervisor")
        self.interval = max(10, interval)
        self._stop = threading.Event()
        self._workers: dict[int, VKLongPoll] = {}

    def stop(self) -> None:
        self._stop.set()
        for w in self._workers.values():
            w.stop()

    def run(self) -> None:
        log.info("Poller supervisor started")
        while not self._stop.is_set():
            try:
                self._reconcile()
            except Exception as exc:  # noqa: BLE001
                log.warning("Poller supervisor error: %s", exc)
            self._stop.wait(self.interval)

    def _reconcile(self) -> None:
        db = SessionLocal()
        try:
            wanted = {
                ch.id: ch
                for ch in db.scalars(
                    select(Channel).where(Channel.type == "vk", Channel.enabled.is_(True))
                )
            }
        finally:
            db.close()

        # stop threads whose channel disappeared or was disabled
        for cid in list(self._workers):
            if cid not in wanted:
                self._workers[cid].stop()
                del self._workers[cid]
                log.info("VK poller stopped for channel %s", cid)

        # start threads for new channels
        for cid, ch in wanted.items():
            if cid in self._workers:
                continue
            cfg = ch.config or {}
            if not (cfg.get("group_id") or settings.vk_group_id):
                continue
            worker = VKLongPoll(cid)
            worker.start()
            self._workers[cid] = worker
            log.info("VK poller started for channel %s", cid)


class EmailPoller(threading.Thread):
    daemon = True

    def __init__(self, interval: int = 60):
        super().__init__(name="email-poller")
        self.interval = max(15, interval)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("Email poller started, interval=%ss", self.interval)
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("Email poll failed: %s", exc)
            self._stop.wait(self.interval)

    def _poll_once(self) -> None:
        db = SessionLocal()
        try:
            channels = list(db.scalars(select(Channel).where(Channel.type == "email", Channel.enabled.is_(True))))
            for ch in channels:
                cfg = ch.config or {}
                host = cfg.get("imap_host") or settings.imap_host
                user = cfg.get("imap_user") or settings.imap_user
                password = cfg.get("imap_password") or settings.imap_password
                if not host or not user:
                    continue
                port = int(cfg.get("imap_port") or settings.imap_port or 993)
                self._poll_mailbox(db, ch, host, port, user, password)
            db.commit()
        finally:
            db.close()

    def _poll_mailbox(self, db, channel: Channel, host: str, port: int, user: str, password: str) -> None:
        conn = imaplib.IMAP4_SSL(host, port)
        try:
            conn.login(user, password)
            conn.select("INBOX")
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                return
            ids = data[0].split()
            for uid in ids[-50:]:
                status, msg_data = conn.fetch(uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                self._handle_message(db, channel, msg)
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def _handle_message(self, db, channel: Channel, msg: email.message.Message) -> None:
        from_addr = parseaddr(msg.get("From", ""))[1] or ""
        from_name = _decode(parseaddr(msg.get("From", ""))[0])
        subject = _decode(msg.get("Subject"))
        message_id = (msg.get("Message-ID") or "").strip()
        in_reply_to = (msg.get("In-Reply-To") or "").strip()
        references = (msg.get("References") or "").strip()
        try:
            created_at = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
            if created_at and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            created_at = None

        body = _extract_body(msg).strip()
        if not body:
            body = "(empty message)"

        conv = None
        parent_ids = [x for x in (references.split() + [in_reply_to]) if x]
        if parent_ids:
            conv = db.scalar(
                select(Conversation).where(
                    Conversation.channel_id == channel.id,
                    Conversation.meta["email_message_id"].as_string().in_(parent_ids),
                )
            )
        if conv is None and subject:
            conv = db.scalar(
                select(Conversation)
                .where(
                    Conversation.channel_id == channel.id,
                    Conversation.status.in_(("open", "pending")),
                    Conversation.subject == subject,
                )
                .order_by(Conversation.id.desc())
            )

        conv_out, _msg, _is_new = ingest_inbound(
            db,
            channel=channel,
            external_id=from_addr,
            body=body,
            contact_name=from_name or from_addr,
            contact_email=from_addr,
            subject=subject,
            message_external_id=message_id or None,
            created_at=created_at,
            meta={"email_message_id": message_id, "in_reply_to": in_reply_to},
            conversation=conv,
        )
        if message_id:
            conv_out.meta = {**(conv_out.meta or {}), "email_message_id": message_id}


class VKLongPoll(threading.Thread):
    """One long-poll thread per VK channel.

    Serving several tenants from one thread is not possible: each channel has
    its own group id, token and cursor.
    """

    daemon = True

    def __init__(self, channel_id: int):
        super().__init__(name=f"vk-longpoll-{channel_id}")
        self.channel_id = channel_id
        self._stop = threading.Event()
        self._server = None
        self._key = None
        self._ts = None
        self._group_id: Optional[str] = None
        self._token: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("VK long poll started")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("VK long poll error: %s", exc)
                self._server = None
                self._stop.wait(10)

    def _load_credentials(self) -> bool:
        db = SessionLocal()
        try:
            ch = db.get(Channel, self.channel_id)
            if ch is None or not ch.enabled:
                return False
            cfg = ch.config or {}
            self._group_id = str(cfg.get("group_id") or settings.vk_group_id or "")
            # same token the adapter sends with: a group key works for both,
            # the OAuth token only for receiving (ADR-023). Preferring the key
            # also survives an OAuth token being revoked in the group settings.
            self._token = pick_vk_token(cfg)
            # resume from the stored cursor: VK keeps a queue for a few hours,
            # so messages sent while the service was down are replayed
            if self._ts is None and cfg.get("long_poll_ts") is not None:
                self._ts = cfg["long_poll_ts"]
            return bool(self._group_id and self._token)
        finally:
            db.close()

    def _ensure_server(self) -> bool:
        if self._server and self._ts is not None:
            return True
        if not self._load_credentials():
            return False
        with httpx.Client(timeout=30) as client:
            # POST keeps the token in the body: GET would put it in the URL,
            # and httpx logs the full URL at INFO level.
            resp = client.post(
                "https://api.vk.com/method/groups.getLongPollServer",
                data={"group_id": self._group_id, "access_token": self._token,
                      "v": settings.vk_api_version},
            )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"VK: {data['error'].get('error_msg')}")
        result = data["response"]
        self._server = result["server"]
        self._key = result["key"]
        if self._ts is None:
            # first ever run for this channel: start from "now"
            self._ts = result["ts"]
        return True

    def _tick(self) -> None:
        if not self._ensure_server():
            self._stop.wait(15)
            return

        # VK holds the request open for `wait` seconds; give the socket room
        # beyond that, otherwise an idle period looks like a failure.
        wait_seconds = 25
        try:
            with httpx.Client(timeout=wait_seconds + 20) as client:
                resp = client.get(
                    self._server,
                    params={"act": "a_check", "key": self._key,
                            "ts": self._ts, "wait": wait_seconds},
                )
            data = resp.json()
        except (httpx.TimeoutException, httpx.TransportError):
            # Nothing to do: the long poll simply returned no updates in time.
            # The cursor stays valid, so just come back for more.
            return

        if "failed" in data:
            # A real protocol failure: the ts/key went stale, reconnect.
            self._server = None
            return
        self._ts = data.get("ts", self._ts)
        self._save_cursor()
        for update in data.get("updates", []):
            if update.get("type") == "message_new":
                self._handle_message(update["object"]["message"])

    def _save_cursor(self) -> None:
        """Persist the Long Poll cursor so a restart does not lose messages."""
        db = SessionLocal()
        try:
            ch = db.get(Channel, self.channel_id)
            if ch is not None:
                ch.config = {**(ch.config or {}), "long_poll_ts": self._ts}
                db.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not save long poll cursor for %s: %s", self.channel_id, exc)
        finally:
            db.close()

    def _handle_message(self, message: dict) -> None:
        db = SessionLocal()
        try:
            ch = db.get(Channel, self.channel_id)
            if ch is None or not ch.enabled:
                return
            peer_id = str(message.get("peer_id") or message.get("from_id") or "")
            body = message.get("text") or ""
            if not peer_id or not body:
                return
            from_id = message.get("from_id")
            author = None
            if from_id and int(from_id) > 0:
                try:
                    with httpx.Client(timeout=20) as client:
                        r = client.post(
                            "https://api.vk.com/method/users.get",
                            data={"user_ids": from_id, "access_token": self._token,
                                  "v": settings.vk_api_version},
                        )
                    res = r.json().get("response") or []
                    if res:
                        author = f"{res[0].get('first_name','')} {res[0].get('last_name','')}".strip()
                except Exception:  # noqa: BLE001
                    author = None
            ingest_inbound(
                db,
                channel=ch,
                external_id=peer_id,
                body=body,
                contact_name=author,
                message_external_id=str(message.get("id") or "") or None,
                created_at=message.get("date") and __import__("datetime").datetime.fromtimestamp(
                    message["date"], tz=timezone.utc
                ) or None,
                meta={"vk_peer_id": peer_id, "vk_from_id": from_id},
            )
            db.commit()
        finally:
            db.close()
