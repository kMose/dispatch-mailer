"""Offscreen desktop-UI smoke tests (no network, no modal dialogs left open)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from dispatch import db  # noqa: E402
from dispatch.config import load_config, write_config  # noqa: E402
from dispatch.engine import preflight  # noqa: E402
from dispatch.ui.main_window import MainWindow  # noqa: E402
from dispatch.ui.onboarding import backup_campaign  # noqa: E402
from dispatch.ui.preflight import PreflightDialog  # noqa: E402
from dispatch.ui.settings import SettingsDialog  # noqa: E402
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


def test_main_window_fits_a_1280_screen(qtbot, campaign, contacts_12):
    cfg = campaign(contacts_12, dry_run=True)
    win = _window(qtbot, cfg)
    assert win.minimumSizeHint().width() <= 1280, (
        f"window cannot shrink below {win.minimumSizeHint().width()}px"
    )
    # friendly status text, not the internal token
    from dispatch.ui.models import COLUMNS
    from PySide6.QtCore import Qt
    status_col = COLUMNS.index("Status")
    labels = {win.model.data(win.model.index(r, status_col), Qt.DisplayRole)
              for r in range(win.model.rowCount())}
    assert "Skipped — too soon" in labels
    assert "skipped:too_soon" not in labels
    win.close()


def test_contact_search_filters_the_table(qtbot, campaign, contacts_12):
    cfg = campaign(contacts_12, dry_run=True)
    win = _window(qtbot, cfg)
    win.search.setText("alice")
    assert win.proxy.rowCount() == 2  # Alice and the duplicate ALICE row
    win.search.setText("zzz-nobody")
    assert win.proxy.rowCount() == 0
    win.search.clear()
    assert win.proxy.rowCount() == 12
    win.close()


def test_switching_campaign_imports_the_new_sheet(qtbot, tmp_path):
    from tests.conftest import make_config

    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    cfg1 = make_config(one, [{"email": "one@example.com"}], dry_run=True)
    cfg2 = make_config(two, [{"email": "two@example.com"},
                             {"email": "three@example.com"}], dry_run=True)
    win = _window(qtbot, cfg1)
    assert {row["email"] for row in win.model.rows} == {"one@example.com"}

    assert win._switch_campaign(cfg2, None) is True
    assert {row["email"] for row in win.model.rows} == {
        "two@example.com", "three@example.com"}
    win.close()


def test_reload_config_picks_up_a_changed_workbook(qtbot, tmp_path, quiet_boxes):
    from tests.conftest import make_config, write_workbook

    cfg = make_config(tmp_path, [{"email": "old@example.com"}], dry_run=True)
    cfg_path = write_config(cfg, tmp_path / "campaign.yaml")
    win = _window(qtbot, cfg)
    win.cfg_path = cfg_path
    assert {row["email"] for row in win.model.rows} == {"old@example.com"}

    write_workbook(tmp_path / "leads.xlsx", [{"email": "new@example.com"}])
    win._reload_config()
    assert {row["email"] for row in win.model.rows} == {"new@example.com"}
    win.close()


def test_backup_campaign_copies_config_and_templates(tmp_path):
    (tmp_path / "campaign.yaml").write_text("campaign: {}\n", encoding="utf-8")
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "a.json").write_text("{}", encoding="utf-8")

    dest = backup_campaign(tmp_path)
    assert dest is not None and dest.is_dir()
    assert (dest / "campaign.yaml").read_text(encoding="utf-8") == "campaign: {}\n"
    assert (dest / "templates" / "a.json").exists()
    assert backup_campaign(tmp_path / "empty") is None


def test_settings_has_tabs_and_saves(qtbot, campaign, quiet_boxes):
    cfg = campaign([{"email": "a@example.com"}], dry_run=True)
    dialog = SettingsDialog(cfg, cfg.config_path)
    qtbot.addWidget(dialog)
    assert dialog.tabs.count() == 5
    assert [dialog.tabs.tabText(i) for i in range(5)] == [
        "Account", "Servers", "Campaign", "Window", "Safety",
    ]
    dialog.tabs.setCurrentIndex(2)
    dialog.campaign_name.setText("Renamed campaign")
    dialog._save()
    assert cfg.campaign.name == "Renamed campaign"
    dialog.close()


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
    assert dialog.tile_eligible[1].text() == "5"
    assert dialog.tile_left[1].text() == "2"
    assert dialog.tile_projected[1].text() != "—"
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
