"""Main window: top bar, contact table, account panel, filterable log."""
from __future__ import annotations

import csv
import queue
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableView, QVBoxLayout,
    QWidget,
)

from .. import db
from ..config import FullConfig, load_config
from ..engine import eligibility
from ..engine import preflight as preflight_mod
from ..engine.worker import Worker, mask_email
from .inbox import InboxCheckThread
from .models import ContactFilterProxy, ContactTableModel
from .preflight import PreflightDialog
from .report import ReportDialog
from .settings import SettingsDialog
from .template_builder import TemplateBuilderDialog

FILTERS = [("all", "All"), ("eligible", "Eligible"), ("sent", "Sent"),
           ("skipped", "Skipped"), ("failed", "Failed"), ("replied", "Replied")]


class MainWindow(QMainWindow):
    def __init__(self, cfg: FullConfig, cfg_path: Path | None):
        super().__init__()
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.conn = db.connect(cfg.db_path)
        db.init_db(self.conn)
        self.worker: Worker | None = None
        self._events: queue.Queue[dict] = queue.Queue()
        self._inbox_thread: InboxCheckThread | None = None
        self._log_lines: list[tuple[str, str, str]] = []
        self._countdown = None
        self._countdown_at = 0.0
        self._reported_runs: set[int] = set()
        self._last_reload = 0.0
        self._refresh_running = False

        self.setWindowTitle(f"Dispatch — {cfg.campaign.name}")
        self.resize(1240, 820)
        self._build_ui()
        self._build_menu()
        self._restore_geometry()
        self._reload_sheet()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(400)

        # Poll the inbox every five minutes (section 11), plus on start/stop.
        self.imap_timer = QTimer(self)
        self.imap_timer.timeout.connect(self._auto_check_inbox)
        self.imap_timer.start(5 * 60 * 1000)

        QShortcut(QKeySequence("Space"), self, activated=self.toggle_pause,
                  context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.start_campaign,
                  context=Qt.WidgetWithChildrenShortcut)
        self._log("info", f"Loaded {cfg.campaign.name} — "
                          + ("DRY RUN" if cfg.safety.dry_run else "LIVE MODE"))

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(14, 12, 14, 10)
        outer.setSpacing(10)

        self.dry_banner = QLabel(
            "DRY RUN — nothing is sent. Messages go to ./outbox at the configured pace."
        )
        self.dry_banner.setObjectName("DryBanner")
        # Without wrapping this one label forces a 2004 px minimum window width.
        self.dry_banner.setWordWrap(True)
        self.dry_banner.setVisible(self.cfg.safety.dry_run)
        outer.addWidget(self.dry_banner)

        top = QFrame()
        top.setObjectName("Card")
        top_outer = QVBoxLayout(top)
        top_outer.setContentsMargins(16, 12, 16, 10)
        top_outer.setSpacing(8)
        top_row = QHBoxLayout()
        title_box = QVBoxLayout()
        self.title = QLabel(self.cfg.campaign.name)
        self.title.setObjectName("Title")
        self.subtitle = QLabel(self.cfg.sender.from_email)
        self.subtitle.setObjectName("Subtitle")
        self.subtitle.setToolTip(f"{self.cfg.sender.from_email} — "
                                 f"{Path(self.cfg.sheet_file).name}")
        self.title.setToolTip(self.cfg.campaign.name)
        self.title.setMaximumWidth(360)
        self.subtitle.setMaximumWidth(360)
        title_box.addWidget(self.title)
        title_box.addWidget(self.subtitle)
        top_row.addLayout(title_box)
        self.status_pill = QLabel("Idle")
        self.status_pill.setObjectName("Pill")
        top_row.addSpacing(12)
        top_row.addWidget(self.status_pill, 0, Qt.AlignVCenter)

        counter_box = QVBoxLayout()
        self.counter = QLabel(f"0 / {self.cfg.campaign.quota} (0%)")
        self.counter.setObjectName("Counter")
        self.counter_note = QLabel("not started")
        self.counter_note.setObjectName("Subtitle")
        self.counter_note.setMaximumWidth(220)
        counter_box.addWidget(self.counter)
        counter_box.addWidget(self.counter_note)
        top_row.addSpacing(24)
        top_row.addLayout(counter_box)

        time_box = QVBoxLayout()
        self.eta = QLabel("ETA —")
        self.eta.setObjectName("Muted")
        self.elapsed = QLabel("0m 00s elapsed")
        self.elapsed.setObjectName("Muted")
        for label in (self.eta, self.elapsed):
            label.setMaximumWidth(180)
        time_box.addWidget(self.eta)
        time_box.addWidget(self.elapsed)
        top_row.addSpacing(24)
        top_row.addLayout(time_box)
        top_row.addStretch(1)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.clicked.connect(self.stop_campaign)
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(self.start_campaign)
        top_row.addWidget(self.start_btn)
        top_row.addWidget(self.pause_btn)
        top_row.addWidget(self.stop_btn)
        top_outer.addLayout(top_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setObjectName("RunProgress")
        top_outer.addWidget(self.progress)
        outer.addWidget(top)

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        chips = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name or email…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        self.search.textChanged.connect(self._set_search)
        chips.addWidget(self.search)
        chips.addSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        for index, (key, label) in enumerate(FILTERS):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setProperty("kind", key)
            if index == 0:
                chip.setChecked(True)
            chip.clicked.connect(lambda checked=False, k=key: self._set_filter(k))
            self.chip_group.addButton(chip)
            chips.addWidget(chip)
        chips.addStretch(1)
        self.row_count = QLabel("")
        self.row_count.setObjectName("Muted")
        chips.addWidget(self.row_count)
        left_layout.addLayout(chips)

        self.model = ContactTableModel()
        self.proxy = ContactFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setStretchLastSection(False)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(34)
        widths = {"#": 34, "Sheet row": 48, "Name": 100, "Email": 150,
                  "Last advertised": 92, "Eligible": 70, "Status": 104,
                  "Sent at": 100, "Error": 130}
        self.table.setMinimumWidth(700)
        for i, column in enumerate(["#", "Sheet row", "Name", "Email", "Last advertised",
                                    "Eligible", "Status", "Sent at", "Error"]):
            self.table.setColumnWidth(i, widths[column])
            header.setSectionResizeMode(i, QHeaderView.Interactive)
        # Email absorbs the spare width; Error keeps a fixed, readable column instead of
        # eating half the grid.
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        left_layout.addWidget(self.table, 1)

        log_head = QHBoxLayout()
        log_head.addWidget(QLabel("Log"))
        self.log_filter = QLineEdit()
        self.log_filter.setPlaceholderText("Filter log…")
        self.log_filter.setMaximumWidth(220)
        self.log_filter.textChanged.connect(self._render_log)
        log_head.addWidget(self.log_filter)
        log_head.addStretch(1)
        export_log = QPushButton("Export log CSV")
        export_log.clicked.connect(self.export_log)
        clear_log = QPushButton("Clear")
        clear_log.clicked.connect(self._clear_log)
        log_head.addWidget(export_log)
        log_head.addWidget(clear_log)
        left_layout.addLayout(log_head)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(170)
        self.log_view.setPlaceholderText("Events appear here. Email domains only — no bodies.")
        left_layout.addWidget(self.log_view)
        splitter.addWidget(left)

        # right: account panel
        panel = QFrame()
        panel.setObjectName("Card")
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(360)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(16, 16, 16, 16)
        pv.setSpacing(8)
        section = QLabel("ACCOUNT")
        section.setObjectName("Section")
        pv.addWidget(section)
        self.account_label = QLabel(self.cfg.sender.from_email)
        self.account_label.setWordWrap(True)
        pv.addWidget(self.account_label)
        pv.addSpacing(6)
        self.next_label = QLabel("Next send —")
        self.next_label.setObjectName("Big")
        pv.addWidget(self.next_label)
        self.next_note = QLabel("idle")
        self.next_note.setObjectName("Subtitle")
        pv.addWidget(self.next_note)
        pv.addSpacing(8)
        self.today_label = QLabel("Today 0/0 · This hour 0/0")
        pv.addWidget(self.today_label)
        self.warmup_label = QLabel("Warm-up ceiling —")
        self.warmup_label.setObjectName("Muted")
        pv.addWidget(self.warmup_label)
        self.pacing_label = QLabel("")
        self.pacing_label.setObjectName("Muted")
        self.pacing_label.setWordWrap(True)
        pv.addWidget(self.pacing_label)
        pv.addSpacing(10)
        section2 = QLabel("HEALTH")
        section2.setObjectName("Section")
        pv.addWidget(section2)
        self.health_label = QLabel("OK")
        self.health_label.setObjectName("Ok")
        self.health_label.setWordWrap(True)
        pv.addWidget(self.health_label)
        pv.addSpacing(10)
        section3 = QLabel("INBOX")
        section3.setObjectName("Section")
        pv.addWidget(section3)
        self.imap_label = QLabel("not checked yet")
        self.imap_label.setObjectName("Muted")
        self.imap_label.setWordWrap(True)
        pv.addWidget(self.imap_label)
        self.recheck_btn = QPushButton("Re-check inbox")
        self.recheck_btn.clicked.connect(self.check_inbox)
        pv.addWidget(self.recheck_btn)
        pv.addSpacing(10)
        self.review_label = QLabel("Needs review: 0")
        self.review_label.setObjectName("Muted")
        pv.addWidget(self.review_label)
        self.review_btn = QPushButton("Review flagged rows…")
        self.review_btn.clicked.connect(self.show_review)
        pv.addWidget(self.review_btn)
        pv.addStretch(1)
        self.pacing_label2 = QLabel(f"Timer: {self.cfg.throttle.mode} · "
                                    f"~{self.cfg.throttle.mean_seconds():.0f}s between sends")
        self.pacing_label2.setObjectName("Muted")
        self.pacing_label2.setWordWrap(True)
        pv.addWidget(self.pacing_label2)
        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([880, 320])
        outer.addWidget(splitter, 1)

        self.statusBar().showMessage(f"{self.cfg_path}  ·  sheet: {self.cfg.sheet_file}")
        self._set_running_controls(False)

    def _build_menu(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        self._action(file_menu, "Open campaign…", self.open_campaign, "Ctrl+O")
        self._action(file_menu, "New campaign…", self.new_campaign)
        file_menu.addSeparator()
        self._action(file_menu, "Export run CSV…", self.export_run_csv)
        self._action(file_menu, "Write results to sheet…", self.write_results)
        file_menu.addSeparator()
        self._action(file_menu, "Quit", self.close, "Ctrl+Q")

        campaign = menu.addMenu("&Campaign")
        self._action(campaign, "Pre-flight report…", self.show_preflight, "Ctrl+P")
        self._action(campaign, "Start", self.start_campaign, "Ctrl+Return")
        self._action(campaign, "Pause / resume", self.toggle_pause, "Space")
        self._action(campaign, "Stop", self.stop_campaign)

        tools = menu.addMenu("&Tools")
        self._action(tools, "Template builder…", self.open_template_builder, "Ctrl+T")
        self._action(tools, "Suppression & review…", self.open_suppression)
        self._action(tools, "Settings…", self.open_settings, "Ctrl+,")
        self._action(tools, "Check inbox now", self.check_inbox)

        help_menu = menu.addMenu("&Help")
        self._action(help_menu, "About Dispatch", self.show_about)

    @staticmethod
    def _action(menu, text, slot, shortcut=None) -> QAction:
        action = QAction(text, menu)
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        menu.addAction(action)
        return action

    # ------------------------------------------------------------- row data
    def reload_contacts(self) -> None:
        rows = self.conn.execute(
            """SELECT c.*, s.state AS last_state, s.sent_at AS last_sent,
                      s.error AS last_send_error, s.dry_run AS last_dry
               FROM contacts c
               LEFT JOIN sends s ON s.id = (SELECT MAX(id) FROM sends WHERE contact_id=c.id)
               WHERE c.in_sheet=1
               ORDER BY COALESCE(c.sheet_row, c.id) ASC"""
        ).fetchall()
        suppression = db.suppression_set(self.conn)
        seen: set[str] = set()
        today = date.today()
        data: list[dict] = []
        for index, row in enumerate(rows, start=1):
            verdict = eligibility.evaluate(
                row, suppression=suppression, seen_emails=seen, today=today,
                frequency_days=self.cfg.campaign.frequency_days,
            )
            if row["email_norm"]:
                seen.add(row["email_norm"])
            last_state = row["last_state"]
            if last_state in ("sending", "queued", "sent", "failed_soft",
                              "failed_hard", "unknown"):
                status = last_state
            elif verdict.eligible:
                status = "pending"
            else:
                status = verdict.status
            name = " ".join(x for x in (row["first_name"], row["last_name"]) if x)
            data.append({
                "idx": index,
                "sheet_row": row["sheet_row"],
                "name": name or "—",
                "email": row["email"] or "(blank)",
                "last_advertised": (row["last_advertised"] or "")[:10],
                "eligible": "Yes" if verdict.eligible else "No",
                "eligible_detail": verdict.reason or "",
                "status": status,
                "sent_at": (row["last_sent"] or "")[:16].replace("T", " "),
                "error": row["last_send_error"] or row["last_error"] or "",
                "replied": bool(row["replied"]),
                "dry": bool(row["last_dry"]),
            })
        self.model.set_rows(data)
        self.row_count.setText(f"{len(data)} rows")

    def _set_filter(self, kind: str) -> None:
        self.proxy.set_kind(kind)

    def _set_search(self, text: str) -> None:
        self.proxy.set_search(text)

    # -------------------------------------------------------------- worker
    def start_campaign(self) -> None:
        if self.worker and self.worker.is_alive():
            QMessageBox.information(self, "Already running", "A run is already in progress.")
            return
        try:
            report = preflight_mod.run_preflight(self.cfg, self.conn)
        except Exception as exc:
            QMessageBox.critical(self, "Pre-flight failed", f"{type(exc).__name__}: {exc}")
            return
        dlg = PreflightDialog(report, self.cfg, self)
        if dlg.exec() != QDialog.Accepted:
            return
        quota = dlg.quota
        if quota <= 0:
            return
        self.reload_contacts()
        self.worker = Worker(self.cfg, quota=quota, on_event=self._on_worker_event,
                             prepared=True)
        self.progress.setValue(0)
        self.worker.start()
        self._set_running_controls(True)
        self.pause_btn.setText("Pause")
        self._log("info", f"Run started — quota {quota}, "
                          + ("dry run" if self.cfg.safety.dry_run else "LIVE"))
        self._refresh_stats()
        self._auto_check_inbox()

    def _on_worker_event(self, event: dict) -> None:
        # Called from the worker thread: never touch widgets here.
        self._events.put(event)

    def toggle_pause(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            return
        if self.worker.paused:
            self.worker.resume()
            self.pause_btn.setText("Pause")
            self._log("info", "Resumed")
        else:
            self.worker.pause()
            self.pause_btn.setText("Resume")
            self._log("info", "Paused")

    def stop_campaign(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self._log("warn", "Stop requested — flushing after the current message")
            self.stop_btn.setEnabled(False)

    def _set_running_controls(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)

    def _tick(self) -> None:
        handled = 0
        while handled < 200:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            handled += 1
            self._handle_event(event)
        self._refresh_stats()
        if self.worker and self.worker.is_alive():
            now = time.monotonic()
            if now - self._last_reload > 1.5:
                self.reload_contacts()
                self._last_reload = now
        self._update_countdown()

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "log":
            self._log(event.get("level", "info"), event.get("message", ""))
        elif kind == "started":
            self._set_running_controls(True)
        elif kind == "status":
            self._set_pill(event.get("status", "idle"), event.get("detail", ""))
        elif kind == "countdown":
            self._countdown = int(event.get("seconds", 0))
            self._countdown_at = time.monotonic()
        elif kind == "progress":
            self._update_progress(event)
        elif kind == "sent":
            self._log("info", f"sent to {mask_email(str(event.get('email') or ''))}")
            self.reload_contacts()
        elif kind == "failed":
            self._log("error", f"{event.get('state')} — "
                               f"{mask_email(str(event.get('email') or ''))}: "
                               f"{event.get('error')}")
        elif kind == "window":
            self.next_note.setText(f"Outside window — opens {event.get('opens_at', '')[:16]}")
        elif kind == "suspended":
            self.next_note.setText(f"Suspended until {str(event.get('until'))[:16]}")
        elif kind == "flushed":
            self._log("info", f"sheet flush: {event.get('summary')}")
        elif kind == "review":
            self._log("warn", f"{event.get('count')} row(s) need review")
        elif kind == "stopped":
            self._finish_run(event)

    def _finish_run(self, event: dict) -> None:
        self._set_running_controls(False)
        self.pause_btn.setText("Pause")
        self.stop_btn.setEnabled(False)
        run_id = event.get("run_id") or (self.worker.run_id if self.worker else None)
        self.reload_contacts()
        self._refresh_stats()
        self._log("info", f"Run finished — {event.get('sent', 0)} sent, "
                          f"{event.get('failed', 0)} failed "
                          f"({str(event.get('reason', '')).replace('_', ' ')})")
        self._auto_check_inbox()
        if run_id and run_id not in self._reported_runs:
            self._reported_runs.add(run_id)
            try:
                dlg = ReportDialog(self.cfg, int(run_id), self.conn, self)
                dlg.exec()
            except Exception as exc:
                self._log("warn", f"could not open the run report: {exc}")

    def _update_progress(self, event: dict) -> None:
        sent = int(event.get("sent", 0))
        quota = int(event.get("quota", self.cfg.campaign.quota)) or 1
        pct = round(100 * sent / quota)
        self.counter.setText(f"{sent} / {quota} ({pct}%)")
        self.progress.setValue(pct)
        self.counter_note.setText(f"{event.get('failed', 0)} failed · "
                                  f"{event.get('remaining', 0)} left")
        eta = int(event.get("eta_seconds", 0))
        self.eta.setText("ETA " + _dur(eta) if eta else "ETA —")
        self.elapsed.setText(_dur(int(event.get("elapsed", 0))) + " elapsed")

    def _update_countdown(self) -> None:
        if self._countdown is None:
            return
        remaining = max(0, self._countdown - (time.monotonic() - self._countdown_at))
        if not (self.worker and self.worker.is_alive()):
            self.next_label.setText("Next send —")
            return
        if self.worker.paused:
            self.next_label.setText("Paused")
        elif remaining <= 0:
            self.next_label.setText("Sending…")
        else:
            self.next_label.setText("Next send in " + _clock(remaining))

    # --------------------------------------------------------------- stats
    def _refresh_stats(self) -> None:
        # The 400 ms tick can fire while the window is closing and the connection is
        # already gone; never let that raise out of the Qt event loop.
        try:
            self._refresh_stats_inner()
        except sqlite3.Error:
            return

    def _refresh_stats_inner(self) -> None:
        cfg = self.cfg
        account = cfg.sender.id
        try:
            today = db.sent_today(self.conn, cfg.safety.dry_run)
            hour_start = datetime.now().astimezone().replace(
                minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
            hour = db.sent_this_hour(self.conn, cfg.safety.dry_run, hour_start)
        except sqlite3.Error:
            today = hour = 0
        self.today_label.setText(
            f"Today {today}/{cfg.limits.daily_cap} · This hour {hour}/{cfg.limits.hourly_cap}")

        first_send = db.get_meta(self.conn, "warmup_first_send")
        try:
            first_date = date.fromisoformat(first_send) if first_send else cfg.warmup.first_send_date
        except ValueError:
            first_date = None
        ceiling = cfg.effective_warmup_cap(first_date, date.today(), cfg.limits.daily_cap)
        self.warmup_label.setText(
            f"Warm-up ceiling {ceiling}" + ("" if cfg.warmup.enabled else " (warm-up off)"))

        try:
            counts = db.inbound_counts(self.conn, account)
            checked = db.get_meta(self.conn, f"imap_checked:{account}")
        except sqlite3.Error:
            counts, checked = {}, None
        replies = counts.get("reply", 0) + counts.get("autoreply", 0)
        bounces = counts.get("bounce_hard", 0) + counts.get("bounce_soft", 0)
        when = "not checked yet"
        if checked:
            try:
                when = "checked " + _ago(datetime.fromisoformat(checked))
            except ValueError:
                when = "checked recently"
        self.imap_label.setText(f"{when} · {replies} replies · {bounces} bounces")

        review = db.pending_review_count(self.conn)
        self.review_label.setText(f"Needs review: {review}")
        self.review_btn.setEnabled(review > 0)

        # pacing
        window = cfg.campaign.send_window
        now_local = datetime.now(window.tz)
        left = window.seconds_left(now_local)
        queued = 0
        if self.worker and self.worker.run_id:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM sends WHERE run_id=? AND state='queued'",
                (self.worker.run_id,)).fetchone()
            queued = int(row["n"])
        mean = max(1.0, cfg.throttle.mean_seconds())
        if queued:
            need = queued * mean
            verdict = "On pace" if need <= left else "Tight"
            self.pacing_label.setText(
                f"{verdict} — {queued} remaining, {_dur(left)} left in window "
                f"(needs ~{_dur(need)})")
        elif window.enabled and not window.is_open(now_local):
            nxt = window.next_open(now_local)
            self.pacing_label.setText(f"Window closed — opens {nxt.strftime('%a %H:%M')}")
        else:
            self.pacing_label.setText(
                f"Window {window.start}–{window.end} {window.timezone}"
                if window.enabled else "Send window disabled")

        if self.worker and self.worker.is_alive():
            status = self.worker.status
            if status in ("idle", "waiting"):
                status = "dry_run" if self.cfg.safety.dry_run else "running"
            self._set_pill(status)
        elif self.worker and self.worker.stop_reason:
            self._set_pill("stopped")

    def _set_pill(self, status: str, detail: str = "") -> None:
        labels = {"idle": "Idle", "running": "Running", "dry_run": "Dry run",
                  "paused": "Paused", "backing_off": "Backing off",
                  "waiting": "Running", "stopped": "Stopped"}
        names = {"running": "PillRunning", "dry_run": "PillRunning",
                 "paused": "PillPaused", "backing_off": "PillPaused",
                 "stopped": "PillStopped"}
        text = labels.get(status, status.title())
        if status == "stopped" and detail:
            text = f"Stopped — {detail.replace('_', ' ')}"
        self.status_pill.setText(text)
        self.status_pill.setObjectName(names.get(status, "Pill"))
        self.status_pill.style().unpolish(self.status_pill)
        self.status_pill.style().polish(self.status_pill)
        if status == "backing_off":
            self.health_label.setText(f"Backing off — {detail or 'retrying'}")
            self.health_label.setObjectName("Error")
        elif status == "stopped" and detail in ("auth_error", "secret_error"):
            self.health_label.setText("Auth error — re-save the password in Settings")
            self.health_label.setObjectName("Error")
        else:
            self.health_label.setText("OK")
            self.health_label.setObjectName("Ok")
        self.health_label.style().unpolish(self.health_label)
        self.health_label.style().polish(self.health_label)

    # ----------------------------------------------------------------- log
    def _log(self, level: str, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log_lines.append((stamp, level, message))
        self._render_log()

    def _render_log(self) -> None:
        needle = self.log_filter.text().strip().lower() if hasattr(self, "log_filter") else ""
        prefix = {"error": "!", "warn": "…", "info": "·"}
        lines = [f"{stamp} {prefix.get(level, '·')} {message}"
                 for stamp, level, message in self._log_lines
                 if not needle or needle in message.lower()]
        self.log_view.setPlainText("\n".join(lines))
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum())

    def _clear_log(self) -> None:
        self._log_lines.clear()
        self.log_view.clear()

    def export_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export log", "dispatch-log.csv",
                                              "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time", "level", "message"])
            writer.writerows(self._log_lines)
        self._log("info", f"log exported to {path}")

    # ------------------------------------------------------------- dialogs
    def show_preflight(self) -> None:
        try:
            report = preflight_mod.run_preflight(self.cfg, self.conn)
        except Exception as exc:
            QMessageBox.critical(self, "Pre-flight failed", f"{type(exc).__name__}: {exc}")
            return
        PreflightDialog(report, self.cfg, self).exec()
        self.reload_contacts()

    def open_template_builder(self) -> None:
        dlg = TemplateBuilderDialog(self.cfg, self.cfg_path, self)
        dlg.saved.connect(self._reload_config)
        dlg.exec()

    def open_suppression(self) -> None:
        from .suppression import SuppressionDialog
        SuppressionDialog(self.cfg, self).exec()
        self.reload_contacts()
        self._refresh_stats()

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self.cfg_path, self)
        if dlg.exec() == QDialog.Accepted:
            self._reload_config()

    def _reload_config(self) -> None:
        if self.cfg_path and self.cfg_path.exists():
            try:
                self.cfg = load_config(self.cfg_path)
            except Exception as exc:
                QMessageBox.warning(self, "Config error", str(exc))
                return
            self._apply_config_to_ui()
            self._reload_sheet()

    def _apply_config_to_ui(self) -> None:
        self.dry_banner.setVisible(self.cfg.safety.dry_run)
        self.title.setText(self.cfg.campaign.name)
        self.setWindowTitle(f"Dispatch — {self.cfg.campaign.name}")
        self.subtitle.setText(self.cfg.sender.from_email)
        self.subtitle.setToolTip(f"{self.cfg.sender.from_email} — "
                                 f"{Path(self.cfg.sheet_file).name}")
        self.pacing_label2.setText(
            f"Timer: {self.cfg.throttle.mode} · "
            f"~{self.cfg.throttle.mean_seconds():.0f}s between sends")
        self.statusBar().showMessage(f"{self.cfg_path}  ·  sheet: {self.cfg.sheet_file}")

    def _reload_sheet(self) -> None:
        """Import the campaign's workbook into SQLite and repaint the grid.

        Switching campaigns must do this: the contacts table still holds the old sheet
        until an import marks those rows out-of-sheet and inserts the new ones.
        """
        if self.worker and self.worker.is_alive():
            return  # never re-import underneath a running sender
        try:
            data = preflight_mod.import_sheet(self.cfg, self.conn)
        except Exception as exc:
            self._log("error", f"cannot read {Path(self.cfg.sheet_file).name}: {exc}")
        else:
            self._log("info", f"imported {len(data.contacts)} row(s) from {data.path.name}")
        self.reload_contacts()
        self._refresh_stats()

    def _switch_campaign(self, cfg: FullConfig, path: Path | None) -> bool:
        if self.worker and self.worker.is_alive():
            QMessageBox.warning(self, "A run is active", "Stop the current run first.")
            return False
        try:
            self.conn.close()
        except Exception:
            pass
        self.cfg = cfg
        self.cfg_path = path
        if path is not None:
            from PySide6.QtCore import QSettings
            QSettings().setValue("last_config", str(path))
        self.conn = db.connect(cfg.db_path)
        db.init_db(self.conn)
        # Filters from the previous campaign would hide the new contacts.
        self.search.clear()
        self.proxy.set_kind("all")
        for button in self.chip_group.buttons():
            if button.property("kind") == "all":
                button.setChecked(True)
        self._apply_config_to_ui()
        self._reload_sheet()
        return True

    def open_campaign(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open campaign", str(Path.cwd()),
                                              "Campaign config (*.yaml *.yml)")
        if not path:
            return
        try:
            cfg = load_config(path)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot open", str(exc))
            return
        self._switch_campaign(cfg, Path(path))

    def new_campaign(self) -> None:
        from .onboarding import OnboardingWizard
        wizard = OnboardingWizard(initial_config=self.cfg, initial_path=self.cfg_path,
                                  parent=self)
        if wizard.exec() != OnboardingWizard.Accepted:
            return
        if wizard.config and wizard.config_path:
            self._switch_campaign(wizard.config, wizard.config_path)

    def _auto_check_inbox(self) -> None:
        from ..mail.secrets import SecretNotFound, resolve_secret
        if self._inbox_thread and self._inbox_thread.isRunning():
            return
        try:
            resolve_secret(self.cfg.sender.secret_ref)
        except SecretNotFound:
            if not getattr(self, "_imap_warned", False):
                self._imap_warned = True
                self._log("warn", "inbox polling paused — no password stored yet")
            return
        self.check_inbox()

    def check_inbox(self) -> None:
        if self._inbox_thread and self._inbox_thread.isRunning():
            return
        self.recheck_btn.setEnabled(False)
        self.recheck_btn.setText("Checking…")
        self._inbox_thread = InboxCheckThread(self.cfg, self)
        self._inbox_thread.done.connect(self._inbox_done)
        self._inbox_thread.failed.connect(self._inbox_failed)
        self._inbox_thread.start()

    def _inbox_done(self, counts: dict) -> None:
        self.recheck_btn.setEnabled(True)
        self.recheck_btn.setText("Re-check inbox")
        self._log("info", "inbox: " + (", ".join(f"{k}={v}" for k, v in counts.items())
                                       or "nothing new"))
        self._refresh_stats()

    def _inbox_failed(self, message: str) -> None:
        self.recheck_btn.setEnabled(True)
        self.recheck_btn.setText("Re-check inbox")
        self._log("error", f"inbox check failed: {message}")
        QMessageBox.warning(self, "Inbox check failed",
                            f"{message}\n\nCheck the IMAP settings and password.")

    def show_review(self) -> None:
        rows = db.unknown_sends(self.conn)
        if not rows:
            QMessageBox.information(self, "Nothing to review", "No flagged rows.")
            return
        lines = [f"row {r['sheet_row']} · {r['email']} · {r['state']} · "
                 f"{r['error'] or 'unknown outcome'}" for r in rows[:100]]
        QMessageBox.warning(
            self, "Flagged rows — never resent automatically",
            "These rows were interrupted mid-send or came back unknown. Check the inbox, "
            "then either remove them from the sheet or set LastAdvertised by hand:\n\n"
            + "\n".join(lines),
        )

    def export_run_csv(self) -> None:
        run = db.latest_run(self.conn)
        if not run:
            QMessageBox.information(self, "No runs", "Nothing has been sent yet.")
            return
        ReportDialog(self.cfg, int(run["id"]), self.conn, self).exec()

    def write_results(self) -> None:
        run = db.latest_run(self.conn)
        if not run:
            QMessageBox.information(self, "No runs", "Write results after a run.")
            return
        ReportDialog(self.cfg, int(run["id"]), self.conn, self).exec()

    def show_about(self) -> None:
        QMessageBox.about(
            self, "About Dispatch",
            "<b>Dispatch 1.0</b><br>Single-account, spreadsheet-driven mailer.<br><br>"
            "Dry run is the default. SQLite is the source of truth during a run; the "
            "workbook is only written on flush, pause, stop or exit.<br><br>"
            "Built with PySide6.",
        )

    # ----------------------------------------------------------- lifecycle
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker and self.worker.is_alive():
            box = QMessageBox(self)
            box.setWindowTitle("A run is in progress")
            box.setText("Pause and close, or stop the run first?")
            pause = box.addButton("Pause and close", QMessageBox.AcceptRole)
            stop = box.addButton("Stop run and close", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() == pause:
                self.worker.pause()
                self.worker.stop()
                self.worker.join(timeout=3)
            elif box.clickedButton() == stop:
                self.worker.stop()
                self.worker.join(timeout=5)
            else:
                event.ignore()
                return
        self.timer.stop()
        self.imap_timer.stop()
        self._save_geometry()
        try:
            self.conn.close()
        except Exception:
            pass
        event.accept()

    def _settings(self) -> QSettings:
        return QSettings("Samlax", "Dispatch")

    def _restore_geometry(self) -> None:
        geometry = self._settings().value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def _save_geometry(self) -> None:
        settings = self._settings()
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("last_config", str(self.cfg_path or ""))


def _dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _ago(when: datetime) -> str:
    now = datetime.now().astimezone()
    if when.tzinfo is None:
        when = when.astimezone()
    delta = max(0, int((now - when).total_seconds()))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    return f"{delta // 3600}h ago"
