"""Eligibility rules as pure functions.

Order matters; first match wins (PROJECT.md section 7).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

TRUTHY = {"y", "yes", "true", "t", "1", "x"}

# RFC5322-lite: one @, sane local part, dotted domain, no whitespace/control chars.
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)

ELIGIBLE = "eligible"

SKIP_IGNORED = "skipped:ignored"
SKIP_SUPPRESSED = "skipped:suppressed"
SKIP_REPLIED = "skipped:replied"
SKIP_INVALID = "skipped:invalid"
SKIP_DUPLICATE = "skipped:duplicate"
SKIP_TOO_SOON = "skipped:too_soon"

SKIP_REASONS = [
    SKIP_IGNORED,
    SKIP_SUPPRESSED,
    SKIP_REPLIED,
    SKIP_INVALID,
    SKIP_DUPLICATE,
    SKIP_TOO_SOON,
]


def normalize_email(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def is_truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().lower() in TRUTHY


def is_valid_email(value: Any) -> bool:
    s = normalize_email(value)
    if not s or len(s) > 254:
        return False
    if not EMAIL_RE.match(s):
        return False
    local, _, domain = s.rpartition("@")
    if not local or not domain:
        return False
    if "." not in domain:
        return False
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return False
    return True


@dataclass(frozen=True)
class Verdict:
    status: str
    eligible: bool
    reason: str | None = None
    days_remaining: int | None = None
    detail: str = ""

    @property
    def label(self) -> str:
        if self.eligible:
            return "Eligible"
        if self.status.startswith("skipped:"):
            return "Skipped: " + self.status.split(":", 1)[1].replace("_", " ")
        return self.status.replace("_", " ")


@dataclass
class ContactRecord:
    """The minimum a contact needs for eligibility (duck-typed elsewhere)."""
    email: str = ""
    last_advertised: date | None = None
    replied: Any = False
    ignored: Any = False
    email_norm: str = field(default="")

    def __post_init__(self) -> None:
        if not self.email_norm:
            self.email_norm = normalize_email(self.email)


def _get(contact: Any, key: str, default: Any = None) -> Any:
    if isinstance(contact, Mapping):
        return contact.get(key, default)
    if hasattr(contact, "keys"):  # sqlite3.Row
        try:
            return contact[key]
        except (KeyError, IndexError):
            return default
    return getattr(contact, key, default)


def evaluate(
    contact: Any,
    *,
    suppression: Iterable[str] = (),
    seen_emails: set[str] | None = None,
    today: date | None = None,
    frequency_days: int = 30,
) -> Verdict:
    """Return the first applicable verdict for one contact."""
    today = today or date.today()
    suppressed = {normalize_email(s) for s in suppression}
    email_norm = normalize_email(_get(contact, "email_norm") or _get(contact, "email", ""))

    if is_truthy(_get(contact, "ignored")):
        return Verdict(SKIP_IGNORED, False, "Ignore column is set")

    if email_norm and email_norm in suppressed:
        return Verdict(SKIP_SUPPRESSED, False, "on the suppression list")

    if is_truthy(_get(contact, "replied")):
        return Verdict(SKIP_REPLIED, False, "already replied")

    if not is_valid_email(email_norm):
        return Verdict(SKIP_INVALID, False, "email failed validation")

    if seen_emails is not None and email_norm in seen_emails:
        return Verdict(SKIP_DUPLICATE, False, "duplicate of an earlier row")

    last = _get(contact, "last_advertised")
    if isinstance(last, str):
        try:
            last = date.fromisoformat(last.strip()[:10])
        except ValueError:
            last = None
    if last is None:
        return Verdict(ELIGIBLE, True, "never advertised")

    gap = (today - last).days
    if gap >= frequency_days:
        return Verdict(ELIGIBLE, True, f"last advertised {gap}d ago")

    remaining = frequency_days - gap
    return Verdict(
        SKIP_TOO_SOON,
        False,
        f"last advertised {gap}d ago; {remaining}d remaining",
        days_remaining=remaining,
    )


def evaluate_all(
    contacts: Iterable[Any],
    *,
    suppression: Iterable[str] = (),
    today: date | None = None,
    frequency_days: int = 30,
) -> list[Verdict]:
    """Evaluate in sheet order so duplicate detection sees earlier rows first."""
    seen: set[str] = set()
    out: list[Verdict] = []
    for c in contacts:
        v = evaluate(c, suppression=suppression, seen_emails=seen, today=today,
                     frequency_days=frequency_days)
        out.append(v)
        email_norm = normalize_email(_get(c, "email_norm") or _get(c, "email", ""))
        if email_norm and v.status not in (SKIP_INVALID,):
            seen.add(email_norm)
    return out
