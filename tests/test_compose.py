"""Composition, footer compliance, escaping, header injection (sections 15.5-15.6)."""
from __future__ import annotations

import email
from email import policy

import pytest

from dispatch.mail import blocks, compose


def test_empty_name_uses_default(campaign):
    cfg = campaign([{"email": "a@example.com", "first_name": None}])
    values = compose.template_values(cfg, {"email": "a@example.com", "first_name": ""})
    assert compose.render_subject(cfg, values) == "Quick question, there"
    assert "{FirstName" not in compose.render_subject(cfg, values)


def test_missing_placeholder_raises(campaign, tmp_path):
    cfg = campaign([{"email": "a@example.com"}])
    cfg.html_file.write_text("<p>Hi {NoSuchColumn}</p>", encoding="utf-8")
    cfg.text_file.write_text("Hi {NoSuchColumn}", encoding="utf-8")
    values = compose.template_values(cfg, {"email": "a@example.com"})
    with pytest.raises(compose.PlaceholderError):
        compose.render_html(cfg, values)


def test_header_injection_is_rejected(campaign):
    cfg = campaign([{"email": "a@example.com"}])
    values = compose.template_values(
        cfg, {"email": "a@example.com", "first_name": "Bob\nBcc: evil@x.com"})
    with pytest.raises(compose.HeaderInjectionError):
        compose.build_message(cfg, values, "<m@samlax.net>")


def test_footer_and_unsubscribe_headers(campaign):
    cfg = campaign([{"email": "a@example.com", "first_name": "Alice"}])
    values = compose.template_values(cfg, {"email": "a@example.com", "first_name": "Alice"})
    msg = compose.build_message(cfg, values, "<m@samlax.net>")
    html = msg.get_body(preferencelist=("html",)).get_content()
    text = msg.get_body(preferencelist=("plain",)).get_content()
    assert "1 Test Street" in html and "Unsubscribe" in html
    assert "1 Test Street" in text and "Unsubscribe:" in text
    assert "List-Unsubscribe" in msg
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg["Message-ID"] == "<m@samlax.net>"
    assert "sam@samlax.net" in msg["From"]


def test_value_is_html_escaped(campaign):
    cfg = campaign([{"email": "a@example.com"}])
    values = compose.template_values(
        cfg, {"email": "a@example.com", "first_name": "<script>alert(1)</script>"})
    html = compose.render_html(cfg, values)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_no_password_in_eml(campaign):
    cfg = campaign([{"email": "a@example.com"}])
    values = compose.template_values(cfg, {"email": "a@example.com", "first_name": "A"})
    raw = compose.build_message(cfg, values, "<m@samlax.net>").as_bytes()
    assert b"test-password" not in raw
    assert b"Bcc:" not in raw


def test_rendered_message_roundtrips(campaign):
    cfg = campaign([{"email": "a@example.com"}])
    values = compose.template_values(cfg, {"email": "a@example.com", "first_name": "A"})
    raw = compose.build_message(cfg, values, "<m@samlax.net>").as_bytes()
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert parsed["To"] == "a@example.com"
    assert parsed.is_multipart()


def test_filters(campaign):
    values = {"FirstName": "  sam  ", "Email": "a@example.com"}
    assert compose.render_string("{FirstName|trim|upper}", values) == "SAM"
    assert compose.render_string("{FirstName|default:\"x\"}", values) == "  sam  "
    assert compose.render_string("{Nope|default:\"fallback\"}", values) == "fallback"


def test_blocks_validator_catches_problems():
    doc = {
        "subject": "", "preheader": "", "max_width_px": 600,
        "blocks": [
            {"type": "heading", "text": "Hi", "level": 2},
            {"type": "image", "url": "http://insecure.example/x.jpg", "alt": "", "width": 600},
            {"type": "paragraph", "text": "Hi ,"},
            {"type": "paragraph", "text": "Unknown {Nope} here [link](http://bad)"},
        ],
    }
    errors, warnings = blocks.validate_document(doc, ["Email"])
    joined = "\n".join(errors)
    assert "Subject is empty" in joined
    assert "https://" in joined
    assert "Hi ," in joined
    assert "Nope" in joined


def test_blocks_renderer_is_email_safe(campaign):
    doc = blocks.load_document(
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "dispatch" / "templates" / "starters" / "promo_card.json"
    )
    html = blocks.render_document_html(doc)
    assert "<style" not in html.lower()
    assert "<script" not in html.lower()
    assert "flex" not in html.lower()
    assert "display:grid" not in html.lower()
    assert 'role="presentation"' in html
    assert 'border="0"' in html
    assert "max-width:100%" in html


def test_plain_text_derived_from_html():
    html = ('<p>Hello <a href="https://samlax.net/x">the range</a></p>'
            '<p>Second paragraph</p>')
    text = blocks.html_to_text(html, width=40)
    assert "the range (https://samlax.net/x)" in text
    assert "Second paragraph" in text


def test_unsubscribe_token_is_stable_and_per_address(campaign):
    cfg = campaign([{"email": "a@example.com"}])
    t1 = compose.unsubscribe_token(cfg, "a@example.com")
    t2 = compose.unsubscribe_token(cfg, "A@EXAMPLE.COM")
    t3 = compose.unsubscribe_token(cfg, "b@example.com")
    assert t1 == t2 != t3
