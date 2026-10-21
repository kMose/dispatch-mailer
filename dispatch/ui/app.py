"""Application bootstrap: theme, single-instance lock, config discovery, onboarding."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QStandardPaths
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from ..config import ConfigError, FullConfig, load_config
from .theme import QSS, light_palette


def _single_instance_lock(app: QApplication) -> QLockFile | None:
    base = QStandardPaths.writableLocation(QStandardPaths.TempLocation)
    lock = QLockFile(str(Path(base) / "dispatch-mailer.lock"))
    # Keep Qt's default staleness check: staleLockTime=0 would make a crashed run lock
    # the app out permanently (the lock file would never be considered stale).
    if not lock.tryLock(100):
        QMessageBox.information(
            None, "Dispatch is already running",
            "Another Dispatch window is open. Close it first — only one process may "
            "write the spreadsheet.",
        )
        return None
    return lock


def _find_config(argv: list[str]) -> Path | None:
    for arg in argv:
        p = Path(arg).expanduser()
        if p.suffix.lower() in (".yaml", ".yml"):
            return p.resolve()
    for candidate in (Path.cwd() / "campaign.yaml", Path(sys.argv[0]).resolve().parent / "campaign.yaml"):
        if candidate.exists():
            return candidate
    return None


def run(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Dispatch")
    app.setOrganizationName("Samlax")
    app.setApplicationDisplayName("Dispatch")
    app.setStyle("Fusion")
    app.setPalette(light_palette())
    app.setStyleSheet(QSS)
    font = QFont("Segoe UI", 9)
    app.setFont(font)

    lock = _single_instance_lock(app)
    if lock is None:
        return 1

    path = _find_config(argv)
    cfg: FullConfig | None = None
    if path and path.exists():
        try:
            cfg = load_config(path)
        except ConfigError as exc:
            QMessageBox.warning(None, "Config error", f"{path}\n\n{exc}")
            cfg = None

    if cfg is None or not cfg.sheet_file.exists():
        from .onboarding import OnboardingWizard
        wizard = OnboardingWizard(initial_config=cfg, initial_path=path)
        if wizard.exec() != OnboardingWizard.Accepted:
            return 0
        cfg = wizard.config
        path = wizard.config_path
        if cfg is None or path is None:
            return 0

    from .main_window import MainWindow
    window = MainWindow(cfg, path)
    window.show()
    code = app.exec()
    try:
        lock.unlock()
    except Exception:
        pass
    return code
