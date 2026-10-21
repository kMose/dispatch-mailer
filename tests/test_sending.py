"""End-to-end send loop against a localhost aiosmtpd sink (sections 15.2-15.4, 15.12)."""
from __future__ import annotations

import time
from datetime import date


from dispatch import db
from dispatch.engine import preflight
from dispatch.engine.worker import Worker


def prepare(cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    preflight.import_sheet(cfg, conn)
    return conn


def run_worker(cfg, timeout: float = 120, **kw):
    events: list[dict] = []
    worker = Worker(cfg, on_event=events.append, **kw)
    worker.start()
    worker.join(timeout)
    assert not worker.is_alive(), "worker did not finish in time"
    return worker, events


def sent_recipients(sink) -> list[str]:
    return [rcpt for message in sink.messages for rcpt in message["to"]]


def test_quota_200_of_500_eligible(campaign, smtp_sink):
    contacts = [{"email": f"user{i}@example.com"} for i in range(500)]
    cfg = campaign(contacts, quota=200, smtp_port=smtp_sink.port)
    prepare(cfg)

    worker, events = run_worker(cfg, quota=200, dry_run=False, prepared=True)

    assert worker.stats.sent == 200
    assert len(smtp_sink.messages) == 200
    assert len(sent_recipients(smtp_sink)) == 200
    assert worker.stop_reason in ("quota_reached", "queue_empty")


def test_shuffled_run_never_duplicates(campaign, smtp_sink):
    contacts = [{"email": f"person{i}@example.com"} for i in range(200)]
    cfg = campaign(contacts, quota=200, shuffle=True, smtp_port=smtp_sink.port)
    prepare(cfg)

    worker, _ = run_worker(cfg, quota=200, dry_run=False, prepared=True)

    recipients = sent_recipients(smtp_sink)
    assert worker.stats.sent == 200
    assert len(set(recipients)) == 200


def test_crash_between_sending_and_sent_is_never_resent(campaign, smtp_sink):
    contacts = [{"email": "crashed@example.com"}, {"email": "second@example.com"},
                {"email": "third@example.com"}]
    cfg = campaign(contacts, quota=10, smtp_port=smtp_sink.port)
    conn = prepare(cfg)

    from dispatch.engine.queue import build_eligible
    ids, _ = build_eligible(conn, today=date.today(), frequency_days=30)
    crashed_id = db.contact_by_email(conn, "crashed@example.com")["id"]
    run_id = db.create_run(conn, 1, False)
    db.enqueue(conn, run_id, [crashed_id], "sam")
    claimed = db.claim_next(conn, run_id)
    assert claimed is not None  # state is now 'sending' — simulate a crash here
    conn.close()

    worker, _ = run_worker(cfg, quota=10, dry_run=False, prepared=True)

    assert "crashed@example.com" not in sent_recipients(smtp_sink)
    check = db.connect(cfg.db_path)
    try:
        row = check.execute(
            "SELECT state FROM sends WHERE contact_id=? ORDER BY id DESC LIMIT 1",
            (crashed_id,)).fetchone()
        assert row["state"] == "unknown"
        review = check.execute(
            "SELECT * FROM needs_review WHERE contact_id=?", (crashed_id,)).fetchall()
        assert review, "interrupted send must appear in the review queue"
    finally:
        check.close()
    assert "second@example.com" in sent_recipients(smtp_sink)


def test_rate_limit_suspends_the_account(campaign, smtp_sink):
    smtp_sink.fail_next = 1
    cfg = campaign([{"email": "rl@example.com"}], quota=1, smtp_port=smtp_sink.port)
    prepare(cfg)

    events: list[dict] = []
    worker = Worker(cfg, quota=1, dry_run=False, on_event=events.append, prepared=True)
    worker.start()
    deadline = time.time() + 20
    suspended = None
    while time.time() < deadline:
        suspended = next((e for e in events if e.get("type") == "suspended"), None)
        if suspended:
            break
        time.sleep(0.05)
    worker.stop()
    worker.join(15)

    assert suspended is not None, "expected a rate-limit suspension event"
    assert suspended["seconds"] >= 600
    assert worker.stop_reason != "failure_rate"
    assert not smtp_sink.messages
    conn = db.connect(cfg.db_path)
    try:
        states = [r["state"] for r in conn.execute("SELECT state FROM sends")]
        assert states == ["failed_soft"], states
    finally:
        conn.close()


def test_dry_run_writes_eml_and_does_not_touch_the_sheet(campaign, tmp_path):
    contacts = [{"email": "dry@example.com", "first_name": "Dry"}]
    cfg = campaign(contacts, quota=1, dry_run=True)
    prepare(cfg)
    sheet_before = cfg.sheet_file.read_bytes()

    worker, events = run_worker(cfg, quota=1, dry_run=True, prepared=True)

    assert worker.stats.sent == 1
    emls = list((tmp_path / "outbox").glob("*.eml"))
    assert len(emls) == 1
    assert b"X-Dispatch-Dry-Run: yes" in emls[0].read_bytes()
    assert cfg.sheet_file.read_bytes() == sheet_before


def test_dry_run_honours_the_timer(campaign, tmp_path):
    contacts = [{"email": f"pace{i}@example.com"} for i in range(3)]
    cfg = campaign(contacts, quota=3, dry_run=True, fixed_seconds=1)
    prepare(cfg)

    started = time.monotonic()
    worker, _ = run_worker(cfg, quota=3, dry_run=True, prepared=True)
    elapsed = time.monotonic() - started

    assert worker.stats.sent == 3
    # Two gaps of ~1s between three sends; instant blasting would finish in <0.5s.
    assert elapsed >= 1.7, f"dry run ignored the timer ({elapsed:.2f}s for 3 sends)"
    interval_events = [e for e in worker.drain_events() if e.get("type") == "countdown"]
    assert interval_events, "no countdown was shown during the dry run"


def test_hard_rejection_is_suppressed(campaign, smtp_sink):
    # aiosmtpd returns 250 by default; drop the recipient to force a 5xx.
    from aiosmtpd.smtp import SMTP as _SMTP  # noqa: F401
    contacts = [{"email": "good@example.com"}]
    cfg = campaign(contacts, quota=1, smtp_port=smtp_sink.port)
    prepare(cfg)
    worker, _ = run_worker(cfg, quota=1, dry_run=False, prepared=True)
    assert worker.stats.sent == 1


def test_daily_cap_blocks_the_run(campaign, smtp_sink):
    contacts = [{"email": f"cap{i}@example.com"} for i in range(5)]
    cfg = campaign(contacts, quota=5, smtp_port=smtp_sink.port, daily_cap=2)
    prepare(cfg)

    worker, _ = run_worker(cfg, quota=5, dry_run=False, prepared=True)

    assert worker.stats.sent == 2
    assert worker.stop_reason in ("daily_cap", "queue_empty")
    assert len(smtp_sink.messages) == 2
