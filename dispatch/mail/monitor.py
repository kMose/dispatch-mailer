"""IMAP reply / bounce / auto-reply / opt-out detection.

Parsing is pure (`parse_message`) so tests can feed saved .eml files without a network.
Only headers plus a 200-character snippet are stored — never full bodies.
"""
from __future__ import annotations

import email
import imaplib
import re
import sqlite3
from dataclasses import dataclass, field
from email import policy
from email.utils import parseaddr

from .. import db

OUT_OF_OFFICE_RE = re.compile(
    r"(out of (the )?office|automatic(al)?[- ]?repl(y|ies)|autoreply|auto[- ]reply|"
    r"abwesenheit|ausencia|absence du bureau|on vacation|away from|"
    r"i am currently out|travelling|maternity leave|annual leave)",
    re.I,
)
OPT_OUT_RE = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt[ -]?out|stop emailing|"
    r"do not (contact|email) me|delete my (details|data))\b",
    re.I,
)
# An exact "STOP" command (the footer asks for it). Matched against the whole line only,
# so "I'll stop by tomorrow" is not an opt-out.
STOP_RE = re.compile(
    r"^(?:please\s+)?stop(?:\s+(?:emailing|mailing|contacting|sending)"
    r"(?:\s+(?:me|us))?)?[\s.!]*$",
    re.I,
)
_SIGNATURE_SPLIT = re.compile(r"\s+(?:--|—)\s*|^\s*(?:sent from my|get outlook)", re.I)
NOREPLY_LOCAL = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "bounce", "bounces", "notifications", "automated",
}
OWN_HEADERS = ("in-reply-to", "references", "x-failed-recipients",
               "auto-submitted", "precedence", "list-id", "x-autoreply")


@dataclass
class Inbound:
    kind: str                    # reply | autoreply | bounce | optout | ignored
    from_addr: str = ""
    subject: str = ""
    message_id: str | None = None
    in_reply_to: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    snippet: str = ""
    uid: str | None = None
    dsn_status: str | None = None
    dsn_recipient: str | None = None
    dsn_original_id: str | None = None
    failed_recipients: list[str] = field(default_factory=list)
    is_auto: bool = False
    stop_command: bool = False

    @property
    def is_hard_bounce(self) -> bool:
        if self.kind != "bounce":
            return False
        if self.dsn_status:
            return self.dsn_status.strip().startswith("5")
        return True  # X-Failed-Recipients without a status means permanent


def _addr(value: str | None) -> str:
    if not value:
        return ""
    return parseaddr(value)[1].strip().lower()


def _ids(value: str | None) -> list[str]:
    if not value:
        return []
    return re.findall(r"<[^<>]+>", value) or [value.strip()]


def _is_noreply(addr: str) -> bool:
    local = addr.split("@")[0].lower()
    return local in NOREPLY_LOCAL or local.startswith("noreply") or local.startswith("no-reply")


_QUOTE_START = re.compile(
    r"(?im)^(?:>|On .{0,120}wrote:|-{2,}\s*Original Message\s*-{2,}|From:\s)"
)


def _strip_quoted(text: str) -> str:
    """Drop the quoted original so our own unsubscribe footer cannot look like an opt-out."""
    match = _QUOTE_START.search(text)
    return text[:match.start()] if match else text


def _looks_like_stop(snippet: str, subject: str) -> bool:
    """True only for a reply that is essentially just STOP (not a sentence containing it)."""
    head = _SIGNATURE_SPLIT.split(snippet or "", maxsplit=1)[0].strip()
    return any(STOP_RE.match(candidate.strip())
               for candidate in (subject or "", head, snippet or "") if candidate.strip())


def _body_snippet(msg, limit: int = 200) -> str:
    text = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and "attachment" not in str(
                    part.get("Content-Disposition", "")
                ):
                    payload = part.get_payload(decode=True) or b""
                    text = payload.decode(part.get_content_charset() or "utf-8", "replace")
                    if text.strip():
                        break
        else:
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        text = ""
    text = _strip_quoted(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_message(raw: bytes | str, uid: str | None = None) -> list[Inbound]:
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    msg = email.message_from_bytes(raw, policy=policy.default)

    from_addr = _addr(msg.get("From"))
    subject = str(msg.get("Subject") or "")
    message_id = (str(msg.get("Message-ID") or "")).strip() or None
    in_reply_to = _ids(msg.get("In-Reply-To"))
    references = _ids(msg.get("References"))
    snippet = _body_snippet(msg)

    failed_header = msg.get("X-Failed-Recipients")
    failed = [a.strip().lower() for a in re.split(r"[,;]", str(failed_header)) if a.strip()] \
        if failed_header else []

    # ---- bounces -----------------------------------------------------
    is_report = msg.get_content_type() == "multipart/report"
    dsn_status = dsn_recipient = dsn_original = None
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            for sub in part.get_payload() or []:
                try:
                    status = str(sub.get("Status") or "").strip()
                    recipient = str(sub.get("Final-Recipient") or "").strip()
                    original = str(sub.get("Original-Message-ID") or "").strip()
                except AttributeError:
                    continue
                dsn_status = dsn_status or status or None
                if recipient:
                    dsn_recipient = dsn_recipient or _addr(recipient.replace("rfc822;", ""))
                dsn_original = dsn_original or original or None

    if is_report or failed or _is_noreply(from_addr) and "mailer-daemon" in from_addr:
        return [Inbound(
            kind="bounce", from_addr=from_addr, subject=subject, message_id=message_id,
            in_reply_to=in_reply_to, references=references, snippet=snippet, uid=uid,
            dsn_status=dsn_status, dsn_recipient=dsn_recipient,
            dsn_original_id=dsn_original, failed_recipients=failed or [],
        )]

    # ---- auto-replies ------------------------------------------------
    auto_submitted = str(msg.get("Auto-Submitted") or "").lower()
    is_auto = (
        ("auto-replied" in auto_submitted or "auto-generated" in auto_submitted)
        or msg.get("X-Autoreply") is not None
        or str(msg.get("Precedence") or "").lower() in ("auto_reply", "auto-reply")
        or bool(OUT_OF_OFFICE_RE.search(subject))
    )

    # ---- mailing lists / machine senders -----------------------------
    if msg.get("List-Id") is not None or str(msg.get("Precedence") or "").lower() in ("bulk", "list"):
        return [Inbound(kind="ignored", from_addr=from_addr, subject=subject,
                        message_id=message_id, snippet=snippet, uid=uid)]
    if not from_addr or _is_noreply(from_addr):
        return [Inbound(kind="ignored", from_addr=from_addr, subject=subject,
                        message_id=message_id, snippet=snippet, uid=uid)]

    kind = "autoreply" if is_auto else "reply"
    stop_command = not is_auto and _looks_like_stop(snippet, subject)
    if not is_auto and (
        stop_command
        or OPT_OUT_RE.search(snippet or "")
        or OPT_OUT_RE.search(subject or "")
    ):
        kind = "optout"
    return [Inbound(
        kind=kind, from_addr=from_addr, subject=subject, message_id=message_id,
        in_reply_to=in_reply_to, references=references, snippet=snippet, uid=uid,
        is_auto=is_auto, stop_command=stop_command,
    )]


def _match_send(conn: sqlite3.Connection, ev: Inbound):
    for mid in list(ev.in_reply_to) + list(ev.references):
        row = db.find_send_by_message_id(conn, mid)
        if row:
            return row
    if ev.dsn_original_id:
        row = db.find_send_by_message_id(conn, ev.dsn_original_id)
        if row:
            return row
    email_addr = ev.dsn_recipient or ev.from_addr
    if email_addr:
        contact = db.contact_by_email(conn, email_addr)
        if contact:
            row = conn.execute(
                "SELECT * FROM sends WHERE contact_id=? ORDER BY id DESC LIMIT 1",
                (contact["id"],),
            ).fetchone()
            if row:
                return row
    return None


def apply_message(conn: sqlite3.Connection, ev: Inbound, account_id: str) -> str:
    """Persist one parsed message. Returns the action taken (for logs/tests)."""
    send = _match_send(conn, ev)
    contact_id = send["contact_id"] if send else None

    if ev.kind == "ignored":
        db.add_inbound(conn, account_id, "ignored", send["id"] if send else None,
                       ev.from_addr, ev.subject, ev.snippet, ev.uid)
        return "ignored"

    if ev.kind == "bounce":
        recipients = ev.failed_recipients or ([ev.dsn_recipient] if ev.dsn_recipient else [])
        if not recipients and contact_id:
            row = db.contact_by_id(conn, contact_id)
            recipients = [row["email_norm"]] if row else []
        status = ev.dsn_status or ("5.0.0" if ev.is_hard_bounce else "4.0.0")
        hard = status.startswith("5")
        for recipient in recipients or [""]:
            if hard:
                if recipient:
                    db.add_suppression(conn, recipient, "hard_bounce", "dsn")
                if send:
                    try:
                        db.mark_failed(conn, send["id"], "failed_hard",
                                       status, f"bounce {status}")
                    except Exception:
                        pass
                    db.add_needs_review(conn, contact_id, recipient or "",
                                        "hard_bounce", f"DSN {status}: {ev.subject[:120]}")
            db.add_inbound(conn, account_id, "bounce_hard" if hard else "bounce_soft",
                           send["id"] if send else None, ev.from_addr, ev.subject,
                           ev.snippet, ev.uid)
        return "bounce_hard" if hard else "bounce_soft"

    if contact_id is None and ev.from_addr:
        contact = db.contact_by_email(conn, ev.from_addr)
        contact_id = contact["id"] if contact else None

    if ev.kind == "reply":
        if contact_id:
            db.mark_replied(conn, contact_id)
        db.add_inbound(conn, account_id, "reply", send["id"] if send else None,
                       ev.from_addr, ev.subject, ev.snippet, ev.uid)
        return "reply"

    if ev.kind == "autoreply":
        if contact_id:
            db.mark_replied(conn, contact_id, autoreply=True)
        db.add_inbound(conn, account_id, "autoreply", send["id"] if send else None,
                       ev.from_addr, ev.subject, ev.snippet, ev.uid)
        return "autoreply"

    if ev.kind == "optout":
        contact = db.contact_by_id(conn, contact_id) if contact_id else None
        email_norm = contact["email_norm"] if contact else ev.from_addr
        if contact_id:
            db.mark_replied(conn, contact_id)
        if ev.stop_command:
            # An explicit STOP is unambiguous, so honour it immediately instead of
            # waiting for approval; the footer tells recipients this is enough.
            db.add_suppression(conn, email_norm, "replied STOP", source="imap")
            db.add_inbound(conn, account_id, "optout", send["id"] if send else None,
                           ev.from_addr, ev.subject, ev.snippet, ev.uid)
            return "optout"
        db.add_needs_review(
            conn, contact_id, email_norm, "opt_out",
            f"Asked to opt out: {ev.snippet[:160]}",
        )
        db.add_inbound(conn, account_id, "optout", send["id"] if send else None,
                       ev.from_addr, ev.subject, ev.snippet, ev.uid)
        return "optout"

    return "ignored"


def uid_start(last_uid: int, stored_validity: str | None,
              current_validity: str | None) -> int:
    """A mailbox recreated with a new UIDVALIDITY restarts UIDs at 1.

    Without this the watermark stays above the new UIDs and replies are missed forever.
    """
    if stored_validity and current_validity and stored_validity != current_validity:
        return 0
    return last_uid


class IMAPMonitor:
    def __init__(self, cfg, password: str, *, max_fetch: int = 100):
        self.cfg = cfg
        self.password = password
        self.max_fetch = max_fetch

    def _connect(self) -> imaplib.IMAP4:
        imap = self.cfg.sender.imap
        if imap.ssl:
            client = imaplib.IMAP4_SSL(imap.host, imap.port, timeout=30)
        else:
            client = imaplib.IMAP4(imap.host, imap.port, timeout=30)
            client.starttls()
        client.login(self.cfg.sender.from_email, self.password)
        return client

    def poll_once(self, conn: sqlite3.Connection) -> dict[str, int]:
        account = self.cfg.sender.id
        counts: dict[str, int] = {}
        key = f"imap_last_uid:{account}"
        last_uid = int(db.get_meta(conn, key, "0") or 0)

        client = self._connect()
        try:
            client.select(self.cfg.sender.imap.mailbox or "INBOX", readonly=True)

            # A recreated mailbox resets UIDs; without this check the watermark would
            # sit above every new message and replies would never match again.
            current_validity = None
            try:
                _, vdata = client.response("UIDVALIDITY")
                if vdata and vdata[0]:
                    raw_validity = vdata[0]
                    current_validity = (raw_validity.decode() if isinstance(raw_validity, bytes)
                                        else str(raw_validity))
            except Exception:
                current_validity = None
            stored_validity = db.get_meta(conn, f"imap_uidvalidity:{account}")
            last_uid = uid_start(last_uid, stored_validity, current_validity)
            if current_validity:
                db.set_meta(conn, f"imap_uidvalidity:{account}", current_validity)

            criterion = f"UID {last_uid + 1}:*" if last_uid else "ALL"
            typ, data = client.uid("search", None, criterion)
            if typ != "OK":
                return counts
            uids = [u.decode() if isinstance(u, bytes) else str(u) for u in (data[0] or b"").split()]
            uids = [u for u in uids if u.isdigit()]
            uids = uids[-self.max_fetch:]
            highest = last_uid
            for uid in uids:
                raw = self._fetch(client, uid)
                if raw is None:
                    continue
                for ev in parse_message(raw, uid=uid):
                    action = apply_message(conn, ev, account)
                    counts[action] = counts.get(action, 0) + 1
                highest = max(highest, int(uid))
            db.set_meta(conn, key, str(highest))
            db.set_meta(conn, f"imap_checked:{account}", db.now_iso())
        finally:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass
        return counts

    @staticmethod
    def _fetch(client, uid: str) -> bytes | None:
        try:
            typ, data = client.uid("fetch", uid, "(BODY.PEEK[HEADER])")
            if typ != "OK" or not data:
                return None
            header = b""
            for chunk in data:
                if isinstance(chunk, tuple) and len(chunk) > 1:
                    header = chunk[1]
                    break
            typ, body_data = client.uid("fetch", uid, "(BODY.PEEK[TEXT]<0.4000>)")
            body = b""
            if typ == "OK":
                for chunk in body_data:
                    if isinstance(chunk, tuple) and len(chunk) > 1:
                        body = chunk[1]
                        break
            return header + b"\r\n\r\n" + body
        except Exception:
            return None


def poll_inbox(cfg, conn: sqlite3.Connection) -> dict[str, int]:
    from .secrets import resolve_secret
    password = resolve_secret(cfg.sender.secret_ref)
    return IMAPMonitor(cfg, password).poll_once(conn)
