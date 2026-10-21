"""Eligibility matrix (PROJECT.md section 15.1) and email validation."""
from __future__ import annotations

from datetime import date

import pytest

from dispatch.engine import eligibility as el

TODAY = date(2026, 10, 19)


def contact(**kw):
    base = {"email": "a@example.com", "last_advertised": None, "replied": False,
            "ignored": False}
    base.update(kw)
    return el.ContactRecord(**base)


@pytest.mark.parametrize("record,expected", [
    # blank date -> eligible
    (contact(last_advertised=None), el.ELIGIBLE),
    # 29 days -> too soon
    (contact(last_advertised=date(2026, 9, 20)), el.SKIP_TOO_SOON),
    # exactly 30 days -> eligible
    (contact(last_advertised=date(2026, 9, 19)), el.ELIGIBLE),
    # 45 days -> eligible
    (contact(last_advertised=date(2026, 9, 4)), el.ELIGIBLE),
    # ignore wins over everything
    (contact(ignored="T", last_advertised=None), el.SKIP_IGNORED),
    (contact(replied="Y", last_advertised=None), el.SKIP_REPLIED),
    (contact(email="not-an-email"), el.SKIP_INVALID),
    (contact(email="user@localhost"), el.SKIP_INVALID),
])
def test_matrix(record, expected):
    verdict = el.evaluate(record, today=TODAY, frequency_days=30)
    assert verdict.status == expected


def test_suppression_beats_eligibility():
    v = el.evaluate(contact(), suppression={"A@Example.com"}, today=TODAY)
    assert v.status == el.SKIP_SUPPRESSED


def test_ignore_beats_suppression():
    v = el.evaluate(contact(ignored="y"), suppression={"a@example.com"}, today=TODAY)
    assert v.status == el.SKIP_IGNORED


def test_replied_beats_duplicate_and_date():
    v = el.evaluate(contact(replied="X", last_advertised=date(2026, 10, 18)),
                    today=TODAY)
    assert v.status == el.SKIP_REPLIED


def test_duplicate_only_after_first_occurrence():
    rows = [contact(email="a@example.com"), contact(email="A@EXAMPLE.com")]
    verdicts = el.evaluate_all(rows, today=TODAY)
    assert verdicts[0].status == el.ELIGIBLE
    assert verdicts[1].status == el.SKIP_DUPLICATE


def test_too_soon_reports_days_remaining():
    v = el.evaluate(contact(last_advertised=date(2026, 10, 1)), today=TODAY)
    assert v.status == el.SKIP_TOO_SOON
    assert v.days_remaining == 12


@pytest.mark.parametrize("value", ["y", "YES", "true", "1", "x", True, 1])
def test_truthy(value):
    assert el.is_truthy(value)


@pytest.mark.parametrize("value", [None, "", "no", "0", False, 0, "later"])
def test_falsy(value):
    assert not el.is_truthy(value)


@pytest.mark.parametrize("email,ok", [
    ("sam@samlax.net", True),
    ("first.last+tag@sub.example.co.uk", True),
    ("Bob\nBcc: evil@x.com", False),
    ("a@b", False),
    ("a@@b.com", False),
    ("a b@example.com", False),
    ("a@example..com", False),
    ("", False),
    (None, False),
])
def test_email_validation(email, ok):
    assert el.is_valid_email(email) is ok
