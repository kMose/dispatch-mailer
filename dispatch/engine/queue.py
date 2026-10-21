"""Work queue: atomic claim / commit around the `sends` table."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from .. import db
from . import eligibility
from .state import SendState


@dataclass
class QueuedContact:
    contact_id: int
    email: str
    sheet_row: int | None
    message_id: str
    attempt_no: int


class Queue:
    """Thin wrapper so the worker never hand-rolls SQL."""

    def __init__(self, conn: sqlite3.Connection, run_id: int):
        self.conn = conn
        self.run_id = run_id

    def claim(self) -> QueuedContact | None:
        row = db.claim_next(self.conn, self.run_id)
        if row is None:
            return None
        return QueuedContact(
            contact_id=row["contact_id"],
            email=row["email"],
            sheet_row=row["sheet_row"],
            message_id=row["message_id"],
            attempt_no=row["attempt_no"],
        )

    def commit_sent(self, contact_id: int, send_id: int, smtp_code: str | None,
                    dry: bool) -> None:
        db.mark_sent(self.conn, send_id, smtp_code, dry)

    def commit_failed(self, send_id: int, state: SendState, code: str | None,
                      error: str | None) -> None:
        db.mark_failed(self.conn, send_id, state.value, code, error)

    def remaining(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sends WHERE run_id=? AND state='queued'",
            (self.run_id,),
        ).fetchone()
        return int(row["n"])

    def totals(self) -> dict[str, int]:
        return db.counts_by_state(self.conn, self.run_id)


def build_eligible(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    frequency_days: int = 30,
) -> tuple[list[int], list[tuple[int, str]]]:
    """Return (eligible contact ids, [(contact_id, skip_status)])."""
    contacts = db.contacts_in_sheet(conn)
    suppression = db.suppression_set(conn)
    verdicts = eligibility.evaluate_all(
        contacts, suppression=suppression, today=today, frequency_days=frequency_days
    )
    # A row whose last send is `sending` or `unknown` must never be resent; it is
    # surfaced for human review instead (PROJECT.md section 2).
    blocked = {
        int(r["contact_id"]) for r in conn.execute(
            "SELECT DISTINCT contact_id FROM sends WHERE state IN ('sending','unknown')"
        )
    }
    eligible: list[int] = []
    skipped: list[tuple[int, str]] = []
    for row, verdict in zip(contacts, verdicts):
        cid = int(row["id"])
        if cid in blocked:
            skipped.append((cid, "unknown"))
        elif verdict.eligible:
            eligible.append(cid)
        else:
            skipped.append((cid, verdict.status))
    return eligible, skipped


def blocked_contact_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        int(r["contact_id"]) for r in conn.execute(
            "SELECT DISTINCT contact_id FROM sends WHERE state IN ('sending','unknown')"
        )
    }


def apply_skips(conn: sqlite3.Connection, skipped: list[tuple[int, str]]) -> None:
    for contact_id, status in skipped:
        conn.execute(
            "UPDATE contacts SET status=? WHERE id=?", (status, contact_id)
        )
