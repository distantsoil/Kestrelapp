"""A model's warning sign that a fact Kestrel holds rules out is left out (owner round 14)."""

from __future__ import annotations

from types import SimpleNamespace

from kestrel.web.routes_pages import watch_lines

SIGNS = {"signals": ["no_comp_no_location_no_team", "staffing_agency_unnamed_client"]}


def _opp(**kw):
    base = {"id": 1, "location_raw": None, "comp_min": None, "comp_max": None, "url": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_named_location_rules_out_the_nothing_named_sign_but_not_the_agency_one():
    opp = _opp(location_raw="North Carolina",
               url="https://job-boards.greenhouse.io/axon/jobs/8010140003")
    # Agencies run their own boards too, so a board never rules out "agency posting".
    assert watch_lines(SIGNS, None, opp) == ["An agency posting; the client is not named."]


def test_with_nothing_known_the_signs_stand():
    lines = watch_lines(SIGNS, None, _opp(url="https://jobs.example-agency.com/123"))
    assert len(lines) == 2


def test_pay_alone_rules_out_the_nothing_named_sign():
    assert watch_lines({"signals": ["no_comp_no_location_no_team"]}, None,
                       _opp(comp_max=150000.0)) == []
