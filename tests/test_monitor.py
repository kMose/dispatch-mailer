"""IMAP reply / bounce / auto-reply / opt-out handling from saved .eml files."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from dispatch import db
from dispatch.engine import eligibility, preflight
from dispatch.mail import monitor

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def sent_contact(cfg, email: str, message_id: str):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    preflight.import_sheet(cfg, conn)
    contact = db.contact_by_email(conn, email)
    run_id = db.create_run(conn, 1, False)
    db.enqueue(conn, run_id, [int(contact["id"])], "sam")
    claimed = db.claim_next(conn, run_id)
    conn.execute("UPDATE sends SET message_id=? WHERE id=?", (message_id, claimed["send_id"]))
    db.mark_sent(conn, claimed["send_id"], "250", dry=False)
    return conn, int(contact["id"])


def test_gmail_550_dsn_suppresses_and_fails_hard(campaign):
    cfg = campaign([
        {"email": "bob@example.com", "first_name": "Bob"},
        {"email": "other@example.com"},
    ])
    conn, contact_id = sent_contact(cfg, "bob@example.com", "<send-bob-001@samlax.net>")

    raw = (FIXTURES / "gmail_550.eml").read_bytes()
    events = monitor.parse_message(raw, uid="42")
    assert events and events[0].kind == "bounce"
    action = monitor.apply_message(conn, events[0], "sam")
    assert action == "bounce_hard"

    assert "bob@example.com" in db.suppression_set(conn)
    send = conn.execute("SELECT * FROM sends WHERE contact_id=?", (contact_id,)).fetchone()
    assert send["state"] == "failed_hard"
    verdict = eligibility.evaluate(db.contact_by_id(conn, contact_id),
                                   suppression=db.suppression_set(conn))
    assert verdict.status == eligibility.SKIP_SUPPRESSED
    snippet = conn.execute("SELECT snippet FROM inbound_events").fetchone()["snippet"]
    assert len(snippet) <= 200
    conn.close()


def test_reply_with_in_reply_to_marks_replied_and_excludes_next_run(campaign):
    cfg = campaign([{"email": "alice@example.com", "first_name": "Alice"}])
    conn, contact_id = sent_contact(cfg, "alice@example.com", "<send-alice-001@samlax.net>")

    events = monitor.parse_message((FIXTURES / "reply_in_reply_to.eml").read_bytes(), uid="7")
    assert [e.kind for e in events] == ["reply"]
    monitor.apply_message(conn, events[0], "sam")

    contact = db.contact_by_id(conn, contact_id)
    assert contact["replied"] == 1
    assert contact["autoreply_only"] == 0
    verdict = eligibility.evaluate(contact, today=date.today())
    assert verdict.status == eligibility.SKIP_REPLIED
    conn.close()


def test_auto_reply_is_not_a_human_reply(campaign):
    cfg = campaign([{"email": "carol@example.com", "first_name": "Carol"}])
    conn, contact_id = sent_contact(cfg, "carol@example.com", "<send-carol-001@samlax.net>")

    events = monitor.parse_message((FIXTURES / "autoreply.eml").read_bytes(), uid="8")
    assert [e.kind for e in events] == ["autoreply"]
    monitor.apply_message(conn, events[0], "sam")

    contact = db.contact_by_id(conn, contact_id)
    assert contact["autoreply_only"] == 1
    assert contact["replied"] == 0
    conn.close()


def test_opt_out_is_queued_for_review_not_auto_suppressed(campaign):
    cfg = campaign([{"email": "dave@example.com", "first_name": "Dave"}])
    conn, contact_id = sent_contact(cfg, "dave@example.com", "<send-dave-001@samlax.net>")

    events = monitor.parse_message((FIXTURES / "optout.eml").read_bytes(), uid="9")
    assert [e.kind for e in events] == ["optout"]
    monitor.apply_message(conn, events[0], "sam")

    assert "dave@example.com" not in db.suppression_set(conn)
    review = db.review_items(conn)
    assert review and review[0]["reason"] == "opt_out"
    assert db.contact_by_id(conn, contact_id)["replied"] == 1
    conn.close()


def test_mailing_list_mail_is_ignored(campaign):
    cfg = campaign([{"email": "alice@example.com"}])
    conn, _ = sent_contact(cfg, "alice@example.com", "<send-alice-001@samlax.net>")
    raw = (b"From: News <news@list.example>\nTo: sam@samlax.net\n"
           b"Subject: Re: Quick question\nList-Id: <news.list.example>\n"
           b"Message-ID: <list-1@list.example>\n\nUnsubscribe from this list\n")
    events = monitor.parse_message(raw)
    assert events[0].kind == "ignored"
    assert monitor.apply_message(conn, events[0], "sam") == "ignored"
    assert db.contact_by_email(conn, "alice@example.com")["replied"] == 0
    conn.close()


def test_bounce_without_dsn_part_uses_failed_recipients(campaign):
    cfg = campaign([{"email": "bob@example.com"}])
    conn, _ = sent_contact(cfg, "bob@example.com", "<send-bob-001@samlax.net>")
    raw = (b"From: Mail Delivery System <MAILER-DAEMON@example.net>\n"
           b"To: sam@samlax.net\nSubject: failure\n"
           b"X-Failed-Recipients: bob@example.com\n"
           b"Content-Type: text/plain\n\ncould not deliver\n")
    events = monitor.parse_message(raw)
    assert events[0].kind == "bounce"
    monitor.apply_message(conn, events[0], "sam")
    assert "bob@example.com" in db.suppression_set(conn)
    conn.close()


def test_soft_dsn_is_not_suppressed(campaign):
    cfg = campaign([{"email": "bob@example.com"}])
    conn, _ = sent_contact(cfg, "bob@example.com", "<send-bob-001@samlax.net>")
    raw = (FIXTURES / "gmail_550.eml").read_bytes().replace(b"Status: 5.1.1", b"Status: 4.2.2")
    raw = raw.replace(b"550-5.1.1", b"450-4.2.2")
    events = monitor.parse_message(raw)
    action = monitor.apply_message(conn, events[0], "sam")
    assert action == "bounce_soft"
    assert "bob@example.com" not in db.suppression_set(conn)
    conn.close()


def test_malformed_message_does_not_crash():
    events = monitor.parse_message(b"not an email at all")
    assert isinstance(events, list)
