"""Pre-flight report dialog: counts, feasibility arithmetic, typed confirmation."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSpinBox, QTextBrowser, QVBoxLayout,
)

from .. import db
from ..config import FullConfig
from ..engine import preflight as preflight_mod
from .inbox import InboxCheckThread


class PreflightDialog(QDialog):
    def __init__(self, report: preflight_mod.PreflightReport, cfg: FullConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.report = report
        self.quota = report.planned
        self._thread: InboxCheckThread | None = None

        self.setWindowTitle("Pre-flight report")
        self.resize(760, 620)
        layout = QVBoxLayout(self)

        self.headline = QLabel()
        self.headline.setObjectName("Title")
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)

        self.dry = QLabel()
        self.dry.setObjectName("DryBanner" if cfg.safety.dry_run else "Muted")
        self.dry.setWordWrap(True)
        layout.addWidget(self.dry)

        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(False)
        layout.addWidget(self.body, 1)

        quota_row = QHBoxLayout()
        quota_row.addWidget(QLabel("Sends this run"))
        self.quota_spin = QSpinBox()
        self.quota_spin.setRange(0, max(1, report.eligible))
        self.quota_spin.setValue(max(0, min(report.planned, report.eligible)))
        self.quota_spin.valueChanged.connect(self._quota_changed)
        quota_row.addWidget(self.quota_spin)
        self.quota_note = QLabel("")
        self.quota_note.setObjectName("Muted")
        quota_row.addWidget(self.quota_note, 1)
        layout.addLayout(quota_row)

        buttons = QDialogButtonBox()
        self.start_btn = buttons.addButton("Start", QDialogButtonBox.AcceptRole)
        self.start_btn.setObjectName("Primary")
        self.start_btn.setEnabled(report.ok and report.planned > 0)
        self.recheck_btn = buttons.addButton("Re-check inbox first", QDialogButtonBox.ActionRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._start)
        buttons.rejected.connect(self.reject)
        self.recheck_btn.clicked.connect(self._recheck)
        layout.addWidget(buttons)

        self._render()
        self._quota_changed(self.quota_spin.value())

    # -- rendering -------------------------------------------------------
    def _render(self) -> None:
        r = self.report
        self.headline.setText(r.headline())
        if self.cfg.safety.dry_run:
            self.dry.setText("DRY RUN is on — this run writes .eml files to ./outbox "
                             "and sends nothing.")
        else:
            self.dry.setText("LIVE RUN — real email will be sent from "
                             f"{self.cfg.sender.from_email}.")
        html = ["<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#1f2933'>"]
        for line in r.lines():
            colour = "#b42318" if line.startswith("ERROR") or line.startswith("Blocked") else "#1f2933"
            html.append(f"<p style='margin:2px 0;color:{colour}'>{_esc(line)}</p>")
        if r.by_reason:
            html.append("<h3 style='margin-top:14px'>Skipped by reason</h3><ul style='margin-top:4px'>")
            for reason, count in sorted(r.by_reason.items()):
                html.append(f"<li>{_esc(reason.replace('skipped:', ''))}: {count}</li>")
            html.append("</ul>")
        if r.ambiguous:
            html.append("<h3 style='margin-top:14px'>Ambiguous dates (check before sending)</h3><ul>")
            for a in r.ambiguous[:20]:
                html.append(f"<li>row {a.row} · {_esc(a.column)} · {_esc(a.raw)} — "
                            f"{_esc(a.detail)}</li>")
            html.append("</ul>")
        html.append("</div>")
        self.body.setHtml("".join(html))
        self.start_btn.setEnabled(self.report.ok and self.quota_spin.value() > 0)

    def _quota_changed(self, value: int) -> None:
        self.quota = value
        if self.report.eligible == 0:
            self.quota_note.setText("Nothing eligible.")
        else:
            mean = self.report.mean_seconds
            self.quota_note.setText(
                f"~{max(1, round(value * mean / 60))} min at the current timer · "
                f"{self.report.eligible} eligible · {self.report.daily_remaining} left today"
            )
        self.start_btn.setEnabled(self.report.ok and value > 0)

    # -- actions ---------------------------------------------------------
    def _recheck(self) -> None:
        self.recheck_btn.setEnabled(False)
        self.recheck_btn.setText("Checking…")
        self._thread = InboxCheckThread(self.cfg, self)
        self._thread.done.connect(self._inbox_done)
        self._thread.failed.connect(self._inbox_failed)
        self._thread.start()

    def _inbox_done(self, counts: dict) -> None:
        self.recheck_btn.setEnabled(True)
        self.recheck_btn.setText("Re-check inbox first")
        QMessageBox.information(self, "Inbox checked",
                                ", ".join(f"{k}: {v}" for k, v in counts.items()) or
                                "No new replies or bounces.")
        self._refresh_report()

    def _inbox_failed(self, message: str) -> None:
        self.recheck_btn.setEnabled(True)
        self.recheck_btn.setText("Re-check inbox first")
        QMessageBox.warning(self, "Inbox check failed",
                            f"{message}\n\nCheck the IMAP host and password in Settings.")

    def _refresh_report(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            db.init_db(conn)
            self.report = preflight_mod.run_preflight(self.cfg, conn,
                                                      quota=self.quota_spin.value())
        finally:
            conn.close()
        self.quota_spin.setMaximum(max(1, self.report.eligible))
        self._render()

    def _start(self) -> None:
        quota = self.quota_spin.value()
        if quota <= 0:
            return
        if not self.cfg.safety.dry_run and quota > self.cfg.safety.require_typed_confirmation_over:
            needed = f"SEND {quota}"
            text, ok = _get_text(
                self, "Confirm a live send",
                f"This will send {quota} real emails from {self.cfg.sender.from_email}.\n"
                f"Type {needed} to continue:",
            )
            if not ok or text.strip() != needed:
                return
        self.quota = quota
        self.accept()


def _esc(text: str) -> str:
    import html
    return html.escape(str(text), quote=False)


def _get_text(parent, title: str, label: str) -> tuple[str, bool]:
    from PySide6.QtWidgets import QInputDialog
    return QInputDialog.getText(parent, title, label)
