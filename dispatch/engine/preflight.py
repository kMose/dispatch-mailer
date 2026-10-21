"""Sheet import + pre-flight report shared by the GUI and the CLI."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from .. import db
from ..config import FullConfig
from ..sheet.reader import Ambiguity, SheetData, read_sheet
from ..sheet.writer import lockfile_for
from . import eligibility, feasibility
from . import queue as work_queue


@dataclass
class PreflightReport:
    cfg: FullConfig
    sheet: SheetData | None = None
    total_rows: int = 0
    eligible: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    invalid: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    ambiguous: list[Ambiguity] = field(default_factory=list)
    quota: int = 0
    planned: int = 0
    mean_seconds: float = 0.0
    warmup_cap: int = 0
    daily_remaining: int = 0
    daily_sent: int = 0
    review_count: int = 0
    feasibility: feasibility.Feasibility | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    sheet_locked: bool = False

    @property
    def ok(self) -> bool:
        return (self.error is None and self.eligible > 0
                and (self.feasibility is None or self.feasibility.ok)
                and not self.sheet_locked)

    def headline(self) -> str:
        if self.error:
            return f"Blocked: {self.error}"
        if self.sheet_locked:
            return "Blocked: the spreadsheet is open in Excel"
        if self.eligible == 0:
            return "Blocked: no eligible contacts"
        if self.feasibility and not self.feasibility.ok:
            return "Blocked: " + self.feasibility.message
        return (f"Ready: {self.planned} of {self.eligible} eligible contact(s), "
                f"~{feasibility.human_duration(self.planned * self.mean_seconds)} projected")

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.sheet:
            out.append(f"Sheet: {self.sheet.path.name} [{self.sheet.tab}] — "
                       f"{self.total_rows} data row(s), {len(self.sheet.headers)} column(s)")
        out.append(f"Quota: {self.quota}    Eligible: {self.eligible}    Planned: {self.planned}")
        if self.by_reason:
            out.append("Skipped: " + ", ".join(
                f"{k.replace('skipped:', '')}={v}" for k, v in sorted(self.by_reason.items())
            ))
        out.append(f"Timer: ~{feasibility.human_duration(self.mean_seconds)} between sends")
        if self.feasibility:
            out.append("Feasibility: " + self.feasibility.message)
            if self.feasibility.suggestions and not self.feasibility.ok:
                out.append("  " + " | ".join(self.feasibility.suggestions))
        if self.error is None:
            out.append(f"Daily: {self.daily_sent} sent today, {self.daily_remaining} left "
                       f"(cap {self.cfg.limits.daily_cap}); warm-up ceiling {self.warmup_cap}")
        if self.review_count:
            out.append(f"Needs review: {self.review_count} interrupted/unknown send(s) — not resent")
        if self.invalid:
            out.append(f"Invalid emails ({len(self.invalid)}): " + ", ".join(self.invalid[:10]))
        if self.duplicates:
            out.append(f"Duplicate emails ({len(self.duplicates)}): " + ", ".join(self.duplicates[:10]))
        if self.ambiguous:
            out.append(f"Ambiguous dates ({len(self.ambiguous)}): " + ", ".join(
                f"row {a.row} {a.raw!r}" for a in self.ambiguous[:10]
            ))
        for warning in self.warnings[:10]:
            out.append(f"Warning: {warning}")
        if self.error:
            out.append(f"ERROR: {self.error}")
        if self.sheet_locked:
            out.append("ERROR: the workbook is locked by Excel — close it and retry.")
        return out

    def text(self) -> str:
        return "\n".join(self.lines())


def import_sheet(cfg: FullConfig, conn: sqlite3.Connection) -> SheetData:
    data = read_sheet(cfg.sheet_file, cfg.campaign.sheet_tab, cfg.campaign.date_order)
    db.import_contacts(conn, data.contacts)
    return data


def run_preflight(cfg: FullConfig, conn: sqlite3.Connection, *,
                  quota: int | None = None,
                  now: datetime | None = None,
                  today: date | None = None,
                  dry_run: bool | None = None) -> PreflightReport:
    quota = int(quota or cfg.campaign.quota)
    today = today or date.today()
    window = cfg.campaign.send_window
    now_local = now.astimezone(window.tz) if now else datetime.now(window.tz)

    report = PreflightReport(cfg=cfg, quota=quota, mean_seconds=cfg.throttle.mean_seconds())
    live = not (cfg.safety.dry_run if dry_run is None else dry_run)
    if live:
        host = cfg.sender.smtp.host.strip()
        if not host or "PROVIDER" in host.upper():
            report.error = ("sender.smtp.host is still a placeholder — set your provider's "
                            "SMTP host (Settings → SMTP / IMAP) before a live run.")
            return report
        if not cfg.template.footer.postal_address.strip():
            report.error = ("The compliance footer has no postal address — every marketing "
                            "email needs a physical address (Settings → Compliance).")
            return report
        try:
            from ..mail.secrets import resolve_secret
            resolve_secret(cfg.sender.secret_ref)
        except Exception as exc:
            report.error = f"SMTP password unavailable: {exc}"
            return report
    report.sheet_locked = lockfile_for(cfg.sheet_file).exists()
    try:
        data = import_sheet(cfg, conn)
    except Exception as exc:
        report.error = str(exc)
        return report

    report.sheet = data
    report.total_rows = len(data.contacts)
    report.warnings.extend(data.warnings)
    report.ambiguous = list(data.ambiguous)

    suppression = db.suppression_set(conn)
    contact_rows = db.contacts_in_sheet(conn)
    verdicts = eligibility.evaluate_all(
        contact_rows, suppression=suppression, today=today,
        frequency_days=cfg.campaign.frequency_days,
    )
    seen_invalid: set[str] = set()
    seen_dupes: set[str] = set()
    blocked_ids = work_queue.blocked_contact_ids(conn)
    for row, verdict in zip(contact_rows, verdicts):
        if verdict.eligible and int(row["id"]) not in blocked_ids:
            report.eligible += 1
            continue
        if verdict.eligible and int(row["id"]) in blocked_ids:
            report.by_reason["needs_review"] = report.by_reason.get("needs_review", 0) + 1
            continue
        report.by_reason[verdict.status] = report.by_reason.get(verdict.status, 0) + 1
        email = row["email_norm"] or row["email"]
        if verdict.status == eligibility.SKIP_INVALID and email not in seen_invalid:
            seen_invalid.add(email)
            report.invalid.append(email)
        if verdict.status == eligibility.SKIP_DUPLICATE and email not in seen_dupes:
            seen_dupes.add(email)
            report.duplicates.append(email)

    # Contacts with an in-flight/unknown send are surfaced for review, never resent.
    report.review_count = len(blocked_ids)
    if report.review_count:
        report.warnings.append(
            f"{report.review_count} contact(s) have an interrupted/unknown send and are "
            f"excluded from this run — review them in the Review queue."
        )

    first_send = None
    stored = db.get_meta(conn, "warmup_first_send")
    if stored:
        try:
            first_send = date.fromisoformat(stored)
        except ValueError:
            first_send = None
    first_send = first_send or cfg.warmup.first_send_date

    report.daily_sent = db.sent_today(conn, cfg.safety.dry_run)
    report.warmup_cap = cfg.effective_warmup_cap(first_send, today, cfg.limits.daily_cap)
    report.daily_remaining = max(0, min(report.warmup_cap, cfg.limits.daily_cap) - report.daily_sent)

    raw_planned = min(quota, report.eligible)
    planned = min(raw_planned, report.daily_remaining) if report.daily_remaining >= 0 else raw_planned
    if report.daily_remaining == 0:
        planned = 0
        report.warnings.append(
            f"Daily/warm-up cap reached ({report.daily_sent} sent today, cap {report.warmup_cap})."
        )
    report.planned = planned

    remaining = feasibility.available_seconds(now_local, window)
    is_open = window.is_open(now_local) or not window.enabled
    opens_at = None
    if not is_open:
        opens_at = window.next_open(now_local).strftime("%a %d %b %H:%M")
        report.warnings.append(
            f"Outside the send window; it next opens {opens_at} and the run will wait."
        )
    report.feasibility = feasibility.check(
        quota=planned, mean_seconds=report.mean_seconds,
        remaining_seconds=remaining, window_end_hhmm=window.end,
        is_open_now=is_open, opens_at=opens_at,
    )
    return report


def remaining_allowance(cfg: FullConfig, conn: sqlite3.Connection, today: date | None = None) -> int:
    today = today or date.today()
    first_send = db.get_meta(conn, "warmup_first_send")
    first = date.fromisoformat(first_send) if first_send else cfg.warmup.first_send_date
    cap = cfg.effective_warmup_cap(first, today, cfg.limits.daily_cap)
    used = db.sent_today(conn, cfg.safety.dry_run)
    return max(0, cap - used)
