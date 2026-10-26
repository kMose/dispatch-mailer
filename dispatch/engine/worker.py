"""Sender worker: one thread, one asyncio loop, one account (N=1), no UI coupling.

Design seams for later scaling: the worker has an `id`, takes a transport factory, and
claims exclusively from its own run — so a second worker with another config/account
would only need a second run row.
"""
from __future__ import annotations

import asyncio
import copy
import math
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from queue import Empty, Queue as ThreadQueue
from typing import Any, Callable

from .. import db
from ..config import FullConfig
from ..mail import compose
from ..mail.secrets import SecretNotFound, resolve_secret
from ..mail.sender import SMTPFailure, SMTPTransport
from ..sheet import writer as sheet_writer
from . import queue as work_queue
from .state import SendState

FLUSH_EVERY = 10
RATE_LIMIT_SUSPEND_SECONDS = 600


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not domain or not local:
        return "***"
    shown = local[:2] + "***" if len(local) > 2 else "*"
    return f"{shown}@{domain}"


def _slug(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (email or "x").lower()).strip("-")[:60] or "x"


@dataclass
class WorkerStats:
    sent: int = 0
    failed: int = 0
    attempts: int = 0


class Worker(threading.Thread):
    def __init__(
        self,
        cfg: FullConfig,
        *,
        quota: int | None = None,
        dry_run: bool | None = None,
        run_id: int | None = None,
        on_event: Callable[[dict], None] | None = None,
        transport_factory: Callable[[FullConfig, str], Any] | None = None,
        prepared: bool = False,
        max_sends: int | None = None,
    ):
        super().__init__(name=f"dispatch-worker-{cfg.sender.id}", daemon=True)
        self.cfg = cfg
        self.id = cfg.sender.id
        self.quota = int(quota or cfg.campaign.quota)
        self.dry_run = cfg.safety.dry_run if dry_run is None else bool(dry_run)
        self.requested_run_id = run_id
        self._on_event = on_event
        self._transport_factory = transport_factory
        self._prepared = prepared
        self.max_sends = max_sends

        self.events: ThreadQueue[dict] = ThreadQueue()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

        self.run_id: int | None = None
        self.status = "idle"
        self.stop_reason: str | None = None
        self.stats = WorkerStats()
        self.started_at: datetime | None = None
        self.ended_at: datetime | None = None
        self.next_send_at_mono: float | None = None
        self.suspended_until: datetime | None = None
        self.last_error: str | None = None
        self._conn: sqlite3.Connection | None = None
        self._queue: work_queue.Queue | None = None
        self._transport = None
        self._next_at = 0.0
        self._unsent_since_flush = 0
        self._flushed_on_pause = False
        self._rng = random.Random()

    # ------------------------------------------------------------------ API
    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()
        self._flushed_on_pause = False

    @property
    def paused(self) -> bool:
        return self._pause_event.is_set()

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.clear()

    def wait_until_done(self, timeout: float | None = None) -> bool:
        self.join(timeout)
        return not self.is_alive()

    def drain_events(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except Empty:
                break
        return out

    # --------------------------------------------------------------- events
    def _emit(self, kind: str, **payload: Any) -> None:
        event = {"type": kind, "at": db.now_iso(), **payload}
        self.events.put(event)
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:
                pass

    def _log(self, level: str, message: str) -> None:
        self._emit("log", level=level, message=message)

    def _set_status(self, status: str, detail: str = "") -> None:
        self.status = status
        self._emit("status", status=status, detail=detail)

    def _progress(self) -> None:
        elapsed = (datetime.now() - self.started_at).total_seconds() if self.started_at else 0
        remaining = self._queue.remaining() if self._queue else 0
        eta = remaining * self.cfg.throttle.mean_seconds()
        self._emit("progress", sent=self.stats.sent, failed=self.stats.failed,
                   quota=self.quota, remaining=remaining, elapsed=int(elapsed),
                   eta_seconds=int(eta), dry_run=self.dry_run)

    # ----------------------------------------------------------- main thread
    def run(self) -> None:  # pragma: no cover - exercised through integration tests
        try:
            asyncio.run(self._async_main())
        except Exception as exc:
            self._log("error", f"worker crashed: {type(exc).__name__}: {exc}")
            self._set_status("stopped", str(exc))
            self._emit("stopped", reason="error", sent=self.stats.sent,
                       failed=self.stats.failed, message=str(exc))

    async def _async_main(self) -> None:
        cfg = self.cfg
        self.started_at = datetime.now()
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        self._conn = conn
        reason = "queue_empty"
        try:
            self._recover_orphans(conn)
            if not self._prepared:
                from .preflight import import_sheet
                import_sheet(cfg, conn)

            eligible, skipped = work_queue.build_eligible(
                conn, today=date.today(), frequency_days=cfg.campaign.frequency_days
            )
            work_queue.apply_skips(conn, skipped)
            if cfg.campaign.shuffle_order:
                self._rng.shuffle(eligible)
            eligible = eligible[: self.quota]
            if self.max_sends is not None:
                eligible = eligible[: self.max_sends]

            if not eligible:
                self._set_status("stopped", "no eligible contacts")
                self._emit("stopped", reason="no_eligible", sent=0, failed=0)
                return

            run_id = self.requested_run_id or db.create_run(conn, len(eligible), self.dry_run)
            self.run_id = run_id
            q = work_queue.Queue(conn, run_id)
            self._queue = q
            db.enqueue(conn, run_id, eligible, cfg.sender.id,
                       domain=cfg.sender.from_email.split("@")[-1] or "localhost")
            self._emit("started", run_id=run_id, quota=len(eligible),
                       dry_run=self.dry_run, account=cfg.sender.from_email)
            self._set_status("dry_run" if self.dry_run else "running")
            self._log("info", f"run {run_id}: {len(eligible)} queued"
                              + (" (dry run — no mail leaves this machine)" if self.dry_run else ""))

            reason = await self._send_loop(conn, q)
        except SecretNotFound as exc:
            self.last_error = str(exc)
            self._log("error", f"cannot read the SMTP password: {exc}")
            self._set_status("stopped", "auth error")
            reason = "secret_error"
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log("error", f"worker error: {self.last_error}")
            reason = "error"
        finally:
            await self._shutdown(conn, reason)

    # ------------------------------------------------------------------ loop
    async def _send_loop(self, conn: sqlite3.Connection, q: work_queue.Queue) -> str:
        cfg = self.cfg
        window = cfg.campaign.send_window
        self._next_at = time.monotonic()
        failures_window: list[bool] = []
        flush_done_for_pause = False

        while True:
            if self._stop_event.is_set():
                return "user_stop"

            if self.paused:
                self._set_status("paused")
                if not flush_done_for_pause:
                    self._flush(conn)
                    flush_done_for_pause = True
                await asyncio.sleep(0.25)
                continue
            flush_done_for_pause = False
            if self.status == "paused":
                self._set_status("dry_run" if self.dry_run else "running")

            now_local = datetime.now(window.tz)
            if window.enabled and not window.is_open(now_local):
                opens = window.next_open(now_local)
                seconds = max(0, int((opens - now_local).total_seconds()))
                self._set_status("waiting", "outside send window")
                self._emit("window", opens_at=opens.isoformat(), seconds=seconds)
                self._log("info", f"outside the send window; next open {opens.strftime('%a %H:%M')}")
                # Wait for the window however far away it is (a Friday-evening start waits
                # for Monday); stop stays interruptible at 0.2s granularity.
                if not await self._sleep(seconds):
                    return "user_stop"
                continue

            # caps ---------------------------------------------------------
            today_count = db.sent_today(conn, self.dry_run)
            first_send = self._first_send_date(conn)
            warm_cap = cfg.effective_warmup_cap(first_send, date.today(), cfg.limits.daily_cap)
            cap = min(cfg.limits.daily_cap, warm_cap)
            if today_count >= cap:
                self._log("warn", f"daily/warm-up cap reached ({today_count}/{cap})")
                return "daily_cap"

            hour_start = datetime.now().astimezone().replace(
                minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
            hour_count = db.sent_this_hour(conn, self.dry_run, hour_start)
            if hour_count >= cfg.limits.hourly_cap:
                self._log("warn", f"hourly cap reached ({hour_count}/{cfg.limits.hourly_cap})")
                return "hourly_cap"

            # timer (measured send-to-send) ---------------------------------
            if not await self._wait_for_slot():
                return "user_stop"

            row = q.claim()
            if row is None:
                return "queue_empty"
            self._emit("current", email=row.email, sheet_row=row.sheet_row)

            contact = db.contact_by_id(conn, row.contact_id)
            try:
                values = compose.values_for_db_row(cfg, contact)
                if self.dry_run:
                    message = compose.build_dry_run_message(cfg, values, row.message_id)
                else:
                    message = compose.build_message(cfg, values, row.message_id)
            except compose.HeaderInjectionError as exc:
                self._fail(row, SendState.FAILED_HARD, None, str(exc))
                self._log("error", f"header injection rejected for row {row.sheet_row} — "
                                   f"not sent: {exc}")
                failures_window.append(True)
                if self._failure_rate(failures_window):
                    return "failure_rate"
                continue
            except compose.TemplateError as exc:
                self._fail(row, SendState.FAILED_HARD, None, str(exc))
                self._log("error", f"compose failed for row {row.sheet_row}: {exc}")
                failures_window.append(True)
                if self._failure_rate(failures_window):
                    return "failure_rate"
                continue

            if self.dry_run:
                path = self._write_eml(message, row.email)
                self._commit_sent(row, "DRY", dry=True)
                self.stats.sent += 1
                self.stats.attempts += 1
                self._log("info", f"dry-run wrote {path.name} for {mask_email(row.email)}")
                self._emit("sent", email=row.email, sheet_row=row.sheet_row, dry_run=True,
                           count=self.stats.sent)
                failures_window.append(False)
                # A dry run rehearses the real pacing: schedule the next slot here too.
                self._next_at = time.monotonic() + self._interval()
                self._progress()
                self._maybe_flush(conn)
                if self.stats.sent >= self.quota:
                    return "quota_reached"
                continue

            # real send -----------------------------------------------------
            try:
                if self._transport is None:
                    password = resolve_secret(cfg.sender.secret_ref)
                    factory = self._transport_factory or (lambda c, p: SMTPTransport(c, p))
                    self._transport = factory(cfg, password)
                code = await self._transport.send(message)
            except SMTPFailure as failure:
                self.stats.failed += 1
                self.stats.attempts += 1
                failures_window.append(True)
                await self._handle_failure(conn, row, failure)
                self._progress()
                if failure.kind == "auth":
                    return "auth_error"
                if failure.kind == "rate_limit":
                    self.suspended_until = datetime.now().astimezone() + timedelta(
                        seconds=RATE_LIMIT_SUSPEND_SECONDS)
                    until = self.suspended_until
                    self._set_status("backing_off", "rate limit")
                    self._emit("suspended", until=until.isoformat(), seconds=RATE_LIMIT_SUSPEND_SECONDS)
                    self._log("error", f"rate limit (421): account suspended until "
                                       f"{until.strftime('%H:%M')}")
                    await self._sleep(RATE_LIMIT_SUSPEND_SECONDS)
                    self.suspended_until = None
                    self._set_status("running")
                    continue
                if failure.kind == "soft":
                    backoff = self._backoff(row.attempt_no)
                    self._next_at = time.monotonic() + backoff
                    self._set_status("backing_off", failure.message[:120])
                    self._emit("backoff", seconds=backoff)
                    self._log("warn", f"soft failure, backing off {backoff}s: {failure.message[:160]}")
                if self._failure_rate(failures_window):
                    return "failure_rate"
                continue

            self._commit_sent(row, code, dry=False)
            self.stats.sent += 1
            self.stats.attempts += 1
            failures_window.append(False)
            self._note_first_send(conn)
            if self.cfg.safety.archive_eml:
                try:
                    archived = self._archive_sent(message, row)
                    self._log("info", f"archived copy: {archived.name}")
                except Exception as exc:
                    # The message is already accepted; never fail or retry a send
                    # because the local archive could not be written.
                    self._log("warn", f"sent, but local .eml archive failed: {exc}")
            self._set_status("running")
            self._log("info", f"sent to {mask_email(row.email)} (row {row.sheet_row}) [{code}]")
            self._emit("sent", email=row.email, sheet_row=row.sheet_row, dry_run=False,
                       count=self.stats.sent)
            self._next_at = time.monotonic() + self._interval()
            self._progress()
            self._maybe_flush(conn)
            if self.stats.sent >= self.quota:
                return "quota_reached"

    # -------------------------------------------------------------- helpers
    def _send_id(self, row) -> int:
        assert self.run_id is not None
        db_row = self._conn.execute(
            "SELECT id FROM sends WHERE run_id=? AND contact_id=? AND state='sending'"
            " ORDER BY id DESC LIMIT 1", (self.run_id, row.contact_id),
        ).fetchone()
        return int(db_row["id"]) if db_row else -1

    def _fail(self, row, state: SendState, code: str | None, error: str) -> None:
        sid = self._send_id(row)
        if sid < 0:
            return
        q = self._queue
        assert q is not None
        q.commit_failed(sid, state, code, error)
        self.stats.failed += 1
        self.stats.attempts += 1
        self._emit("failed", email=row.email, state=state.value, error=error[:300])
        self._progress()

    async def _handle_failure(self, conn: sqlite3.Connection, row, failure: SMTPFailure) -> None:
        sid = self._send_id(row)
        if sid < 0:
            return
        q = self._queue
        assert q is not None
        if failure.kind in ("hard", "rate_limit"):
            state = (SendState.FAILED_HARD if failure.kind == "hard"
                     else SendState.FAILED_SOFT)
            q.commit_failed(sid, state, failure.code, failure.message)
            if failure.kind == "hard":
                db.add_suppression(conn, row.email, "hard_bounce", "smtp")
                self._log("error", f"hard rejection for {mask_email(row.email)} "
                                   f"[{failure.code}] — suppressed")
            self._emit("failed", email=row.email, state=state.value,
                       error=failure.message[:300])
        elif failure.kind == "auth":
            q.commit_failed(sid, SendState.FAILED_SOFT, failure.code, failure.message)
            self.last_error = "authentication failed"
            self._log("error", "SMTP authentication failed — stopping the run. "
                               "Re-save the password in Settings.")
            self._emit("failed", email=row.email, state="failed_soft", error="auth failed")
        else:
            q.commit_failed(sid, SendState.FAILED_SOFT, failure.code, failure.message)
            self._emit("failed", email=row.email, state="failed_soft",
                       error=failure.message[:300])

    def _commit_sent(self, row, code: str, dry: bool) -> None:
        sid = self._send_id(row)
        if sid < 0:
            return
        assert self._queue is not None
        self._queue.commit_sent(row.contact_id, sid, code, dry)

    def _write_eml(self, message, email: str) -> Path:
        outbox = self.cfg.outbox_dir
        outbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = outbox / f"{stamp}-{_slug(email)}.eml"
        n = 1
        while path.exists():
            path = outbox / f"{stamp}-{_slug(email)}-{n}.eml"
            n += 1
        path.write_bytes(message.as_bytes(policy=message.policy.clone(linesep="\r\n")))
        return path

    def _archive_sent(self, message, row) -> Path:
        """Write the exact bytes of a successful live send under outbox/sent/<date>/.

        Bcc is removed from the copy so a test/archive recipient is not exposed if the
        file is forwarded, while To/Cc and the full MIME body stay intact.
        """
        now = datetime.now()
        folder = self.cfg.outbox_dir / "sent" / now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        slug = _slug(getattr(row, "email", ""))
        sheet_row = getattr(row, "sheet_row", None) or 0
        path = folder / f"{now:%H%M%S-%f}-row{sheet_row}-{slug}.eml"
        archived = copy.deepcopy(message)
        if "Bcc" in archived:
            del archived["Bcc"]
        path.write_bytes(archived.as_bytes(policy=archived.policy.clone(linesep="\r\n")))
        return path

    def _interval(self) -> float:
        t = self.cfg.throttle
        if t.mode == "fixed":
            return max(0.0, float(t.fixed_seconds))
        lo, hi = t.random_seconds
        return float(self._rng.uniform(lo, hi))

    def _backoff(self, attempt_no: int) -> int:
        seq = self.cfg.throttle.backoff_seconds
        return int(seq[min(max(attempt_no - 1, 0), len(seq) - 1)])

    def _failure_rate(self, window: list[bool]) -> bool:
        recent = window[-20:]
        if len(recent) < 10:
            return False
        failures = sum(1 for f in recent if f)
        rate = failures / len(recent)
        if rate > self.cfg.safety.stop_on_failure_rate:
            self._log("error", f"failure rate {rate:.0%} over the last {len(recent)} "
                               f"attempts — stopping")
            return True
        return False

    def _first_send_date(self, conn: sqlite3.Connection) -> date | None:
        stored = db.get_meta(conn, "warmup_first_send")
        if stored:
            try:
                return date.fromisoformat(stored)
            except ValueError:
                pass
        return self.cfg.warmup.first_send_date

    def _note_first_send(self, conn: sqlite3.Connection) -> None:
        if not self.cfg.warmup.enabled:
            return
        if db.get_meta(conn, "warmup_first_send") is None:
            db.set_meta(conn, "warmup_first_send", date.today().isoformat())
            self._log("info", "warm-up clock started today (week 0)")

    async def _wait_for_slot(self) -> bool:
        while True:
            if self._stop_event.is_set():
                return False
            if self.paused:
                # Freeze here: returning to the loop would let the caller claim and
                # send one more message after the operator pressed Pause.
                await asyncio.sleep(0.2)
                continue
            remaining = self._next_at - time.monotonic()
            if remaining <= 0:
                return True
            self._emit("countdown", seconds=int(math.ceil(remaining)))
            if not await self._sleep(min(1.0, remaining)):
                return False

    async def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep. Returns False when a stop was requested."""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if self._stop_event.is_set():
                return False
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(0.2, remaining))

    def _maybe_flush(self, conn: sqlite3.Connection) -> None:
        self._unsent_since_flush += 1
        if self._unsent_since_flush >= FLUSH_EVERY:
            self._flush(conn)
            self._unsent_since_flush = 0

    def _flush(self, conn: sqlite3.Connection) -> None:
        if self.run_id is None or self.dry_run:
            return  # a dry run never rewrites the operator's spreadsheet
        try:
            result = sheet_writer.flush_run(conn, self.cfg, self.run_id)
            self._emit("flushed", summary=result.summary)
            self._log("info", f"sheet flush: {result.summary}")
        except sheet_writer.SheetLockedError as exc:
            self._log("warn", f"sheet flush skipped: {exc}")
        except Exception as exc:
            self._log("warn", f"sheet flush failed: {type(exc).__name__}: {exc}")

    def _recover_orphans(self, conn: sqlite3.Connection) -> None:
        stale = db.mark_unknown_stale(conn)
        if not stale:
            return
        self._log("warn", f"{len(stale)} send(s) from a previous run were interrupted "
                          f"mid-send; marked unknown and left for review (never resent).")
        for sid in stale:
            row = conn.execute(
                "SELECT s.contact_id, c.email_norm FROM sends s JOIN contacts c"
                " ON c.id=s.contact_id WHERE s.id=?", (sid,)
            ).fetchone()
            if row:
                db.add_needs_review(conn, row["contact_id"], row["email_norm"],
                                    "unknown", "interrupted before delivery was confirmed")
        self._emit("review", count=db.pending_review_count(conn))

    async def _shutdown(self, conn: sqlite3.Connection, reason: str) -> None:
        self.stop_reason = reason
        self.ended_at = datetime.now()
        try:
            if self._transport is not None:
                await self._transport.close()
        except Exception:
            pass
        if self.run_id is not None:
            try:
                self._flush(conn)
            except Exception:
                pass
            try:
                status = "stopped" if reason not in ("queue_empty", "quota_reached") else "completed"
                db.finish_run(conn, self.run_id, status, reason)
            except Exception:
                pass
        self._set_status("stopped", reason)
        self._emit("stopped", reason=reason, sent=self.stats.sent,
                   failed=self.stats.failed, run_id=self.run_id)
        self._log("info", f"run finished: {self.stats.sent} sent, {self.stats.failed} failed "
                          f"({reason.replace('_', ' ')})")
        try:
            conn.close()
        except Exception:
            pass
