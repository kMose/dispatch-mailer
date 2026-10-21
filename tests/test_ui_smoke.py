"""Offscreen desktop-UI smoke tests (no network, no modal dialogs left open)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from dispatch.config import load_config  # noqa: E402
from dispatch.ui.main_window import MainWindow  # noqa: E402
from dispatch.ui.template_builder import TemplateBuilderDialog  # noqa: E402


@pytest.fixture
def quiet_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(lambda *a, **k: QMessageBox.Ok))


def _window(qtbot, cfg):
    win = MainWindow(cfg, cfg.config_path)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(50)
    return win


def test_main_window_lists_contacts_and_filters(qtbot, campaign, contacts_12):
    cfg = campaign(contacts_12, dry_run=True)
    win = _window(qtbot, cfg)
    assert win.model.rowCount() == 12
    assert win.dry_banner.isVisible()

    win._set_filter("eligible")
    assert win.proxy.rowCount() == 4
    win._set_filter("failed")
    assert win.proxy.rowCount() == 0
    win._set_filter("all")
    assert win.proxy.rowCount() == 12
    assert win.counter.text().startswith("0 /")
    win.close()


def test_main_window_sorts_by_email(qtbot, campaign, contacts_12):
    from PySide6.QtCore import Qt
    cfg = campaign(contacts_12, dry_run=True)
    win = _window(qtbot, cfg)
    win.proxy.sort(3, Qt.AscendingOrder)
    first = win.proxy.index(0, 3).data()
    assert isinstance(first, str) and first
    win.proxy.sort(3, Qt.DescendingOrder)
    assert win.proxy.index(0, 3).data() != first
    win.close()


def test_template_builder_saves_artifacts(qtbot, campaign, quiet_boxes):
    cfg = campaign([{"email": "a@example.com", "first_name": "A"}])
    dialog = TemplateBuilderDialog(cfg, cfg.config_path)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.subject_edit.setText("Hello {FirstName|default:\"there\"}")
    dialog._add_block("paragraph")
    dialog._validate(show=True)
    dialog._save()

    assert cfg.blocks_file.exists()
    assert cfg.html_file.exists()
    assert cfg.text_file.exists()
    html = cfg.html_file.read_text(encoding="utf-8")
    assert "Hello" in html or "{" in html
    saved = load_config(cfg.config_path) if cfg.config_path else cfg
    assert saved.template.subject == "Hello {FirstName|default:\"there\"}"


def test_template_builder_blocks_save_on_invalid_document(qtbot, campaign, quiet_boxes,
                                                           tmp_path):
    cfg = campaign([{"email": "a@example.com"}])
    dialog = TemplateBuilderDialog(cfg, cfg.config_path)
    qtbot.addWidget(dialog)
    dialog.subject_edit.setText("")  # invalid
    before = cfg.blocks_file.read_text(encoding="utf-8")
    dialog._save()
    assert cfg.blocks_file.read_text(encoding="utf-8") == before
