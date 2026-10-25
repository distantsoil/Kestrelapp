"""The operator's name as two fields (D-UI-117): `first_name` (required, greets the operator) and
`last_name` (may be empty). `full_name` is derived from them and is what a resume header prints.

A legacy profile carries only `full_name`. `normalize_name_fields` reads it into the two fields
without discarding it, so an old profile.yml keeps loading and its original text survives. The
split is on the FIRST space (D-UI-117: "Jordan A. Rivera" is "Jordan" and "A. Rivera"); the
operator can correct it on the About you tab. A one-word name leaves the last name empty."""

from __future__ import annotations

from typing import Any


def clean(value: Any) -> str:
    """One line of text with runs of whitespace collapsed; None becomes empty."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def split_full_name(full_name: Any) -> tuple[str, str]:
    """('first', 'last') from one full name, split on the first space."""
    text = clean(full_name)
    if not text:
        return "", ""
    first, sep, last = text.partition(" ")
    return first, last if sep else ""


def join_full_name(first_name: Any, last_name: Any) -> str:
    """`first_name last_name`, trimmed; this is the resume header's name."""
    return " ".join(p for p in (clean(first_name), clean(last_name)) if p)


def normalize_name_fields(block: dict) -> dict:
    """The track block with `first_name`, `last_name` and `full_name` consistent.

    * `first_name` set: it and `last_name` win and `full_name` is re-derived from them.
    * Only a legacy `full_name`: split it (first space) into the two fields; the original
      `full_name` text is kept as written.
    * No name at all: returned unchanged (no blank or fictional value is invented).
    Returns a new dict; the input is not modified."""
    out = dict(block)
    first = clean(out.get("first_name"))
    last = clean(out.get("last_name"))
    legacy = clean(out.get("full_name"))
    if first:
        out["first_name"], out["last_name"] = first, last
        out["full_name"] = join_full_name(first, last)
    elif legacy:
        out["first_name"], out["last_name"] = split_full_name(legacy)
    return out


def greeting_name(block: dict | None) -> str:
    """What to call the operator: the first name, from a track block (a legacy full_name
    included). Empty when the profile has no name."""
    if not block:
        return ""
    return clean(normalize_name_fields(dict(block)).get("first_name"))
