"""Background IMAP check so the UI never blocks on the network."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from .. import db
from ..config import FullConfig
from ..mail.monitor import poll_inbox


class InboxCheckThread(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, cfg: FullConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg

    def run(self) -> None:  # pragma: no cover - network
        conn = None
        try:
            conn = db.connect(self.cfg.db_path)
            db.init_db(conn)
            counts = poll_inbox(self.cfg, conn)
            self.done.emit(counts)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
