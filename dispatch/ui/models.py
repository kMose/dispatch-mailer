"""Table models for the contact grid (virtualised, sortable, filterable)."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor, QFont

COLUMNS = ["#", "Sheet row", "Name", "Email", "Last advertised",
           "Eligible", "Status", "Sent at", "Error"]

ROW_COLORS = {
    "sending": QColor("#fff4d6"),
    "queued": QColor("#f4f7fb"),
    "sent": QColor("#eef8f1"),
    "failed_hard": QColor("#fdeceb"),
    "failed_soft": QColor("#fff6e5"),
    "unknown": QColor("#f3e8ff"),
}


class ContactTableModel(QAbstractTableModel):
    def __init__(self, rows: list[dict[str, Any]] | None = None):
        super().__init__()
        self._rows: list[dict[str, Any]] = rows or []

    # -- data plumbing ---------------------------------------------------
    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self._rows

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = COLUMNS[index.column()]

        if role == Qt.DisplayRole:
            value = row.get(_key_for(col), "")
            if col == "Status" and value == "sent" and row.get("dry"):
                return "sent (dry)"
            return "" if value is None else str(value)
        if role == Qt.UserRole:
            value = row.get(_key_for(col), "")
            if col == "#":
                return row.get("idx", 0)
            if col == "Sheet row":
                return row.get("sheet_row") or 0
            return "" if value is None else str(value).lower()
        if role == Qt.ToolTipRole:
            return row.get("error") or row.get("eligible_detail") or None
        if role == Qt.BackgroundRole:
            return QBrush(ROW_COLORS.get(row.get("status", ""), QColor("transparent")))
        if role == Qt.FontRole:
            if row.get("status") in ("failed_hard", "unknown"):
                f = QFont()
                f.setBold(True)
                return f
        if role == Qt.ForegroundRole:
            if row.get("status") == "failed_hard":
                return QBrush(QColor("#a02020"))
            if row.get("status") == "sent":
                return QBrush(QColor("#1c6b3c"))
            if str(row.get("status", "")).startswith("skipped"):
                return QBrush(QColor("#7c8494"))
        return None


def _key_for(column: str) -> str:
    return {
        "#": "idx",
        "Sheet row": "sheet_row",
        "Name": "name",
        "Email": "email",
        "Last advertised": "last_advertised",
        "Eligible": "eligible",
        "Status": "status",
        "Sent at": "sent_at",
        "Error": "error",
    }[column]


class ContactFilterProxy(QSortFilterProxyModel):
    """Filter chips: all / eligible / sent / skipped / failed / replied."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind = "all"
        self.setSortRole(Qt.UserRole)
        self.setDynamicSortFilter(True)

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if model is None:
            return True
        row = model.rows[source_row]
        status = str(row.get("status", ""))
        kind = self.kind
        if kind == "all":
            return True
        if kind == "eligible":
            return row.get("eligible") == "Yes"
        if kind == "sent":
            return status == "sent"
        if kind == "skipped":
            return status.startswith("skipped")
        if kind == "failed":
            return status in ("failed_hard", "failed_soft", "unknown")
        if kind == "replied":
            return bool(row.get("replied"))
        return True
