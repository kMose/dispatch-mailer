"""Suppression list and review queue: the two places a human must decide.

Hard bounces are added automatically; a mistaken one can be removed here. Opt-out
replies are queued for approval instead of being suppressed silently.
"""
from __future__ import annotations

import csv

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QInputDialog, QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from .. import db
from ..config import FullConfig
from ..engine.eligibility import is_valid_email, normalize_email


class SuppressionDialog(QDialog):
    def __init__(self, cfg: FullConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.conn = db.connect(cfg.db_path)
        db.init_db(self.conn)
        self.setWindowTitle("Suppression & review")
        self.resize(820, 580)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_suppression_tab(), "Suppressed addresses")
        self.tabs.addTab(self._build_review_tab(), "Needs review")
        layout.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh()

    # ------------------------------------------------------------------ tabs
    def _build_suppression_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Addresses here are never contacted again. Hard bounces are added "
            "automatically. Remove one only if you are sure the address is valid."
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        layout.addWidget(note)

        self.sup_table = QTableWidget(0, 4)
        self.sup_table.setHorizontalHeaderLabels(["Email", "Reason", "Added", "Source"])
        self.sup_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sup_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sup_table.verticalHeader().setVisible(False)
        self.sup_table.horizontalHeader().setStretchLastSection(True)
        self.sup_table.setColumnWidth(0, 260)
        self.sup_table.setColumnWidth(1, 130)
        self.sup_table.setColumnWidth(2, 170)
        layout.addWidget(self.sup_table, 1)

        row = QHBoxLayout()
        add = QPushButton("Add address…")
        add.clicked.connect(self._add_address)
        remove = QPushButton("Remove selected")
        remove.setObjectName("Danger")
        remove.clicked.connect(self._remove_selected)
        export = QPushButton("Export CSV…")
        export.clicked.connect(self._export)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        row.addWidget(export)
        layout.addLayout(row)
        return page

    def _build_review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Opt-out replies and interrupted sends wait here. Approving an opt-out adds "
            "the address to the suppression list; dismissing keeps the contact active."
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        layout.addWidget(note)

        self.review_table = QTableWidget(0, 4)
        self.review_table.setHorizontalHeaderLabels(["Email", "Reason", "Detail", "When"])
        self.review_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.review_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.review_table.verticalHeader().setVisible(False)
        self.review_table.horizontalHeader().setStretchLastSection(True)
        self.review_table.setColumnWidth(0, 240)
        self.review_table.setColumnWidth(1, 120)
        self.review_table.setColumnWidth(2, 300)
        layout.addWidget(self.review_table, 1)

        row = QHBoxLayout()
        approve = QPushButton("Approve opt-out")
        approve.setObjectName("Primary")
        approve.clicked.connect(self._approve_opt_out)
        dismiss = QPushButton("Mark resolved")
        dismiss.clicked.connect(self._resolve_selected)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(approve)
        row.addWidget(dismiss)
        row.addStretch(1)
        row.addWidget(refresh)
        layout.addLayout(row)
        return page

    # ------------------------------------------------------------------ data
    def refresh(self) -> None:
        rows = db.suppression_rows(self.conn)
        self.sup_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.sup_table.setItem(i, 0, QTableWidgetItem(r["email_norm"]))
            self.sup_table.setItem(i, 1, QTableWidgetItem(r["reason"] or ""))
            self.sup_table.setItem(i, 2, QTableWidgetItem((r["added_at"] or "")[:16]))
            self.sup_table.setItem(i, 3, QTableWidgetItem(r["source"] or ""))

        items = db.review_items(self.conn)
        self.review_table.setRowCount(len(items))
        for i, r in enumerate(items):
            email_item = QTableWidgetItem(r["email_norm"] or "(unknown)")
            email_item.setData(Qt.UserRole, int(r["id"]))
            self.review_table.setItem(i, 0, email_item)
            self.review_table.setItem(i, 1, QTableWidgetItem(r["reason"] or ""))
            self.review_table.setItem(i, 2, QTableWidgetItem(r["detail"] or ""))
            self.review_table.setItem(i, 3, QTableWidgetItem((r["created_at"] or "")[:16]))

    # --------------------------------------------------------------- actions
    def _add_address(self) -> None:
        email, ok = QInputDialog.getText(self, "Suppress an address", "Email address:")
        if not ok or not email.strip():
            return
        email = normalize_email(email)
        if not is_valid_email(email):
            QMessageBox.warning(self, "Not a valid email", f"{email!r} does not look like "
                                                           "a deliverable address.")
            return
        reason, ok = QInputDialog.getItem(
            self, "Reason", "Why is this address suppressed?",
            ["manual", "opt_out", "complaint", "hard_bounce"], 0, False,
        )
        if not ok:
            return
        db.add_suppression(self.conn, email, reason, "operator")
        self.refresh()

    def _remove_selected(self) -> None:
        row = self.sup_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Nothing selected", "Select an address first.")
            return
        email = self.sup_table.item(row, 0).text()
        answer = QMessageBox.question(
            self, "Remove from suppression?",
            f"{email} will become eligible again (if the sheet allows it).\n\nRemove?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        db.remove_suppression(self.conn, email)
        self.refresh()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export suppression list",
                                              "suppression.csv", "CSV (*.csv)")
        if not path:
            return
        rows = db.suppression_rows(self.conn)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["email_norm", "reason", "added_at", "source"])
            for r in rows:
                writer.writerow([r["email_norm"], r["reason"], r["added_at"], r["source"]])
        QMessageBox.information(self, "Exported", f"{len(rows)} row(s) written to\n{path}")

    def _selected_review_id(self) -> int | None:
        row = self.review_table.currentRow()
        if row < 0:
            return None
        item = self.review_table.item(row, 0)
        return int(item.data(Qt.UserRole)) if item else None

    def _approve_opt_out(self) -> None:
        review_id = self._selected_review_id()
        if review_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a review item first.")
            return
        email = self.review_table.item(self.review_table.currentRow(), 0).text()
        if not is_valid_email(email):
            QMessageBox.warning(self, "Cannot suppress", "This item has no valid address.")
            return
        db.add_suppression(self.conn, email, "opt_out", "review")
        db.resolve_review(self.conn, review_id)
        self.refresh()

    def _resolve_selected(self) -> None:
        review_id = self._selected_review_id()
        if review_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a review item first.")
            return
        db.resolve_review(self.conn, review_id)
        self.refresh()

    # -------------------------------------------------------------- lifecycle
    def closeEvent(self, event):  # noqa: N802
        try:
            self.conn.close()
        except Exception:
            pass
        super().closeEvent(event)

    def reject(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        super().reject()
