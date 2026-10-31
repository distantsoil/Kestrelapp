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
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from kestrel import paths
from kestrel.db.enums import TRACKS
from kestrel.errors import UserFacingError, plain_os_reason, yaml_where
from kestrel.rules.margin import MARGIN_STOPS
from kestrel.services import names

log = structlog.get_logger()

SECTIONS = ("about", "employment", "consulting", "location")

# Personal fields on each track block (the "About you" tab). Lists are one item per line.
# `first_name` is required and greets the operator; `last_name` may be empty; `full_name` is
# derived from them on save (D-UI-117) and is what a resume header prints.
ABOUT_TEXT = ("first_name", "last_name", "email", "phone", "location", "headline", "work_auth")
ABOUT_LISTS = ("must_have", "must_avoid")
# is_active is not offered: nothing in the pipeline reads it yet (reviewer finding 4).
ABOUT_FLAGS = ("requires_sponsorship",)

EMPLOYMENT_LISTS = ("target_titles", "anti_titles", "excluded_employment_types")
EMPLOYMENT_INTS = ("comp_floor_usd", "comp_target_usd")
# The near-floor margin stops (D-RULES-2): 0 is Exact. A form that omits the field leaves the
# stored value alone.
EMPLOYMENT_MARGIN = "comp_floor_margin_pct"
CONSULTING_MARGIN = "day_rate_floor_margin_pct"
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
# Work mode: the ways of working the person would take, shown as three checks on the Employment
# and Consulting tabs. Stored as `work_modes` in the track's terms and read by the scoring
# context; it is a preference, never a filter.
WORK_MODES = (("remote", "Remote"), ("hybrid", "Hybrid"), ("onsite", "On-site"))

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

_MODE_TERMS = {"employment": "employment_terms", "consulting": "consulting_terms"}


def _mode_terms_key(track: str) -> str:
    return _MODE_TERMS.get(track, "employment_terms")


def effective_modes(block: dict, track: str) -> list[str]:
    """The work modes a track's block really states (D-UI-214). `work_modes` is the source of
    truth for whether remote and hybrid are accepted. A profile that never saved it (an older
    file, a blank one) is read from the Location flags instead, so nothing contradicts."""
    terms = block.get(_mode_terms_key(track))
    terms = terms if isinstance(terms, dict) else {}
    if terms.get("work_modes") is not None:
        known = {k for k, _ in WORK_MODES}
        return [m for m in terms["work_modes"] if m in known]
    geo = block.get("geo_rules") if isinstance(block.get("geo_rules"), dict) else {}
    modes = []
    if geo.get("remote_ok"):
        modes.append("remote")
    if geo.get("hybrid_requires_metro"):
        modes.append("hybrid")
    return modes


def effective_geo(block: dict, track: str) -> dict:
    """geo_rules as the Location tab shows it: the remote and hybrid boxes follow the work
    modes, so the two tabs always read the same answer."""
    geo = dict(block.get("geo_rules") if isinstance(block.get("geo_rules"), dict) else {})
    modes = effective_modes(block, track)
    geo["remote_ok"] = "remote" in modes
    flag = geo.get("hybrid_requires_metro")
    if flag is None:  # Location never saved this: Hybrid chosen means the metro rule by default
        flag = "hybrid" in modes
    geo["hybrid_requires_metro"] = bool(flag) and "hybrid" in modes
    return geo


def _reconcile(block: dict, merged: dict, track: str, section: str) -> None:
    """Make the work modes and the Location flags agree after a save (D-UI-214). `block` is the
    track block before the save, `merged` after it; `merged` is changed in place.
    Saving Work mode (Employment or Consulting) decides remote and hybrid: remote_ok follows
    Remote, and a newly ticked Hybrid ticks "Hybrid roles must be in a metro" (owner default),
    an unticked one clears it. Saving Location decides the other way: remote_ok adds or drops
    Remote, and the hybrid-metro rule ticked adds Hybrid. Unticking the rule keeps Hybrid."""
    key = _mode_terms_key(track)
    geo = merged.get("geo_rules") if isinstance(merged.get("geo_rules"), dict) else {}
    terms = merged.get(key) if isinstance(merged.get(key), dict) else {}
    before = effective_modes(block, track)
    if section in ("employment", "consulting"):
        if not geo:
            return  # Location was never saved: it stays "not saved yet" and reads the modes
        modes = effective_modes(merged, track)
        geo["remote_ok"] = "remote" in modes
        if "hybrid" not in modes:
            geo["hybrid_requires_metro"] = False
        elif "hybrid" not in before:
            geo["hybrid_requires_metro"] = True
    elif section == "location":
        modes = set(before)
        modes.discard("remote")
        if geo.get("remote_ok"):
            modes.add("remote")
        if geo.get("hybrid_requires_metro"):
            modes.add("hybrid")
        terms["work_modes"] = [k for k, _ in WORK_MODES if k in modes]
        merged[key] = terms
    else:
        return
    merged["geo_rules"] = geo

PROFILE_NAME = "profile.yml"
PROFILE_BAK = "profile.yml.bak"
_INCOMING = "profile.yml.editing"  # the prefix of a save's temp file; each save gets its own

# One lock for every read-modify-write of profile.yml. Saves run on request threads and the
# import thread (asyncio.to_thread), and two of them reading the same file would lose one's
# change. Re-entrant because add_target_title / add_proof_points hold it across their read and
# the save_section they end in.
_WRITE_LOCK = threading.RLock()


def _temp_beside(target: Path) -> Path:
    """A new, unique, empty file next to `target` for a write that ends in os.replace."""
    fd, name = tempfile.mkstemp(prefix=_INCOMING + ".", dir=target.parent)
    os.close(fd)
    return Path(name)


class ProfileEditError(UserFacingError, ValueError):
    """A save that must not happen: the form was unusable, or the written file failed the
    loader (in which case the previous file is back in place)."""


# The two ways Undo can be refused, one subclass and one sentence each. The Undo route answers
# from the sentence of the class it caught, never from the exception it holds, so the page text
# is a constant a code scanner can follow (D-SEC-4).
UNKNOWN_TRACK = "That track is not one Kestrel knows, so nothing was added."
NO_EARLIER_VERSION = "There is no earlier version to go back to."
EARLIER_VERSION_UNREADABLE = "The earlier version could not be loaded; nothing was changed."


class NoEarlierVersion(ProfileEditError):
    """Undo was asked for and no profile.yml.bak exists."""


class EarlierVersionUnreadable(ProfileEditError):
    """profile.yml.bak exists but the loader would not read it; the current file was kept."""


# The sentences a save of profile.yml can refuse with that are Kestrel's own and a closed list
# (the Settings Undo for the shortest project shows one of these, or a fixed fallback, never the
# exception's text: kestrel.web.errors.authored_error, D-SEC-4). `{line}` stands for one whole
# number. Each is built where it is raised, from the same pieces.
NOT_LOADED = "The saved profile could not be loaded; the previous file was kept."
NOT_YAML = f"{PROFILE_NAME} is not valid YAML."
NOT_YAML_AT = f"{PROFILE_NAME} is not valid YAML (line {{line}})."
UNREADABLE_DENIED = f"{PROFILE_NAME} could not be read: permission was denied"
UNREADABLE_MISSING = f"{PROFILE_NAME} could not be read: the file was not found"
SAVE_REFUSALS: tuple[str, ...] = (
    NOT_LOADED, NOT_YAML, NOT_YAML_AT, UNREADABLE_DENIED, UNREADABLE_MISSING,
)


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
    return [t for t in tracks if not names.clean(state.tracks[t].get("first_name"))]


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
            block = names.normalize_name_fields(block)
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


def _modes_from_form(form: Any) -> list[str]:
    """The work modes whose check is on, in the fixed order."""
    return [key for key, _label in WORK_MODES if _flag(form, f"mode_{key}")]


def _margin_from_form(form: Any, key: str, terms: dict) -> None:
    """Set `terms[key]` to a near-floor margin stop when the form sent one (blank = not sent)."""
    raw = str(form.get(key) or "").strip().lower()
    if not raw:
        return
    if raw == "exact":
        raw = "0"
    try:
        pct = int(raw.rstrip("%"))
    except ValueError as exc:
        raise ProfileEditError("Pick a near-floor margin from the list.") from exc
    if pct not in MARGIN_STOPS:
        raise ProfileEditError("Pick a near-floor margin from the list.")
    terms[key] = pct


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
        if not out["first_name"] and str(form.get("full_name") or "").strip():
            # An older form (or a script) that still sends one full name: split it the same way
            # the migration does (first space), so nothing is lost.
            out["first_name"], out["last_name"] = names.split_full_name(form.get("full_name"))
        out["first_name"] = names.clean(out["first_name"])
        out["last_name"] = names.clean(out["last_name"])
        if not out["first_name"]:
            raise ProfileEditError("Add your first name.")
        out["full_name"] = names.join_full_name(out["first_name"], out["last_name"])
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
        terms["work_modes"] = _modes_from_form(form)
        for k in EMPLOYMENT_INTS:
            terms[k] = _int(form.get(k), k.replace("_", " "))  # None clears: no floor
        _margin_from_form(form, EMPLOYMENT_MARGIN, terms)
        return {"employment_terms": terms}
    if section == "consulting":
        terms = {k: lines(form.get(k)) for k in CONSULTING_LISTS}
        for k in CONSULTING_INTS:
            terms[k] = _int(form.get(k), k.replace("_", " "))
        for k in CONSULTING_TEXT:
            terms[k] = str(form.get(k) or "").strip()
        terms["work_modes"] = _modes_from_form(form)
        _margin_from_form(form, CONSULTING_MARGIN, terms)
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
def save_section(track: str, section: str, values: dict, *, keep_undo: bool = True) -> Path:
    """Merge one section into the track's block and write profile.yml atomically. A first save
    creates the file with only this track block (the other track stays absent until it is
    saved, so Today keeps saying that track has no profile). The previous file is kept as
    profile.yml.bak and restored if the loader refuses the result. `keep_undo=False` (the proof
    point import) leaves profile.yml.bak alone, so the Profile tab's Undo still goes back to the
    last save the operator made there and never rolls back an import."""
    with _WRITE_LOCK:
        return _save_section(track, section, values, keep_undo)


def _save_section(track: str, section: str, values: dict, keep_undo: bool) -> Path:
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
    _reconcile(data.get(track) if isinstance(data.get(track), dict) else {}, block, track,
               section)
    data[track] = block

    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    bak = target.with_name(PROFILE_BAK)
    previous = target.read_bytes() if target.is_file() else None
    incoming = _temp_beside(target)
    try:
        incoming.write_text(text, encoding="utf-8")
        if keep_undo:
            if previous is not None:
                shutil.copy2(target, bak)
            else:
                bak.unlink(missing_ok=True)  # a first save has nothing to undo to; no stale backup
        os.replace(incoming, target)
    finally:
        incoming.unlink(missing_ok=True)
    try:
        ok = load_profile(track) is not None
    except (TypeError, ValueError):
        ok = False
    if not ok:
        if previous is not None:
            _write_bytes_atomic(target, previous)
        else:
            target.unlink(missing_ok=True)
        raise ProfileEditError(NOT_LOADED)
    log.info("profile_saved", track=track, section=section)
    return target


def _write_bytes_atomic(target: Path, data: bytes) -> None:
    tmp = _temp_beside(target)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def undo_token() -> str:
    """A stamp of the profile.yml on disk right now. A save hands it to its Undo; Undo is only
    honored while the file still carries it, so an old toast can never restore a version that a
    newer save has already replaced (Undo goes back one save, no further)."""
    import hashlib

    try:
        return hashlib.sha256(profile_path().read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def restore_backup() -> Path:
    """Undo of the last save: put profile.yml.bak back as profile.yml (D-UI-119). Refused when
    there is no backup or the loader would not read it; the current file is untouched then."""
    with _WRITE_LOCK:
        return _restore_backup()


def _restore_backup() -> Path:
    from kestrel.pipeline.profile import load_profile

    target = profile_path()
    bak = target.with_name(PROFILE_BAK)
    if not bak.is_file():
        raise NoEarlierVersion(NO_EARLIER_VERSION)
    current = target.read_bytes() if target.is_file() else None
    incoming = _temp_beside(target)
    try:
        shutil.copy2(bak, incoming)
        os.replace(incoming, target)  # atomic, like a save: a failed copy never truncates the file
    finally:
        incoming.unlink(missing_ok=True)
    try:
        data = _read(target)
        ok = all(load_profile(t) is not None for t in TRACKS if isinstance(data.get(t), dict))
    except (TypeError, ValueError, ProfileEditError):
        ok = False
    if not ok:
        if current is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(current)
        raise EarlierVersionUnreadable(EARLIER_VERSION_UNREADABLE)
    log.info("profile_restored")
    return target


def _same_title(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def add_target_title(title: str) -> bool:
    """Add one title to the employment track's target titles, keeping every other term as it is
    (the Employment form's save, for one title: P15 Accept, D-TTL-3). True when the title was
    added, False when an equal one (ignoring case and spacing) was already there. A profile with
    no employment terms yet gets the blank terms a form save would write, so the new file filters
    nothing but the titles. Raises ProfileEditError when profile.yml cannot be read or written."""
    title = " ".join(title.split())
    if not title:
        raise ProfileEditError("A title cannot be empty.")
    with _WRITE_LOCK:  # the read below and the save it ends in are one step
        return _add_target_title(title)


def _add_target_title(title: str) -> bool:
    state = load_state()
    if state.problem:
        raise ProfileEditError(state.problem)
    terms = dict(state.tracks["employment"].get("employment_terms") or {})
    titles = [str(t) for t in (terms.get("target_titles") or [])]
    if any(_same_title(title, t) for t in titles):
        return False
    terms["target_titles"] = [*titles, title]
    terms.setdefault("anti_titles", [])
    terms.setdefault("excluded_employment_types", [])
    terms.setdefault("seniority_floor", "")
    for key in EMPLOYMENT_INTS:
        terms.setdefault(key, None)
    save_section("employment", "employment", {"employment_terms": terms})
    return True


def _norm_claim(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def add_proof_points(track: str, points: list[dict]) -> int:
    """Add proof points to a track's block in profile.yml, the file scoring reads (issue #68).
    The same atomic write, .bak and loader check as every other profile save. A point whose claim
    is already there (ignoring case and spacing) is skipped, so importing the same resume twice
    adds nothing; a new point whose key is taken gets a fresh one (`r1_cost_2`) instead of
    replacing the old. Returns how many were added; 0 writes nothing. Raises ProfileEditError
    when profile.yml cannot be read or written, or when `track` is not a known track.

    The write keeps the Profile tab's Undo backup as it was (`keep_undo=False`): Undo goes back
    one save the operator made there, and an import in between must not become the thing it
    rolls back."""
    if track not in TRACKS:
        raise ProfileEditError(UNKNOWN_TRACK)
    with _WRITE_LOCK:
        return _add_proof_points(track, points)


def _add_proof_points(track: str, points: list[dict]) -> int:
    state = load_state()
    if state.problem:
        raise ProfileEditError(state.problem)
    existing = [
        dict(p) for p in (state.tracks[track].get("proof_points") or []) if isinstance(p, dict)
    ]
    taken = {str(p.get("key")) for p in existing}
    claims = {_norm_claim(p.get("claim")) for p in existing}
    new: list[dict] = []
    for p in points:
        claim = " ".join(str(p.get("claim") or "").split())
        if not claim or _norm_claim(claim) in claims:
            continue
        claims.add(_norm_claim(claim))
        base = str(p.get("key") or "").strip() or f"p{len(existing) + len(new) + 1}"
        key, n = base, 2
        while key in taken:
            key, n = f"{base}_{n}", n + 1
        taken.add(key)
        new.append({
            "key": key, "claim": claim, "evidence": str(p.get("evidence") or "").strip(),
            "metric": p.get("metric") or None,
        })
    if not new:
        return 0
    save_section(track, "proof_points", {"proof_points": [*existing, *new]}, keep_undo=False)
    return len(new)
