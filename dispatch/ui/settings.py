"""Settings dialog: account, servers, limits, window, safety, compliance."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..config import FullConfig, write_config
from ..mail.secrets import SecretNotFound, mask, resolve_secret, store_secret

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIMEZONES = ["Europe/London", "Europe/Dublin", "Europe/Paris", "Europe/Berlin",
             "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
             "Australia/Sydney", "Asia/Singapore", "UTC"]


class SettingsDialog(QDialog):
    def __init__(self, cfg: FullConfig, cfg_path: Path | None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.setWindowTitle("Settings")
        self.resize(720, 760)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setSpacing(12)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        # -- account -----------------------------------------------------
        account = QGroupBox("Sending account")
        form = QFormLayout(account)
        self.from_name = QLineEdit(cfg.sender.from_name)
        self.from_email = QLineEdit(cfg.sender.from_email)
        self.reply_to = QLineEdit(cfg.sender.reply_to or "")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Not shown once stored")
        self.password_status = QLabel(self._password_state())
        self.password_status.setObjectName("Muted")
        save_pw = QPushButton("Save password to OS keyring")
        save_pw.clicked.connect(self._save_password)
        self.secret_ref = QLineEdit(cfg.sender.secret_ref)
        form.addRow("From name", self.from_name)
        form.addRow("From email", self.from_email)
        form.addRow("Reply-To", self.reply_to)
        form.addRow("Password", self.password)
        form.addRow("", self.password_status)
        form.addRow("", save_pw)
        form.addRow("secret_ref", self.secret_ref)
        layout.addWidget(account)

        # -- servers -----------------------------------------------------
        servers = QGroupBox("SMTP / IMAP")
        grid = QGridLayout(servers)
        self.smtp_host = QLineEdit(cfg.sender.smtp.host)
        self.smtp_port = QSpinBox(); self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(cfg.sender.smtp.port)
        self.smtp_starttls = QCheckBox("STARTTLS")
        self.smtp_starttls.setChecked(cfg.sender.smtp.starttls)
        self.smtp_ssl = QCheckBox("Implicit TLS")
        self.smtp_ssl.setChecked(cfg.sender.smtp.ssl)
        self.smtp_timeout = QSpinBox(); self.smtp_timeout.setRange(5, 300)
        self.smtp_timeout.setValue(cfg.sender.smtp.timeout)
        self.imap_host = QLineEdit(cfg.sender.imap.host)
        self.imap_port = QSpinBox(); self.imap_port.setRange(1, 65535)
        self.imap_port.setValue(cfg.sender.imap.port)
        self.imap_ssl = QCheckBox("SSL")
        self.imap_ssl.setChecked(cfg.sender.imap.ssl)
        self.imap_box = QLineEdit(cfg.sender.imap.mailbox)

        grid.addWidget(QLabel("SMTP host"), 0, 0); grid.addWidget(self.smtp_host, 0, 1)
        grid.addWidget(QLabel("Port"), 1, 0); grid.addWidget(self.smtp_port, 1, 1)
        row = QHBoxLayout(); row.addWidget(self.smtp_starttls); row.addWidget(self.smtp_ssl)
        row.addWidget(QLabel("Timeout")); row.addWidget(self.smtp_timeout); row.addStretch(1)
        grid.addLayout(row, 2, 0, 1, 2)
        grid.addWidget(QLabel("IMAP host"), 3, 0); grid.addWidget(self.imap_host, 3, 1)
        grid.addWidget(QLabel("Port"), 4, 0); grid.addWidget(self.imap_port, 4, 1)
        row2 = QHBoxLayout(); row2.addWidget(self.imap_ssl)
        row2.addWidget(QLabel("Mailbox")); row2.addWidget(self.imap_box); row2.addStretch(1)
        grid.addLayout(row2, 5, 0, 1, 2)
        layout.addWidget(servers)

        # -- campaign ----------------------------------------------------
        campaign = QGroupBox("Campaign & limits")
        cform = QFormLayout(campaign)
        self.campaign_name = QLineEdit(cfg.campaign.name)
        self.quota = QSpinBox(); self.quota.setRange(1, 100000); self.quota.setValue(cfg.campaign.quota)
        self.frequency = QSpinBox(); self.frequency.setRange(1, 3650)
        self.frequency.setValue(cfg.campaign.frequency_days)
        self.daily_cap = QSpinBox(); self.daily_cap.setRange(1, 100000)
        self.daily_cap.setValue(cfg.limits.daily_cap)
        self.hourly_cap = QSpinBox(); self.hourly_cap.setRange(1, 10000)
        self.hourly_cap.setValue(cfg.limits.hourly_cap)
        self.warmup_enabled = QCheckBox("Warm-up ramp (per week)")
        self.warmup_enabled.setChecked(cfg.warmup.enabled)
        self.warmup_ramp = QLineEdit(", ".join(str(x) for x in cfg.warmup.ramp))
        self.throttle_mode = QComboBox(); self.throttle_mode.addItems(["fixed", "random"])
        self.throttle_mode.setCurrentText(cfg.throttle.mode)
        self.fixed_seconds = QSpinBox(); self.fixed_seconds.setRange(0, 86400)
        self.fixed_seconds.setValue(cfg.throttle.fixed_seconds)
        self.random_min = QSpinBox(); self.random_min.setRange(0, 86400)
        self.random_min.setValue(cfg.throttle.random_seconds[0])
        self.random_max = QSpinBox(); self.random_max.setRange(0, 86400)
        self.random_max.setValue(cfg.throttle.random_seconds[1])
        cform.addRow("Campaign name", self.campaign_name)
        cform.addRow("Quota per run", self.quota)
        cform.addRow("Frequency (days)", self.frequency)
        cform.addRow("Daily cap", self.daily_cap)
        cform.addRow("Hourly cap", self.hourly_cap)
        cform.addRow("", self.warmup_enabled)
        cform.addRow("Warm-up ramp", self.warmup_ramp)
        cform.addRow("Timer mode", self.throttle_mode)
        cform.addRow("Fixed seconds", self.fixed_seconds)
        row3 = QHBoxLayout(); row3.addWidget(QLabel("Random min")); row3.addWidget(self.random_min)
        row3.addWidget(QLabel("max")); row3.addWidget(self.random_max); row3.addStretch(1)
        cform.addRow("Random seconds", row3)
        layout.addWidget(campaign)

        # -- window ------------------------------------------------------
        window = QGroupBox("Send window")
        wform = QFormLayout(window)
        self.window_enabled = QCheckBox("Only send inside this window")
        self.window_enabled.setChecked(cfg.campaign.send_window.enabled)
        self.window_start = QLineEdit(cfg.campaign.send_window.start)
        self.window_end = QLineEdit(cfg.campaign.send_window.end)
        self.tz = QComboBox(); self.tz.setEditable(True)
        self.tz.addItems(TIMEZONES)
        self.tz.setCurrentText(cfg.campaign.send_window.timezone)
        self.days: dict[str, QCheckBox] = {}
        days_row = QHBoxLayout()
        for day in DAY_NAMES:
            box = QCheckBox(day)
            box.setChecked(day in cfg.campaign.send_window.days)
            self.days[day] = box
            days_row.addWidget(box)
        days_row.addStretch(1)
        wform.addRow("", self.window_enabled)
        wform.addRow("Start (HH:MM)", self.window_start)
        wform.addRow("End (HH:MM)", self.window_end)
        wform.addRow("Days", days_row)
        wform.addRow("Timezone", self.tz)
        layout.addWidget(window)

        # -- safety ------------------------------------------------------
        safety = QGroupBox("Safety")
        sform = QFormLayout(safety)
        self.dry_run = QCheckBox("Dry run — write .eml files to ./outbox, send nothing")
        self.dry_run.setChecked(cfg.safety.dry_run)
        self.typed_over = QSpinBox(); self.typed_over.setRange(0, 100000)
        self.typed_over.setValue(cfg.safety.require_typed_confirmation_over)
        self.stop_rate = QDoubleSpinBox(); self.stop_rate.setRange(0.0, 1.0)
        self.stop_rate.setSingleStep(0.01); self.stop_rate.setDecimals(2)
        self.stop_rate.setValue(cfg.safety.stop_on_failure_rate)
        sform.addRow("", self.dry_run)
        sform.addRow("Typed confirmation over", self.typed_over)
        sform.addRow("Stop on failure rate", self.stop_rate)
        layout.addWidget(safety)

        # -- compliance --------------------------------------------------
        compliance = QGroupBox("Compliance footer (program-generated, cannot be removed)")
        comp = QFormLayout(compliance)
        self.postal = QLineEdit(cfg.template.footer.postal_address)
        self.unsub_url = QLineEdit(cfg.template.footer.unsubscribe_url)
        self.unsub_mail = QLineEdit(cfg.template.footer.unsubscribe_mailto)
        comp.addRow("Postal address", self.postal)
        comp.addRow("Unsubscribe URL", self.unsub_url)
        comp.addRow("Unsubscribe mailto", self.unsub_mail)
        layout.addWidget(compliance)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        suppression_btn = buttons.addButton("Suppression & review…",
                                            QDialogButtonBox.ActionRole)
        suppression_btn.clicked.connect(self._open_suppression)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _open_suppression(self) -> None:
        from .suppression import SuppressionDialog
        SuppressionDialog(self.cfg, self).exec()

    # ------------------------------------------------------------------
    def _password_state(self) -> str:
        try:
            return "Stored: " + mask(resolve_secret(self.cfg.sender.secret_ref))
        except SecretNotFound as exc:
            return f"Not available: {exc}"

    def _save_password(self) -> None:
        value = self.password.text()
        if not value:
            QMessageBox.information(self, "Nothing to save", "Type the password first.")
            return
        try:
            store_secret(self.secret_ref.text().strip() or self.cfg.sender.secret_ref, value)
        except Exception as exc:
            QMessageBox.warning(self, "Could not save", f"{type(exc).__name__}: {exc}")
            return
        self.password.clear()
        self.cfg.sender.secret_ref = self.secret_ref.text().strip() or self.cfg.sender.secret_ref
        self.password_status.setText(self._password_state())
        QMessageBox.information(self, "Saved", "Password stored in the OS keyring.")

    def _save(self) -> None:
        cfg = self.cfg
        try:
            cfg.sender.from_name = self.from_name.text().strip()
            cfg.sender.from_email = self.from_email.text().strip()
            cfg.sender.reply_to = self.reply_to.text().strip() or None
            cfg.sender.secret_ref = self.secret_ref.text().strip()
            cfg.sender.smtp.host = self.smtp_host.text().strip()
            cfg.sender.smtp.port = self.smtp_port.value()
            cfg.sender.smtp.starttls = self.smtp_starttls.isChecked()
            cfg.sender.smtp.ssl = self.smtp_ssl.isChecked()
            cfg.sender.smtp.timeout = self.smtp_timeout.value()
            cfg.sender.imap.host = self.imap_host.text().strip()
            cfg.sender.imap.port = self.imap_port.value()
            cfg.sender.imap.ssl = self.imap_ssl.isChecked()
            cfg.sender.imap.mailbox = self.imap_box.text().strip() or "INBOX"

            cfg.campaign.name = self.campaign_name.text().strip() or "Untitled campaign"
            cfg.campaign.quota = self.quota.value()
            cfg.campaign.frequency_days = self.frequency.value()
            cfg.limits.daily_cap = self.daily_cap.value()
            cfg.limits.hourly_cap = self.hourly_cap.value()
            cfg.warmup.enabled = self.warmup_enabled.isChecked()
            ramp = [int(x.strip()) for x in self.warmup_ramp.text().split(",") if x.strip().isdigit()]
            cfg.warmup.ramp = ramp or cfg.warmup.ramp
            cfg.throttle.mode = self.throttle_mode.currentText()
            cfg.throttle.fixed_seconds = self.fixed_seconds.value()
            lo, hi = self.random_min.value(), self.random_max.value()
            if hi < lo:
                raise ValueError("random timer: max must be >= min")
            cfg.throttle.random_seconds = [lo, hi]

            cfg.campaign.send_window.enabled = self.window_enabled.isChecked()
            cfg.campaign.send_window.start = self.window_start.text().strip()
            cfg.campaign.send_window.end = self.window_end.text().strip()
            cfg.campaign.send_window.timezone = self.tz.currentText().strip()
            days = [d for d, box in self.days.items() if box.isChecked()]
            if not days:
                raise ValueError("pick at least one send day")
            cfg.campaign.send_window.days = days

            cfg.safety.require_typed_confirmation_over = self.typed_over.value()
            cfg.safety.stop_on_failure_rate = self.stop_rate.value()
            if cfg.safety.dry_run and not self.dry_run.isChecked():
                answer = QMessageBox.question(
                    self, "Turn off dry run?",
                    "Real email will be sent from this account — but only after you confirm "
                    "again on each run.\n\nTurn dry run off?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
            cfg.safety.dry_run = self.dry_run.isChecked()

            cfg.template.footer.postal_address = self.postal.text().strip()
            cfg.template.footer.unsubscribe_url = self.unsub_url.text().strip()
            cfg.template.footer.unsubscribe_mailto = self.unsub_mail.text().strip()

            # Re-validate through pydantic (raises on bad HH:MM, timezone, ranges).
            from ..config import FullConfig
            FullConfig.model_validate(cfg.model_dump())
        except Exception as exc:
            QMessageBox.warning(self, "Check the values", f"{type(exc).__name__}: {exc}")
            return

        if not cfg.template.footer.postal_address:
            QMessageBox.warning(self, "Postal address required",
                                "A physical postal address is required in every marketing email.")
            return
        if self.cfg_path:
            try:
                write_config(cfg, self.cfg_path)
            except Exception as exc:
                QMessageBox.warning(self, "Cannot write config", f"{exc}")
                return
        self.accept()
