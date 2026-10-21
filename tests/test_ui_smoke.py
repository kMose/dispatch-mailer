"""Offscreen desktop-UI smoke tests (no network, no modal dialogs left open)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from dispatch import db  # noqa: E402
from dispatch.config import load_config  # noqa: E402
from dispatch.engine import preflight  # noqa: E402
from dispatch.ui.main_window import MainWindow  # noqa: E402
from dispatch.ui.preflight import PreflightDialog  # noqa: E402
from dispatch.ui.suppression import SuppressionDialog  # noqa: E402
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


def test_suppression_dialog_removes_and_approves(qtbot, campaign, quiet_boxes, monkeypatch):
    cfg = campaign([{"email": "a@example.com"}])
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.add_suppression(conn, "bad@example.com", "hard_bounce", "dsn")
    db.add_needs_review(conn, None, "opt@example.com", "opt_out", "asked to be removed")
    conn.close()

    dialog = SuppressionDialog(cfg)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.sup_table.rowCount() == 1
    assert dialog.review_table.rowCount() == 1

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    dialog.sup_table.selectRow(0)
    dialog._remove_selected()
    assert dialog.sup_table.rowCount() == 0

    dialog.review_table.selectRow(0)
    dialog._approve_opt_out()
    assert dialog.review_table.rowCount() == 0

    check = db.connect(cfg.db_path)
    assert "opt@example.com" in db.suppression_set(check)
    assert not db.review_items(check)
    check.close()
    dialog.close()


def test_preflight_quota_is_clipped_to_daily_allowance(qtbot, campaign):
    contacts = [{"email": f"q{i}@example.com"} for i in range(5)]
    cfg = campaign(contacts, daily_cap=2, dry_run=True)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    report = preflight.run_preflight(cfg, conn)
    dialog = PreflightDialog(report, cfg)
    qtbot.addWidget(dialog)
    assert report.eligible == 5
    assert report.daily_remaining == 2
    assert dialog.quota_spin.maximum() == 2
    dialog.close()
    conn.close()


def test_template_builder_blocks_save_on_invalid_document(qtbot, campaign, quiet_boxes,
                                                           tmp_path):
    cfg = campaign([{"email": "a@example.com"}])
    dialog = TemplateBuilderDialog(cfg, cfg.config_path)
    qtbot.addWidget(dialog)
    dialog.subject_edit.setText("")  # invalid
    before = cfg.blocks_file.read_text(encoding="utf-8")
    dialog._save()
    assert cfg.blocks_file.read_text(encoding="utf-8") == before
