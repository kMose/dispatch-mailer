"""Worker edge cases: template errors, missing secrets, window waits, stops."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dispatch import db
from dispatch.engine import preflight
from dispatch.engine.worker import Worker, mask_email


def prepare(cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    preflight.import_sheet(cfg, conn)
    conn.close()


def run(cfg, timeout=30, **kw):
    events: list[dict] = []
    worker = Worker(cfg, on_event=events.append, **kw)
    worker.start()
    worker.join(timeout)
    assert not worker.is_alive()
    return worker, events


def test_missing_template_marks_rows_failed_not_crashing(campaign, smtp_sink):
    cfg = campaign([{"email": "a@example.com"}, {"email": "b@example.com"}],
                   smtp_port=smtp_sink.port)
    prepare(cfg)
    cfg.html_file.unlink()
    worker, events = run(cfg, quota=2, dry_run=False, prepared=True)
    assert worker.stats.sent == 0
    assert worker.stats.failed == 2
    assert not smtp_sink.messages
    assert any(e.get("type") == "failed" for e in events)


def test_missing_password_stops_the_run(campaign, smtp_sink):
    cfg = campaign([{"email": "a@example.com"}], smtp_port=smtp_sink.port)
    prepare(cfg)
    cfg.sender.secret_ref = "env:DISPATCH_TEST_PASSWORD_MISSING"
    worker, events = run(cfg, quota=1, dry_run=False, prepared=True)
    assert worker.stop_reason == "secret_error"
    assert worker.stats.sent == 0
    assert worker.last_error and "password" in worker.last_error.lower()


def test_outside_window_waits_then_stops(campaign):
    contacts = [{"email": "a@example.com"}]
    cfg = campaign(contacts, window_enabled=True)
    window = cfg.campaign.send_window
    now = datetime.now(window.tz)
    window.start = "00:00"
    window.end = "00:01"
    window.days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    prepare(cfg)

    events: list[dict] = []
    worker = Worker(cfg, quota=1, dry_run=True, on_event=events.append, prepared=True)
    worker.start()
    deadline = time.time() + 10
    while time.time() < deadline and not any(e.get("type") == "window" for e in events):
        time.sleep(0.05)
    worker.stop()
    worker.join(10)
    assert any(e.get("type") == "window" for e in events)
    assert worker.stats.sent == 0
    assert worker.stop_reason == "user_stop"


def test_header_injection_row_is_not_sent(campaign, smtp_sink):
    cfg = campaign([{"email": "a@example.com", "first_name": "Bob\nBcc: evil@x.com"}],
                   smtp_port=smtp_sink.port)
    prepare(cfg)
    worker, _ = run(cfg, quota=1, dry_run=False, prepared=True)
    assert not smtp_sink.messages
    assert worker.stats.failed == 1
    conn = db.connect(cfg.db_path)
    state = conn.execute("SELECT state FROM sends").fetchone()["state"]
    conn.close()
    assert state == "failed_hard"


def test_pause_halts_sending(campaign, smtp_sink):
    contacts = [{"email": f"p{i}@example.com"} for i in range(10)]
    cfg = campaign(contacts, quota=10, fixed_seconds=0, smtp_port=smtp_sink.port)
    prepare(cfg)
    events: list[dict] = []
    worker = Worker(cfg, quota=10, dry_run=False, on_event=events.append,
                    prepared=True)
    worker.start()
    worker.pause()
    time.sleep(0.6)
    during = len(smtp_sink.messages)
    assert worker.status == "paused" or worker.paused
    assert during <= 1
    worker.resume()
    worker.join(30)
    assert worker.stats.sent == 10


def test_manual_stop_is_clean(campaign, smtp_sink):
    contacts = [{"email": f"s{i}@example.com"} for i in range(50)]
    cfg = campaign(contacts, quota=50, smtp_port=smtp_sink.port)
    prepare(cfg)
    worker = Worker(cfg, quota=50, dry_run=False, prepared=True)
    worker.start()
    deadline = time.time() + 10
    while time.time() < deadline and worker.stats.sent < 2:
        time.sleep(0.05)
    worker.stop()
    worker.join(20)
    assert worker.stop_reason == "user_stop"
    assert worker.stats.sent < 50
    assert worker.status == "stopped"
    assert worker.drain_events() is not None


def test_mask_email_keeps_domain_only():
    assert mask_email("alice@example.com") == "al***@example.com"
    assert mask_email("a@example.com") == "*@example.com"
    assert mask_email("") == "***"


def test_worker_events_are_json_friendly(campaign):
    cfg = campaign([{"email": "a@example.com"}], dry_run=True)
    prepare(cfg)
    worker, events = run(cfg, quota=1, dry_run=True, prepared=True)
    for event in events:
        assert isinstance(event, dict) and "type" in event and "at" in event
