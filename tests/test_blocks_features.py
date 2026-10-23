"""Rendering features added to the block builder: breaks, alignment, lists, styles."""
from __future__ import annotations

import pytest

from dispatch.mail import blocks


def _doc(*items, subject="A useful note"):
    return {"version": 1, "name": "t", "subject": subject, "preheader": "p",
            "max_width_px": 600, "blocks": list(items)}


def test_line_breaks_survive_rendering():
    html = blocks.render_document_html(_doc(
        {"type": "paragraph", "text": "First line\nSecond line\n\nFourth"}))
    assert "First line<br>Second line<br><br>Fourth" in html
    text = blocks.render_document_text(_doc(
        {"type": "paragraph", "text": "First line\nSecond line"}))
    assert "First line\nSecond line" in text


def test_alignment_is_per_block():
    html = blocks.render_document_html(_doc(
        {"type": "heading", "text": "Title", "align": "center"},
        {"type": "paragraph", "text": "Body copy", "align": "justify"}))
    assert "text-align:center" in html
    assert "text-align:justify" in html


def test_invalid_alignment_falls_back_left():
    html = blocks.render_document_html(_doc(
        {"type": "paragraph", "text": "Body", "align": "diagonal"}))
    assert "text-align:left" in html


def test_lists_render_in_html_and_plain_text():
    doc = _doc({"type": "list", "text": "One\nTwo", "style": "number"})
    html = blocks.render_document_html(doc)
    assert "<ol" in html and "<li" in html
    assert "1. One" in blocks.render_document_text(doc)
    bullet = _doc({"type": "list", "text": "Alpha\nBeta"})
    assert "• Alpha" in blocks.render_document_text(bullet)


def test_signature_and_button_styling():
    doc = _doc(
        {"type": "signature", "name": "Sam", "title": "Founder", "company": "Samlax",
         "email": "sam@samlax.net", "link": "https://samlax.net"},
        {"type": "button", "text": "Book a call", "url": "https://samlax.net/call",
         "align": "left", "bg": "#ff0000", "color": "#000000"},
    )
    html = blocks.render_document_html(doc)
    assert "mailto:sam@samlax.net" in html
    assert "Founder" in html and "Samlax" in html
    assert 'align="left"' in html
    assert "#ff0000" in html and "color:#000000" in html


def test_image_caption_is_rendered():
    html = blocks.render_document_html(_doc(
        {"type": "image", "url": "https://samlax.net/x.png", "alt": "x",
         "caption": "A caption", "align": "center"}))
    assert "A caption" in html


def test_quality_checks_flag_all_caps_and_spam():
    errors, warnings = blocks.validate_document(_doc(
        {"type": "paragraph", "text": "A short useful paragraph with enough words to pass."},
        subject="FREE MONEY NOW!!!"))
    assert not errors
    joined = " ".join(warnings)
    assert "ALL CAPS" in joined
    assert "spam-trigger" in joined


def test_unknown_placeholder_in_a_list_is_an_error():
    errors, _ = blocks.validate_document(_doc(
        {"type": "paragraph", "text": "Hello there, this is a real paragraph."},
        {"type": "list", "text": "Hi {Nickname}"}))
    assert any("Nickname" in error for error in errors)


@pytest.mark.parametrize("color", ["#fff", "red", "javascript:alert(1)"])
def test_button_colour_is_validated(color):
    html = blocks.render_document_html(_doc(
        {"type": "button", "text": "Go", "url": "https://samlax.net", "bg": color}))
    assert "bgcolor=\"#1a5fb4\"" in html
