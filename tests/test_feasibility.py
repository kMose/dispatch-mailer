"""Feasibility arithmetic (section 15.9)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dispatch.engine import feasibility


def test_quota_does_not_fit_five_hour_window():
    result = feasibility.check(quota=200, mean_seconds=180, remaining_seconds=5 * 3600)
    assert result.ok is False
    assert result.expected_seconds == 36000
    assert "10h00m" in result.message
    assert "5h00m" in result.message
    assert result.max_quota == 100
    assert any("Reduce quota to 100" in s for s in result.suggestions)


def test_quota_fits():
    result = feasibility.check(quota=100, mean_seconds=180, remaining_seconds=6 * 3600)
    assert result.ok is True
    assert "Feasible" in result.message


def test_outside_window_is_blocked():
    result = feasibility.check(quota=10, mean_seconds=60, remaining_seconds=0,
                               is_open_now=False)
    assert result.ok is False
    assert "Outside the send window" in result.message


def test_human_duration():
    assert feasibility.human_duration(3600) == "1h00m"
    assert feasibility.human_duration(65) == "1m"
    assert feasibility.human_duration(0) == "0m"


def test_window_helpers():
    from dispatch.config import SendWindow
    window = SendWindow(start="09:00", end="18:00", timezone="Europe/London")
    tz = ZoneInfo("Europe/London")
    inside = datetime(2026, 10, 19, 12, 0, tzinfo=tz)
    outside = datetime(2026, 10, 19, 20, 0, tzinfo=tz)
    assert window.is_open(inside) is True
    assert window.is_open(outside) is False
    assert window.seconds_left(inside) == 6 * 3600
    nxt = window.next_open(outside)
    assert nxt.day == 20 and nxt.hour == 9
