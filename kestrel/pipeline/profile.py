"""Load the operator's profile for one track as a nested-attribute object.

The Tier 0 rules (SPEC.md 6.1) reference the profile with dotted access:
`profile.employment_terms.comp_floor_usd`, `profile.geo_rules`, `profile.requires_sponsorship`,
`profile.must_have`. The ORM `profile` row stores those as JSON dicts, and `simpleeval` cannot do
attribute access into a dict, so we build a `SimpleNamespace` tree exactly as
`kestrel.rules.sample` does for the load-time guard. Only the operator's `profile.yml` is read.
With none, the profile is EMPTY (`empty_block`): no proof points, narrative, titles, floor or
must-haves, so nothing ever scores, sketches or writes against the fictional
`profile.example.yml` (D-PROF-4, issue #70). The example stays as documentation and test data,
reachable only on purpose through `load_example_profile`. This module is the one place that
reads the operator's profile file; `profile_readiness.operator_profile` delegates here.

Each track's block in the YAML (`employment:` / `consulting:`) is self-contained: it carries that
track's own `*_terms`, `geo_rules`, `must_have`, `must_avoid`, `proof_points`, and narrative. The
employment Tier 0 rules only read `employment_terms`; the consulting rules only read
`consulting_terms`; so per-track loading is correct and no cross-track merge is needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import structlog
import yaml

from kestrel import paths
from kestrel.rules.margin import normalize_margin
from kestrel.services.names import normalize_name_fields

log = structlog.get_logger()


def _ns(obj: Any) -> Any:
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


PROFILE_NAME = "profile.yml"
EXAMPLE_NAME = "profile.example.yml"


def _read_operator_yaml() -> tuple[dict | None, bool]:
    """(mapping or None, unreadable). A missing file or an empty one is simply no profile; a file
    that is there but cannot be read, is not valid YAML or is not a mapping is `unreadable`."""
    path = paths.config_dir() / PROFILE_NAME
    if not path.exists():
        return None, False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        # The type only: a parse error quotes the operator's own text.
        log.warning("profile_yml_unreadable", error_type=type(exc).__name__)
        return None, True
    if data is None:
        return None, False
    if not isinstance(data, dict):
        log.warning("profile_yml_unreadable", error_type="NotAMapping")
        return None, True
    return data, False


def operator_yaml() -> dict | None:
    """The operator's profile.yml as a mapping, or None when there is none, it cannot be read, or
    it is not a mapping (scoring then treats the profile as empty and never crashes a run, hard
    rule 4; `profile_unreadable` tells the pages and the doctor). Never the shipped example."""
    return _read_operator_yaml()[0]


def profile_unreadable() -> bool:
    """True when profile.yml exists but cannot be used."""
    return _read_operator_yaml()[1]


def _load_yaml() -> dict:
    return operator_yaml() or {}


def operator_block(track: str) -> dict | None:
    """The track's raw block from the operator's own profile.yml, or None."""
    block = _load_yaml().get(track)
    return block if isinstance(block, dict) else None


def empty_block(track: str) -> dict:
    """What a missing profile.yml means: every field Tier 0 and the prompts read, with nothing in
    it. Empty lists and no floor filter nothing (hard rule 4: uncertainty escalates, never
    rejects); the prompts render "unspecified"."""
    block: dict[str, Any] = {
        "track": track, "is_active": True, "first_name": "", "last_name": "", "email": "",
        "phone": "", "location": "", "headline": "", "work_auth": "", "narrative": "",
        "requires_sponsorship": False, "must_have": [], "must_avoid": [], "proof_points": [],
        "geo_rules": {
            "remote_ok": None, "remote_country_allowlist": [], "onsite_states": [],
            "onsite_metros": [], "hybrid_requires_metro": False, "relocation_acceptable": None,
        },
    }
    if track == "consulting":
        block["consulting_terms"] = {
            "day_rate_floor": None, "day_rate_target": None, "min_engagement_days": None,
            "service_lines": [], "target_org_size": [], "target_sectors": [],
            "excluded_sectors": [],
        }
    else:
        block["employment_terms"] = {
            "target_titles": [], "anti_titles": [], "seniority_floor": "",
            "comp_floor_usd": None, "comp_target_usd": None, "excluded_employment_types": [],
        }
    return block


def _build(block: dict) -> SimpleNamespace:
    block = normalize_name_fields(block)
    block.setdefault("requires_sponsorship", False)
    # The near-floor margins (D-RULES-2): an absent key reads as the default, so a profile saved
    # before the setting existed keeps near-floor roles and no rule meets a missing attribute.
    for terms_key, margin_key in (
        ("employment_terms", "comp_floor_margin_pct"),
        ("consulting_terms", "day_rate_floor_margin_pct"),
    ):
        terms = block.get(terms_key)
        if isinstance(terms, dict):  # a track without this section stays without it
            block[terms_key] = {**terms, margin_key: normalize_margin(terms.get(margin_key))}
    return _ns(block)


def load_profile(track: str) -> SimpleNamespace | None:
    """The track's profile block as a nested SimpleNamespace. With no usable profile.yml it is the
    EMPTY profile, never the example. None when the file exists but has no block for the track.
    `requires_sponsorship` is always present (defaults False) so the sponsorship rules never trip
    on a missing attribute."""
    data = _load_yaml()
    if not data:
        return _build(empty_block(track))
    block = data.get(track)
    if not isinstance(block, dict):
        return None
    return _build(dict(block))


def load_example_profile(track: str) -> SimpleNamespace | None:
    """The shipped fictional profile.example.yml block, for tests and documentation only."""
    path = paths.config_file(EXAMPLE_NAME)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path else None
    except (OSError, yaml.YAMLError):
        return None
    block = data.get(track) if isinstance(data, dict) else None
    return _build(dict(block)) if isinstance(block, dict) else None
