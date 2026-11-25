"""CSV template and import parsing. Pure functions: the route does the database work."""
from __future__ import annotations

import csv
import io

from .repo import Column

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
DELIMITERS = (",", ";", "\t")

FORMAT_HELP = {
    "text": ("Any text", "ISO 9001"),
    "longtext": ("Any text, also several lines", "Audit by external body"),
    "number": ("A number", "12.5"),
    "date": ("Day-month-year: DD-MM-YYYY", "23-09-2026"),
    "select": ("One of the options; a new value is added as a new option", None),
    "checkbox": ("Yes or No (empty = No)", "Yes"),
    "duration": ("Number plus days or weeks", "2 weeks"),
}


ROLE_EXAMPLES = {"center": "Head office", "title": "ISO 9001", "subtitle": "Quality management"}


class ImportError_(Exception):
    """The file as a whole cannot be read (not row errors)."""


def template_csv(columns: list[Column], delimiter: str = ",") -> bytes:
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter).writerow([c.name for c in columns])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel shows diacritics correctly


def decode(raw: bytes) -> tuple[str, bool]:
    """Return (text, was_utf8). Excel's non-UTF-8 'CSV' is read as Windows Central European."""
    try:
        return raw.decode("utf-8-sig"), True
    except UnicodeDecodeError:
        return raw.decode("cp1250", errors="replace"), False


def sniff_delimiter(first_line: str) -> str:
    counts = {d: first_line.count(d) for d in DELIMITERS}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _clean_cell(value: str) -> str:
    value = (value or "").strip()
    # Undo the formula-injection guard our own export adds ('=..., '+..., '-..., '@...)
    if len(value) > 1 and value[0] == "'" and value[1] in "=+-@":
        value = value[1:]
    return value


def parse(raw: bytes, columns: list[Column]) -> tuple[list[tuple[int, dict]], list[str]]:
    """Parse an uploaded CSV.

    Returns (rows, warnings). Each row is (line_number, {column_key: raw_value}) ready for
    repo.build_data. Problems with the file as a whole raise ImportError_.
    """
    if len(raw) > MAX_BYTES:
        raise ImportError_(f"The file is too large (max {MAX_BYTES // 1024 // 1024} MB).")
    text, was_utf8 = decode(raw)
    warnings = []
    if not was_utf8:
        warnings.append("The file was not saved as UTF-8. If special letters (ă, ș, ț…) look wrong, "
                        "save it in Excel as “CSV UTF-8” and import again.")
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise ImportError_("The file is empty. Use the template: the first row must contain the column names.")
    reader = csv.reader(io.StringIO(text), delimiter=sniff_delimiter(lines[0]))
    header = [_clean_cell(h) for h in next(reader)]

    by_name = {c.name.strip().lower(): c for c in columns}
    mapping: dict[int, Column] = {}
    unknown = []
    for idx, name in enumerate(header):
        if not name:
            continue
        col = by_name.get(name.lower())
        if col is None:
            unknown.append(name)
        elif col in mapping.values():
            raise ImportError_(f"The column “{name}” appears twice in the first row.")
        else:
            mapping[idx] = col
    if not mapping:
        raise ImportError_("None of the column names in the first row match this table. "
                           "Download the template and keep its first row unchanged.")
    if unknown:
        warnings.append("Ignored columns that do not exist in the table: " + ", ".join(f"“{u}”" for u in unknown) + ".")
    missing = [c.name for c in columns if c not in mapping.values()]
    if missing:
        warnings.append("Not in the file (left empty or default): " + ", ".join(f"“{m}”" for m in missing) + ".")

    rows: list[tuple[int, dict]] = []
    for line_no, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue  # skip empty lines
        if len(rows) >= MAX_ROWS:
            raise ImportError_(f"Too many rows (max {MAX_ROWS} per import). Split the file.")
        values: dict = {}
        for idx, col in mapping.items():
            cell = _clean_cell(cells[idx]) if idx < len(cells) else ""
            if cell == "" and col.default is not None:
                values[col.key] = col.default  # e.g. notification window: 1 week
            elif col.type == "checkbox":
                values[col.key] = cell.lower() in ("yes", "y", "true", "1", "x", "✓", "da")
            else:
                values[col.key] = cell
        for col in columns:  # columns missing from the file still get their default
            if col not in mapping.values() and col.default is not None:
                values[col.key] = col.default
        rows.append((line_no, values))
    if not rows:
        raise ImportError_("The file has column names but no entries below them.")
    return rows, warnings
