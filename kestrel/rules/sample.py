"""A reference Tier 0 evaluation context (opp, org, profile, source), built from the packaged
example profile. Used by preflight and by the filters-load test to exercise every rule's `when:`
expression so a config that references an unknown symbol or a generator expression fails loudly at
load rather than silently at runtime. Values are plausible but their only job is to make every rule
evaluate without error."""

from __future__ import annotations

from datetime import date, timedelta
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


def _example_profile() -> SimpleNamespace:
    path = paths.config_file("profile.example.yml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path else {}
    employment = data.get("employment", {})
    consulting = data.get("consulting", {})
    # One combined profile carrying both track term-sets, geo_rules, and the sponsorship flag, so
    # both employment and consulting rules can be evaluated against a single context object.
    profile = dict(employment)
    profile["consulting_terms"] = consulting.get("consulting_terms", {})
    profile.setdefault("requires_sponsorship", False)
    return _ns(profile)


def sample_context() -> dict[str, Any]:
    recent = date.today() - timedelta(days=5)
    opp = SimpleNamespace(
        title="Operations Analyst",
        description_md="Build weekly operations reports and improve order-to-cash processes.",
        comp_max=85000,
        comp_min=70000,
        comp_source="parsed",
        date_posted=recent,
        event_date=recent,
        remote_type="hybrid",
        country="US",
        state="OH",
        city="Columbus",
        location_raw="Columbus, OH (hybrid)",
        seen_count=1,
        ghost_score=0.1,
        url="https://example.com/jobs/123",
        trigger_data={"engagement_days": 40},
    )
    org = SimpleNamespace(
        name="Example Services Group",
        sector="professional_services",
        ownership="pe_backed",
        size_band="100-500",
        signals=[
            {
                "event_type": "acquisition",
                "event_date": (date.today() - timedelta(days=20)).isoformat(),
            }
        ],
    )
    source = SimpleNamespace(kind="greenhouse", name="Example board")
    return {"opp": opp, "org": org, "profile": _example_profile(), "source": source}
