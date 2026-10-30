"""ISO 3166 alpha-2 codes as the country names the location parser reads (SPEC.md 5.4).

Some payloads give a country only as a two-letter code. Passed through as written, a bare "CA"
parses as California, so an adapter turns the code into a name first. The names come from the
bundled `iso3166.csv`, the same table `parse_location` reads."""

from __future__ import annotations

import csv
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def _names() -> dict[str, str]:
    names: dict[str, str] = {}
    with files("kestrel.resources").joinpath("iso3166.csv").open(
        "r", encoding="utf-8", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            alias = (row.get("aliases") or "").split("|")[0].strip()
            names[row["alpha_2"].upper()] = alias or row["name"]
    return names


def country_name(value: str | None) -> str | None:
    """A two-letter ISO code as the country's name ("US" is "United States"); anything else,
    including a code the table does not know, is returned as written."""
    if value and len(value) == 2:
        return _names().get(value.upper(), value)
    return value
