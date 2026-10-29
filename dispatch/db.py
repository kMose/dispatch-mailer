"""SQLite schema and DAO. SQLite is the source of truth during a run."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .engine.eligibility import normalize_email

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, ended_at TEXT,
  quota INTEGER, sent_count INTEGER DEFAULT 0,
  failed_count INTEGER DEFAULT 0, status TEXT, stop_reason TEXT,
  dry_run INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  email_norm TEXT NOT NULL,
  first_name TEXT, last_name TEXT, company TEXT,
  last_advertised TEXT,
  replied INTEGER DEFAULT 0,
  autoreply_only INTEGER DEFAULT 0,
  ignored INTEGER DEFAULT 0,
  sheet_row INTEGER,
  extra_json TEXT,
  last_contacted TEXT,
  status TEXT,
  last_error TEXT,
  attempts INTEGER DEFAULT 0,
  in_sheet INTEGER DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER REFERENCES campaign_runs(id),
  contact_id INTEGER REFERENCES contacts(id),
  account_id TEXT,
  message_id TEXT NOT NULL,
  state TEXT NOT NULL,
  attempt_at TEXT, sent_at TEXT, smtp_code TEXT, error TEXT,
  attempt_no INTEGER DEFAULT 0,
  dry_run INTEGER DEFAULT 0,
  ord INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS suppression (
  email_norm TEXT PRIMARY KEY,
  reason TEXT, added_at TEXT, source TEXT
);

CREATE TABLE IF NOT EXISTS inbound_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT, kind TEXT,
  matched_send_id INTEGER, from_addr TEXT, subject TEXT, detected_at TEXT,
  snippet TEXT, uid TEXT
);

CREATE TABLE IF NOT EXISTS needs_review (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id INTEGER, email_norm TEXT, reason TEXT, detail TEXT,
  created_at TEXT, resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT
);

CREATE INDEX IF NOT EXISTS idx_contacts_email_norm ON contacts(email_norm);
CREATE INDEX IF NOT EXISTS idx_sends_message_id ON sends(message_id);
CREATE INDEX IF NOT EXISTS idx_sends_state ON sends(state);
CREATE INDEX IF NOT EXISTS idx_sends_run ON sends(run_id);
CREATE INDEX IF NOT EXISTS idx_sends_contact ON sends(contact_id);
CREATE INDEX IF NOT EXISTS idx_inbound_kind ON inbound_events(kind);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    p = str(path)
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if p != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------
def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# --------------------------------------------------------------------------
# contacts
# --------------------------------------------------------------------------
@dataclass
class ImportStats:
    rows: int = 0
    inserted: int = 0
    updated: int = 0
    removed: int = 0


def import_contacts(conn: sqlite3.Connection, contacts: Iterable[Any]) -> ImportStats:
    """Upsert by normalized email so contact ids (and send history) survive re-imports."""
    stats = ImportStats()
    # Include contacts that left the sheet earlier: if one comes back, it must keep its
    # id so its send history and review items stay attached.
    existing: dict[str, int] = {}
    for r in conn.execute(
        "SELECT id, email_norm FROM contacts ORDER BY in_sheet DESC, id ASC"
    ):
        existing.setdefault(r["email_norm"], r["id"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE contacts SET in_sheet=0 WHERE in_sheet=1")
        seen: set[str] = set()
        for c in contacts:
            norm = normalize_email(getattr(c, "email", "") or "")
            stats.rows += 1
            extra = json.dumps(getattr(c, "extra", {}) or {}, ensure_ascii=False)
            last_adv = getattr(c, "last_advertised", None)
            last_adv_s = last_adv.isoformat() if hasattr(last_adv, "isoformat") else (last_adv or None)
            last_contacted = getattr(c, "last_contacted", None)
            last_contacted_s = (
                last_contacted.isoformat() if hasattr(last_contacted, "isoformat")
                else (last_contacted or None)
            )
            values = (
                getattr(c, "email", "") or "",
                norm,
                getattr(c, "first_name", None),
                getattr(c, "last_name", None),
                getattr(c, "company", None),
                last_adv_s,
                1 if getattr(c, "replied", False) else 0,
                1 if getattr(c, "ignored", False) else 0,
                getattr(c, "sheet_row", None),
                extra,
                last_contacted_s,
                getattr(c, "status", None),
                getattr(c, "last_error", None),
                int(getattr(c, "attempts", 0) or 0),
                now_iso(),
            )
            if norm and norm in existing and norm not in seen:
                seen.add(norm)
                cid = existing[norm]
                conn.execute(
                    """UPDATE contacts SET email=?, first_name=COALESCE(?, first_name),
                       last_name=COALESCE(?, last_name), company=COALESCE(?, company),
                       last_advertised=CASE
                          WHEN ? IS NULL THEN last_advertised
                          WHEN last_advertised IS NULL THEN ?
                          WHEN ? > last_advertised THEN ?
                          ELSE last_advertised END,
                       replied=MAX(replied, ?), ignored=?, sheet_row=?, extra_json=?,
                       last_contacted=COALESCE(?, last_contacted),
                       status=COALESCE(?, status), last_error=COALESCE(?, last_error),
                       attempts=MAX(attempts, ?), in_sheet=1
                       WHERE id=?""",
                    (
                        values[0], values[2], values[3], values[4],
                        last_adv_s, last_adv_s, last_adv_s, last_adv_s,
                        values[6], values[7], values[8], values[9],
                        last_contacted_s, values[11], values[12], values[13],
                        cid,
                    ),
                )
                stats.updated += 1
            else:
                if norm:
                    seen.add(norm)
                cur = conn.execute(
                    """INSERT INTO contacts(email, email_norm, first_name, last_name, company,
                       last_advertised, replied, autoreply_only, ignored, sheet_row, extra_json,
                       last_contacted, status, last_error, attempts, in_sheet, created_at)
                       VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,1,?)""",
                    (
                        values[0], values[1], values[2], values[3], values[4], values[5],
                        values[6], values[7], values[8], values[9], values[10], values[11],
                        values[12], values[13], values[14],
                    ),
                )
                existing[norm or f"__blank_{cur.lastrowid}"] = cur.lastrowid
                stats.inserted += 1
        stats.removed = conn.execute(
            "SELECT COUNT(*) AS n FROM contacts WHERE in_sheet=0"
        ).fetchone()["n"]
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stats


def contacts_in_sheet(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM contacts WHERE in_sheet=1 ORDER BY COALESCE(sheet_row, id) ASC, id ASC"
    ))


def contact_by_id(conn: sqlite3.Connection, contact_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()


def contact_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contacts WHERE email_norm=? ORDER BY in_sheet DESC, id DESC LIMIT 1",
        (normalize_email(email),),
    ).fetchone()


def mark_replied(conn: sqlite3.Connection, contact_id: int, autoreply: bool = False) -> None:
    if autoreply:
        conn.execute("UPDATE contacts SET autoreply_only=1 WHERE id=?", (contact_id,))
    else:
        conn.execute("UPDATE contacts SET replied=1 WHERE id=?", (contact_id,))


# --------------------------------------------------------------------------
# runs and sends
# --------------------------------------------------------------------------
def create_run(conn: sqlite3.Connection, quota: int, dry_run: bool) -> int:
    cur = conn.execute(
        "INSERT INTO campaign_runs(started_at, quota, sent_count, failed_count, status, dry_run)"
        " VALUES(?,?,0,0,'running',?)",
        (now_iso(), quota, 1 if dry_run else 0),
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, stop_reason: str | None) -> None:
    conn.execute(
        "UPDATE campaign_runs SET ended_at=?, status=?, stop_reason=?,"
        " sent_count=(SELECT COUNT(*) FROM sends WHERE run_id=? AND state='sent'),"
        " failed_count=(SELECT COUNT(*) FROM sends WHERE run_id=? AND state IN"
        " ('failed_hard','failed_soft','unknown')) WHERE id=?",
        (now_iso(), status, stop_reason, run_id, run_id, run_id),
    )


def run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM campaign_runs WHERE id=?", (run_id,)).fetchone()


def latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM campaign_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def enqueue(conn: sqlite3.Connection, run_id: int, contact_ids: list[int],
            account_id: str, domain: str = "samlax.net") -> int:
    count = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for ord_, cid in enumerate(contact_ids):
            conn.execute(
                """INSERT INTO sends(run_id, contact_id, account_id, message_id, state, ord)
                   VALUES(?,?,?,?, 'queued', ?)""",
                (run_id, cid, account_id, f"<{uuid.uuid4()}@{domain}>", ord_),
            )
            count += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return count


def claim_next(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    """Atomically move the next queued row to `sending` and return send+contact."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM sends WHERE run_id=? AND state='queued' ORDER BY ord, id LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE sends SET state='sending', attempt_at=?, attempt_no=attempt_no+1 WHERE id=?",
            (now_iso(), row["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return conn.execute(
        """SELECT s.id AS send_id, s.contact_id, s.message_id, s.attempt_no,
                  s.state AS send_state, c.*
           FROM sends s JOIN contacts c ON c.id = s.contact_id
           WHERE s.id=?""",
        (row["id"],),
    ).fetchone()


def mark_sent(conn: sqlite3.Connection, send_id: int, smtp_code: str | None, dry: bool) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE sends SET state='sent', sent_at=?, smtp_code=?, dry_run=? WHERE id=?",
            (now_iso(), smtp_code, 1 if dry else 0, send_id),
        )
        if not dry:
            row = conn.execute(
                "SELECT contact_id FROM sends WHERE id=?", (send_id,)
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE contacts SET
                         last_advertised = CASE
                           WHEN last_advertised IS NULL OR ? > last_advertised THEN ?
                           ELSE last_advertised END,
                         last_contacted = ?,
                         attempts = attempts + 1,
                         status = 'sent', last_error = NULL
                       WHERE id=?""",
                    (today_iso(), today_iso(), today_iso(), row["contact_id"]),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def mark_failed(conn: sqlite3.Connection, send_id: int, state: str,
                smtp_code: str | None = None, error: str | None = None) -> None:
    from .engine.state import check_transition
    row = conn.execute("SELECT state FROM sends WHERE id=?", (send_id,)).fetchone()
    if row is None:
        return
    check_transition(row["state"], state)
    conn.execute(
        "UPDATE sends SET state=?, smtp_code=?, error=?, sent_at=NULL WHERE id=?",
        (state, smtp_code, (error or "")[:2000], send_id),
    )
    if state in ("failed_hard", "unknown"):
        conn.execute(
            """UPDATE contacts SET
                 attempts = attempts + 1,
                 status = ?,
                 last_error = ?
               WHERE id=(SELECT contact_id FROM sends WHERE id=?)""",
            (state, (error or "")[:500], send_id),
        )


def mark_unknown_stale(conn: sqlite3.Connection, run_id: int | None = None) -> list[int]:
    """Recover rows left in `sending` by a crash. They are never resent."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if run_id is None:
            rows = list(conn.execute("SELECT id FROM sends WHERE state='sending'"))
        else:
            rows = list(conn.execute(
                "SELECT id FROM sends WHERE state='sending' AND run_id=?", (run_id,)
            ))
        ids = [r["id"] for r in rows]
        for sid in ids:
            conn.execute(
                "UPDATE sends SET state='unknown', error=COALESCE(error,"
                "'interrupted while sending - review before resending') WHERE id=?",
                (sid,),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ids


def add_needs_review(conn: sqlite3.Connection, contact_id: int | None, email_norm: str,
                     reason: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO needs_review(contact_id, email_norm, reason, detail, created_at)"
        " VALUES(?,?,?,?,?)",
        (contact_id, email_norm, reason, detail[:1000], now_iso()),
    )


def review_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM needs_review WHERE resolved=0 ORDER BY id DESC"
    ))


def resolve_review(conn: sqlite3.Connection, review_id: int) -> None:
    conn.execute("UPDATE needs_review SET resolved=1 WHERE id=?", (review_id,))


def unknown_sends(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT s.*, c.email, c.sheet_row FROM sends s
           JOIN contacts c ON c.id=s.contact_id
           WHERE s.state IN ('sending','unknown') ORDER BY s.id DESC"""
    ))


def counts_by_state(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM sends WHERE run_id=? GROUP BY state", (run_id,)
    )
    return {r["state"]: r["n"] for r in rows}


def sent_today(conn: sqlite3.Connection, dry: bool) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM sends WHERE state='sent' AND dry_run=?"
        " AND substr(sent_at,1,10)=?",
        (1 if dry else 0, today_iso()),
    ).fetchone()["n"]


def sent_this_hour(conn: sqlite3.Connection, dry: bool, hour_start: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM sends WHERE state='sent' AND dry_run=? AND sent_at>=?",
        (1 if dry else 0, hour_start),
    ).fetchone()["n"]


def run_timeline(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT s.*, c.email, c.first_name, c.last_name, c.sheet_row
           FROM sends s LEFT JOIN contacts c ON c.id=s.contact_id
           WHERE s.run_id=? ORDER BY s.id""", (run_id,),
    ))


def pending_review_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM sends WHERE state IN ('sending','unknown')"
    ).fetchone()["n"]


# --------------------------------------------------------------------------
# suppression
# --------------------------------------------------------------------------
def add_suppression(conn: sqlite3.Connection, email: str, reason: str,
                    source: str = "manual") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO suppression(email_norm, reason, added_at, source)"
        " VALUES(?,?,?,?)",
        (normalize_email(email), reason, now_iso(), source),
    )


def remove_suppression(conn: sqlite3.Connection, email: str) -> None:
    conn.execute("DELETE FROM suppression WHERE email_norm=?", (normalize_email(email),))


def suppression_set(conn: sqlite3.Connection) -> set[str]:
    return {r["email_norm"] for r in conn.execute("SELECT email_norm FROM suppression")}


def suppression_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM suppression ORDER BY added_at DESC"))


# --------------------------------------------------------------------------
# inbound
# --------------------------------------------------------------------------
def add_inbound(conn: sqlite3.Connection, account_id: str, kind: str,
                matched_send_id: int | None, from_addr: str, subject: str,
                snippet: str = "", uid: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO inbound_events(account_id, kind, matched_send_id, from_addr,
           subject, detected_at, snippet, uid) VALUES(?,?,?,?,?,?,?,?)""",
        (account_id, kind, matched_send_id, from_addr, subject[:300], now_iso(),
         snippet[:200], uid),
    )
    return int(cur.lastrowid)


def inbound_uid_exists(conn: sqlite3.Connection, account_id: str, uid: str) -> bool:
    if not uid:
        return False
    row = conn.execute(
        "SELECT 1 FROM inbound_events WHERE account_id=? AND uid=? LIMIT 1",
        (account_id, str(uid)),
    ).fetchone()
    return row is not None


def inbound_counts(conn: sqlite3.Connection, account_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM inbound_events WHERE account_id=? GROUP BY kind",
        (account_id,),
    )
    return {r["kind"]: r["n"] for r in rows}


def find_send_by_message_id(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
    if not message_id:
        return None
    msg = message_id.strip()
    row = conn.execute("SELECT * FROM sends WHERE message_id=?", (msg,)).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT * FROM sends WHERE replace(replace(message_id,'<',''),'>','') ="
        " replace(replace(?,'<',''),'>','') LIMIT 1", (msg,),
    ).fetchone()


__all__ = [name for name in dir() if not name.startswith("_")]
