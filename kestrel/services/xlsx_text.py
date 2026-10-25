"""Read the first sheet of an .xlsx file as CSV text, for Import past applications (SPEC.md 5.6:
"choose a CSV, spreadsheet or Markdown file").

An .xlsx file is a zip of XML. Only what an application tracker needs is read: the first
worksheet's cells as text (shared strings, inline strings, numbers; a date cell comes out as the
number Excel stores, converted to an ISO date when its style says it is a date). Formulas are
not run, macros are never touched, and nothing is fetched. The zip is size-checked and the XML is
refused if it declares a DOCTYPE or an ENTITY, so a crafted file cannot expand into memory.

No third-party package: stdlib `zipfile` and `xml.etree` only."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

from kestrel.errors import UserFacingError

MAX_MEMBER = 8 * 1024 * 1024  # bytes of any one XML part after unzipping
MAX_COLUMNS = 16_384
MAX_CELLS = 1_000_000
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_COL = re.compile(r"^([A-Z]+)")
_EPOCH = date(1899, 12, 30)


class SpreadsheetError(UserFacingError, ValueError):
    """The file is not a spreadsheet Kestrel can read."""


def looks_like_xlsx(data: bytes) -> bool:
    return data[:4] == b"PK\x03\x04"


def _part(z: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_MEMBER:
        raise SpreadsheetError("That spreadsheet is too large to read here. Save it as a CSV.")
    raw = z.read(name)
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in raw.lower():
        raise SpreadsheetError("That spreadsheet could not be read safely. Save it as a CSV.")
    return raw


def _col_index(ref: str) -> int:
    m = _COL.match(ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
        if n > MAX_COLUMNS:
            raise SpreadsheetError(
                "That spreadsheet has too many columns to read here. Save it as a CSV."
            )
    return n - 1


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(t.text or "" for t in node.iter(f"{{{_NS['m']}}}t"))


def _date_styles(z: zipfile.ZipFile) -> set[int]:
    """Cell style ids whose number format is a date (built-in 14 to 22, or a custom code with a
    year or a day in it)."""
    raw = _part(z, "xl/styles.xml")
    if raw is None:
        return set()
    root = ET.fromstring(raw)
    custom: dict[int, str] = {}
    for nf in root.iterfind(".//m:numFmts/m:numFmt", _NS):
        try:
            custom[int(nf.get("numFmtId", "-1"))] = (nf.get("formatCode") or "").lower()
        except ValueError:
            continue
    out: set[int] = set()
    for i, xf in enumerate(root.iterfind(".//m:cellXfs/m:xf", _NS)):
        try:
            fid = int(xf.get("numFmtId", "0"))
        except ValueError:
            continue
        code = custom.get(fid, "")
        if 14 <= fid <= 22 or any(c in code for c in ("yy", "dd", "mmm")):
            out.add(i)
    return out


def to_csv(data: bytes) -> str:
    """The first worksheet as CSV text. Raises SpreadsheetError with a plain reason."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SpreadsheetError("That file is not a spreadsheet Kestrel can read.") from exc
    with z:
        names = z.namelist()
        sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            raise SpreadsheetError("That file has no sheet to read.")
        shared: list[str] = []
        raw_ss = _part(z, "xl/sharedStrings.xml")
        if raw_ss is not None:
            for si in ET.fromstring(raw_ss).iterfind("m:si", _NS):
                shared.append(_text(si).strip())
        dates = _date_styles(z)
        sheet_raw = _part(z, sheets[0])
        if sheet_raw is None:
            raise SpreadsheetError("That file has no sheet to read.")
        root = ET.fromstring(sheet_raw)
        out = io.StringIO()
        writer = csv.writer(out)
        row_count = 0
        output_bytes = 0
        total_cells = 0
        for row in root.iterfind(".//m:sheetData/m:row", _NS):
            cells: dict[int, str] = {}
            cell_sizes: dict[int, int] = {}
            row_value_bytes = 0
            width = 0
            for c in row.iterfind("m:c", _NS):
                idx = _col_index(c.get("r", ""))
                kind = c.get("t", "n")
                v = c.find("m:v", _NS)
                if kind == "s" and v is not None and (v.text or "").isdigit():
                    i = int(v.text or "0")
                    value = shared[i] if i < len(shared) else ""
                elif kind == "inlineStr":
                    value = _text(c.find("m:is", _NS))
                elif v is not None and v.text is not None:
                    value = v.text
                    try:
                        style = int(c.get("s", "-1"))
                    except ValueError:
                        style = -1
                    if kind == "n" and style in dates:
                        try:
                            value = (_EPOCH + timedelta(days=int(float(value)))).isoformat()
                        except (ValueError, OverflowError):
                            pass
                else:
                    value = ""
                value = value.strip()
                value_bytes = len(value.encode("utf-8")) + value.count('"')
                if any(char in value for char in ',"\r\n'):
                    value_bytes += 2
                row_value_bytes += value_bytes - cell_sizes.get(idx, 0)
                width = max(width, idx + 1)
                if output_bytes + row_value_bytes + width + 1 > MAX_OUTPUT_BYTES:
                    raise SpreadsheetError(
                        "That spreadsheet is too large to read here. Save it as a CSV."
                    )
                cells[idx] = value
                cell_sizes[idx] = value_bytes
            if cells:
                width = max(cells) + 1
                total_cells += width
                if total_cells > MAX_CELLS:
                    raise SpreadsheetError(
                        "That spreadsheet has too many cells to read here. Save it as a CSV."
                    )
                row_bytes = row_value_bytes + width + 1
                if width == 1 and not cells.get(0):
                    row_bytes += 2
                output_bytes += row_bytes
                if output_bytes > MAX_OUTPUT_BYTES:
                    raise SpreadsheetError(
                        "That spreadsheet is too large to read here. Save it as a CSV."
                    )
                writer.writerow([cells.get(column, "") for column in range(width)])
                row_count += 1
    if row_count < 2:
        raise SpreadsheetError("Need a header row and at least one data row.")
    return out.getvalue()
