"""Templating -> EmailMessage.

The engine consumes rendered artifacts only: subject string, html file, text file.
Every substituted value is HTML-escaped in HTML output and CR/LF-stripped before it
can reach a header. The compliance footer is program-generated and cannot be removed
by a template.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import re
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any, Mapping

BUILTINS = {
    "Email",
    "UnsubscribeURL",
    "UnsubscribeToken",
    "PostalAddress",
}

PLACEHOLDER_RE = re.compile(r"\{([^{}|]+?)((?:\|[^{}]*)?)\}")
FILTER_RE = re.compile(r'([a-z]+)(?::"([^"]*)")?')
HEADER_UNSAFE = re.compile(r"[\r\n]")


class TemplateError(Exception):
    pass


class PlaceholderError(TemplateError):
    pass


class HeaderInjectionError(TemplateError):
    pass


def source_values(contact: Any, extra_columns: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Flatten a contact (row, dataclass or dict) into placeholder keys."""
    if isinstance(contact, Mapping):
        raw = dict(contact)
    elif hasattr(contact, "as_template_values"):
        raw = dict(contact.as_template_values())
    else:
        raw = {}
        for key in ("email", "first_name", "last_name", "company",
                    "last_advertised", "last_contacted", "sheet_row"):
            raw[key] = getattr(contact, key, None)
    for key, value in raw.items():
        if isinstance(value, (date,)):
            raw[key] = value.isoformat()
    if extra_columns:
        for key, value in extra_columns.items():
            raw.setdefault(key, value)
    # Common aliases, case/spacing insensitive.
    aliases = {
        "email": ["Email", "email"],
        "first_name": ["FirstName", "First Name", "firstname", "First_Name"],
        "last_name": ["LastName", "Last Name", "lastname"],
        "company": ["Company", "company"],
    }
    values: dict[str, Any] = {}
    for key, names in aliases.items():
        if key in raw:
            for n in names:
                values[n] = raw[key]
    values.update(raw)
    return values


def unsubscribe_token(cfg, email: str) -> str:
    seed = f"{cfg.campaign.name}|{cfg.sender.from_email}|{email.strip().lower()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def unsubscribe_url(cfg, email: str, token: str | None = None) -> str:
    token = token or unsubscribe_token(cfg, email)
    footer = cfg.template.footer
    if footer.unsubscribe_url:
        return footer.unsubscribe_url.replace("{token}", token)
    mailto = footer.unsubscribe_mailto or f"unsubscribe@{cfg.sender.from_email.split('@')[-1]}"
    return f"mailto:{mailto}?subject=unsubscribe"


def values_for_db_row(cfg, row) -> dict[str, Any]:
    """Values for a sqlite contact row, including every unlisted sheet column."""
    extra: dict[str, Any] = {}
    try:
        raw = row["extra_json"] if "extra_json" in row.keys() else None
        if raw:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                extra = loaded
    except Exception:
        extra = {}
    base: dict[str, Any] = {
        "email": row["email"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "company": row["company"],
        "last_advertised": row["last_advertised"],
        "last_contacted": row["last_contacted"] if "last_contacted" in row.keys() else None,
        "sheet_row": row["sheet_row"] if "sheet_row" in row.keys() else None,
    }
    for key, value in extra.items():
        base.setdefault(key, value)
    values = source_values(base, extra_columns=extra)
    email = str(values.get("Email") or "")
    token = unsubscribe_token(cfg, email)
    values["UnsubscribeToken"] = token
    values["UnsubscribeURL"] = unsubscribe_url(cfg, email, token)
    values["PostalAddress"] = cfg.template.footer.postal_address
    return values


def template_values(cfg, contact: Any) -> dict[str, Any]:
    values = source_values(contact)
    for key in ("FirstName", "LastName", "Company", "Email"):
        values.setdefault(key, "")
    email = str(values.get("Email") or values.get("email") or "")
    token = unsubscribe_token(cfg, email)
    values["UnsubscribeToken"] = token
    values["UnsubscribeURL"] = unsubscribe_url(cfg, email, token)
    values["PostalAddress"] = cfg.template.footer.postal_address
    values.setdefault("Email", email)
    return values


def _apply_filter(value: str, name: str, arg: str | None) -> str:
    if name == "default":
        if value.strip() == "":
            return arg or ""
        return value
    if name == "upper":
        return value.upper()
    if name == "lower":
        return value.lower()
    if name == "title":
        return value.title()
    if name == "trim":
        return value.strip()
    raise TemplateError(f"unknown filter {name!r}")


def _lookup(values: Mapping[str, Any], key: str) -> str:
    if key in values:
        value = values[key]
        return "" if value is None else str(value)
    # tolerate {First Name} / {first_name} / {FIRSTNAME}
    target = re.sub(r"[\s_]+", "", key).lower()
    for k, v in values.items():
        if re.sub(r"[\s_]+", "", str(k)).lower() == target:
            return "" if v is None else str(v)
    raise PlaceholderError(f"unknown placeholder {{{key}}}")


def render_string(template: str, values: Mapping[str, Any], *,
                  html_escape: bool = False) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        filters = match.group(2) or ""
        parsed = FILTER_RE.findall(filters)
        try:
            text = _lookup(values, key)
        except PlaceholderError:
            # A missing column with |default:"…" is not an error — that is the point.
            if not any(name == "default" for name, _ in parsed):
                raise
            text = ""
        for name, arg in parsed:
            text = _apply_filter(text, name, arg)
        return html_mod.escape(text, quote=True) if html_escape else text

    return PLACEHOLDER_RE.sub(repl, template)


def unresolved_placeholders(template: str, values: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for match in PLACEHOLDER_RE.finditer(template):
        key = match.group(1).strip()
        filters = match.group(2) or ""
        has_default = any(name == "default" for name, _ in FILTER_RE.findall(filters))
        try:
            _lookup(values, key)
        except PlaceholderError:
            if not has_default and key not in missing:
                missing.append(key)
    return missing


def sanitize_header(name: str, value: str) -> str:
    if HEADER_UNSAFE.search(value):
        raise HeaderInjectionError(
            f"header injection attempt in {name}: value contains a line break"
        )
    return value.strip()


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TemplateError(f"template file missing: {path}")
    except OSError as exc:
        raise TemplateError(f"cannot read {path}: {exc}")


FOOTER_STYLE = (
    "margin:0;padding:18px 0 0 0;border-top:1px solid #e5e7eb;"
    "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    "font-size:12px;line-height:1.5;color:#6b7280;"
)


def footer_html(cfg, email: str) -> str:
    postal = html_mod.escape(cfg.template.footer.postal_address or "", quote=True)
    parts = [
        f'<div style="{FOOTER_STYLE}">',
        f'<p style="margin:0 0 6px 0;">{postal}</p>' if postal else "",
        '<p style="margin:0;">Reply STOP to unsubscribe.</p>',
        "</div>",
    ]
    return "".join(parts)


def footer_text(cfg, email: str) -> str:
    postal = cfg.template.footer.postal_address
    head = f"\n\n--\n{postal}\n" if postal else "\n\n--\n"
    return f"{head}Reply STOP to unsubscribe.\n"


def render_subject(cfg, values: Mapping[str, Any], subject: str | None = None) -> str:
    raw = subject if subject is not None else cfg.template.subject
    rendered = render_string(raw, values, html_escape=False)
    rendered = sanitize_header("Subject", rendered)
    if not rendered:
        raise TemplateError("subject is empty after rendering")
    return rendered


def render_html(cfg, values: Mapping[str, Any]) -> str:
    body = _read_file(cfg.html_file)
    missing = unresolved_placeholders(body, values)
    if missing:
        raise PlaceholderError(
            "template uses unknown placeholder(s): " + ", ".join("{" + m + "}" for m in missing)
        )
    rendered = render_string(body, values, html_escape=True)
    email = str(values.get("Email", ""))
    return _inject_footer(rendered, footer_html(cfg, email))


def render_text(cfg, values: Mapping[str, Any]) -> str:
    body = _read_file(cfg.text_file)
    missing = unresolved_placeholders(body, values)
    if missing:
        raise PlaceholderError(
            "template uses unknown placeholder(s): " + ", ".join("{" + m + "}" for m in missing)
        )
    rendered = render_string(body, values, html_escape=False)
    return rendered.rstrip() + footer_text(cfg, values.get("Email", ""))


def _inject_footer(html: str, footer: str) -> str:
    """Append inside </body> when present, otherwise at the very end."""
    if re.search(r"</body\s*>", html, flags=re.I):
        return re.sub(r"</body\s*>", footer + "</body>", html, count=1, flags=re.I)
    return html + footer


def build_message(cfg, values: Mapping[str, Any], message_id: str | None = None,
                  *, to: str | None = None, bcc: list[str] | None = None,
                  subject: str | None = None,
                  html_body: str | None = None, text_body: str | None = None,
                  extra_headers: Mapping[str, str] | None = None) -> EmailMessage:
    recipient = to or str(values.get("Email", ""))
    recipient = sanitize_header("To", recipient)
    if not recipient or "@" not in recipient:
        raise TemplateError(f"invalid recipient address {recipient!r}")

    msg = EmailMessage()
    msg["From"] = formataddr((cfg.sender.from_name, cfg.sender.from_email))
    msg["To"] = recipient
    extra = [sanitize_header("Bcc", address) for address in (bcc or []) if address.strip()]
    if extra:
        msg["Bcc"] = ", ".join(extra)
    msg["Subject"] = subject if subject is not None else render_subject(cfg, values)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id or make_msgid(domain=cfg.sender.from_email.split("@")[-1])
    if cfg.sender.reply_to:
        msg["Reply-To"] = cfg.sender.reply_to

    unsub = unsubscribe_url(cfg, recipient)
    mailto = cfg.template.footer.unsubscribe_mailto
    if mailto:
        msg["List-Unsubscribe"] = f"<{unsub}>, <mailto:{mailto}?subject=unsubscribe>"
    else:
        msg["List-Unsubscribe"] = f"<{unsub}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    for key, value in (extra_headers or {}).items():
        msg[key] = sanitize_header(key, value)

    text = text_body if text_body is not None else render_text(cfg, values)
    html = html_body if html_body is not None else render_html(cfg, values)
    msg.set_content(text, subtype="plain", charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")
    return msg


def build_dry_run_message(cfg, values: Mapping[str, Any], message_id: str | None = None
                          ) -> EmailMessage:
    return build_message(cfg, values, message_id,
                         extra_headers={"X-Dispatch-Dry-Run": "yes"})


def render_blocks_document(cfg, doc: Mapping[str, Any], values: Mapping[str, Any]
                           ) -> tuple[str, str]:
    """Render a block document for a contact, including the mandatory footer."""
    from . import blocks as blocks_mod
    body = blocks_mod.render_document_html(doc)
    missing = unresolved_placeholders(body, values)
    if missing:
        raise PlaceholderError(
            "template uses unknown placeholder(s): " + ", ".join("{" + m + "}" for m in missing)
        )
    rendered = render_string(body, values, html_escape=True)
    email = str(values.get("Email", ""))
    html = _inject_footer(rendered, footer_html(cfg, email))
    return html, blocks_mod.html_to_text(html)


def preview_paths(outbox: Path, email: str) -> tuple[Path, Path]:
    stem = re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-") or "preview"
    return outbox / f"preview-{stem}.html", outbox / f"preview-{stem}.txt"


def write_preview(cfg, values: Mapping[str, Any], outbox: Path | None = None) -> tuple[Path, Path]:
    outbox = outbox or cfg.outbox_dir
    outbox.mkdir(parents=True, exist_ok=True)
    html_path, text_path = preview_paths(outbox, str(values.get("Email") or "sample"))
    html_path.write_text(render_html(cfg, values), encoding="utf-8")
    text_path.write_text(render_text(cfg, values), encoding="utf-8")
    return html_path, text_path
