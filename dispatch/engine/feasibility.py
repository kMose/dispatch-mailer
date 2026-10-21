"""Feasibility arithmetic: will the quota fit in the remaining send window?"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


def human_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


@dataclass
class Feasibility:
    ok: bool
    quota: int
    mean_seconds: float
    expected_seconds: int
    remaining_seconds: int
    message: str
    suggestions: list[str] = field(default_factory=list)
    max_quota: int = 0
    is_open_now: bool = True

    @property
    def expected_human(self) -> str:
        return human_duration(self.expected_seconds)

    @property
    def remaining_human(self) -> str:
        return human_duration(self.remaining_seconds)


def check(
    *,
    quota: int,
    mean_seconds: float,
    remaining_seconds: int,
    window_end_hhmm: str = "18:00",
    is_open_now: bool = True,
    min_interval: int = 60,
) -> Feasibility:
    """Compare expected run time against the window that is actually left today."""
    mean_seconds = max(1.0, float(mean_seconds))
    expected = int(round(quota * mean_seconds))
    remaining = max(0, int(remaining_seconds))
    max_quota = int(remaining // mean_seconds) if mean_seconds else quota
    ok = expected <= remaining and is_open_now

    if ok:
        msg = (f"Feasible: {quota} sends at ~{human_duration(mean_seconds)} each need "
               f"~{human_duration(expected)}; {human_duration(remaining)} of window left.")
        return Feasibility(True, quota, mean_seconds, expected, remaining, msg,
                           max_quota=max_quota, is_open_now=is_open_now)

    if not is_open_now:
        msg = (f"Outside the send window. This quota needs ~{human_duration(expected)} "
               f"and the window does not open today.")
    else:
        msg = (f"This quota needs ~{human_duration(expected)} but only "
               f"{human_duration(remaining)} of your send window remains today.")

    suggestions = [
        f"Reduce quota to {max(max_quota, 0)}",
        f"Extend the window to {window_end_hhmm}",
        f"Set the timer to {max(30, int(mean_seconds * 0.4))}-{max(60, int(mean_seconds * 0.6))}s",
    ]
    return Feasibility(False, quota, mean_seconds, expected, remaining, msg, suggestions,
                       max_quota=max_quota, is_open_now=is_open_now)


def remaining_window_seconds(now: datetime, window) -> int:
    """Seconds left in today's window for a SendWindow config object."""
    return window.seconds_left(now)


def proposed_resume(now: datetime, window) -> datetime:
    return window.next_open(now)


def quota_that_fits(remaining_seconds: int, mean_seconds: float) -> int:
    if mean_seconds <= 0:
        return 0
    return max(0, int(remaining_seconds // mean_seconds))
