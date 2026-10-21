"""Pre-flight report: counts, caps, feasibility, blocking conditions."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from dispatch import db
from dispatch.engine import preflight


def fresh(cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    return conn


def test_report_counts_and_readiness(campaign, contacts_12):
    cfg = campaign(contacts_12)
    conn = fresh(cfg)
    report = preflight.run_preflight(cfg, conn, quota=200)
    conn.close()

    assert report.total_rows == 12
    assert report.eligible == 4
    assert report.by_reason["skipped:too_soon"] == 2
    assert report.by_reason["skipped:ignored"] == 1
    assert report.by_reason["skipped:replied"] == 1
    assert report.by_reason["skipped:invalid"] == 3
    assert report.by_reason["skipped:duplicate"] == 1
    assert "grace.hall@example" in report.invalid
    assert "alice@example.com" in report.duplicates
    assert report.ambiguous
    assert report.planned == 4
    assert report.ok is True
    assert "Ready" in report.headline()
    text = report.text()
    assert "Eligible: 4" in text
    assert "Ambiguous dates" in text


def test_blocked_when_quota_does_not_fit(campaign):
    contacts = [{"email": f"u{i}@example.com"} for i in range(300)]
    cfg = campaign(contacts, quota=200, window_enabled=True, fixed_seconds=180)
    cfg.campaign.send_window.start = "09:00"
    cfg.campaign.send_window.end = "18:00"
    cfg.campaign.send_window.days = ["Mon", "Tue", "Wed", "Thu", "Fri",
                                      "Sat", "Sun"]
    now = datetime(2026, 10, 19, 13, 0, tzinfo=ZoneInfo("Europe/London"))
    conn = fresh(cfg)
    report = preflight.run_preflight(cfg, conn, quota=200, now=now,
                                     today=date(2026, 10, 19))
    conn.close()

    assert report.ok is False
    assert report.feasibility is not None and report.feasibility.ok is False
    assert "10h00m" in report.text()
    assert "5h00m" in report.text()
    assert "Blocked" in report.headline()


def test_warmup_cap_clips_the_plan(campaign):
    contacts = [{"email": f"w{i}@example.com"} for i in range(20)]
    cfg = campaign(contacts, quota=20, warmup=True)
    cfg.warmup.ramp = [5]
    conn = fresh(cfg)
    db.set_meta(conn, "warmup_first_send", date.today().isoformat())
    report = preflight.run_preflight(cfg, conn, quota=20)
    conn.close()
    assert report.warmup_cap == 5
    assert report.planned == 5


def test_daily_cap_reached_blocks_planning(campaign):
    contacts = [{"email": f"d{i}@example.com"} for i in range(5)]
    cfg = campaign(contacts, quota=5, daily_cap=2)
    conn = fresh(cfg)
    preflight.import_sheet(cfg, conn)
    contact = db.contact_by_email(conn, "d0@example.com")
    run_id = db.create_run(conn, 2, False)
    db.enqueue(conn, run_id, [int(contact["id"])], "sam")
    claimed = db.claim_next(conn, run_id)
    conn.execute("UPDATE sends SET sent_at=? WHERE id=?",
                 (datetime.now().astimezone().isoformat(timespec="seconds"),
                  claimed["send_id"]))
    conn.execute("UPDATE sends SET state='sent' WHERE id=?", (claimed["send_id"],))
    report = preflight.run_preflight(cfg, conn, quota=5)
    conn.close()
    assert report.daily_sent == 1
    assert report.daily_remaining == 1
    assert report.planned == 1


def test_interrupted_send_is_excluded_and_flagged(campaign):
    contacts = [{"email": "crashed@example.com"}, {"email": "ok@example.com"}]
    cfg = campaign(contacts)
    conn = fresh(cfg)
    preflight.import_sheet(cfg, conn)
    contact = db.contact_by_email(conn, "crashed@example.com")
    run_id = db.create_run(conn, 1, False)
    db.enqueue(conn, run_id, [int(contact["id"])], "sam")
    db.claim_next(conn, run_id)  # left as 'sending'

    report = preflight.run_preflight(cfg, conn)
    conn.close()
    assert report.eligible == 1
    assert report.review_count == 1
    assert report.by_reason["needs_review"] == 1
    assert any("review" in w.lower() for w in report.warnings)


def test_missing_sheet_is_reported_not_raised(campaign, tmp_path):
    cfg = campaign([{"email": "a@example.com"}])
    cfg.campaign.sheet_path = str(tmp_path / "missing.xlsx")
    conn = fresh(cfg)
    report = preflight.run_preflight(cfg, conn)
    conn.close()
    assert report.error and "not found" in report.error.lower()
    assert report.ok is False


def test_locked_sheet_blocks_the_run(campaign):
    cfg = campaign([{"email": "a@example.com"}])
    lock = cfg.sheet_file.with_name("~$" + cfg.sheet_file.name)
    lock.write_text("open in excel")
    try:
        conn = fresh(cfg)
        report = preflight.run_preflight(cfg, conn)
        conn.close()
        assert report.sheet_locked is True
        assert report.ok is False
        assert "Excel" in report.headline()
    finally:
        lock.unlink()


def test_live_run_blocks_on_placeholder_smtp_host(campaign):
    cfg = campaign([{"email": "a@example.com"}])
    cfg.sender.smtp.host = "smtp.PROVIDER"
    conn = fresh(cfg)
    live = preflight.run_preflight(cfg, conn, dry_run=False)
    dry = preflight.run_preflight(cfg, conn, dry_run=True)
    conn.close()
    assert live.ok is False and "placeholder" in (live.error or "").lower()
    assert dry.error is None


def test_live_run_requires_a_postal_address(campaign):
    cfg = campaign([{"email": "a@example.com"}])
    cfg.template.footer.postal_address = "   "
    conn = fresh(cfg)
    report = preflight.run_preflight(cfg, conn, dry_run=False)
    conn.close()
    assert not report.ok
    assert "postal address" in (report.error or "").lower()


def test_formula_dates_are_flagged(campaign):
    from openpyxl import Workbook
    cfg = campaign([{"email": "a@example.com"}])
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(["Email", "LastAdvertised"])
    ws.append(["a@example.com", "=DATE(2026,1,1)"])
    ws.append(["b@example.com", "2026-01-02"])
    wb.save(cfg.sheet_file)
    wb.close()

    from dispatch.sheet.reader import read_sheet
    data = read_sheet(cfg.sheet_file)
    assert any("formulas in a date column" in w for w in data.warnings)


def test_remaining_allowance(campaign):
    cfg = campaign([{"email": "a@example.com"}], daily_cap=10)
    conn = fresh(cfg)
    assert preflight.remaining_allowance(cfg, conn) == 10
    conn.close()
