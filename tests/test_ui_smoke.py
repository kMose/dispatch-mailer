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
    # Treat any confirmation as "discard": tests must never sit on a modal dialog.
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))


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


def test_new_campaign_keeps_servers_and_template(qtbot, tmp_path):
    from dispatch.ui.onboarding import OnboardingWizard
    from tests.conftest import make_config, write_workbook

    base, new = tmp_path / "old", tmp_path / "new"
    base.mkdir()
    new.mkdir()
    cfg = make_config(base, [{"email": "a@example.com"}], dry_run=True)
    cfg.sender.smtp.host = "mail.private.example"
    cfg.sender.smtp.port = 2525
    cfg.sender.smtp.starttls = True
    cfg.sender.smtp.ssl = False
    cfg.sender.imap.host = "imap.private.example"
    cfg.sender.imap.port = 143
    cfg.sender.imap.ssl = False
    cfg.sender.secret_ref = "keyring:dispatch/sam"

    sheet = write_workbook(new / "NewLeads.xlsx", [{"email": "b@example.com"}])
    wizard = OnboardingWizard(initial_config=cfg)
    qtbot.addWidget(wizard)
    wizard.sheet_page.path_edit.setText(str(sheet))
    wizard.accept()

    assert wizard.config is not None
    assert wizard.config.sender.smtp.host == "mail.private.example"
    assert wizard.config.sender.smtp.port == 2525
    assert wizard.config.sender.smtp.starttls is True
    assert wizard.config.sender.imap.host == "imap.private.example"
    assert wizard.config.sender.imap.port == 143
    assert wizard.config.sender.secret_ref == "keyring:dispatch/sam"
    assert wizard.config.template.blocks_path == cfg.template.blocks_path


def test_godaddy_preset_uses_implicit_tls(qtbot, tmp_path):
    from dispatch.ui.onboarding import OnboardingWizard
    from tests.conftest import write_workbook

    sheet = write_workbook(tmp_path / "leads.xlsx", [{"email": "a@example.com"}])
    wizard = OnboardingWizard()
    qtbot.addWidget(wizard)
    wizard.sheet_page.path_edit.setText(str(sheet))
    wizard.sender_page.provider.setCurrentText("GoDaddy / secureserver.net")
    wizard.accept()

    assert wizard.config is not None
    sender = wizard.config.sender
    assert (sender.smtp.host, sender.smtp.port) == ("smtpout.secureserver.net", 465)
    assert sender.smtp.ssl is True and sender.smtp.starttls is False
    assert (sender.imap.host, sender.imap.port) == ("imap.secureserver.net", 993)
    assert sender.imap.ssl is True


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


def test_template_builder_opens_another_template(qtbot, campaign, tmp_path, quiet_boxes):
    from dispatch.mail import blocks
    from tests.conftest import STARTERS

    cfg = campaign([{"email": "a@example.com"}], dry_run=True)
    path = tmp_path / "second.json"
    doc = blocks.load_document(STARTERS / "personal_note.json")
    doc["name"] = "Second"
    doc["subject"] = "Second subject"
    blocks.save_document(doc, path)

    dialog = TemplateBuilderDialog(cfg, None)
    qtbot.addWidget(dialog)
    assert dialog._open_path(path) is True
    assert dialog.name_edit.text() == "Second"
    assert dialog.subject_edit.text() == "Second subject"
    assert cfg.template.blocks_path == str(path)

    dialog._save()
    assert (tmp_path / "second.html").exists()
    assert (tmp_path / "second.txt").exists()
    dialog.close()


def test_builder_undo_redo_and_duplicate_block(qtbot, campaign, quiet_boxes):
    cfg = campaign([{"email": "a@example.com"}], dry_run=True)
    dialog = TemplateBuilderDialog(cfg, None)
    qtbot.addWidget(dialog)
    start = len(dialog.doc["blocks"])

    dialog._add_block("divider")
    assert len(dialog.doc["blocks"]) == start + 1
    dialog._undo()
    assert len(dialog.doc["blocks"]) == start
    dialog._redo()
    assert len(dialog.doc["blocks"]) == start + 1

    dialog.current = 0
    dialog._duplicate_block()
    assert len(dialog.doc["blocks"]) == start + 2
    assert dialog.doc["blocks"][0] == dialog.doc["blocks"][1]
    dialog._saved_json = dialog._snapshot_json()
    dialog.close()


def test_builder_drag_reorder_updates_the_document(qtbot, campaign, quiet_boxes):
    cfg = campaign([{"email": "a@example.com"}], dry_run=True)
    dialog = TemplateBuilderDialog(cfg, None)
    qtbot.addWidget(dialog)
    before = [block["type"] for block in dialog.doc["blocks"]]

    item = dialog.block_list.takeItem(0)
    dialog.block_list.addItem(item)
    dialog._reorder_from_list()

    assert [block["type"] for block in dialog.doc["blocks"]] == before[1:] + before[:1]
    dialog._saved_json = dialog._snapshot_json()
    dialog.close()


def test_builder_autosave_and_history(qtbot, campaign, quiet_boxes):
    cfg = campaign([{"email": "a@example.com"}], dry_run=True)
    dialog = TemplateBuilderDialog(cfg, None)
    qtbot.addWidget(dialog)
    dialog.current = 0
    dialog._rebuild_editor()
    dialog._commit("text", "Changed body text for autosave")

    dialog._autosave()
    autosave = dialog._autosave_path()
    assert autosave is not None and autosave.exists()

    assert dialog._save() is True
    assert not autosave.exists()
    assert dialog._is_dirty() is False
    assert dialog._history_dir().exists()

    dialog.close()


def test_builder_preview_width_and_template_library(qtbot, campaign, quiet_boxes):
    cfg = campaign([{"email": "a@example.com"}], dry_run=True)
    dialog = TemplateBuilderDialog(cfg, None)
    qtbot.addWidget(dialog)

    dialog._set_preview_width(375)
    assert dialog.preview.width() == 375
    assert dialog.mobile_btn.isChecked() and not dialog.desktop_btn.isChecked()
    dialog._set_preview_width(600)
    assert dialog.desktop_btn.isChecked()

    assert dialog.template_combo.count() >= 1
    assert dialog.template_combo.currentText() == "personal_note"
    assert "/60" in dialog.subject_hint.text()
    dialog._saved_json = dialog._snapshot_json()
    dialog.close()


def test_builder_renders_html_and_builds_a_bcc_test_message(qtbot, campaign, quiet_boxes):
    from dispatch.mail import compose

    cfg = campaign([{"email": "a@example.com"}], dry_run=True)
    dialog = TemplateBuilderDialog(cfg, None)
    qtbot.addWidget(dialog)

    values, html, text, subject = dialog._render_current()
    assert "<html" in html.lower()
    assert subject
    message = compose.build_message(
        cfg, values, to="a@example.com", bcc=["b@example.com"],
        subject=subject, html_body=html, text_body=text)
    assert "b@example.com" in str(message["Bcc"])

    dialog._saved_json = dialog._snapshot_json()
    dialog.close()


def test_import_new_sheet_keeps_the_campaign_and_history(qtbot, tmp_path, quiet_boxes):
    from tests.conftest import HEADERS, make_config, write_workbook

    base = tmp_path / "campaign"
    base.mkdir()
    cfg = make_config(base, [{"email": "a@example.com"}, {"email": "b@example.com"}],
                      dry_run=True)
    cfg_path = write_config(cfg, base / "campaign.yaml")
    win = MainWindow(cfg, cfg_path)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(50)

    before = db.contact_by_email(win.conn, "b@example.com")["id"]
    new_sheet = write_workbook(
        tmp_path / "second.xlsx",
        [{"email": "b@example.com"}, {"email": "c@example.com"},
         {"email": "d@example.com"}],
        headers=HEADERS)
    old_sheet = cfg.sheet_file

    assert win._apply_sheet_import(new_sheet, "Leads") is True
    assert cfg.sheet_file == new_sheet.resolve()
    assert {row["email"] for row in win.model.rows} == {
        "b@example.com", "c@example.com", "d@example.com"}
    assert win.conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE in_sheet=1").fetchone()[0] == 3
    assert db.contact_by_email(win.conn, "b@example.com")["id"] == before
    assert old_sheet.exists()
    assert list(base.glob("campaign.yaml.bak-*"))
    win.close()


def test_rename_campaign_updates_title_and_config(qtbot, tmp_path, quiet_boxes):
    from tests.conftest import make_config

    base = tmp_path / "campaign"
    base.mkdir()
    cfg = make_config(base, [{"email": "a@example.com"}], dry_run=True)
    cfg_path = write_config(cfg, base / "campaign.yaml")
    win = MainWindow(cfg, cfg_path)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(30)

    assert win._set_campaign_name("  Samlax Schools  ") is True
    assert cfg.campaign.name == "Samlax Schools"
    assert "Samlax Schools" in win.title.text()
    assert load_config(cfg_path).campaign.name == "Samlax Schools"
    win.close()


def test_import_new_sheet_follows_a_sheet_derived_name(qtbot, tmp_path, quiet_boxes):
    from tests.conftest import HEADERS, make_config, write_workbook

    base = tmp_path / "campaign"
    base.mkdir()
    cfg = make_config(base, [{"email": "a@example.com"}], dry_run=True)
    cfg.campaign.name = cfg.sheet_file.stem  # name came from the old sheet
    win = MainWindow(cfg, None)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(30)

    new_sheet = write_workbook(tmp_path / "NewLeads.xlsx",
                               [{"email": "b@example.com"}], headers=HEADERS)
    assert win._apply_sheet_import(new_sheet, "Leads") is True
    assert cfg.campaign.name == "NewLeads"
    win.close()


def test_import_new_sheet_rejects_a_workbook_without_email(qtbot, tmp_path, quiet_boxes):
    from tests.conftest import make_config, write_workbook

    base = tmp_path / "campaign"
    base.mkdir()
    cfg = make_config(base, [{"email": "a@example.com"}], dry_run=True)
    win = MainWindow(cfg, None)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(30)

    bad = write_workbook(tmp_path / "bad.xlsx", [{"x": "value"}],
                         headers=["Name", "Notes"])
    original = cfg.campaign.sheet_path
    assert win._apply_sheet_import(bad, "Leads") is False
    assert cfg.campaign.sheet_path == original
    win.close()


def test_onboarding_restores_an_existing_campaign_yaml(qtbot, tmp_path, quiet_boxes):
    from PySide6.QtWidgets import QWizard

    from dispatch.ui.onboarding import OnboardingWizard
    from tests.conftest import make_config

    base = tmp_path / "campaign"
    base.mkdir()
    cfg = make_config(base, [{"email": "a@example.com"}], dry_run=True)
    cfg_path = write_config(cfg, base / "campaign.yaml")

    wizard = OnboardingWizard()
    qtbot.addWidget(wizard)
    wizard.show()
    assert wizard._restore_from(cfg_path) is True
    assert wizard.config is not None
    assert wizard.config.campaign.name == cfg.campaign.name
    assert wizard.config_path == cfg_path
    assert wizard.result() == QWizard.Accepted


def test_config_export_uses_absolute_paths_and_reloads(qtbot, tmp_path, quiet_boxes):
    from dispatch.config import dump_config
    from tests.conftest import make_config

    base = tmp_path / "campaign"
    base.mkdir()
    cfg = make_config(base, [{"email": "a@example.com"}], dry_run=True)
    exported = tmp_path / "elsewhere" / "exported.yaml"
    exported.parent.mkdir()

    exported.write_text(dump_config(cfg, absolute_paths=True), encoding="utf-8")
    loaded = load_config(exported)
    assert loaded.sheet_file == cfg.sheet_file.resolve()
    assert loaded.campaign.name == cfg.campaign.name
    assert loaded.html_file.exists()

    win = MainWindow(cfg, cfg.config_path)
    qtbot.addWidget(win)
    target = tmp_path / "from_window.yaml"
    assert win._export_campaign_to(target) is True
    assert load_config(target).sheet_file == cfg.sheet_file.resolve()
    win.close()


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
