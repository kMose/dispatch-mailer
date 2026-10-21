"""Post-run report: totals, per-hour breakdown, sheet write-back with a diff preview."""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTextBrowser, QVBoxLayout,
)

from .. import db
from ..config import FullConfig
from ..sheet import writer as sheet_writer


class ReportDialog(QDialog):
    def __init__(self, cfg: FullConfig, run_id: int, conn: sqlite3.Connection, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.run_id = run_id
        self.conn = conn
        self.setWindowTitle("Run report")
        self.resize(720, 560)
        layout = QVBoxLayout(self)

        title = QLabel(f"Run #{run_id} — {cfg.campaign.name}")
        title.setObjectName("Title")
        layout.addWidget(title)

        self.body = QTextBrowser()
        layout.addWidget(self.body, 1)

        row = QHBoxLayout()
        export_btn = QPushButton("Export CSV…")
        export_btn.clicked.connect(self.export_csv)
        self.write_btn = QPushButton("Write results to sheet…")
        self.write_btn.setObjectName("Primary")
        self.write_btn.clicked.connect(self.write_sheet)
        row.addWidget(export_btn)
        row.addWidget(self.write_btn)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._render()

    def _rows(self):
        return db.run_timeline(self.conn, self.run_id)

    def _render(self) -> None:
        counts = db.counts_by_state(self.conn, self.run_id)
        run = db.run(self.conn, self.run_id)
        rows = self._rows()
        hours: dict[str, dict[str, int]] = {}
        for r in rows:
            if r["sent_at"]:
                hour = str(r["sent_at"])[11:13] + ":00"
                bucket = hours.setdefault(hour, {"sent": 0, "failed": 0})
                if r["state"] == "sent":
                    bucket["sent"] += 1
                else:
                    bucket["failed"] += 1
        parts = ["<div style='font-family:Segoe UI,Arial;font-size:13px'>"]
        if run:
            parts.append(f"<p><b>Started</b> {run['started_at']} &nbsp; "
                         f"<b>Ended</b> {run['ended_at'] or '—'} &nbsp; "
                         f"<b>Stop reason</b> {run['stop_reason'] or '—'}</p>")
        parts.append("<h3>Totals</h3><ul>")
        for state in ("sent", "failed_soft", "failed_hard", "unknown", "queued"):
            if counts.get(state):
                parts.append(f"<li>{state.replace('_', ' ')}: {counts[state]}</li>")
        if not counts:
            parts.append("<li>No sends recorded.</li>")
        parts.append("</ul>")
        if hours:
            parts.append("<h3>Per hour</h3><table cellpadding='4'><tr><th align='left'>Hour</th>"
                         "<th align='left'>Sent</th><th align='left'>Failed</th></tr>")
            for hour in sorted(hours):
                parts.append(f"<tr><td>{hour}</td><td>{hours[hour]['sent']}</td>"
                             f"<td>{hours[hour]['failed']}</td></tr>")
            parts.append("</table>")
        review = [r for r in rows if r["state"] in ("unknown", "sending")]
        if review:
            parts.append("<h3>Needs review (never resent automatically)</h3><ul>")
            for r in review[:20]:
                parts.append(f"<li>row {r['sheet_row']} · {r['email']} · {r['state']}</li>")
            parts.append("</ul>")
        parts.append("</div>")
        self.body.setHtml("".join(parts))
        self.write_btn.setEnabled(bool(rows))

    # -- actions ---------------------------------------------------------
    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export run results", "results.csv",
                                              "CSV (*.csv)")
        if not path:
            return
        rows = self._rows()
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["sheet_row", "email", "first_name", "last_name", "state",
                        "sent_at", "smtp_code", "error", "message_id"])
            for r in rows:
                w.writerow([r["sheet_row"], r["email"], r["first_name"], r["last_name"],
                            r["state"], r["sent_at"], r["smtp_code"], r["error"],
                            r["message_id"]])
        QMessageBox.information(self, "Exported", f"{len(rows)} row(s) written to\n{path}")

    def write_sheet(self) -> None:
        try:
            updates = sheet_writer.collect_updates(self.conn, self.run_id)
            changes = sheet_writer.plan(self.cfg.sheet_file, updates,
                                        self.cfg.campaign.sheet_tab)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot write", f"{type(exc).__name__}: {exc}")
            return
        if not changes:
            QMessageBox.information(self, "Nothing to write",
                                    "The spreadsheet already reflects this run.")
            return
        dlg = SheetDiffDialog(changes, self.cfg, self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            result = sheet_writer.write_back(self.cfg.sheet_file, updates,
                                             self.cfg.campaign.sheet_tab)
        except sheet_writer.SheetLockedError as exc:
            QMessageBox.warning(self, "Sheet is open in Excel", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Write failed", f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(self, "Written", result.summary + f"\n{result.path}")


class SheetDiffDialog(QDialog):
    def __init__(self, changes, cfg: FullConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Write results to sheet — preview")
        self.resize(640, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"{len(changes)} cell(s) will change in {Path(cfg.sheet_file).name}. "
            "LastAdvertised only moves forward; Replied is only ever set, never cleared."
        ))
        text = QTextBrowser()
        text.setPlainText("\n".join(c.describe() for c in changes[:500]))
        layout.addWidget(text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Write")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
