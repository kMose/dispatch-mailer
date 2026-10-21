"""Failure-path coverage: auth, hard/soft rejections, caps, resume, unicode."""
from __future__ import annotations

import time
from datetime import date
from email import message_from_bytes, policy

from dispatch import db
from dispatch.engine import preflight
from dispatch.engine.worker import Worker
from dispatch.mail import compose
from dispatch.mail.sender import SMTPFailure
from dispatch.sheet import writer as sheet_writer


class StubTransport:
    """Raises the same classified failure a real server would."""

    def __init__(self, failure: SMTPFailure | None = None):
        self.failure = failure
        self.sent = 0

    async def send(self, message):
        if self.failure:
            raise self.failure
        self.sent += 1
        return "250"

    async def close(self):
        pass


def prepare(cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    preflight.import_sheet(cfg, conn)
    conn.close()


def run(cfg, transport, timeout=30, **kw):
    events: list[dict] = []
    worker = Worker(cfg, on_event=events.append,
                    transport_factory=lambda c, p: transport,
                    prepared=True, **kw)
    worker.start()
    worker.join(timeout)
    assert not worker.is_alive()
    return worker, events


def test_auth_failure_stops_immediately(campaign):
    cfg = campaign([{"email": "a@example.com"}, {"email": "b@example.com"}])
    prepare(cfg)
    transport = StubTransport(SMTPFailure("auth", "535", "bad credentials"))
    worker, events = run(cfg, transport, quota=2, dry_run=False)

    assert worker.stop_reason == "auth_error"
    assert worker.stats.sent == 0
    conn = db.connect(cfg.db_path)
    states = [r["state"] for r in conn.execute("SELECT state FROM sends")]
    conn.close()
    assert states[0] == "failed_soft", "auth failures must stay retryable"
    assert "sent" not in states and "sending" not in states


def test_hard_rejection_suppresses_the_address(campaign):
    cfg = campaign([{"email": "gone@example.com"}])
    prepare(cfg)
    worker, _ = run(cfg, StubTransport(SMTPFailure("hard", "550", "no such user")),
                    quota=1, dry_run=False)
    assert worker.stats.failed == 1
    conn = db.connect(cfg.db_path)
    assert "gone@example.com" in db.suppression_set(conn)
    state = conn.execute("SELECT state FROM sends").fetchone()["state"]
    conn.close()
    assert state == "failed_hard"


def test_soft_rejection_keeps_the_row_retryable(campaign):
    cfg = campaign([{"email": "later@example.com"}])
    prepare(cfg)
    run(cfg, StubTransport(SMTPFailure("soft", "451", "try later")),
        quota=1, dry_run=False)
    conn = db.connect(cfg.db_path)
    state = conn.execute("SELECT state FROM sends").fetchone()["state"]
    conn.close()
    assert state == "failed_soft"
    assert "later@example.com" not in db.suppression_set(db.connect(cfg.db_path))


def test_failure_rate_stops_the_run(campaign):
    contacts = [{"email": f"f{i}@example.com"} for i in range(12)]
    cfg = campaign(contacts, quota=12, fail_rate=0.10)
    prepare(cfg)
    worker, _ = run(cfg, StubTransport(SMTPFailure("hard", "550", "nope")),
                    quota=12, dry_run=False)
    assert worker.stop_reason == "failure_rate"
    assert worker.stats.sent == 0
    assert worker.stats.failed >= 10


def test_hourly_cap_stops_the_run(campaign):
    contacts = [{"email": f"h{i}@example.com"} for i in range(5)]
    cfg = campaign(contacts, quota=5, dry_run=True, hourly_cap=2)
    prepare(cfg)
    worker, _ = run(cfg, StubTransport(), quota=5, dry_run=True)
    assert worker.stats.sent == 2
    assert worker.stop_reason == "hourly_cap"


def test_resume_after_pause_finishes_the_queue(campaign):
    contacts = [{"email": "r1@example.com"}, {"email": "r2@example.com"}]
    cfg = campaign(contacts, quota=2, dry_run=True, fixed_seconds=1)
    prepare(cfg)
    worker = Worker(cfg, quota=2, dry_run=True, prepared=True)
    worker.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and worker.stats.sent < 1:
            time.sleep(0.05)
        worker.pause()
        paused_at = worker.stats.sent
        time.sleep(1.5)
        assert worker.stats.sent == paused_at
        worker.resume()
    finally:
        worker.join(20)
    assert worker.stats.sent == 2


def test_unicode_names_survive_headers_and_body(campaign):
    cfg = campaign([{"email": "unicode@example.com", "first_name": "José 李"}])
    values = compose.template_values(cfg, {"email": "unicode@example.com",
                                          "first_name": "José 李"})
    raw = compose.build_message(cfg, values, "<unicode@samlax.net>").as_bytes()
    parsed = message_from_bytes(raw, policy=policy.default)
    assert "José 李" in parsed["Subject"]
    text = parsed.get_body(preferencelist=("plain",)).get_content()
    assert "José 李" in text


def test_write_back_leaves_a_backup(campaign):
    cfg = campaign([{"email": "backup@example.com"}])
    sheet_writer.write_back(cfg.sheet_file,
                            [sheet_writer.Update(2, last_advertised=date.today())])
    backup = cfg.sheet_file.with_name(cfg.sheet_file.stem + ".dispatch-backup.xlsx")
    assert backup.exists() and backup.stat().st_size > 0


def test_second_flush_is_idempotent(campaign):
    cfg = campaign([{"email": "idem@example.com"}])
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    preflight.import_sheet(cfg, conn)
    cid = int(db.contacts_in_sheet(conn)[0]["id"])
    run_id = db.create_run(conn, 1, False)
    db.enqueue(conn, run_id, [cid], "sam")
    claimed = db.claim_next(conn, run_id)
    db.mark_sent(conn, claimed["send_id"], "250", dry=False)

    first = sheet_writer.flush_run(conn, cfg, run_id)
    second = sheet_writer.flush_run(conn, cfg, run_id)
    conn.close()
    assert first.changes
    assert not second.changes
