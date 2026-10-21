"""Date parsing: real datetimes, Excel serials, text dates, ambiguity (section 15.8)."""
from __future__ import annotations

from datetime import date, datetime


from dispatch.sheet.reader import read_sheet, parse_date_cell, parse_date_text


def test_dmy_vs_mdy():
    assert parse_date_text("03/04/2026", "DMY")[0] == date(2026, 4, 3)
    assert parse_date_text("03/04/2026", "MDY")[0] == date(2026, 3, 4)


def test_ambiguous_flag():
    parsed, ambiguous, _ = parse_date_text("03/04/2026", "DMY")
    assert parsed == date(2026, 4, 3)
    assert ambiguous is True

    parsed, ambiguous, _ = parse_date_text("25/04/2026", "DMY")
    assert parsed == date(2026, 4, 25)
    assert ambiguous is False


def test_iso_order_flags_slash_dates():
    parsed, ambiguous, note = parse_date_text("03/04/2026", "ISO")
    assert parsed is None
    assert ambiguous is True
    assert "ISO" in note


def test_unambiguous_formats():
    assert parse_date_text("2026-04-03", "DMY")[0] == date(2026, 4, 3)
    assert parse_date_text("3 Apr 2026", "DMY")[0] == date(2026, 4, 3)
    assert parse_date_text("Apr 3, 2026", "DMY")[0] == date(2026, 4, 3)
    assert parse_date_text("20260403", "MDY")[0] == date(2026, 4, 3)
    assert parse_date_cell(datetime(2026, 4, 3, 9, 0), "DMY")[0] == date(2026, 4, 3)
    assert parse_date_cell(46115, "DMY")[0] == date(2026, 4, 3)


def test_invalid_calendar_date_falls_back_and_flags():
    parsed, ambiguous, note = parse_date_text("31/04/2026", "DMY")
    assert parsed is None
    # 31 April is invalid as DMY, but it is 2026-04-31? no: MDY would be month 31 -> invalid
    assert ambiguous is False
    assert "unparseable" in note


def test_ambiguous_dates_reported_by_reader(tmp_path, campaign, contacts_12):
    cfg = campaign(contacts_12)
    data = read_sheet(cfg.sheet_file, cfg.campaign.sheet_tab, "DMY")
    rows = [a.row for a in data.ambiguous]
    assert 7 in rows  # Frank's 03/04/2026
    frank = next(c for c in data.contacts if c.email == "frank@example.com")
    assert frank.last_advertised == date(2026, 4, 3)


def test_reader_handles_serial_numbers(tmp_path, campaign):
    from dispatch.sheet.reader import parse_date_cell
    assert parse_date_cell(45000, "DMY")[0] == date(2023, 3, 15)


def test_blank_and_text_dates(tmp_path, campaign):
    cfg = campaign([
        {"email": "a@example.com", "last_advertised": "  "},
        {"email": "b@example.com", "last_advertised": "2026-01-02"},
    ])
    data = read_sheet(cfg.sheet_file)
    assert data.contacts[0].last_advertised is None
    assert data.contacts[1].last_advertised == date(2026, 1, 2)
