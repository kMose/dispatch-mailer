"""Send-state enum and legal transitions (single source of truth)."""
from __future__ import annotations

from enum import Enum


class SendState(str, Enum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED_SOFT = "failed_soft"
    FAILED_HARD = "failed_hard"
    UNKNOWN = "unknown"


STATUSES = {
    "pending",
    "queued",
    "sending",
    "sent",
    "skipped:ignored",
    "skipped:suppressed",
    "skipped:replied",
    "skipped:invalid",
    "skipped:duplicate",
    "skipped:too_soon",
    "failed_soft",
    "failed_hard",
    "unknown",
}

NEEDS_REVIEW = {SendState.SENDING.value, SendState.UNKNOWN.value}
RESEND_FORBIDDEN = NEEDS_REVIEW | {SendState.SENT.value}

# from -> allowed next states
TRANSITIONS: dict[str, set[str]] = {
    SendState.QUEUED.value: {
        SendState.SENDING.value,
        SendState.FAILED_HARD.value,  # composition rejected before transport
        SendState.UNKNOWN.value,
        SendState.SENT.value,  # dry-run short-circuit
    },
    SendState.SENDING.value: {
        SendState.SENT.value,
        SendState.FAILED_SOFT.value,
        SendState.FAILED_HARD.value,
        SendState.UNKNOWN.value,
    },
    SendState.FAILED_SOFT.value: {
        SendState.SENDING.value,
        SendState.FAILED_HARD.value,
        SendState.UNKNOWN.value,
        SendState.SENT.value,
    },
    # A delivered message can still bounce later (DSN); that is a hard failure.
    SendState.SENT.value: {SendState.FAILED_HARD.value},
    SendState.FAILED_HARD.value: set(),
    SendState.UNKNOWN.value: set(),
}


class IllegalTransition(RuntimeError):
    pass


def can_transition(old: str, new: str) -> bool:
    return new in TRANSITIONS.get(old, set())


def check_transition(old: str, new: str) -> None:
    if not can_transition(old, new):
        raise IllegalTransition(f"{old} -> {new} is not allowed")


def is_resendable(state: str) -> bool:
    """A row may be attempted again only if its last state is a recoverable failure."""
    return state == SendState.FAILED_SOFT.value
