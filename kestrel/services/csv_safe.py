"""Cells for a CSV the operator will open in a spreadsheet (D-SEC-10).

A posting title, a company name or a note comes from the web, and a cell that starts with `=`,
`+`, `-`, `@`, a tab or a carriage return is run as a formula by Excel, Numbers and Sheets. The
fix is the standard one: a single quote in front, which the spreadsheet shows as nothing. Only
text is touched; a number stays a number."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_TRIGGERS):
        return "'" + value
    return value


def safe_row(row: Iterable[Any]) -> list[Any]:
    return [safe_cell(v) for v in row]


def safe_rows(rows: Iterable[Iterable[Any]]) -> list[list[Any]]:
    return [safe_row(r) for r in rows]


def safe_record(record: dict[str, Any]) -> dict[str, Any]:
    return {k: safe_cell(v) for k, v in record.items()}
