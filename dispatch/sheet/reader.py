"""Tolerant spreadsheet reader.

Every contact keeps its 1-based sheet row and every unlisted column verbatim in
`extra`, so the writer can round-trip a workbook without touching what it does not
understand.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel

from ..engine.eligibility import is_truthy


class SheetError(Exception):
    pass


def norm_header(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[\s_\-]+", "", str(value).strip().lower())


KNOWN_HEADERS = {
    "firstname": "first_name",
    "lastname": "last_name",
    "email": "email",
    "emailaddress": "email",
    "company": "company",
    "organisation": "company",
    "organization": "company",
    "lastadvertised": "last_advertised",
    "replied": "replied",
    "ignore": "ignored",
    "lastcontacted": "last_contacted",
    "status": "status",
    "lasterror": "last_error",
    "attempts": "attempts",
}

DATE_COLUMNS = {"last_advertised", "last_contacted"}

# Salutation is kept in `extra` so it survives round-trips without a schema column, but
# these header spellings all map to the canonical `{Salutation}` placeholder.
SALUTATION_HEADERS = {"salutation", "honorific", "prefix", "nameprefix"}

_SALUTATION_CANON = {
    "mr": "Mr.", "mrs": "Mrs.", "ms": "Ms.", "miss": "Miss", "mx": "Mx.",
    "dr": "Dr.", "prof": "Prof.", "sir": "Sir", "madam": "Madam",
    "dame": "Dame", "rev": "Rev.",
}

_ISO_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?$")
_DMY_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$")
_YYYYMMDD_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_TEXT_DMY_RE = re.compile(r"^(\d{1,2})[ -]([A-Za-z]{3,9})[ -](\d{2,4})$")
_TEXT_MDY_RE = re.compile(r"^([A-Za-z]{3,9})[ -](\d{1,2}),?[ -](\d{2,4})$")


@dataclass
class Ambiguity:
    row: int
    column: str
    raw: str
    detail: str


@dataclass
class Contact:
    email: str
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    last_advertised: date | None = None
    replied: bool = False
    ignored: bool = False
    last_contacted: date | None = None
    sheet_row: int | None = None
    status: str | None = None
    last_error: str | None = None
    attempts: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return " ".join(x for x in (self.first_name, self.last_name) if x).strip()

    def as_template_values(self) -> dict[str, Any]:
        """Spelling variants so {First Name}, {first_name} and {FirstName} all work."""
        values: dict[str, Any] = dict(self.extra or {})
        base = {
            "Email": self.email,
            "FirstName": self.first_name or "",
            "LastName": self.last_name or "",
            "Company": self.company or "",
            "LastAdvertised": self.last_advertised.isoformat() if self.last_advertised else "",
            "LastContacted": self.last_contacted.isoformat() if self.last_contacted else "",
            "sheet_row": self.sheet_row or "",
        }
        values.update({k: v for k, v in base.items() if v not in (None, "")})
        for key, value in list(values.items()):
            if value is None:
                values[key] = ""
            # alias without spaces/underscores
            alias = re.sub(r"[\s_]+", "", str(key))
            values.setdefault(alias, values[key])
        return values


@dataclass
class SheetData:
    path: Path
    tab: str
    headers: list[str]
    contacts: list[Contact]
    ambiguous: list[Ambiguity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.contacts)

    def header_names(self) -> list[str]:
        return list(self.headers)

    def sample_values(self) -> dict[str, Any]:
        if not self.contacts:
            return {}
        return self.contacts[0].as_template_values()


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _expand_year(year: int) -> int:
    if year < 100:
        return 2000 + year if year < 70 else 1900 + year
    return year


def _valid(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date_text(value: str, date_order: str = "DMY") -> tuple[date | None, bool, str]:
    """Return (date, ambiguous, note).

    `ISO` mode refuses slash-format dates outright rather than guessing; `DMY`/`MDY`
    parse them but flag genuinely ambiguous ones (both fields <= 12 and different).
    """
    s = value.strip()
    if not s:
        return None, False, ""

    m = _ISO_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        parsed = _valid(y, mo, d)
        return parsed, False, "" if parsed else "invalid calendar date"

    m = _YYYYMMDD_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        parsed = _valid(y, mo, d)
        return parsed, False, "" if parsed else "invalid calendar date"

    m = _TEXT_DMY_RE.match(s)
    if m:
        d, mon, y = m.groups()
        month = _MONTH_NAMES.get(mon[:3].lower())
        if month:
            parsed = _valid(_expand_year(int(y)), month, int(d))
            return parsed, False, "" if parsed else "invalid calendar date"

    m = _TEXT_MDY_RE.match(s)
    if m:
        mon, d, y = m.groups()
        month = _MONTH_NAMES.get(mon[:3].lower())
        if month:
            parsed = _valid(_expand_year(int(y)), month, int(d))
            return parsed, False, "" if parsed else "invalid calendar date"

    m = _DMY_RE.match(s)
    if m:
        a, b, c = (int(x) for x in m.groups())
        year = _expand_year(c)
        if date_order in ("ISO", "auto"):
            return None, True, "non-ISO date under ISO order"
        day, month = (a, b) if date_order == "DMY" else (b, a)
        parsed = _valid(year, month, day)
        ambiguous = a <= 12 and b <= 12 and a != b
        note = ""
        if parsed is None:
            # Perhaps the configured order is wrong; record why instead of guessing.
            alt = _valid(year, a, b) if date_order == "DMY" else _valid(year, b, a)
            if alt is not None:
                note = (f"{s!r} is not valid as {date_order}; "
                        f"it would be {alt.isoformat()} in the other order")
                return None, True, note
            return None, False, "unparseable date"
        return parsed, ambiguous, note

    return None, False, "unrecognised date format"


def parse_date_cell(value: Any, date_order: str = "DMY") -> tuple[date | None, bool, str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, False, ""
    if isinstance(value, datetime):
        return value.date(), False, ""
    if isinstance(value, date):
        return value, False, ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 1 <= float(value) <= 2958465:
            try:
                return from_excel(value).date(), False, ""
            except Exception:  # pragma: no cover - openpyxl edge cases
                return None, False, "invalid Excel serial"
        return None, False, "unrecognised numeric date"
    return parse_date_text(str(value), date_order)


def _int_or_none(value: Any) -> int | None:
    if _blank(value):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def read_sheet(path: str | Path, tab: str | None = None,
               date_order: str = "DMY") -> SheetData:
    p = Path(path)
    if not p.exists():
        raise SheetError(f"spreadsheet not found: {p}")
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        raise SheetError(f"expected an .xlsx workbook, got {p.suffix}")
    try:
        wb = load_workbook(p, data_only=True, read_only=False)
    except Exception as exc:
        raise SheetError(f"cannot open {p.name}: {exc}") from exc

    try:
        if tab:
            if tab not in wb.sheetnames:
                raise SheetError(f"tab {tab!r} not found (sheets: {', '.join(wb.sheetnames)})")
            ws = wb[tab]
        else:
            ws = wb.active
        tab_name = ws.title

        # Locate the header row (first row with any non-blank cell).
        header_row = None
        headers: list[str] = []
        for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=min(ws.max_row, 20),
                                                 values_only=True), start=1):
            if any(not _blank(v) for v in row):
                header_row = r_idx
                headers = ["" if _blank(v) else str(v).strip() for v in row]
                break
        if header_row is None:
            raise SheetError("spreadsheet is empty")

        col_map: dict[int, str] = {}
        for idx, name in enumerate(headers, start=1):
            key = KNOWN_HEADERS.get(norm_header(name))
            if key and key not in col_map.values():
                col_map[idx] = key

        if "email" not in col_map.values():
            raise SheetError("no 'Email' column found in the header row")

        warnings: list[str] = []
        ambiguous: list[Ambiguity] = []
        contacts: list[Contact] = []
        empty_email_rows = 0

        for r_idx, row in enumerate(
            ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1
        ):
            if all(_blank(v) for v in row):
                continue
            values: dict[str, Any] = {}
            extra: dict[str, Any] = {}
            for idx, raw in enumerate(row, start=1):
                header = headers[idx - 1] if idx - 1 < len(headers) else f"Column{idx}"
                key = col_map.get(idx)
                if key:
                    values[key] = raw
                elif not _blank(raw):
                    if norm_header(header) in SALUTATION_HEADERS:
                        extra["Salutation"] = normalize_salutation(raw)
                    else:
                        extra[header or f"Column{idx}"] = _raw_text(raw)

            email = _raw_text(values.get("email")) or ""
            if not email:
                empty_email_rows += 1

            parsed_dates: dict[str, date | None] = {}
            for field_name in DATE_COLUMNS:
                if field_name not in values:
                    parsed_dates[field_name] = None
                    continue
                parsed, is_ambiguous, note = parse_date_cell(
                    values.get(field_name), date_order
                )
                if is_ambiguous:
                    ambiguous.append(Ambiguity(
                        r_idx, field_name,
                        _raw_text(values.get(field_name)),
                        note or "day/month order is ambiguous",
                    ))
                elif note and not _blank(values.get(field_name)):
                    warnings.append(f"row {r_idx}: {field_name}: {note}")
                parsed_dates[field_name] = parsed

            contact = Contact(
                email=email,
                first_name=_raw_text(values.get("first_name")) or None,
                last_name=_raw_text(values.get("last_name")) or None,
                company=_raw_text(values.get("company")) or None,
                last_advertised=parsed_dates.get("last_advertised"),
                last_contacted=parsed_dates.get("last_contacted"),
                replied=is_truthy(values.get("replied")),
                ignored=is_truthy(values.get("ignored")),
                sheet_row=r_idx,
                status=_raw_text(values.get("status")) or None,
                last_error=_raw_text(values.get("last_error")) or None,
                attempts=_int_or_none(values.get("attempts")) or 0,
                extra=extra,
            )
            contacts.append(contact)

        # openpyxl with data_only=True returns None for a formula whose result has never
        # been cached (a sheet edited outside Excel). That looks like "never advertised"
        # and could cause a duplicate send, so check the raw cells too.
        formula_rows = _formula_date_rows(p, tab_name, header_row,
                                          [idx for idx, key in col_map.items()
                                           if key in DATE_COLUMNS])
        if formula_rows:
            warnings.append(
                f"{len(formula_rows)} row(s) have formulas in a date column "
                f"(rows {', '.join(str(r) for r in formula_rows[:10])}"
                f"{'…' if len(formula_rows) > 10 else ''}); open the workbook in Excel "
                f"and save once so their values are cached, or replace them with dates."
            )

        if empty_email_rows:
            warnings.append(f"{empty_email_rows} row(s) have no email address")
        if not contacts:
            warnings.append("no data rows found below the header")
        return SheetData(p, tab_name, headers, contacts, ambiguous, warnings)
    finally:
        wb.close()


def _formula_date_rows(path: Path, tab: str, header_row: int,
                       columns: list[int], max_rows: int = 20000) -> list[int]:
    """Row numbers whose date cell holds a formula (second, cheap pass)."""
    if not columns:
        return []
    try:
        wb = load_workbook(path, data_only=False, read_only=False)
    except Exception:
        return []
    try:
        ws = wb[tab]
        found: list[int] = []
        last = min(ws.max_row, header_row + max_rows)
        for row in range(header_row + 1, last + 1):
            for col in columns:
                value = ws.cell(row=row, column=col).value
                if isinstance(value, str) and value.startswith("="):
                    found.append(row)
                    break
        return found
    except Exception:
        return []
    finally:
        wb.close()


def _raw_text(value: Any) -> str:
    """Normalise a cell: trim, collapse runs of spaces, drop invisible characters.

    Spreadsheets routinely arrive with non-breaking spaces, tabs, stray newlines and
    double spaces around names. Collapsing them here means `{FirstName}` can never render
    as `John   ` or `John\\u00a0` and greetings do not gain a phantom gap.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ")
    text = re.sub(r"[\t\r\n\v\f]+", " ", text)
    text = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060]", "", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def normalize_salutation(value: Any) -> str:
    """`Mr`, `MR`, `Mr.` -> `Mr.`; unknown values are only cleaned, never invented."""
    text = _raw_text(value)
    key = text.rstrip(".").strip().lower()
    return _SALUTATION_CANON.get(key, text)
