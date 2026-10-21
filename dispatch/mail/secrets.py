"""Secret resolution. Passwords live in the OS keyring, never in YAML/SQLite/.eml.

`env:NAME` is supported for headless test rigs and CI only; production uses
`keyring:<service>/<username>`.
"""
from __future__ import annotations

import os


class SecretNotFound(RuntimeError):
    pass


def parse_ref(ref: str) -> tuple[str, str, str]:
    if not ref:
        raise SecretNotFound("secret_ref is empty")
    if ":" not in ref:
        raise SecretNotFound(f"secret_ref {ref!r} must look like keyring:service/user")
    kind, _, rest = ref.partition(":")
    if kind in ("keyring", "env"):
        return kind, rest, ""
    raise SecretNotFound(f"unsupported secret_ref scheme {kind!r}")


def resolve_secret(ref: str) -> str:
    kind, rest, _ = parse_ref(ref)
    if kind == "env":
        value = os.environ.get(rest, "")
        if not value:
            raise SecretNotFound(
                f"environment variable {rest} is empty (test/dev secret)"
            )
        return value
    service, _, username = rest.partition("/")
    if not service or not username:
        raise SecretNotFound(f"keyring ref {ref!r} must be keyring:service/username")
    try:
        import keyring
        value = keyring.get_password(service, username)
    except Exception as exc:  # keyring backend missing/locked
        raise SecretNotFound(f"cannot read OS keyring: {exc}") from exc
    if not value:
        raise SecretNotFound(
            "no password stored for this account — open Settings → Sending account "
            "and save it (or point sender.secret_ref at an env:VAR for headless use)"
        )
    return value


def store_secret(ref: str, password: str) -> None:
    kind, rest, _ = parse_ref(ref)
    if kind == "env":
        raise SecretNotFound("env: refs cannot be written; set the variable instead")
    service, _, username = rest.partition("/")
    import keyring
    keyring.set_password(service, username, password)


def delete_secret(ref: str) -> None:
    kind, rest, _ = parse_ref(ref)
    if kind != "keyring":
        return
    service, _, username = rest.partition("/")
    try:
        import keyring
        keyring.delete_password(service, username)
    except Exception:
        pass


def mask(value: str | None) -> str:
    if not value:
        return "(none)"
    if len(value) <= 4:
        return "****"
    return value[:2] + "****" + value[-2:]
