"""SMTP transport for one account. Errors are classified for the worker."""
from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass

import aiosmtplib

from ..config import FullConfig


@dataclass
class SMTPFailure(Exception):
    kind: str  # hard | soft | rate_limit | auth
    code: str | None
    message: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.kind} ({self.code}): {self.message}"


def classify(code: int | None, text: str) -> str:
    text_l = (text or "").lower()
    if code == 535 or "authentication" in text_l and code in (530, 534):
        return "auth"
    if code in (530, 534, 535, 550) and "auth" in text_l:
        return "auth"
    if code == 421 and ("4.7.28" in text_l or "quota" in text_l or "rate limit" in text_l):
        return "rate_limit"
    if "rate limit" in text_l or "quota exceeded" in text_l:
        return "rate_limit"
    if code is not None and 400 <= code < 500:
        return "soft"
    if code is not None and 500 <= code < 600:
        return "hard"
    return "soft"


def failure_from_exception(exc: BaseException) -> SMTPFailure:
    if isinstance(exc, SMTPFailure):
        return exc
    if isinstance(exc, aiosmtplib.SMTPAuthenticationError):
        return SMTPFailure("auth", "535", str(exc))
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        recipients = getattr(exc, "recipients", {}) or {}
        code, text = next(iter(recipients.values()), (None, str(exc)))
        code_s = str(code) if code is not None else None
        return SMTPFailure(classify(code, str(text)), code_s, str(text))
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        code = getattr(exc, "code", None)
        return SMTPFailure(classify(code, str(exc)), str(code) if code else None, str(exc))
    if isinstance(exc, (aiosmtplib.SMTPConnectError, aiosmtplib.SMTPServerDisconnected,
                        asyncio.TimeoutError, TimeoutError, OSError, ssl.SSLError)):
        return SMTPFailure("soft", None, f"connection problem: {exc}")
    return SMTPFailure("soft", None, f"{type(exc).__name__}: {exc}")


class SMTPTransport:
    """One authenticated connection, reused for `max_per_connection` messages."""

    def __init__(self, cfg: FullConfig, password: str):
        self.cfg = cfg
        self.password = password
        self._client: aiosmtplib.SMTP | None = None
        self._sent_on_connection = 0
        self.smtp = cfg.sender.smtp

    async def _connect(self) -> aiosmtplib.SMTP:
        client = aiosmtplib.SMTP(
            hostname=self.smtp.host,
            port=self.smtp.port,
            start_tls=bool(self.smtp.starttls),
            use_tls=bool(self.smtp.ssl),
            timeout=self.smtp.timeout,
        )
        await client.connect()
        if self.password:
            try:
                await client.login(self.cfg.sender.from_email, self.password)
            except Exception:
                await _quiet_close(client)
                raise
        self._client = client
        self._sent_on_connection = 0
        return client

    async def send(self, message) -> str:
        if self._client is None:
            await self._connect()
        assert self._client is not None
        try:
            code, response = await self._client.send_message(message)
        except Exception as exc:
            await self.close()
            raise failure_from_exception(exc) from exc
        self._sent_on_connection += 1
        if self._sent_on_connection >= max(1, self.cfg.sender.max_per_connection):
            await self.close()
        return str(code)

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await _quiet_close(client)

    async def __aenter__(self) -> "SMTPTransport":
        await self._connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


async def _quiet_close(client) -> None:
    try:
        await client.quit()
    except Exception:
        try:
            client.close()
        except Exception:
            pass
