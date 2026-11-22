"""The deterministic blocker check (issue #208 item 1, D-SCORE-22).

INVARIANT: uncertainty escalates, never rejects. A model blocker forces `pass` only after a check
against the operator's own data upholds it; a blocker the data contradicts, or that needs
confirming, leaves the model's verdict standing and becomes a Worth checking note; the raw blocker
and the check result are stored. Invented text only; the model is faked and nothing reaches a
network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity
from kestrel.pipeline import scoring, tier2
from kestrel.pipeline.profile import _build, empty_block
from kestrel.web.fit import blocker_lines, forced_words
from tests.pipeline_fakes import FakeLLM

_DIMS = dict.fromkeys(
    ("role_fit", "domain_match", "company_context", "comp_logistics", "trajectory"), 3.6)


def _profile(**over) -> SimpleNamespace:
    block = empty_block("employment")
    block["employment_terms"].update(comp_floor_usd=150000, comp_target_usd=210000,
                                     comp_floor_margin_pct=5)
    block["geo_rules"].update(remote_ok=True, remote_country_allowlist=["US"],
                              onsite_states=["OR"], relocation_acceptable=False)
    for key, value in over.items():
        if key in block["geo_rules"]:
            block["geo_rules"][key] = value
        elif key in block["employment_terms"]:
            block["employment_terms"][key] = value
        else:
            block[key] = value
    return _build(block)


def _opp(**over) -> SimpleNamespace:
    base = dict(remote_type="remote", country="US", state=None, city=None,
                location_raw="Remote, US", title="Systems Lead", description_md="Remote role.",
                comp_min=170000, comp_max=200000, comp_currency="USD", comp_interval="year",
                comp_source="posted", comp_currency_known=True, comp_interval_known=True,
                employment_type="fulltime")
    base.update(over)
    return SimpleNamespace(**base)


def _post(kind: str, opp, profile, *, signal="not_mentioned", sponsor=False, track="employment"):
    return scoring.post_process(
        track=track, dimension_scores=_DIMS, holistic=3.6, model_verdict="consider",
        blockers=[{"kind": kind, "detail": "Invented detail from the model."}],
        legitimacy_status="clean", work_auth_signal=signal, requires_sponsorship=sponsor,
        settings=get_settings(), opp=opp, profile=profile,
    )


# ------------------------------------------------------------------------------------- comp
def test_comp_upheld_when_the_band_tops_out_below_the_floor_less_the_margin():
    r = _post("comp", _opp(comp_min=90000, comp_max=120000), _profile())
    assert r.verdict == "pass"
    assert r.forced_by == "blocker:comp"
    assert r.blockers[0]["check"] == "upheld"


def test_comp_contradicted_when_the_band_reaches_the_floor_even_below_the_target():
    # Run 1 of the spot check: a band wholly above the floor whose midpoint is under the target.
    r = _post("comp", _opp(comp_min=160000, comp_max=190000), _profile())
    assert r.verdict == "consider"  # the model's verdict stands
    assert r.forced_by is None
    assert (r.blockers[0]["check"], r.blockers[0]["check_reason"]) == (
        "contradicted", "band_reaches_floor")
    # The raw blocker is stored as the model gave it.
    assert r.blockers[0]["detail"] == "Invented detail from the model."


@pytest.mark.parametrize("opp_over, reason", [
    ({"comp_min": None, "comp_max": None}, "no_band"),
    ({"comp_source": "inferred", "comp_min": 60000, "comp_max": 70000}, "no_band"),
    ({"comp_currency": "EUR", "comp_min": 60000, "comp_max": 70000}, "other_currency"),
    ({"comp_min": 140000, "comp_max": 145000}, "near_floor"),
])
def test_comp_needs_confirming_never_rejects(opp_over, reason):
    r = _post("comp", _opp(**opp_over), _profile())
    assert r.verdict == "consider"
    assert r.forced_by is None
    assert (r.blockers[0]["check"], r.blockers[0]["check_reason"]) == ("needs_confirming", reason)


def test_comp_needs_confirming_without_a_floor():
    r = _post("comp", _opp(comp_min=50000, comp_max=60000), _profile(comp_floor_usd=None))
    assert r.verdict == "consider"
    assert r.blockers[0]["check_reason"] == "no_floor"


def test_hourly_pay_is_annualized_before_the_floor_comparison():
    r = _post("comp", _opp(comp_min=80, comp_max=95, comp_interval="hour"), _profile())
    assert r.blockers[0]["check"] == "contradicted"  # 95 x 2080 = 197,600


# --------------------------------------------------------------------------------- location
def test_location_contradicted_when_remote_in_an_allowed_country():
    # Run 2 of the spot check: remote from the candidate's country, open to remote.
    r = _post("location", _opp(), _profile())
    assert r.verdict == "consider"
    assert (r.blockers[0]["check"], r.blockers[0]["check_reason"]) == (
        "contradicted", "remote_in_allowed_country")


def test_location_contradicted_when_an_onsite_state_matches():
    r = _post("location", _opp(remote_type="onsite", state="OR", location_raw="Bend, OR"),
              _profile())
    assert r.verdict == "consider"
    assert r.blockers[0]["check_reason"] == "onsite_place_matches"


def test_location_upheld_when_onsite_outside_the_operators_places():
    r = _post("location", _opp(remote_type="onsite", state="ME", city="Bangor",
                               location_raw="Bangor, ME"), _profile())
    assert r.verdict == "pass"
    assert r.blockers[0]["check"] == "upheld"


def test_not_choosing_remote_is_a_preference_never_a_dealbreaker():
    # remote_ok is a preference the model reads, not a filter (profile_edit; review finding 1).
    r = _post("location", _opp(), _profile(remote_ok=False))
    assert r.verdict == "consider"
    assert (r.blockers[0]["check"], r.blockers[0]["check_reason"]) == (
        "needs_confirming", "remote_not_chosen")


def test_an_onsite_role_with_no_known_place_is_never_upheld():
    r = _post("location", _opp(remote_type="onsite", location_raw=None, state=None, city=None),
              _profile())
    assert r.verdict == "consider"
    assert r.blockers[0]["check_reason"] == "location_unknown"


def test_a_starting_figure_alone_never_upholds_a_pay_blocker():
    r = _post("comp", _opp(comp_min=90000, comp_max=None), _profile())
    assert r.verdict == "consider"
    assert r.blockers[0]["check_reason"] == "no_top"


def test_a_consulting_rate_with_no_interval_is_never_read_as_a_salary():
    block = empty_block("consulting")
    block["consulting_terms"].update(day_rate_floor=1000)
    r = _post("rate", _opp(comp_min=900, comp_max=1100, comp_interval=None), _build(block),
              track="consulting")
    assert r.verdict == "consider"
    assert r.blockers[0]["check_reason"] == "no_interval"


def test_a_citizenship_requirement_needs_confirming():
    r = _post("work_authorization", _opp(), _profile(), signal="citizenship_required")
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("consider", "citizenship_required")


@pytest.mark.parametrize("opp_over, reason", [
    ({"remote_type": "unknown", "location_raw": "See posting", "state": None},
     "remote_type_unknown"),
    ({"country": None}, "remote_country_unknown"),
])
def test_location_needs_confirming_never_rejects(opp_over, reason):
    r = _post("location", _opp(**opp_over), _profile())
    assert r.verdict == "consider"
    assert (r.blockers[0]["check"], r.blockers[0]["check_reason"]) == ("needs_confirming", reason)


# ------------------------------------------------------------------------ work authorization
def test_work_auth_upheld_only_on_an_explicit_refusal_the_profile_needs():
    r = _post("work_authorization", _opp(), _profile(), signal="explicit_no_sponsorship",
              sponsor=True)
    assert r.verdict == "pass"
    assert r.forced_by == "blocker:work_authorization"
    assert [b["check"] for b in r.blockers] == ["upheld"]  # never a second, system copy


def test_work_auth_contradicted_when_no_sponsorship_is_needed():
    r = _post("work_authorization", _opp(), _profile(), signal="explicit_no_sponsorship")
    assert r.verdict == "consider"
    assert r.blockers[0]["check_reason"] == "no_sponsorship_needed"


def test_work_auth_needs_confirming_when_the_posting_does_not_refuse():
    r = _post("work_authorization", _opp(), _profile(), signal="not_mentioned", sponsor=True)
    assert r.verdict == "consider"
    assert r.blockers[0]["check_reason"] == "sponsorship_not_stated"


@pytest.mark.parametrize("kind, detail, track", [
    ("other", "The pay is far below what you need.", "employment"),  # the relabel bypass
    ("other", "You would have to work on site in another state.", "employment"),
    ("clearance", "Needs an active clearance.", "employment"),
    ("licensure", "Needs a license.", "employment"),
    ("employment_type", "Contract only.", "employment"),
    ("duration", "Too short.", "consulting"),
    ("sector", "An excluded sector.", "consulting"),
    ("capacity", "No room this quarter.", "consulting"),
    ("conflict_of_interest", "Competes with a client.", "consulting"),
    ("a_future_kind", "Anything new.", "employment"),
])
def test_a_kind_the_code_cannot_check_never_forces_pass(kind, detail, track):
    # Owner via parent (D-SCORE-28): a model blocker never forces pass on its own.
    r = scoring.post_process(
        track=track, dimension_scores=_DIMS, holistic=3.6, model_verdict="consider",
        blockers=[{"kind": kind, "detail": detail}], legitimacy_status="clean",
        work_auth_signal="not_mentioned", requires_sponsorship=False, settings=get_settings(),
        opp=_opp(), profile=_profile(),
    )
    assert r.verdict == "consider" and r.forced_by is None
    assert (r.blockers[0]["check"], r.blockers[0]["check_reason"]) == (
        "needs_confirming", "not_checkable")


def test_consulting_rate_checks_the_day_rate_floor():
    block = empty_block("consulting")
    block["consulting_terms"].update(day_rate_floor=1200, day_rate_target=1600)
    prof = _build(block)
    above = _post("rate", _opp(comp_min=1300, comp_max=1500, comp_interval="day"), prof,
                  track="consulting")
    below = _post("rate", _opp(comp_min=500, comp_max=700, comp_interval="day"), prof,
                  track="consulting")
    assert above.blockers[0]["check"] == "contradicted"
    assert below.blockers[0]["check"] == "upheld"


# ---------------------------------------------------------------------------------- the words
def test_the_ui_says_the_model_flagged_it_and_explains_a_blocker_that_did_not_count():
    assert forced_words("blocker:location") == (
        "The model flagged: the location does not work for you.")
    rows = blocker_lines([
        {"kind": "location", "detail": "x", "check": "contradicted",
         "check_reason": "remote_in_allowed_country"},
        {"kind": "clearance", "detail": "Needs a clearance.", "check": "unchecked"},
        {"kind": "comp", "detail": "Old row."},  # stored before the check: counted then
    ])
    assert rows[0] == {"counted": False, "label": "Location", "detail": "x", "note": (
        "The model flagged location, but the role is remote in your country, "
        "so it did not count.")}
    assert [r["counted"] for r in rows] == [False, True, True]


# --------------------------------------------------------------------------- the real handler
class _Blockers(FakeLLM):
    def __init__(self, settings, blockers) -> None:
        super().__init__(settings)
        self.blockers = blockers

    async def complete(self, **kw):
        out = await super().complete(**kw)
        body = json.loads(out.content)
        body["blockers"] = self.blockers
        body["holistic"] = 3.6
        body["verdict"] = "consider"
        out.content = json.dumps(body)
        return out


async def _role(s) -> int:
    o = Opportunity(
        track="employment", opp_type="job_posting", title="Systems Lead",
        org_name="Larkspur Ferries", stage="t1_pass", stage_reason="invented",
        description_md="Run the fleet scheduling systems for a ferry line. " * 12,
        seen_count=1, ghost_score=0.0, remote_type="remote", country="US",
        location_raw="Remote, US", comp_min=165000, comp_max=195000, comp_currency="USD",
        comp_currency_known=True, comp_interval_known=True,
        comp_interval="year", comp_source="posted",
    )
    s.add(o)
    await s.commit()
    return o.id


async def test_tier2_keeps_the_models_verdict_when_the_profile_contradicts_the_blockers(sm):
    async with sm() as s:
        a = await _role(s)
    raw = [{"kind": "location", "detail": "Remote from your state may be acceptable; confirm."},
           {"kind": "comp", "detail": "The band midpoint is under your target."}]
    llm = _Blockers(get_settings(), raw)
    out = await tier2.handle_t2(sm, llm, get_settings(), opportunity_id=a,
                                profiles={"employment": _profile(), "consulting": None})
    assert out.action == "t2_done"
    async with sm() as s:
        ev = (await s.execute(select(Evaluation).where(Evaluation.opportunity_id == a))
              ).scalar_one()
    assert ev.verdict != "pass"
    assert ev.verdict_model == "consider"
    assert ev.forced_by is None
    assert [(b["kind"], b["detail"], b["check"]) for b in ev.blockers] == [
        ("location", raw[0]["detail"], "contradicted"),
        ("comp", raw[1]["detail"], "contradicted"),
    ]


async def test_tier2_forces_pass_when_the_profile_upholds_the_blocker(sm):
    async with sm() as s:
        a = await _role(s)
    llm = _Blockers(get_settings(), [{"kind": "comp", "detail": "The band is far below."}])
    async with sm() as s:
        o = await s.get(Opportunity, a)
        o.comp_min, o.comp_max = 80000, 95000
        await s.commit()
    out = await tier2.handle_t2(sm, llm, get_settings(), opportunity_id=a,
                                profiles={"employment": _profile(), "consulting": None})
    assert out.verdict == "pass"
    async with sm() as s:
        ev = (await s.execute(select(Evaluation).where(Evaluation.opportunity_id == a))
              ).scalar_one()
    assert ev.forced_by == "blocker:comp"
    assert ev.blockers[0]["check"] == "upheld"



# ------------------------------------------------------------------- review round 2 cases
def test_one_figure_stored_as_min_equals_max_is_not_a_band():
    r = _post("comp", _opp(comp_min=90000, comp_max=90000), _profile())
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("consider", "no_top")


def test_a_consulting_rate_stored_as_yearly_is_never_read_as_a_salary():
    block = empty_block("consulting")
    block["consulting_terms"].update(day_rate_floor=1000)
    r = _post("rate", _opp(comp_min=900, comp_max=950, comp_interval="year"), _build(block),
              track="consulting")
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("consider", "no_interval")


def test_a_remote_role_in_a_country_the_operator_did_not_list_still_counts():
    r = _post("location", _opp(country="DE", location_raw="Remote, Germany"),
              _profile(remote_ok=False))
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("pass", "location_outside_rules")


def test_citizenship_counts_when_the_profile_needs_sponsorship():
    r = _post("work_authorization", _opp(), _profile(), signal="citizenship_required",
              sponsor=True)
    assert (r.verdict, r.blockers[0]["check"]) == ("pass", "upheld")



# --------------------------------------------------------------- review round 3 (parent)
def test_a_willing_to_relocate_operator_never_has_a_location_blocker_upheld():
    far = _opp(remote_type="onsite", state="ME", city="Bangor", location_raw="Bangor, ME")
    assert _post("location", far, _profile()).verdict == "pass"  # the control: upheld
    r = _post("location", far, _profile(relocation_acceptable=True))
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("consider", "willing_to_relocate")


def test_an_unknown_remote_type_with_a_named_place_needs_confirming():
    # No remote wording anywhere: geo_evaluate alone would deny it (SPEC 6.1 step 5).
    r = _post("location", _opp(remote_type="unknown", state="ME", city="Bangor",
                               location_raw="Bangor, ME", description_md="Run the dock."),
              _profile())
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("consider", "remote_type_unknown")


@pytest.mark.parametrize("over", [
    {"comp_currency_known": False},  # "80k-100k": no currency stated, USD assumed
    {"comp_currency_known": None},  # a row from before migration 0037
])
def test_a_currency_that_was_not_stated_is_never_compared_with_a_usd_floor(over):
    r = _post("comp", _opp(comp_min=70000, comp_max=80000, **over), _profile())
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("consider", "currency_unknown")


def test_a_canadian_band_is_its_own_currency_and_never_read_as_usd():
    from kestrel.ingest.comp import parse_comp

    comp = parse_comp("Pay: CA$70k-80k a year")
    assert (comp.currency, comp.currency_known) == ("CAD", True)
    r = _post("comp", _opp(comp_min=comp.min, comp_max=comp.max, comp_currency=comp.currency),
              _profile(comp_floor_usd=100000))
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("consider", "other_currency")
    assert parse_comp("A$90k-110k").currency == "AUD"
    assert parse_comp("80k-100k").currency_known is False


def test_remote_with_no_countries_listed_claims_no_country():
    r = _post("location", _opp(), _profile(remote_country_allowlist=[]))
    assert (r.blockers[0]["check"], r.blockers[0]["check_reason"]) == (
        "contradicted", "remote_anywhere")
    note = blocker_lines(r.blockers)[0]["note"]
    assert "your country" not in note and "did not limit remote work to any country" in note


# ----------------------------------------------------------------- review round 4 (parent)
@pytest.mark.parametrize("text, code, known", [
    ("CAD$90k-95k", "CAD", True), ("AUD$90k-95k", "AUD", True), ("S$90k-95k", "SGD", True),
    ("HK$90k-95k", "HKD", True), ("R$90k-95k", "BRL", True), ("MX$90k-95k", "MXN", True),
    ("ZZ$90k-95k", "USD", False),  # an unlisted mark: never USD on trust
    ("$80k - C$100k", "USD", False),  # a mixed band
])
def test_every_currency_mark_is_its_own_code_or_unknown(text, code, known):
    from kestrel.ingest.comp import parse_comp

    comp = parse_comp(text)
    assert (comp.currency, comp.currency_known) == (code, known)


@pytest.mark.parametrize("text, code, known", [
    ("CAD $100k-$120k/year", "CAD", True), ("AUD $90k - $110k", "AUD", True),
    ("SGD $8,000/month", "SGD", True), ("HKD $50k-$60k/month", "HKD", True),
    ("cad $95k-$99k", "CAD", True), ("CAD $100k - CAD $120k", "CAD", True),
    ("AUD\t\t$90k", "AUD", True), ("CAD\u00a0$100k-$120k", "CAD", True),  # spaces on one line
    ("CA$100k-$120k", "CAD", True),  # the code on the first figure names the band's dollar
    ("$80k - CAD $100k", "USD", False),  # a bare "$" first, a code second: still mixed
    ("BRL $5k/month", "USD", False),  # an unlisted spaced code is never a bare "$"
])
def test_a_spaced_code_before_a_dollar_is_that_code(text, code, known):
    """Round 5: a currency code, a space and "$" is that code, never a bare "$"."""
    from kestrel.ingest.comp import parse_comp

    comp = parse_comp(text)
    assert (comp.currency, comp.currency_known, comp.bare_dollar) == (code, known, False)


def test_a_code_ending_one_line_is_not_the_currency_of_the_next_line():
    """Round 7 review: "... in USD" at a line's end and "$5" at the next line's start are two
    things; the figure is a bare "$" (USD only for a US posting)."""
    from kestrel.ingest.comp import parse_comp

    comp = parse_comp("Paid in CAD\n$100k-$120k a year")
    assert (comp.currency, comp.bare_dollar) == ("USD", True)


def test_an_ordinary_word_before_a_dollar_stays_a_bare_dollar():
    from kestrel.ingest.comp import parse_comp

    comp = parse_comp("Base pay $90k-$100k")
    assert (comp.currency, comp.currency_known, comp.bare_dollar) == ("USD", True, True)


def test_a_singapore_band_is_never_upheld_against_a_usd_floor():
    from kestrel.ingest.comp import parse_comp

    comp = parse_comp("S$90k-95k")
    r = _post("comp", _opp(comp_min=comp.min, comp_max=comp.max, comp_currency=comp.currency),
              _profile(comp_floor_usd=120000))
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("consider", "other_currency")


@pytest.mark.parametrize("over, reason", [
    ({"comp_interval_known": False}, "no_interval"),  # "40-50" with no unit
    ({"comp_interval_known": None}, "no_interval"),  # a row from before 0037
    ({"comp_min": 40, "comp_max": 50}, "implausible"),  # a stated year that is not a salary
])
def test_an_unknown_interval_or_an_implausible_salary_never_forces_pass(over, reason):
    r = _post("comp", _opp(**over), _profile())
    assert (r.verdict, r.blockers[0]["check_reason"]) == ("consider", reason)
