"""Block document -> email-safe HTML / plain text.

Single-column table layout, inline styles only: no flexbox, grid, <style> blocks,
scripts, web fonts or CSS variables. Everything a user types is escaped first.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import textwrap
from pathlib import Path
from typing import Any, Mapping

FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
TEXT_COLOR = "#1f2933"
MUTED = "#5b6773"
LINK = "#1a5fb4"
BUTTON_BG = "#1a5fb4"

BLOCK_TYPES = ["heading", "paragraph", "image", "button", "divider", "spacer"]

DEFAULT_DOC = {
    "version": 1,
    "name": "New template",
    "subject": "Quick question, {FirstName|default:\"there\"}",
    "preheader": "",
    "max_width_px": 600,
    "blocks": [
        {"type": "paragraph", "text": "Hi {FirstName|default:\"there\"},"},
        {"type": "paragraph", "text": "I wanted to share something short and useful."},
        {"type": "paragraph", "text": "— Sam"},
    ],
}

_LINK_MD = re.compile(r"\[([^\]]+)\]\((https://[^\s)]+)\)")
_ANY_LINK_MD = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD_MD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_MD = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
# Placeholders must survive HTML escaping so the engine can still parse `|default:"…"`.
_PLACEHOLDER = re.compile(r"\{[^{}|]+(?:\|[^{}]*)?\}")


class BlockError(ValueError):
    pass


def _escape_keeping_placeholders(text: str) -> str:
    out: list[str] = []
    last = 0
    for match in _PLACEHOLDER.finditer(text):
        out.append(html_mod.escape(text[last:match.start()], quote=True))
        out.append(match.group(0))
        last = match.end()
    out.append(html_mod.escape(text[last:], quote=True))
    return "".join(out)


def inline_markdown(text: str, *, escape: bool = True) -> str:
    """Escape everything, then allow exactly **bold**, *italic* and [label](https://url)."""
    out = _escape_keeping_placeholders(text) if escape else text

    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if not url.startswith("https://"):
            raise BlockError(f"link must start with https:// ({url})")
        return (f'<a href="{html_mod.escape(url, quote=True)}" '
                f'style="color:{LINK};text-decoration:underline;">{label}</a>')

    out = _LINK_MD.sub(link, out)
    out = _BOLD_MD.sub(r'<strong style="font-weight:600;">\1</strong>', out)
    out = _ITALIC_MD.sub(r"<em>\1</em>", out)
    return out


def load_document(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BlockError(f"template not found: {p}")
    except json.JSONDecodeError as exc:
        raise BlockError(f"{p.name}: invalid JSON ({exc})")
    if not isinstance(doc, dict) or not isinstance(doc.get("blocks"), list):
        raise BlockError(f"{p.name}: expected an object with a blocks array")
    doc.setdefault("subject", "")
    doc.setdefault("preheader", "")
    doc.setdefault("max_width_px", 600)
    doc.setdefault("name", p.stem)
    doc.setdefault("version", 1)
    return doc


def save_document(doc: Mapping[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def _style(base: str, extra: str = "") -> str:
    return f"{base};{extra}" if extra else base


def _heading(block: Mapping[str, Any]) -> str:
    level = int(block.get("level") or 2)
    level = min(max(level, 1), 3)
    size = {1: 24, 2: 20, 3: 17}[level]
    text = inline_markdown(str(block.get("text", "")))
    return (f'<h{level} style="margin:0 0 14px 0;font-family:{FONT};font-size:{size}px;'
            f'line-height:1.3;font-weight:600;color:{TEXT_COLOR};">{text}</h{level}>')


def _paragraph(block: Mapping[str, Any]) -> str:
    text = inline_markdown(str(block.get("text", "")))
    if not text.strip():
        return ""
    return (f'<p style="margin:0 0 16px 0;font-family:{FONT};font-size:16px;'
            f'line-height:1.5;color:{TEXT_COLOR};">{text}</p>')


def _image(block: Mapping[str, Any]) -> str:
    url = str(block.get("url") or "").strip()
    if not url:
        return ""
    if not url.startswith("https://"):
        raise BlockError(f"image URL must start with https:// ({url})")
    alt = html_mod.escape(str(block.get("alt") or ""), quote=True)
    width = int(block.get("width") or 600)
    align = str(block.get("align") or "center")
    link = block.get("link")
    img = (f'<img src="{html_mod.escape(url, quote=True)}" alt="{alt}" width="{width}" '
           f'border="0" style="display:block;width:100%;max-width:100%;height:auto;border:0;'
           f'outline:none;text-decoration:none;">')
    if link:
        if not str(link).startswith("https://"):
            raise BlockError(f"image link must start with https:// ({link})")
        img = (f'<a href="{html_mod.escape(str(link), quote=True)}" target="_blank" '
               f'style="text-decoration:none;">{img}</a>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:0 0 20px 0;"><tr><td align="{align}">'
            f'{img}</td></tr></table>')


def _button(block: Mapping[str, Any]) -> str:
    url = str(block.get("url") or "").strip()
    if not url.startswith("https://"):
        raise BlockError(f"button URL must start with https:// ({url or 'missing'})")
    label = html_mod.escape(str(block.get("text") or "Learn more"), quote=True)
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:6px 0 22px 0;"><tr>'
        f'<td align="center" bgcolor="{BUTTON_BG}" style="border-radius:4px;">'
        f'<a href="{html_mod.escape(url, quote=True)}" target="_blank" '
        f'style="display:inline-block;padding:12px 24px;font-family:{FONT};font-size:15px;'
        f'line-height:1.2;color:#ffffff;text-decoration:none;border-radius:4px;">{label}</a>'
        f'</td></tr></table>'
    )


def _divider(_: Mapping[str, Any]) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:8px 0 22px 0;"><tr>'
            f'<td style="border-top:1px solid #e5e7eb;font-size:1px;line-height:1px;">'
            f'&nbsp;</td></tr></table>')


def _spacer(block: Mapping[str, Any]) -> str:
    height = min(max(int(block.get("height") or 24), 4), 200)
    return (f'<div style="height:{height}px;line-height:{height}px;font-size:1px;">'
            f'&nbsp;</div>')


RENDERERS = {
    "heading": _heading,
    "paragraph": _paragraph,
    "image": _image,
    "button": _button,
    "divider": _divider,
    "spacer": _spacer,
}


def render_blocks_body(doc: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for block in doc.get("blocks", []):
        btype = block.get("type")
        renderer = RENDERERS.get(str(btype))
        if renderer is None:
            raise BlockError(f"unknown block type {btype!r}")
        parts.append(renderer(block))
    return "".join(parts)


def render_document_html(doc: Mapping[str, Any]) -> str:
    width = int(doc.get("max_width_px") or 600)
    width = min(max(width, 320), 900)
    body = render_blocks_body(doc)
    preheader = str(doc.get("preheader") or "")
    pre = ""
    if preheader:
        pre = (f'<div style="display:none;font-size:1px;color:#ffffff;line-height:1px;'
               f'max-height:0;max-width:0;opacity:0;overflow:hidden;">'
               f'{html_mod.escape(preheader)}</div>')
    return (
        '<!DOCTYPE html>\n<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title></title></head>'
        '<body style="margin:0;padding:0;background-color:#f4f5f7;">'
        f'{pre}'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#f4f5f7" style="background-color:#f4f5f7;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        f'<table role="presentation" width="{width}" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="#ffffff" style="width:{width}px;max-width:{width}px;'
        'background-color:#ffffff;border-collapse:collapse;">'
        '<tr><td style="padding:32px 32px 24px 32px;">'
        f'{body}'
        '</td></tr></table>'
        '</td></tr></table>'
        '</body></html>\n'
    )


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_END_RE = re.compile(r"</(?:p|div|h[1-6]|tr|table|li|blockquote)\s*>", re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_LINK_RE = re.compile(
    r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_STYLE_RE = re.compile(r"<(style|script|head)\b.*?</\1\s*>", re.I | re.S)


_HIDDEN_RE = re.compile(
    r'<div[^>]*style="[^"]*display\s*:\s*none[^"]*"[^>]*>.*?</div\s*>', re.I | re.S
)


def html_to_text(html: str, width: int = 72) -> str:
    s = _COMMENT_RE.sub("", html)
    s = _STYLE_RE.sub("", s)
    s = _HIDDEN_RE.sub("", s)

    def link_repl(match: re.Match) -> str:
        href = html_mod.unescape(match.group(1))
        label = html_mod.unescape(_TAG_RE.sub("", match.group(2))).strip()
        if not label or label == href:
            return href
        return f"{label} ({href})"

    s = _LINK_RE.sub(link_repl, s)
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_END_RE.sub("\n\n", s)
    s = _TAG_RE.sub("", s)
    s = html_mod.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    paragraphs: list[str] = []
    for para in s.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        paragraphs.append(textwrap.fill(
            para, width=width, break_long_words=False, break_on_hyphens=False
        ))
    return "\n\n".join(paragraphs) + "\n"


def render_document_text(doc: Mapping[str, Any]) -> str:
    """Plain text derived from the generated HTML (same order, same links)."""
    return html_to_text(render_document_html(doc))


def validate_document(doc: Mapping[str, Any], columns: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Blocks Save and Send-test when errors exist."""
    errors: list[str] = []
    warnings: list[str] = []
    subject = str(doc.get("subject") or "").strip()
    if not subject:
        errors.append("Subject is empty.")
    elif len(subject) > 78:
        errors.append(f"Subject is {len(subject)} characters; the limit is 78.")
    elif len(subject) > 60:
        warnings.append(f"Subject is {len(subject)} characters; over 60 may truncate.")

    blocks = doc.get("blocks") or []
    if not blocks:
        errors.append("Add at least one block.")
    paragraphs = [b for b in blocks if b.get("type") == "paragraph"]
    words = sum(len(re.findall(r"[A-Za-z]{2,}", str(b.get("text", "")))) for b in paragraphs)
    if not paragraphs or words < 3:
        errors.append("Include at least one paragraph of real words (image-only email is a spam signal).")

    known = set(columns or []) | {
        "Email", "FirstName", "LastName", "Company", "UnsubscribeURL",
        "UnsubscribeToken", "PostalAddress", "LastAdvertised", "LastContacted",
    }
    known_norm = {re.sub(r"[\s_]+", "", k).lower() for k in known}

    def check_placeholders(text: str, where: str) -> None:
        for match in re.finditer(r"\{([^{}|]+)(?:\|[^{}]*)?\}", str(text)):
            key = match.group(1).strip()
            if re.sub(r"[\s_]+", "", key).lower() not in known_norm:
                errors.append(f"{where}: unknown placeholder {{{key}}}.")

    check_placeholders(subject, "Subject")
    for i, block in enumerate(blocks, start=1):
        btype = block.get("type")
        for field in ("text", "alt"):
            if block.get(field):
                check_placeholders(block[field], f"Block {i} ({btype})")
        if btype in ("image", "button"):
            url = str(block.get("url") or "")
            check_placeholders(url, f"Block {i} ({btype})")
            if not url.strip():
                errors.append(f"Block {i} ({btype}) has no URL.")
            elif not url.startswith("https://"):
                errors.append(f"Block {i} ({btype}) URL must start with https://.")
        link = block.get("link")
        if link and not str(link).startswith("https://"):
            errors.append(f"Block {i} image link must start with https://.")
        if btype == "paragraph":
            for label, url in _ANY_LINK_MD.findall(str(block.get("text", ""))):
                if not url.startswith("https://"):
                    errors.append(f"Block {i}: link '{label}' must start with https://.")
            text = str(block.get("text", ""))
            if re.search(r"\bHi\s*,", text) or re.search(r"\bHi\s+there\s*,", text):
                errors.append(f"Block {i}: greeting renders as 'Hi ,' — add a default name.")
        if btype == "image" and not block.get("alt"):
            warnings.append(f"Block {i}: image has no alt text.")
        if btype == "heading" and int(block.get("level") or 2) not in (1, 2, 3):
            warnings.append(f"Block {i}: heading level must be 1-3.")

    try:
        rendered = render_document_html(doc)
        if len(rendered.encode("utf-8")) > 100 * 1024:
            warnings.append("Generated HTML is over 100 KB; Gmail may clip it.")
        body_text = html_to_text(rendered)
        if re.search(r"\bHi\s*,", body_text) or re.search(r"\bHi\s+there\s*,", body_text):
            errors.append("Greeting reads 'Hi ,' after rendering — check the default filter.")
    except BlockError as exc:
        errors.append(str(exc))

    if not str(doc.get("preheader") or "").strip():
        warnings.append("No preheader — the inbox will show the first line of body text.")
    return errors, warnings


def new_document(name: str = "New template") -> dict[str, Any]:
    doc = json.loads(json.dumps(DEFAULT_DOC))
    doc["name"] = name
    return doc


def sample_values(email: str = "john@example.com") -> dict[str, Any]:
    return {
        "Email": email,
        "FirstName": "John",
        "LastName": "Smith",
        "Company": "Acme Ltd",
        "PostalAddress": "Samlax, 1 Example Street, London, E1 6AN",
        "UnsubscribeToken": "sampledemo000001",
        "UnsubscribeURL": "https://samlax.net/u/sampledemo000001",
        "LastAdvertised": "2026-01-15",
    }
