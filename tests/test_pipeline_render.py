"""The deterministic prompt renderer: `{if trigger_event:}`/`{if posted:}` conditionals, the P2C
for-each-org-signal line, and flat placeholder substitution that leaves no template brace behind."""

from __future__ import annotations

import re
from types import SimpleNamespace

from kestrel.llm.prompts import load_prompt
from kestrel.pipeline.context import (
    build_tier1_values,
    build_tier2_values,
    comp_display,
    org_signals_lines,
    render_user,
)
from kestrel.pipeline.profile import load_profile

# The only braces left in a rendered prompt come from substituted *content* (e.g. a description).
# A leftover TEMPLATE placeholder is a bare {token} with no spaces; assert none of those remain.
_LEFTOVER = re.compile(r"\{[A-Za-z_][\w.\"' ]*\}")


def _emp_opp():
    return SimpleNamespace(
        title="VP of Information Technology",
        org_name="Acme Corp",
        description_md="Own IT strategy and security for a services firm. " * 5,
        location_raw="Austin, TX",
        remote_type="hybrid",
        employment_type="full_time",
        comp_min=190000,
        comp_max=230000,
        comp_currency="USD",
        comp_interval="year",
        comp_source="parsed",
        date_posted="2026-07-01",
        event_date=None,
        trigger_data=None,
        url="https://example.com/j/1",
        opp_type="job_posting",
        seen_count=2,
        first_seen_at="2026-07-01T00:00:00.000000+00:00",
        last_seen_at="2026-07-10T00:00:00.000000+00:00",
        version=1,
        ghost_score=0.1,
        state="TX",
        country="US",
        city="Austin",
    )


def test_comp_display_and_fallback():
    opp = _emp_opp()
    assert comp_display(opp) == "$190,000 - $230,000 / year"
    opp.comp_min = opp.comp_max = None
    assert comp_display(opp) == ""  # falsy so the template's `or "not stated"` fires


def test_tier1_employment_leaves_no_placeholder():
    spec = load_prompt("triage/v2")
    opp = _emp_opp()
    profile = load_profile("employment")
    values = build_tier1_values(
        opp=opp, org=SimpleNamespace(name="Acme", sector=None, size_band=None, ownership=None,
                                     signals=[]),
        profile=profile, source_name="board", n_tokens=1200,
    )
    out = render_user(spec.user, values, trigger_event=False, posted=True)
    assert "not stated" not in out  # comp IS stated here
    assert "$190,000" in out
    assert not _LEFTOVER.search(out), _LEFTOVER.search(out).group()


def test_tier2_employment_leaves_no_placeholder_and_shows_history():
    spec = load_prompt("evaluate/v2")
    opp = _emp_opp()
    profile = load_profile("employment")
    values = build_tier2_values(
        opp=opp, org=SimpleNamespace(name="Acme", sector="tech", size_band="100-500",
                                     ownership="private", signals=[]),
        profile=profile, source_name="board", max_jd_tokens=6000, few_shot="", comp_intel=None,
    )
    out = render_user(spec.user, values, trigger_event=False, posted=True)
    assert "insufficient data (n<5)" in out  # comp_intel None -> honest fallback (hard rule 6)
    assert "Ghost score:" in out
    assert not _LEFTOVER.search(out), _LEFTOVER.search(out).group()


def test_consulting_trigger_event_shows_trigger_hides_posting():
    spec = load_prompt("triage_consulting/v2")
    opp = _emp_opp()
    opp.opp_type = "trigger_event"
    opp.description_md = None
    opp.event_date = "2026-07-01"
    opp.trigger_data = {"event_type": "cio_departure", "evidence_quote": "CIO resigned",
                        "implied_need": "interim IT leadership"}
    profile = load_profile("consulting")
    org = SimpleNamespace(name="Beacon Health", sector="healthcare", size_band="500-1000",
                          ownership="pe_backed", signals=[])
    values = build_tier1_values(opp=opp, org=org, profile=profile, source_name="edgar",
                                n_tokens=1200)
    out = render_user(spec.user, values, trigger_event=True, posted=False)
    assert "TRIGGER EVENT" in out
    assert "cio_departure" in out
    assert "POSTING" not in out  # posted section dropped
    assert not _LEFTOVER.search(out), _LEFTOVER.search(out).group()


def test_consulting_eval_renders_org_signals_line():
    spec = load_prompt("evaluate_consulting/v2")
    opp = _emp_opp()
    opp.opp_type = "contract_posting"
    profile = load_profile("consulting")
    org = SimpleNamespace(
        name="Beacon Health", sector="healthcare", size_band="500-1000", ownership="pe_backed",
        signals=[{"category": "acquisition", "statement": "acquired a competitor",
                  "event_date": "2026-06-01", "url": "https://x"}],
    )
    values = build_tier2_values(opp=opp, org=org, profile=profile, source_name="edgar",
                                max_jd_tokens=6000, few_shot="", comp_intel=None)
    out = render_user(spec.user, values, trigger_event=False, posted=True,
                      org_signals=org_signals_lines(org))
    assert "acquired a competitor" in out
    assert "for each org signal" not in out  # the for-each directive was replaced
    assert not _LEFTOVER.search(out), _LEFTOVER.search(out).group()
