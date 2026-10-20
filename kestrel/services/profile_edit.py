"""Edit the operator's profile.yml from the Profile page (D-UI-21).

The Tier 0 filters, Tier 2 context, the stories and the negotiation scripts all read
`profile.yml` through `kestrel.pipeline.profile.load_profile`. Until 0.3.0 the only way to set
the numbers those rules use (comp floor, seniority floor, excluded titles, locations) was to
edit the file by hand. This module is the write side: one section of one track at a time,
merged into the file that is already there, written atomically with a .bak of the previous
file, and confirmed through the real loader before the write is called done.

What the forms edit is exactly what the code reads (grep `profile.` in filters.yml and the
pipeline): the personal block, `employment_terms`, `consulting_terms` and `geo_rules`, plus the
must-have and must-avoid lists. `narrative` and `proof_points` are left as they are: they are
long-form text the operator writes once, not settings.

When there is no profile.yml yet (or a tab has never been saved), the forms start blank: the
shipped example's terms are offered only as placeholder hints (`ProfileState.examples`), never as
form values, so saving an untouched tab stores nothing that filters (release round review,
finding 1). The example's fictional name, narrative and proof points are never copied into the
operator's file."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from kestrel import paths
from kestrel.db.enums import TRACKS
from kestrel.errors import UserFacingError, plain_os_reason, yaml_where

log = structlog.get_logger()

SECTIONS = ("about", "employment", "consulting", "location")

# Personal fields on each track block (the "About you" tab). Lists are one item per line.
ABOUT_TEXT = ("full_name", "email", "phone", "location", "headline", "work_auth")
ABOUT_LISTS = ("must_have", "must_avoid")
# is_active is not offered: nothing in the pipeline reads it yet (reviewer finding 4).
ABOUT_FLAGS = ("requires_sponsorship",)

EMPLOYMENT_LISTS = ("target_titles", "anti_titles", "excluded_employment_types")
EMPLOYMENT_INTS = ("comp_floor_usd", "comp_target_usd")
# The words seniority_rank() recognizes, lowest first, as the floor select offers them.
SENIORITY_FLOORS = (
    ("", "Any level (no floor)"),  # the permissive default: nothing is hidden for its level
    ("lead", "Lead"),
    ("manager", "Manager"),
    ("senior manager", "Senior manager"),
    ("director", "Director or Head of"),
    ("senior director", "Senior director"),
    ("vp", "Vice president"),
    ("svp", "Senior vice president"),
    ("chief technology officer", "C-level"),
)
EMPLOYMENT_TYPES = ("internship", "part_time", "contract", "temporary")

CONSULTING_LISTS = ("service_lines", "target_org_size", "target_sectors", "excluded_sectors")
CONSULTING_INTS = ("day_rate_floor", "day_rate_target", "min_engagement_days", "max_concurrent")
CONSULTING_TEXT = ("travel_tolerance",)

GEO_LISTS = ("remote_country_allowlist", "onsite_states")
GEO_FLAGS = ("remote_ok", "hybrid_requires_metro", "relocation_acceptable")
GEO_INTS = ("travel_days_per_month_max",)

# The term sections a track block can hold, and what an unsaved one starts as: nothing that
# filters. remote_ok is a preference the scoring model reads, not a filter.
TERM_SECTIONS = {
    "employment": ("employment_terms", "geo_rules"),
    "consulting": ("consulting_terms", "geo_rules"),
}
BLANK_TERMS: dict[str, dict] = {"geo_rules": {"remote_ok": True}}

PROFILE_NAME = "profile.yml"
PROFILE_BAK = "profile.yml.bak"
_INCOMING = "profile.yml.editing"


class ProfileEditError(UserFacingError, ValueError):
    """A save that must not happen: the form was unusable, or the written file failed the
    loader (in which case the previous file is back in place)."""


@dataclass
class ProfileState:
    """What the page shows: the operator's blocks (or the example's terms with blank personal
    fields), and whether an operator file exists at all."""

    exists: bool
    tracks: dict[str, dict] = field(default_factory=dict)
    # Set when the file exists but cannot be read or parsed: then no tab may save, because a
    # save would replace the operator's file with one section (reviewer finding 3).
    problem: str | None = None
    # Tracks that have a block in the operator's file (the others show the example's terms).
    present: tuple[str, ...] = ()
    # Per track, the term sections (employment_terms, consulting_terms, geo_rules) that are
    # NOT in the file yet (D-UI-25). They start blank; the example only supplies placeholders.
    from_example: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Per track, the shipped example's terms, for placeholder hints only (never form values).
    examples: dict[str, dict] = field(default_factory=dict)


def profile_path() -> Path:
    return paths.config_dir() / PROFILE_NAME


def _read(path: Path | None) -> dict:
    """The file as a mapping. Raises ProfileEditError when it exists but cannot be read or
    parsed: that is never "no profile", it is a file the operator must look at."""
    if path is None or not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("profile_read_failed", error_type=type(exc).__name__, error=str(exc))
        raise ProfileEditError(f"{path.name} could not be read: {plain_os_reason(exc)}") from exc
    except yaml.YAMLError as exc:
        log.warning("profile_yaml_invalid", error=str(exc))
        raise ProfileEditError(f"{path.name} is not valid YAML{yaml_where(exc)}.") from exc
    return data if isinstance(data, dict) else {}


def _read_example(name: str) -> dict:
    try:
        return _read(paths.config_file(name))
    except ProfileEditError as exc:  # a broken shipped example is a packaging bug
        log.warning("profile_example_unreadable", error=str(exc))
        return {}


def _example_terms(track: str) -> dict:
    """The shipped example's TERMS for a track, used only as placeholder hints."""
    block = _read_example("profile.example.yml").get(track)
    if not isinstance(block, dict):
        return {}
    keep = {}
    for key in ("employment_terms", "consulting_terms", "geo_rules"):
        if isinstance(block.get(key), dict):
            keep[key] = block[key]
    return keep


def about_missing(state: ProfileState | None = None) -> list[str]:
    """Tracks whose About you tab has no name yet: every track with a block in the file, or
    the employment track when there is no file at all."""
    state = state or load_state()
    tracks = state.present or ("employment",)
    return [t for t in tracks if not str(state.tracks[t].get("full_name") or "").strip()]


def load_state() -> ProfileState:
    problem: str | None = None
    try:
        data = _read(profile_path())
    except ProfileEditError as exc:
        data, problem = {}, str(exc)
    exists = bool(data) or problem is not None
    tracks: dict[str, dict] = {}
    present: list[str] = []
    from_example: dict[str, tuple[str, ...]] = {}
    examples: dict[str, dict] = {}
    for track in TRACKS:
        block = data.get(track)
        example = _example_terms(track)
        if isinstance(block, dict):
            present.append(track)
            block = dict(block)
        else:
            block = {"track": track, "is_active": True}
        # A section the file does not have yet starts BLANK (release round review, finding 1):
        # the example's values are shown only as placeholders, so an untouched tab saved as is
        # filters nothing. The tab says it is not saved yet (D-UI-25).
        missing = tuple(
            k for k in TERM_SECTIONS.get(track, ()) if not isinstance(block.get(k), dict)
        )
        for key in missing:
            block[key] = dict(BLANK_TERMS.get(key, {}))
        from_example[track] = missing
        examples[track] = example
        tracks[track] = block
    return ProfileState(
        exists=exists, tracks=tracks, problem=problem, present=tuple(present),
        from_example=from_example, examples=examples,
    )


# ------------------------------------------------------------------------------- form parsing
def lines(value: str | None) -> list[str]:
    """A textarea with one item per line (commas are allowed inside an item)."""
    return [ln.strip() for ln in (value or "").splitlines() if ln.strip()]


def _int(value: str | None, name: str) -> int | None:
    raw = (value or "").replace(",", "").replace("$", "").strip()
    if not raw:
        return None
    try:
        n = int(float(raw))
    except (ValueError, OverflowError) as exc:
        raise ProfileEditError(f"{name} must be a whole number, not {value!r}.") from exc
    if n < 0:
        raise ProfileEditError(f"{name} cannot be negative.")
    return n


def _flag(form: Any, name: str) -> bool:
    return str(form.get(name) or "") in ("on", "true", "1")


def parse_metros(value: str | None) -> list[dict]:
    """One metro per line: `Austin: Round Rock, Cedar Park`. The name is always an alias too,
    since `geo_allows` matches aliases only."""
    out: list[dict] = []
    for ln in lines(value):
        name, _, rest = ln.partition(":")
        name = name.strip()
        if not name:
            continue
        aliases = [a.strip() for a in rest.split(",") if a.strip()]
        if name not in aliases:
            aliases.insert(0, name)
        out.append({"name": name, "aliases": aliases})
    return out


def metros_text(block: dict) -> str:
    geo = block.get("geo_rules") if isinstance(block.get("geo_rules"), dict) else {}
    out = []
    for m in geo.get("onsite_metros") or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        aliases = [a for a in (m.get("aliases") or []) if a != m["name"]]
        out.append(f"{m['name']}: {', '.join(aliases)}" if aliases else str(m["name"]))
    return "\n".join(out)


def section_from_form(section: str, form: Any) -> dict:
    """The fields of one section as they will sit in the track block. Raises ProfileEditError
    on a value that cannot be stored."""
    if section == "about":
        out: dict[str, Any] = {k: (str(form.get(k) or "").strip()) for k in ABOUT_TEXT}
        for k in ABOUT_LISTS:
            out[k] = lines(form.get(k))
        for k in ABOUT_FLAGS:
            out[k] = _flag(form, k)
        return out
    if section == "employment":
        terms: dict[str, Any] = {k: lines(form.get(k)) for k in EMPLOYMENT_LISTS[:2]}
        terms["excluded_employment_types"] = [
            t for t in EMPLOYMENT_TYPES if _flag(form, f"type_{t}")
        ]
        floor = str(form.get("seniority_floor") or "").strip()
        if floor not in {k for k, _ in SENIORITY_FLOORS}:
            raise ProfileEditError("Pick a seniority floor from the list.")
        terms["seniority_floor"] = floor
        for k in EMPLOYMENT_INTS:
            terms[k] = _int(form.get(k), k.replace("_", " "))  # None clears: no floor
        return {"employment_terms": terms}
    if section == "consulting":
        terms = {k: lines(form.get(k)) for k in CONSULTING_LISTS}
        for k in CONSULTING_INTS:
            terms[k] = _int(form.get(k), k.replace("_", " "))
        for k in CONSULTING_TEXT:
            terms[k] = str(form.get(k) or "").strip()
        return {"consulting_terms": terms}
    if section == "location":
        geo: dict[str, Any] = {k: _flag(form, k) for k in GEO_FLAGS}
        for k in GEO_LISTS:
            geo[k] = lines(form.get(k))
        geo["onsite_metros"] = parse_metros(form.get("onsite_metros"))
        for k in GEO_INTS:
            geo[k] = _int(form.get(k), k.replace("_", " "))
        return {"geo_rules": geo}
    raise ProfileEditError(f"Unknown section {section!r}.")


# ------------------------------------------------------------------------------------ writing
def save_section(track: str, section: str, values: dict) -> Path:
    """Merge one section into the track's block and write profile.yml atomically. A first save
    creates the file with only this track block (the other track stays absent until it is
    saved, so Today keeps saying that track has no profile). The previous file is kept as
    profile.yml.bak and restored if the loader refuses the result."""
    from kestrel.pipeline.profile import load_profile

    if track not in TRACKS:
        raise ProfileEditError(f"Unknown track {track!r}.")
    paths.ensure_dirs()
    target = profile_path()
    data = _read(target)  # raises on an unreadable file: never overwrite what cannot be read
    block = data.get(track) if isinstance(data.get(track), dict) else {"track": track}
    block = dict(block)
    block.setdefault("track", track)
    block.setdefault("is_active", True)
    for key, val in values.items():
        if isinstance(val, dict) and isinstance(block.get(key), dict):
            merged = dict(block[key])
            merged.update(val)
            block[key] = merged
        else:
            block[key] = val
    data[track] = block

    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    incoming = target.with_name(_INCOMING)
    bak = target.with_name(PROFILE_BAK)
    incoming.write_text(text, encoding="utf-8")
    had_previous = target.is_file()
    if had_previous:
        shutil.copy2(target, bak)
    os.replace(incoming, target)
    try:
        ok = load_profile(track) is not None
    except (TypeError, ValueError):
        ok = False
    if not ok:
        if had_previous:
            shutil.copy2(bak, target)
        else:
            target.unlink(missing_ok=True)
        raise ProfileEditError("The saved profile could not be loaded; the previous file was kept.")
    log.info("profile_saved", track=track, section=section)
    return target
