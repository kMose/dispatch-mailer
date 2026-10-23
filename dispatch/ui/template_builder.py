"""Block-based template builder (plus read-only mode for hand-written HTML).

Deliberately simple: a vertical list of blocks, a field editor, and an approximate
preview. Not a drag-and-drop designer, not a canvas.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTextBrowser, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from ..config import FullConfig, write_config
from ..mail import blocks, compose
from ..mail.secrets import SecretNotFound, resolve_secret
from ..mail.sender import SMTPFailure, SMTPTransport
from ..sheet.reader import read_sheet

STARTERS = Path(__file__).resolve().parent.parent / "templates" / "starters"
BUILTIN_PLACEHOLDERS = ["FirstName", "LastName", "Email", "Company",
                        "UnsubscribeURL", "PostalAddress"]


class BlockList(QListWidget):
    """List that reports internal drag-and-drop moves back to the dialog."""

    reordered = Signal()

    def dropEvent(self, event):  # noqa: N802
        super().dropEvent(event)
        self.reordered.emit()


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
        self._text_edit: QPlainTextEdit | None = None
        self._undo_stack: list[str] = []
        self._redo_stack: list[str] = []
        self._snap_timer = QTimer(self)
        self._snap_timer.setSingleShot(True)
        self._snap_timer.setInterval(600)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(30_000)
        self._autosave_timer.timeout.connect(self._autosave)
        self._preview_width = 600

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
        self._saved_json = self._snapshot_json()
        self._setup_shortcuts()
        self._refresh_template_combo()
        self._update_meta_hints()
        self._autosave_timer.start()
        self._maybe_restore_autosave()

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
        self.subject_hint = QLabel("")
        self.subject_hint.setObjectName("Muted")
        self.subject_hint.setWordWrap(True)
        form.addRow("", self.subject_hint)
        form.addRow("Preheader", self.preheader_edit)
        self.preheader_hint = QLabel("")
        self.preheader_hint.setObjectName("Muted")
        self.preheader_hint.setWordWrap(True)
        form.addRow("", self.preheader_hint)
        layout.addWidget(top)

        library = QHBoxLayout()
        library.addWidget(QLabel("Template"))
        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(180)
        self.template_combo.activated.connect(self._library_selected)
        library.addWidget(self.template_combo, 1)
        rename_btn = QPushButton("Rename…")
        rename_btn.clicked.connect(self._rename_template)
        library.addWidget(rename_btn)
        history_btn = QPushButton("History…")
        history_btn.clicked.connect(self._show_history)
        library.addWidget(history_btn)
        layout.addLayout(library)

        splitter = QSplitter(Qt.Horizontal)
        # left: blocks
        left = QWidget()
        left.setMinimumWidth(220)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        blocks_label = QLabel("Blocks")
        blocks_label.setObjectName("Section")
        left_layout.addWidget(blocks_label)
        self.block_list = BlockList()
        self.block_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.block_list.setTextElideMode(Qt.ElideRight)
        self.block_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.block_list.setDefaultDropAction(Qt.MoveAction)
        self.block_list.currentRowChanged.connect(self._row_changed)
        self.block_list.reordered.connect(self._reorder_from_list)
        self.block_list.installEventFilter(self)
        left_layout.addWidget(self.block_list, 1)
        tools = QHBoxLayout()
        for text, slot, tip in (("▲", lambda: self._move(-1), "Move up"),
                                ("▼", lambda: self._move(1), "Move down"),
                                ("Copy", self._duplicate_block, "Duplicate block"),
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
                            ("list", "Bulleted / numbered list"),
                            ("image", "Image"), ("button", "Button"),
                            ("signature", "Signature"),
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
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Format"))
        for text, kind, tip in (("B", "bold", "Bold (Ctrl+B)"),
                                ("I", "italic", "Italic (Ctrl+I)"),
                                ("U", "underline", "Underline (Ctrl+U)"),
                                ("Link", "link", "Insert link (Ctrl+K)")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setFixedWidth(46 if kind != "link" else 56)
            button.clicked.connect(lambda checked=False, k=kind: self._format(k))
            fmt_row.addWidget(button)
        fmt_row.addStretch(1)
        mid_layout.addLayout(fmt_row)
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
        self.desktop_btn = QPushButton("Desktop")
        self.desktop_btn.setCheckable(True)
        self.desktop_btn.setChecked(True)
        self.desktop_btn.clicked.connect(lambda: self._set_preview_width(600))
        self.mobile_btn = QPushButton("Mobile")
        self.mobile_btn.setCheckable(True)
        self.mobile_btn.clicked.connect(lambda: self._set_preview_width(375))
        head2.addWidget(self.desktop_btn)
        head2.addWidget(self.mobile_btn)
        self.sample_combo = QComboBox()
        self.sample_combo.currentIndexChanged.connect(self._sample_changed)
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
        open_btn = QPushButton("Open in browser")
        open_btn.clicked.connect(self._open_in_browser)
        brow.addWidget(open_btn)
        html_btn = QPushButton("HTML / .eml…")
        html_btn.clicked.connect(self._show_html)
        brow.addWidget(html_btn)
        check_btn = QPushButton("Check image")
        check_btn.clicked.connect(self._check_image)
        brow.addWidget(check_btn)
        footer_btn = QPushButton("Footer…")
        footer_btn.clicked.connect(self._edit_footer)
        brow.addWidget(footer_btn)
        right_layout.addLayout(brow)
        splitter.addWidget(right)
        splitter.setSizes([240, 380, 520])
        layout.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        new_btn = QPushButton("New…")
        new_btn.clicked.connect(self._new_template)
        open_btn = QPushButton("Open…")
        open_btn.clicked.connect(self._open_template)
        validate_btn = QPushButton("Validate")
        validate_btn.clicked.connect(lambda: self._validate(show=True))
        test_btn = QPushButton("Send test…")
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
        bottom.addWidget(open_btn)
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

    # ------------------------------------------------------- undo / dirty
    def _snapshot_json(self) -> str:
        return (json.dumps(self.doc, sort_keys=True, ensure_ascii=False)
                if self.doc else "")

    def _push_undo(self, immediate: bool = False) -> None:
        if self.raw or not self.doc:
            return
        if immediate:
            self._undo_stack.append(self._snapshot_json())
            if len(self._undo_stack) > 80:
                del self._undo_stack[:-80]
            self._redo_stack.clear()
            self._snap_timer.stop()  # next text edit starts its own undo step
            return
        if not self._snap_timer.isActive():
            self._undo_stack.append(self._snapshot_json())
            if len(self._undo_stack) > 80:
                del self._undo_stack[:-80]
            self._redo_stack.clear()
        self._snap_timer.start()

    def _restore_snapshot(self, source: list[str], target: list[str]) -> None:
        if not source or not self.doc:
            return
        target.append(self._snapshot_json())
        self.doc = json.loads(source.pop())
        self._loading = True
        self.name_edit.setText(self.doc.get("name", ""))
        self.subject_edit.setText(self.doc.get("subject", ""))
        self.preheader_edit.setText(self.doc.get("preheader", ""))
        self._loading = False
        self.current = min(max(self.current, 0),
                           max(0, len(self.doc.get("blocks", [])) - 1))
        self._refresh_list()
        self._rebuild_editor()
        self._mark_dirty()
        self._schedule_preview()

    def _undo(self) -> None:
        self._restore_snapshot(self._undo_stack, self._redo_stack)

    def _redo(self) -> None:
        self._restore_snapshot(self._redo_stack, self._undo_stack)

    def _is_dirty(self) -> bool:
        return bool(self.doc) and self._snapshot_json() != self._saved_json

    def _mark_dirty(self) -> None:
        suffix = " *" if self._is_dirty() else ""
        self.setWindowTitle(f"Template builder — {self.cfg.campaign.name}{suffix}")
        self._update_meta_hints()

    def _confirm_discard(self) -> bool:
        if not self._is_dirty():
            return True
        choice = QMessageBox.question(
            self, "Unsaved changes", "Save changes to this template before leaving?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Save:
            return self._save()
        return True

    def reject(self) -> None:  # noqa: D401
        if not self._confirm_discard():
            return
        self._autosave_timer.stop()
        super().reject()

    def closeEvent(self, event):  # noqa: N802
        if not self._confirm_discard():
            event.ignore()
            return
        self._autosave_timer.stop()
        event.accept()

    def _setup_shortcuts(self) -> None:
        bindings = (
            (QKeySequence.Save, self._save),
            (QKeySequence("Ctrl+Shift+S"), self._save_as),
            (QKeySequence.Open, self._open_template),
            (QKeySequence.Undo, self._undo),
            (QKeySequence.Redo, self._redo),
            (QKeySequence("Ctrl+Shift+Z"), self._redo),
            (QKeySequence("Ctrl+D"), self._duplicate_block),
            (QKeySequence("Alt+Up"), lambda: self._move(-1)),
            (QKeySequence("Alt+Down"), lambda: self._move(1)),
            (QKeySequence("Ctrl+B"), lambda: self._format("bold")),
            (QKeySequence("Ctrl+I"), lambda: self._format("italic")),
            (QKeySequence("Ctrl+U"), lambda: self._format("underline")),
            (QKeySequence("Ctrl+K"), lambda: self._format("link")),
        )
        for sequence, slot in bindings:
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

    def _format(self, kind: str) -> None:
        widget = self._focus_widget if isinstance(self._focus_widget, QPlainTextEdit) \
            else self._text_edit
        if widget is None:
            return
        cursor = widget.textCursor()
        selected = cursor.selectedText()
        if kind == "link":
            label = selected or "link text"
            insert = f"[{label}](https://)"
            offset, length = 1, len(label)
        else:
            marker = {"bold": "**", "italic": "*", "underline": "__"}[kind]
            text = selected or "text"
            insert = f"{marker}{text}{marker}"
            offset, length = len(marker), len(text)
        cursor.insertText(insert)
        start = cursor.position() - (len(insert) - offset)
        cursor.setPosition(start)
        cursor.setPosition(start + length, cursor.MoveMode.KeepAnchor)
        widget.setTextCursor(cursor)
        widget.setFocus()

    # ------------------------------------------------------ preview / meta
    def _set_preview_width(self, width: int) -> None:
        self._preview_width = width
        self.desktop_btn.setChecked(width == 600)
        self.mobile_btn.setChecked(width == 375)
        self.preview.setFixedWidth(width)
        self._render_preview()

    def _update_meta_hints(self) -> None:
        if not hasattr(self, "subject_hint") or self.raw:
            return
        values = compose.template_values(self.cfg, self._values())
        subject = self.subject_edit.text()
        try:
            rendered = compose.render_subject(self.cfg, values, subject)
        except Exception:
            rendered = subject
        count = len(rendered)
        self.subject_hint.setText(
            f"{count}/60 characters · preview: “{rendered}”"
            + (" — over 78 will fail validation" if count > 78 else
               " — may truncate on phones" if count > 60 else ""))
        self.subject_hint.setStyleSheet("color:#b42318;" if count > 78 else "")
        pre = self.preheader_edit.text()
        try:
            pre_rendered = compose.render_subject(self.cfg, values, pre) if pre else ""
        except Exception:
            pre_rendered = pre
        self.preheader_hint.setText(
            f"{len(pre_rendered)}/90 characters · shown after the subject"
            + (" — preview: “" + pre_rendered + "”" if pre_rendered else ""))

    def _reorder_from_list(self) -> None:
        if not self.doc or self.raw:
            return
        order = [self.block_list.item(i).data(Qt.UserRole)
                 for i in range(self.block_list.count())]
        old = self.doc.get("blocks", [])
        if sorted(x for x in order if isinstance(x, int)) != list(range(len(old))):
            self._refresh_list()
            return
        self._push_undo(immediate=True)
        self.doc["blocks"] = [old[i] for i in order]
        self.current = self.block_list.currentRow()
        self._refresh_list()
        self._rebuild_editor()
        self._mark_dirty()
        self._schedule_preview()

    def _duplicate_block(self) -> None:
        if not self.doc or self.raw or self.current < 0:
            return
        self._push_undo(immediate=True)
        copy = json.loads(json.dumps(self.doc["blocks"][self.current]))
        self.doc["blocks"].insert(self.current + 1, copy)
        self.current += 1
        self._refresh_list()
        self._rebuild_editor()
        self._mark_dirty()
        self._schedule_preview()

    # --------------------------------------------------- autosave / versions
    def _autosave_path(self) -> Path | None:
        if self.raw or not self.cfg.template.blocks_path:
            return None
        base = self.cfg.blocks_file
        return base.parent / ".autosave" / f"{base.stem}.json"

    def _autosave(self) -> None:
        path = self._autosave_path()
        if path is None or not self._is_dirty():
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._snapshot_json(), encoding="utf-8")
        except OSError:
            pass

    def _maybe_restore_autosave(self) -> None:
        path = self._autosave_path()
        if path is None or not path.exists():
            return
        base = self.cfg.blocks_file
        if base.exists() and path.stat().st_mtime <= base.stat().st_mtime:
            path.unlink(missing_ok=True)
            return
        if QMessageBox.question(
            self, "Recover unsaved draft?",
            f"An autosaved draft of {base.stem} is newer than the saved template.\n\n"
            "Restore it?", QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        ) != QMessageBox.Yes:
            return
        try:
            doc = blocks.load_document(path)
        except blocks.BlockError:
            return
        self.doc = doc
        self._loading = True
        self.name_edit.setText(doc.get("name", ""))
        self.subject_edit.setText(doc.get("subject", ""))
        self.preheader_edit.setText(doc.get("preheader", ""))
        self._loading = False
        self.current = 0
        self._refresh_list()
        self._rebuild_editor()
        self._mark_dirty()
        self._schedule_preview()

    def _history_dir(self) -> Path:
        base = self.cfg.blocks_file
        return base.parent / ".history" / base.stem

    def _archive_version(self, base: Path) -> None:
        source = base.with_suffix(".json")
        if not source.exists():
            return
        folder = base.parent / ".history" / base.stem
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = folder / f"{stamp}.json"
        counter = 2
        while target.exists():
            target = folder / f"{stamp}-{counter}.json"
            counter += 1
        shutil.copy2(source, target)
        for old in sorted(folder.glob("*.json"))[:-5]:
            old.unlink(missing_ok=True)

    def _show_history(self) -> None:
        if self.raw:
            return
        folder = self._history_dir()
        versions = sorted(folder.glob("*.json"), reverse=True) if folder.exists() else []
        if not versions:
            QMessageBox.information(self, "History", "No earlier versions yet. "
                                                      "Each Save keeps the last five.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"History — {self.cfg.blocks_file.stem}")
        dialog.resize(460, 340)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        for path in versions:
            item = QListWidgetItem(path.stem)
            item.setData(Qt.UserRole, str(path))
            listing.addItem(item)
        listing.setCurrentRow(0)
        layout.addWidget(listing, 1)

        def restore() -> None:
            item = listing.currentItem()
            if item is None:
                return
            try:
                doc = blocks.load_document(item.data(Qt.UserRole))
            except blocks.BlockError as exc:
                QMessageBox.warning(dialog, "Cannot restore", str(exc))
                return
            self.doc = doc
            self._loading = True
            self.name_edit.setText(doc.get("name", ""))
            self.subject_edit.setText(doc.get("subject", ""))
            self.preheader_edit.setText(doc.get("preheader", ""))
            self._loading = False
            self.current = 0
            self._refresh_list()
            self._rebuild_editor()
            self._mark_dirty()
            self._schedule_preview()
            dialog.accept()

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        restore_btn = buttons.addButton("Restore", QDialogButtonBox.AcceptRole)
        restore_btn.clicked.connect(restore)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    # ------------------------------------------------------ template library
    def _refresh_template_combo(self) -> None:
        if self.raw or not hasattr(self, "template_combo"):
            return
        folder = self.cfg.blocks_file.parent
        current = str(self.cfg.blocks_file.resolve()) if self.cfg.template.blocks_path else ""
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for path in sorted(folder.glob("*.json"), key=lambda p: p.stem.lower()):
            self.template_combo.addItem(path.stem, str(path.resolve()))
        index = self.template_combo.findData(current)
        self.template_combo.setCurrentIndex(index if index >= 0 else -1)
        self.template_combo.blockSignals(False)

    def _library_selected(self, index: int) -> None:
        path = self.template_combo.itemData(index)
        if not path:
            return
        if Path(path) == self.cfg.blocks_file.resolve():
            return
        if not self._open_path(Path(path)):
            self._refresh_template_combo()

    def _rename_template(self) -> None:
        if self.raw or not self.doc:
            return
        old = self.cfg.blocks_file
        stem, ok = QInputDialog.getText(self, "Rename template", "New file name:",
                                        text=old.stem)
        if not ok:
            return
        stem = stem.strip()
        if not stem or any(char in stem for char in '\\/:*?"<>|'):
            QMessageBox.warning(self, "Invalid name", "Use letters, numbers, spaces, "
                                                      "dashes or underscores.")
            return
        target = old.with_name(stem)
        if target.with_suffix(".json").exists() and target != old:
            QMessageBox.warning(self, "Name in use", f"{target.name}.json already exists.")
            return
        if target == old:
            return
        try:
            for suffix in (".json", ".html", ".txt"):
                source = old.with_suffix(suffix)
                if source.exists():
                    source.rename(target.with_suffix(suffix))
        except OSError as exc:
            QMessageBox.critical(self, "Rename failed", str(exc))
            return
        self._apply_to_config(target.with_suffix(""))
        if self.cfg_path:
            write_config(self.cfg, self.cfg_path)
        self.doc["name"] = stem
        self.name_edit.setText(stem)
        self._saved_json = self._snapshot_json()
        self._refresh_template_combo()
        self._mark_dirty()
        self.saved.emit()
        QMessageBox.information(self, "Renamed", f"Now {target.stem}.json")

    # ------------------------------------------------------------- footer
    def _edit_footer(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Compliance footer")
        dialog.resize(520, 220)
        form = QFormLayout(dialog)
        postal = QLineEdit(self.cfg.template.footer.postal_address)
        unsub = QLineEdit(self.cfg.template.footer.unsubscribe_url)
        mailto = QLineEdit(self.cfg.template.footer.unsubscribe_mailto)
        form.addRow("Postal address", postal)
        form.addRow("Unsubscribe URL", unsub)
        form.addRow("Unsubscribe mailto", mailto)
        note = QLabel("Appended to every email automatically; it cannot be removed by a "
                      "template.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        form.addRow("", note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        self.cfg.template.footer.postal_address = postal.text().strip()
        self.cfg.template.footer.unsubscribe_url = unsub.text().strip()
        self.cfg.template.footer.unsubscribe_mailto = mailto.text().strip()
        if self.cfg_path:
            write_config(self.cfg, self.cfg_path)
        self._schedule_preview()

    # -------------------------------------------------------- render helpers
    def _render_current(self) -> tuple[dict, str, str, str]:
        values = compose.template_values(self.cfg, self._values())
        if self.raw:
            html = compose.render_html(self.cfg, values)
            text = compose.render_text(self.cfg, values)
            subject = compose.render_subject(self.cfg, values)
            return values, html, text, subject
        if not self.doc:
            raise blocks.BlockError("no template loaded")
        html, text = compose.render_blocks_document(self.cfg, self.doc, values)
        subject = compose.render_subject(self.cfg, values, self.doc.get("subject"))
        return values, html, text, subject

    def _show_html(self) -> None:
        try:
            values, html, text, subject = self._render_current()
        except Exception as exc:
            QMessageBox.warning(self, "Cannot render", f"{type(exc).__name__}: {exc}")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("HTML source and .eml export")
        dialog.resize(900, 680)
        layout = QVBoxLayout(dialog)
        viewer = QTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(html)
        layout.addWidget(viewer, 1)
        row = QHBoxLayout()
        row.addStretch(1)

        def save_eml() -> None:
            path, _ = QFileDialog.getSaveFileName(
                dialog, "Save test email", f"{self.cfg.blocks_file.stem}.eml",
                "Email file (*.eml)")
            if not path:
                return
            try:
                message = compose.build_message(
                    self.cfg, values, to=self.cfg.sender.from_email,
                    subject=f"[TEST] {subject}", html_body=html, text_body=text)
                Path(path).write_bytes(message.as_bytes())
            except Exception as exc:
                QMessageBox.warning(dialog, "Cannot save", str(exc))
                return
            QMessageBox.information(dialog, "Saved", path)

        save_btn = QPushButton("Save .eml…")
        save_btn.clicked.connect(save_eml)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        row.addWidget(save_btn)
        row.addWidget(close_btn)
        layout.addLayout(row)
        dialog.exec()

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
        text = str(block.get("text") or block.get("url") or block.get("name") or "")
        text = text.replace("\n", " ")
        return f"{btype}: {text[:46]}" if text else str(btype)

    def _refresh_list(self) -> None:
        self._loading = True
        self.block_list.clear()
        for index, block in enumerate((self.doc or {}).get("blocks", [])):
            item = QListWidgetItem(self._label(block))
            item.setData(Qt.UserRole, index)
            self.block_list.addItem(item)
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

    def _align_combo(self, layout: QFormLayout, block: dict, options: list[str]) -> None:
        combo = QComboBox()
        combo.addItems(options)
        current = str(block.get("align") or options[0])
        combo.setCurrentText(current if current in options else options[0])
        combo.currentTextChanged.connect(lambda value: self._commit("align", value))
        layout.addRow("Alignment", combo)

    def _color_field(self, layout: QFormLayout, block: dict, key: str,
                     label: str, default: str) -> None:
        row = QHBoxLayout()
        edit = QLineEdit(str(block.get(key) or default))
        edit.installEventFilter(self)
        edit.textChanged.connect(lambda value, k=key: self._commit(k, value.strip()))
        pick = QPushButton("…")
        pick.setFixedWidth(30)
        pick.setToolTip("Choose colour")

        def choose() -> None:
            color = QColorDialog.getColor(QColor(edit.text() or default), self)
            if color.isValid():
                edit.setText(color.name())

        pick.clicked.connect(choose)
        row.addWidget(edit, 1)
        row.addWidget(pick)
        layout.addRow(label, row)

    def _rebuild_editor(self) -> None:
        old = self.editor_scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        holder = QWidget()
        layout = QFormLayout(holder)
        block = self._block()
        self._text_edit = None
        if block is None:
            layout.addRow(QLabel("Select a block, or add one."))
        else:
            btype = block.get("type")
            if btype in ("heading", "paragraph", "list"):
                edit = QPlainTextEdit(str(block.get("text", "")))
                edit.setMinimumHeight(120)
                edit.setUndoRedoEnabled(False)  # document-level undo owns this
                edit.installEventFilter(self)
                edit.textChanged.connect(lambda w=edit: self._commit("text", w.toPlainText()))
                layout.addRow("Text", edit)
                self._text_edit = edit
                if btype == "heading":
                    level = QComboBox()
                    level.addItems(["1", "2", "3"])
                    level.setCurrentText(str(block.get("level", 2)))
                    level.currentTextChanged.connect(lambda v: self._commit("level", int(v)))
                    layout.addRow("Level", level)
                elif btype == "list":
                    style = QComboBox()
                    style.addItems(["Bulleted", "Numbered"])
                    style.setCurrentText("Numbered" if block.get("style") == "number"
                                         else "Bulleted")
                    style.currentTextChanged.connect(
                        lambda value: self._commit(
                            "style", "number" if value == "Numbered" else "bullet"))
                    layout.addRow("Style", style)
                self._align_combo(layout, block,
                                  ["left", "center", "right", "justify"])
                tip = QLabel("Enter starts a new line (kept as a line break). Markdown "
                             "subset: **bold**, *italic*, __underline__, "
                             "[label](https://url).")
                tip.setObjectName("Muted")
                tip.setWordWrap(True)
                layout.addRow("", tip)
            elif btype == "image":
                for key, label in (("url", "Image URL"), ("alt", "Alt text"),
                                   ("caption", "Caption (optional)"),
                                   ("link", "Click-through URL (optional)")):
                    edit = QLineEdit(str(block.get(key) or ""))
                    edit.installEventFilter(self)
                    edit.textChanged.connect(
                        lambda value, k=key: self._commit(k, value.strip() or None))
                    layout.addRow(label, edit)
                width = QSpinBox()
                width.setRange(200, 900)
                width.setValue(int(block.get("width") or 600))
                width.valueChanged.connect(lambda v: self._commit("width", int(v)))
                layout.addRow("Width (px)", width)
                self._align_combo(layout, block, ["center", "left", "right"])
            elif btype == "button":
                for key, label in (("text", "Label"), ("url", "URL")):
                    edit = QLineEdit(str(block.get(key) or ""))
                    edit.installEventFilter(self)
                    edit.textChanged.connect(lambda v, k=key: self._commit(k, v))
                    layout.addRow(label, edit)
                self._align_combo(layout, block, ["center", "left", "right"])
                self._color_field(layout, block, "bg", "Background", "#1a5fb4")
                self._color_field(layout, block, "color", "Text colour", "#ffffff")
            elif btype == "signature":
                for key, label in (("name", "Name"), ("title", "Title"),
                                   ("company", "Company"), ("phone", "Phone"),
                                   ("email", "Email"), ("link", "Website (https://)")):
                    edit = QLineEdit(str(block.get(key) or ""))
                    edit.installEventFilter(self)
                    edit.textChanged.connect(lambda v, k=key: self._commit(k, v))
                    layout.addRow(label, edit)
                self._align_combo(layout, block,
                                  ["left", "center", "right", "justify"])
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
        self._push_undo()
        block[key] = value
        item = self.block_list.item(self.current)
        if item is not None:
            item.setText(self._label(block))
        self._mark_dirty()
        self._schedule_preview()

    def _meta_changed(self) -> None:
        if self._loading or not self.doc:
            return
        self._push_undo()
        self.doc["subject"] = self.subject_edit.text()
        self.doc["preheader"] = self.preheader_edit.text()
        self.doc["name"] = self.name_edit.text()
        self._mark_dirty()
        self._schedule_preview()

    def _add_block(self, kind: str) -> None:
        if not self.doc or self.raw:
            return
        self._push_undo(immediate=True)
        defaults = {
            "heading": {"type": "heading", "text": "Your headline", "level": 2,
                        "align": "left"},
            "paragraph": {"type": "paragraph", "text": "Write something useful.",
                          "align": "left"},
            "list": {"type": "list", "style": "bullet",
                     "text": "First point\nSecond point", "align": "left"},
            "image": {"type": "image", "url": self.cfg.template.image_url or
                      "https://samlax.net/img/hero.jpg",
                      "alt": "", "caption": "", "width": 600, "link": None,
                      "align": "center"},
            "button": {"type": "button", "text": "Learn more",
                       "url": "https://samlax.net/", "align": "center",
                       "bg": "#1a5fb4", "color": "#ffffff"},
            "signature": {"type": "signature", "name": self.cfg.sender.from_name,
                          "title": "", "company": "", "phone": "",
                          "email": self.cfg.sender.from_email, "link": "",
                          "align": "left"},
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
        self._push_undo(immediate=True)
        items[self.current], items[target] = items[target], items[self.current]
        self.current = target
        self._refresh_list()
        self._rebuild_editor()
        self._schedule_preview()

    def _delete_block(self) -> None:
        if not self.doc or self.current < 0:
            return
        self._push_undo(immediate=True)
        del self.doc["blocks"][self.current]
        self.current = min(self.current, len(self.doc["blocks"]) - 1)
        self._refresh_list()
        self._rebuild_editor()
        self._schedule_preview()

    # ---------------------------------------------------------- preview
    def _sample_changed(self, _index: int) -> None:
        self._update_meta_hints()
        self._schedule_preview()

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

    def _save(self) -> bool:
        if self.raw:
            return False
        if not self._validate(show=True):
            return False
        base = self.cfg.blocks_file
        try:
            self._archive_version(base)
            self._write_files(base)
            self._apply_to_config(base)
            if self.cfg_path:
                write_config(self.cfg, self.cfg_path)
            autosave = self._autosave_path()
            if autosave:
                autosave.unlink(missing_ok=True)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"{type(exc).__name__}: {exc}")
            return False
        self._saved_json = self._snapshot_json()
        self._mark_dirty()
        self._refresh_template_combo()
        self.saved.emit()
        QMessageBox.information(self, "Saved", f"Template saved to\n{base}")
        return True

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
            self._archive_version(base)
            self._write_files(base)
            self._apply_to_config(base)
            if self.cfg_path:
                write_config(self.cfg, self.cfg_path)
            autosave = self._autosave_path()
            if autosave:
                autosave.unlink(missing_ok=True)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"{type(exc).__name__}: {exc}")
            return
        self._saved_json = self._snapshot_json()
        self._mark_dirty()
        self._refresh_template_combo()
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
        self._saved_json = self._snapshot_json()
        self._mark_dirty()
        self._refresh_template_combo()
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
        if not self._confirm_discard():
            return
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
        self._mark_dirty()
        self._schedule_preview()

    def _open_template(self) -> None:
        start = str(self.cfg.blocks_file.parent) if self.cfg.template.blocks_path else str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(
            self, "Open template", start, "Block template (*.json)")
        if path:
            self._open_path(Path(path))

    def _open_path(self, path: Path) -> bool:
        """Load a block document and make it the campaign's active template."""
        if not self._confirm_discard():
            return False
        try:
            doc = blocks.load_document(path)
        except blocks.BlockError as exc:
            QMessageBox.warning(self, "Cannot open template", str(exc))
            return False
        self.doc = doc
        self.raw = False
        self.current = 0 if doc.get("blocks") else -1
        self.banner.hide()
        self.raw_switch.hide()
        self.name_edit.setText(doc.get("name", path.stem))
        self.subject_edit.setText(doc.get("subject", ""))
        self.preheader_edit.setText(doc.get("preheader", ""))
        self._set_editing_enabled(True)
        self._refresh_list()
        self._rebuild_editor()
        self._apply_to_config(path.with_suffix(""))
        if self.cfg_path:
            try:
                write_config(self.cfg, self.cfg_path)
            except Exception as exc:
                QMessageBox.warning(self, "Config not updated", str(exc))
        self._saved_json = self._snapshot_json()
        self._mark_dirty()
        self._refresh_template_combo()
        self._schedule_preview()
        self.saved.emit()
        return True

    def _switch_to_blocks(self) -> None:
        if not self._confirm_discard():
            return
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
        self._mark_dirty()
        self._schedule_preview()

    # --------------------------------------------------------- send test
    def _send_test(self) -> None:
        if not self._validate(show=True):
            return
        raw, ok = QInputDialog.getText(
            self, "Send test",
            "Send to (comma-separated). The first address is To, the rest are Bcc:",
            text=self.cfg.sender.from_email)
        if not ok:
            return
        recipients = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
        if not recipients or any("@" not in address or " " in address for address in recipients):
            QMessageBox.warning(self, "Invalid address",
                                "Enter one or more complete email addresses, comma-separated.")
            return
        try:
            values, html, text, subject = self._render_current()
            message = compose.build_message(
                self.cfg, values, to=recipients[0], bcc=recipients[1:],
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
        joined = ", ".join(recipients)
        self._test_thread.done.connect(
            lambda code: QMessageBox.information(
                self, "Test sent", f"Accepted by {self.cfg.sender.smtp.host} ({code}).\n"
                                   f"Check {joined}."))
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
        if (obj is self.block_list and event.type() == QEvent.KeyPress
                and event.key() == Qt.Key_Delete):
            self._delete_block()
            return True
        return super().eventFilter(obj, event)


def _vertical_spacer():
    from PySide6.QtWidgets import QSpacerItem, QSizePolicy
    return QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
