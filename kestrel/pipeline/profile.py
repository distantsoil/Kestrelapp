"""Load the operator's profile for one track as a nested-attribute object.

The Tier 0 rules (SPEC.md 6.1) reference the profile with dotted access:
`profile.employment_terms.comp_floor_usd`, `profile.geo_rules`, `profile.requires_sponsorship`,
`profile.must_have`. The ORM `profile` row stores those as JSON dicts, and `simpleeval` cannot do
attribute access into a dict, so we build a `SimpleNamespace` tree exactly as
`kestrel.rules.sample` does for the load-time guard. The operator's `profile.yml` wins; the shipped
fictional `profile.example.yml` is the fallback so a fresh install still evaluates.

Each track's block in the YAML (`employment:` / `consulting:`) is self-contained: it carries that
track's own `*_terms`, `geo_rules`, `must_have`, `must_avoid`, `proof_points`, and narrative. The
employment Tier 0 rules only read `employment_terms`; the consulting rules only read
`consulting_terms`; so per-track loading is correct and no cross-track merge is needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import yaml

from kestrel import paths


def _ns(obj: Any) -> Any:
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


def _load_yaml() -> dict:
    """profile.yml (the operator's, never committed) wins; profile.example.yml is the fallback."""
    for name in ("profile.yml", "profile.example.yml"):
        path = paths.config_file(name)
        if path is not None:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(data, dict):
                return data
    return {}


def load_profile(track: str) -> SimpleNamespace | None:
    """The track's profile block as a nested SimpleNamespace, or None when the track is absent.
    `requires_sponsorship` is always present (defaults False) so the sponsorship rules never trip
    on a missing attribute."""
    block = _load_yaml().get(track)
    if not isinstance(block, dict):
        return None
    block = dict(block)
    block.setdefault("requires_sponsorship", False)
    return _ns(block)
