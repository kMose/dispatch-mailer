"""Block-based template builder (plus read-only mode for hand-written HTML).

Deliberately simple: a vertical list of blocks, a field editor, and an approximate
preview. Not a drag-and-drop designer, not a canvas.
"""
from __future__ import annotations

import asyncio
import tempfile
import urllib.request
import webbrowser
from pathlib import Path

from PySide6.QtCore import QEvent, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from ..config import FullConfig, write_config
from ..mail import blocks, compose
from ..mail.secrets import SecretNotFound, resolve_secret
from ..mail.sender import SMTPFailure, SMTPTransport
from ..sheet.reader import read_sheet

STARTERS = Path(__file__).resolve().parent.parent / "templates" / "starters"
BUILTIN_PLACEHOLDERS = ["FirstName", "LastName", "Email", "Company",
                        "UnsubscribeURL", "PostalAddress"]


class SendTestThread(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, cfg: FullConfig, message, password: str, parent=None):
        super().__init__(parent)
        self.cfg, self.message, self.password = cfg, message, password

    def run(self) -> None:  # pragma: no cover - network
        async def go():
            transport = SMTPTransport(self.cfg, self.password)
            try:
                return await transport.send(self.message)
            finally:
                await transport.close()

        try:
            code = asyncio.run(go())
            self.done.emit(code)
        except SMTPFailure as exc:
            self.failed.emit(f"{exc.kind}: {exc.message}")
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class TemplateBuilderDialog(QDialog):
    saved = Signal()

    def __init__(self, cfg: FullConfig, cfg_path: Path | None = None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.cfg_path = cfg_path or cfg.config_path
        self.raw = cfg.template_kind() == "raw"
        self.doc: dict | None = None
        self.contacts: list = []
        self.current = -1
        self._loading = False
        self._focus_widget = None
        self._test_thread = None

        if not self.raw:
            try:
                self.doc = blocks.load_document(cfg.blocks_file)
            except blocks.BlockError:
                self.doc = blocks.new_document(cfg.campaign.name)
            self.current = 0 if self.doc.get("blocks") else -1

        self.setWindowTitle("Template builder")
        self.resize(1180, 780)
        self.columns: list[str] = []
        self._build()
        self._load_contacts()
        self._populate_placeholders()
        self._refresh_list()
        self._rebuild_editor()
        self._schedule_preview()

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        self.banner = QLabel(
            "Raw HTML template — switch to Blocks to edit visually. "
            "This view is read-only; edit the .html/.txt pair on disk."
        )
        self.banner.setObjectName("DryBanner")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)
        self.raw_switch = QPushButton("Switch to Blocks…")
        self.raw_switch.clicked.connect(self._switch_to_blocks)
        if self.raw:
            row = QHBoxLayout()
            row.addWidget(self.banner, 1)
            row.addWidget(self.raw_switch)
            layout.removeWidget(self.banner)
            layout.addLayout(row)
        else:
            self.banner.hide()
            self.raw_switch.hide()

        top = QFrame()
        top.setObjectName("Card")
        form = QFormLayout(top)
        self.name_edit = QLineEdit(self.doc.get("name", "") if self.doc else self.cfg.campaign.name)
        self.subject_edit = QLineEdit(self.doc.get("subject", "") if self.doc else self.cfg.template.subject)
        self.preheader_edit = QLineEdit(self.doc.get("preheader", "") if self.doc else "")
        self.subject_edit.textChanged.connect(self._meta_changed)
        self.preheader_edit.textChanged.connect(self._meta_changed)
        form.addRow("Template name", self.name_edit)
        form.addRow("Subject", self.subject_edit)
        form.addRow("Preheader", self.preheader_edit)
        layout.addWidget(top)

        splitter = QSplitter(Qt.Horizontal)
        # left: blocks
        left = QWidget()
        left.setMinimumWidth(220)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        blocks_label = QLabel("Blocks")
        blocks_label.setObjectName("Section")
        left_layout.addWidget(blocks_label)
        self.block_list = QListWidget()
        self.block_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.block_list.setTextElideMode(Qt.ElideRight)
        self.block_list.currentRowChanged.connect(self._row_changed)
        left_layout.addWidget(self.block_list, 1)
        tools = QHBoxLayout()
        for text, slot, tip in (("▲", lambda: self._move(-1), "Move up"),
                                ("▼", lambda: self._move(1), "Move down"),
                                ("Delete", self._delete_block, "Delete block")):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            tools.addWidget(btn)
        left_layout.addLayout(tools)
        self.add_btn = QToolButton()
        self.add_btn.setText("+ Add block")
        self.add_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.add_btn)
        for kind, label in (("heading", "Heading"), ("paragraph", "Paragraph"),
                            ("image", "Image"), ("button", "Button"),
                            ("divider", "Divider"), ("spacer", "Spacer")):
            action = menu.addAction(label)
            action.triggered.connect(lambda checked=False, k=kind: self._add_block(k))
        self.add_btn.setMenu(menu)
        left_layout.addWidget(self.add_btn)
        splitter.addWidget(left)

        # middle: fields
        middle = QWidget()
        mid_layout = QVBoxLayout(middle)
        mid_layout.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        settings_label = QLabel("Block settings")
        settings_label.setObjectName("Section")
        head.addWidget(settings_label)
        head.addStretch(1)
        head.addWidget(QLabel("Insert placeholder"))
        self.placeholder_combo = QComboBox()
        self.placeholder_combo.addItem("Choose…")
        self.placeholder_combo.activated.connect(self._insert_placeholder)
        head.addWidget(self.placeholder_combo)
        mid_layout.addLayout(head)
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor_scroll.viewport().setAutoFillBackground(False)
        mid_layout.addWidget(self.editor_scroll, 1)
        splitter.addWidget(middle)

        # right: preview
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        head2 = QHBoxLayout()
        preview_label = QLabel("Approximate preview")
        preview_label.setObjectName("Section")
        head2.addWidget(preview_label)
        head2.addStretch(1)
        self.sample_combo = QComboBox()
        self.sample_combo.currentIndexChanged.connect(lambda _: self._schedule_preview())
        head2.addWidget(self.sample_combo)
        right_layout.addLayout(head2)
        self.preview = QTextBrowser()
        self.preview.setHtml(
            "<p style='color:#98a2b3;font-family:Segoe UI;font-size:13px;'>"
            "Your preview appears here.</p>"
        )
        right_layout.addWidget(self.preview, 1)
        hint = QLabel("QTextBrowser ignores some email CSS. Check the exact render in a browser.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        right_layout.addWidget(hint)
        brow = QHBoxLayout()
        open_btn = QPushButton("Open exact render in browser")
        open_btn.clicked.connect(self._open_in_browser)
        brow.addWidget(open_btn)
        check_btn = QPushButton("Check image")
        check_btn.clicked.connect(self._check_image)
        brow.addWidget(check_btn)
        right_layout.addLayout(brow)
        splitter.addWidget(right)
        splitter.setSizes([240, 380, 520])
        layout.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        new_btn = QPushButton("New…")
        new_btn.clicked.connect(self._new_template)
        validate_btn = QPushButton("Validate")
        validate_btn.clicked.connect(lambda: self._validate(show=True))
        test_btn = QPushButton("Send test to myself")
        test_btn.clicked.connect(self._send_test)
        save_btn = QPushButton("Save")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self._save)
        save_as_btn = QPushButton("Save as…")
        save_as_btn.clicked.connect(self._save_as)
        dup_btn = QPushButton("Duplicate")
        dup_btn.clicked.connect(self._duplicate)
        del_btn = QPushButton("Delete")
        del_btn.setObjectName("Danger")
        del_btn.clicked.connect(self._delete_template)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(new_btn)
        bottom.addWidget(validate_btn)
        bottom.addStretch(1)
        bottom.addWidget(test_btn)
        bottom.addSpacing(10)
        bottom.addWidget(save_btn)
        bottom.addWidget(save_as_btn)
        bottom.addSpacing(10)
        bottom.addWidget(dup_btn)
        bottom.addWidget(del_btn)
        bottom.addSpacing(10)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        if self.raw:
            self._set_editing_enabled(False)

    def _set_editing_enabled(self, enabled: bool) -> None:
        for w in (self.name_edit, self.subject_edit, self.preheader_edit,
                  self.block_list, self.add_btn):
            w.setEnabled(enabled)

    # ------------------------------------------------------------- data
    def _load_contacts(self) -> None:
        try:
            data = read_sheet(self.cfg.sheet_file, self.cfg.campaign.sheet_tab,
                              self.cfg.campaign.date_order)
            self.contacts = data.contacts
            self.columns = [h for h in data.headers if h]
        except Exception:
            self.contacts = []
        self.sample_combo.addItem("Sample data — John Smith / Acme Ltd")
        for c in self.contacts[:200]:
            self.sample_combo.addItem(f"Row {c.sheet_row} — {c.full_name or c.email or '(blank)'}")

    def _populate_placeholders(self) -> None:
        self.placeholder_combo.clear()
        self.placeholder_combo.addItem("Choose…")
        for name in self.columns + [b for b in BUILTIN_PLACEHOLDERS if b not in self.columns]:
            self.placeholder_combo.addItem("{" + name + "}")

    def _values(self) -> dict:
        index = self.sample_combo.currentIndex()
        if index <= 0 or not self.contacts:
            return {
                "Email": "john@example.com", "FirstName": "John", "LastName": "Smith",
                "Company": "Acme Ltd",
            }
        return dict(self.contacts[min(index - 1, len(self.contacts) - 1)].as_template_values())

    # ----------------------------------------------------------- blocks
    def _label(self, block: dict) -> str:
        btype = block.get("type", "?")
        text = str(block.get("text") or block.get("url") or "").replace("\n", " ")
        return f"{btype}: {text[:46]}" if text else str(btype)

    def _refresh_list(self) -> None:
        self._loading = True
        self.block_list.clear()
        for block in (self.doc or {}).get("blocks", []):
            self.block_list.addItem(QListWidgetItem(self._label(block)))
        if self.block_list.count():
            self.current = max(0, min(self.current, self.block_list.count() - 1))
            self.block_list.setCurrentRow(self.current)
        else:
            self.current = -1
        self._loading = False
        self._populate_placeholders()
        self._refresh_add_state()

    def _refresh_add_state(self) -> None:
        self.add_btn.setEnabled(not self.raw)

    def _row_changed(self, row: int) -> None:
        if self._loading:
            return
        self.current = row
        self._rebuild_editor()
        self._schedule_preview()

    def _rebuild_editor(self) -> None:
        old = self.editor_scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        holder = QWidget()
        layout = QFormLayout(holder)
        block = self._block()
        if block is None:
            layout.addRow(QLabel("Select a block, or add one."))
        else:
            btype = block.get("type")
            if btype in ("heading", "paragraph"):
                edit = QPlainTextEdit(str(block.get("text", "")))
                edit.setMinimumHeight(120)
                edit.installEventFilter(self)
                edit.textChanged.connect(lambda w=edit: self._commit("text", w.toPlainText()))
                layout.addRow("Text", edit)
                if btype == "heading":
                    level = QComboBox()
                    level.addItems(["1", "2", "3"])
                    level.setCurrentText(str(block.get("level", 2)))
                    level.currentTextChanged.connect(lambda v: self._commit("level", int(v)))
                    layout.addRow("Level", level)
                tip = QLabel("Markdown subset: **bold**, *italic*, [label](https://url). "
                             "Everything else is escaped.")
                tip.setObjectName("Muted")
                tip.setWordWrap(True)
                layout.addRow("", tip)
            elif btype == "image":
                for key, label in (("url", "Image URL"), ("alt", "Alt text"),
                                   ("link", "Click-through URL (optional)")):
                    edit = QLineEdit(str(block.get(key) or ""))
                    edit.installEventFilter(self)
                    edit.textChanged.connect(lambda v, k=key: self._commit(k, v.strip() or None))
                    layout.addRow(label, edit)
                width = QSpinBox()
                width.setRange(200, 900)
                width.setValue(int(block.get("width") or 600))
                width.valueChanged.connect(lambda v: self._commit("width", int(v)))
                layout.addRow("Width (px)", width)
                align = QComboBox()
                align.addItems(["center", "left", "right"])
                align.setCurrentText(str(block.get("align") or "center"))
                align.currentTextChanged.connect(lambda v: self._commit("align", v))
                layout.addRow("Alignment", align)
            elif btype == "button":
                for key, label in (("text", "Label"), ("url", "URL")):
                    edit = QLineEdit(str(block.get(key) or ""))
                    edit.installEventFilter(self)
                    edit.textChanged.connect(lambda v, k=key: self._commit(k, v))
                    layout.addRow(label, edit)
            elif btype == "spacer":
                height = QSpinBox()
                height.setRange(4, 200)
                height.setValue(int(block.get("height") or 24))
                height.valueChanged.connect(lambda v: self._commit("height", int(v)))
                layout.addRow("Height (px)", height)
            else:
                layout.addRow(QLabel("This block has no options."))
        layout.addItem(_vertical_spacer())
        self.editor_scroll.setWidget(holder)

    def _block(self) -> dict | None:
        if not self.doc or not (0 <= self.current < len(self.doc.get("blocks", []))):
            return None
        return self.doc["blocks"][self.current]

    def _commit(self, key: str, value) -> None:
        block = self._block()
        if block is None or self._loading:
            return
        block[key] = value
        item = self.block_list.item(self.current)
        if item is not None:
            item.setText(self._label(block))
        self._schedule_preview()

    def _meta_changed(self) -> None:
        if self._loading or not self.doc:
            return
        self.doc["subject"] = self.subject_edit.text()
        self.doc["preheader"] = self.preheader_edit.text()
        self.doc["name"] = self.name_edit.text()
        self._schedule_preview()

    def _add_block(self, kind: str) -> None:
        if not self.doc or self.raw:
            return
        defaults = {
            "heading": {"type": "heading", "text": "Your headline", "level": 2},
            "paragraph": {"type": "paragraph", "text": "Write something useful."},
            "image": {"type": "image", "url": self.cfg.template.image_url or
                      "https://samlax.net/img/hero.jpg",
                      "alt": "", "width": 600, "link": None, "align": "center"},
            "button": {"type": "button", "text": "Learn more",
                       "url": "https://samlax.net/"},
            "divider": {"type": "divider"},
            "spacer": {"type": "spacer", "height": 24},
        }
        self.doc["blocks"].append(defaults[kind])
        self.current = len(self.doc["blocks"]) - 1
        self._refresh_list()
        self._rebuild_editor()
        self._schedule_preview()

    def _move(self, delta: int) -> None:
        if not self.doc or self.current < 0:
            return
        target = self.current + delta
        items = self.doc["blocks"]
        if not (0 <= target < len(items)):
            return
        items[self.current], items[target] = items[target], items[self.current]
        self.current = target
        self._refresh_list()
        self._rebuild_editor()
        self._schedule_preview()

    def _delete_block(self) -> None:
        if not self.doc or self.current < 0:
            return
        del self.doc["blocks"][self.current]
        self.current = min(self.current, len(self.doc["blocks"]) - 1)
        self._refresh_list()
        self._rebuild_editor()
        self._schedule_preview()

    # ---------------------------------------------------------- preview
    def _schedule_preview(self) -> None:
        QTimer.singleShot(180, self._render_preview)

    def _render_preview(self) -> None:
        values = compose.template_values(self.cfg, self._values())
        try:
            if self.raw:
                html = compose.render_html(self.cfg, values)
            else:
                if not self.doc:
                    return
                html, _ = compose.render_blocks_document(self.cfg, self.doc, values)
            self.preview.setHtml(html)
        except Exception as exc:
            self.preview.setPlainText(f"Preview error: {type(exc).__name__}: {exc}")

    def _open_in_browser(self) -> None:
        values = compose.template_values(self.cfg, self._values())
        try:
            if self.raw:
                html = compose.render_html(self.cfg, values)
            else:
                html, _ = compose.render_blocks_document(self.cfg, self.doc, values)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot render", str(exc))
            return
        path = Path(tempfile.gettempdir()) / "dispatch-preview.html"
        path.write_text(html, encoding="utf-8")
        webbrowser.open(path.as_uri())

    def _check_image(self) -> None:
        url = self.cfg.template.image_url or ""
        if self.doc and not self.raw:
            url = next((str(b.get("url")) for b in self.doc.get("blocks", [])
                        if b.get("type") == "image" and b.get("url")), url)
        if not url:
            QMessageBox.information(self, "No image", "Add an image block first.")
            return
        if not url.startswith("https://"):
            QMessageBox.warning(self, "Not https", "Image URLs must start with https://")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                ctype = response.headers.get("Content-Type", "")
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Image check failed", f"{url}\n\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        ok = status == 200 and ctype.startswith("image/")
        QMessageBox.information(
            self, "Image check",
            f"{url}\n\nHTTP {status}\nContent-Type: {ctype or '(missing)'}\n\n"
            + ("Looks good." if ok else "Needs a 200 response with an image/* content type."),
        )

    # ------------------------------------------------------------- meta
    def _headers(self) -> list[str]:
        return self.columns + BUILTIN_PLACEHOLDERS

    def _validate(self, show: bool = True) -> bool:
        if self.raw:
            errors, warnings = [], []
            if not self.cfg.html_file.exists():
                errors.append(f"HTML file missing: {self.cfg.html_file}")
            if not self.cfg.template.subject.strip():
                errors.append("Subject is empty.")
        else:
            if not self.doc:
                return False
            self.doc["subject"] = self.subject_edit.text()
            self.doc["preheader"] = self.preheader_edit.text()
            self.doc["name"] = self.name_edit.text()
            errors, warnings = blocks.validate_document(self.doc, self._headers())
        if show:
            if errors or warnings:
                text = ""
                if errors:
                    text += "Errors (block Save and Send test):\n• " + "\n• ".join(errors)
                if warnings:
                    text += ("\n\n" if text else "") + "Warnings:\n• " + "\n• ".join(warnings)
                (QMessageBox.warning if errors else QMessageBox.information)(
                    self, "Validation", text)
            else:
                QMessageBox.information(self, "Validation", "No problems found.")
        return not errors

    def _apply_to_config(self, base: Path) -> None:
        assert self.doc is not None
        self.cfg.template.kind = "blocks"
        self.cfg.template.blocks_path = str(base.with_suffix(".json"))
        self.cfg.template.html_path = str(base.with_suffix(".html"))
        self.cfg.template.text_path = str(base.with_suffix(".txt"))
        self.cfg.template.subject = str(self.doc.get("subject") or self.cfg.template.subject)
        if self.doc.get("image_url"):
            self.cfg.template.image_url = str(self.doc["image_url"])

    def _write_files(self, base: Path) -> None:
        assert self.doc is not None
        blocks.save_document(self.doc, base.with_suffix(".json"))
        base.with_suffix(".html").write_text(blocks.render_document_html(self.doc),
                                             encoding="utf-8")
        base.with_suffix(".txt").write_text(blocks.render_document_text(self.doc),
                                            encoding="utf-8")

    def _save(self) -> None:
        if self.raw:
            return
        if not self._validate(show=True):
            return
        base = self.cfg.blocks_file
        try:
            self._write_files(base)
            self._apply_to_config(base)
            if self.cfg_path:
                write_config(self.cfg, self.cfg_path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"{type(exc).__name__}: {exc}")
            return
        self.saved.emit()
        QMessageBox.information(self, "Saved", f"Template saved to\n{base}")

    def _save_as(self) -> None:
        if self.raw or not self.doc:
            return
        if not self._validate(show=True):
            return
        start = str(self.cfg.blocks_file.with_name(
            self.name_edit.text().strip().replace(" ", "_") or "template") )
        path, _ = QFileDialog.getSaveFileName(self, "Save template as", start,
                                              "Block template (*.json)")
        if not path:
            return
        base = Path(path).with_suffix("")
        try:
            self._write_files(base)
            self._apply_to_config(base)
            if self.cfg_path:
                write_config(self.cfg, self.cfg_path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"{type(exc).__name__}: {exc}")
            return
        self.saved.emit()
        QMessageBox.information(self, "Saved", f"Template saved to\n{base.with_suffix('.json')}")

    def _duplicate(self) -> None:
        if self.raw or not self.doc:
            return
        if not self._validate(show=True):
            return
        base = self.cfg.blocks_file
        stem = base.stem
        candidate = base.with_name(stem + "-copy")
        n = 2
        while candidate.with_suffix(".json").exists():
            candidate = base.with_name(f"{stem}-copy{n}")
            n += 1
        self.doc["name"] = (self.doc.get("name") or stem) + " (copy)"
        self.name_edit.setText(self.doc["name"])
        try:
            self._write_files(candidate)
            self._apply_to_config(candidate)
            if self.cfg_path:
                write_config(self.cfg, self.cfg_path)
        except Exception as exc:
            QMessageBox.critical(self, "Duplicate failed", str(exc))
            return
        self.saved.emit()
        QMessageBox.information(self, "Duplicated", f"Created {candidate.with_suffix('.json')}")

    def _delete_template(self) -> None:
        if self.raw or not self.doc:
            return
        if QMessageBox.question(self, "Delete template?",
                                f"Delete {self.cfg.blocks_file.name} and its .html/.txt?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        for path in (self.cfg.blocks_file, self.cfg.html_file, self.cfg.text_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self.saved.emit()
        self.accept()

    def _new_template(self) -> None:
        choice, ok = QInputDialog.getItem(
            self, "New template", "Start from:",
            ["Personal note (recommended)", "Promo card", "Announcement"], 0, False)
        if not ok:
            return
        key = {"Personal note (recommended)": "personal_note",
               "Promo card": "promo_card",
               "Announcement": "announcement"}[choice]
        try:
            self.doc = blocks.load_document(STARTERS / f"{key}.json")
        except blocks.BlockError as exc:
            QMessageBox.warning(self, "Cannot load starter", str(exc))
            return
        self.raw = False
        self.current = 0
        self.banner.hide()
        self.raw_switch.hide()
        self.name_edit.setText(self.doc.get("name", "New template"))
        self.subject_edit.setText(self.doc.get("subject", ""))
        self.preheader_edit.setText(self.doc.get("preheader", ""))
        self._set_editing_enabled(True)
        self._refresh_list()
        self._rebuild_editor()
        self._schedule_preview()

    def _switch_to_blocks(self) -> None:
        if QMessageBox.question(
            self, "Switch to Blocks?",
            "Arbitrary HTML cannot be converted into blocks. A new block document will be "
            "created; your existing .html/.txt files stay untouched until you save.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.raw = False
        self.doc = blocks.new_document(self.cfg.campaign.name)
        self.current = 0
        self.banner.hide()
        self.raw_switch.hide()
        self.name_edit.setText(self.doc["name"])
        self.subject_edit.setText(self.cfg.template.subject)
        self._set_editing_enabled(True)
        self._refresh_list()
        self._rebuild_editor()
        self._schedule_preview()

    # --------------------------------------------------------- send test
    def _send_test(self) -> None:
        if not self._validate(show=True):
            return
        values = compose.template_values(self.cfg, self._values())
        try:
            if self.raw:
                html = compose.render_html(self.cfg, values)
                text = compose.render_text(self.cfg, values)
                subject = compose.render_subject(self.cfg, values)
            else:
                html, text = compose.render_blocks_document(self.cfg, self.doc, values)
                subject = compose.render_subject(self.cfg, values, self.doc.get("subject"))
            message = compose.build_message(
                self.cfg, values, to=self.cfg.sender.from_email,
                subject=f"[TEST] {subject}", html_body=html, text_body=text,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Cannot render", f"{type(exc).__name__}: {exc}")
            return
        try:
            password = resolve_secret(self.cfg.sender.secret_ref)
        except SecretNotFound as exc:
            QMessageBox.warning(self, "No password", str(exc))
            return
        self._test_thread = SendTestThread(self.cfg, message, password, self)
        self._test_thread.done.connect(
            lambda code: QMessageBox.information(
                self, "Test sent", f"Accepted by {self.cfg.sender.smtp.host} ({code}).\n"
                                   f"Check {self.cfg.sender.from_email}."))
        self._test_thread.failed.connect(
            lambda msg: QMessageBox.warning(self, "Test failed", msg))
        self._test_thread.start()

    # -------------------------------------------------------- placeholders
    def _insert_placeholder(self, index: int) -> None:
        if index <= 0:
            return
        token = self.placeholder_combo.itemText(index)
        widget = self._focus_widget
        if widget is None:
            widget = self.subject_edit
        if isinstance(widget, QPlainTextEdit):
            widget.insertPlainText(token)
        elif isinstance(widget, QLineEdit):
            widget.insert(token)
        widget.setFocus()

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.FocusIn:
            self._focus_widget = obj
        return super().eventFilter(obj, event)


def _vertical_spacer():
    from PySide6.QtWidgets import QSpacerItem, QSizePolicy
    return QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
