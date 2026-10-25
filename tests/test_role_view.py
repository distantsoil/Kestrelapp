"""The Role page's data builders (kestrel.web.role_view): pure functions, no database."""

from __future__ import annotations

from types import SimpleNamespace

from kestrel.web import role_view as rv

EV = {
    "dimensions": {"role_fit": {"score": 4.7, "why": "Maps directly."}, "trajectory": 4.0},
    "blocks": {
        "b_fit": {
            "matches": [
                {"requirement": "A", "evidence": "Proof A", "strength": "strong"},
                {"requirement": "B", "evidence": "Proof B", "strength": "adjacent"},
            ],
            "gaps": [
                {"requirement": "C", "severity": "addressable", "mitigation": "Do X"},
                {"requirement": "D", "severity": "noise"},
                {"requirement": "E", "severity": "fatal"},
            ],
        },
        "c_level": {"relative_level": "above"},
        "d_comp": {"vs_floor": "above_target"},
    },
}
OPP = SimpleNamespace(
    track="employment",
    comp_min=100000,
    comp_max=120000,
    comp_interval="year",
    comp_currency="USD",
    comp_source="parsed",
    stage_reason=None,
)


def test_dimensions_use_the_plain_words_and_skip_what_the_model_left_out():
    rows = rv.dimension_rows(EV, "employment")
    assert [r["name"] for r in rows] == ["Role fit", "Career direction"]
    assert rows[0]["pct"] == 94 and rows[1]["why"] == ""


def test_requirements_split_met_partly_and_not_met_and_drop_noise():
    reqs = rv.requirements(OPP, EV)
    assert [(r["text"], r["state"]) for r in reqs] == [
        ("A", "met"),
        ("B", "part"),
        ("C", "gap"),
        ("E", "gap"),
    ]
    assert reqs[3]["fatal"] is True and reqs[2]["how"] == "Do X"
    w = rv.wheel(reqs)
    assert w["center"] == "1 of 4" and (w["met"], w["part"], w["gap"]) == (1, 1, 2)
    assert len(w["arcs"]) == 4 and w["arcs"][1]["offset"].startswith("-")
    assert rv.wheel([])["center"] == "0 of 0"


def test_the_read_names_the_models_evidence_and_never_invents():
    card = rv.read_card(OPP, EV, "strong fit")
    assert card["title"] == "Why it is a strong match"
    assert "Proof A." in card["text"] and "The pay sits above your target." in card["text"]
    assert "Worth watching: C." in card["text"]
    assert rv.read_card(OPP, {"blocks": {}}, "strong fit") is None


def test_pay_bracket_needs_five_observations_hard_rule_6():
    few = rv.pay_band(OPP, EV, None, 3, 90000, 110000)
    assert few["similar"] == {"text": "Similar roles: Too few (3).", "n": 3, "enough": False}
    assert few["band"]["bracket"] is None and few["band"]["marker"] is not None
    stats = {"n": 6, "p25": 105000, "p75": 125000, "lo": 1, "hi": 2, "median": 1, "scope": "TX"}
    ok = rv.pay_band(OPP, EV, stats, 6, 90000, 110000)
    assert ok["similar"]["enough"] and "6 roles" in ok["similar"]["text"]
    assert ok["band"]["bracket"]["width"] > 0 and ok["tone"] == "good"
    # Close floor and target do not print their labels on top of each other.
    close = rv.pay_band(OPP, EV, None, 0, 100000, 101000)
    assert close["band"]["ticks"][1]["low"] is True


def test_level_ladder_marks_the_role_and_you():
    box = rv.level_box(OPP, EV)
    pins = {s["word"]: s["pin"] for s in box["ladder"]}
    assert pins == {"Below": "", "Your level": "You", "Above": "This role"}
    assert (
        rv.level_box(OPP, {"blocks": {"c_level": {"relative_level": "unclear"}}})["ladder"] is None
    )


def test_history_is_newest_first_and_ends_with_where_it_was_found():
    items = [
        {
            "kind": "evaluation",
            "at": "2026-10-12T05:00:00+00:00",
            "tier": 2,
            "score": 4.6,
            "model": "m",
            "prompt": "evaluate/v7",
            "verdict": "apply",
            "label": "x",
            "detail": "",
        },
        {
            "kind": "evaluation",
            "at": "2026-10-11T05:00:00+00:00",
            "tier": 1,
            "score": None,
            "verdict": "keep",
            "label": "x",
            "detail": "",
            "model": None,
            "prompt": None,
        },
        {
            "kind": "label",
            "at": "2026-10-13T05:00:00+00:00",
            "label": "You rated the fit",
            "detail": "Good",
        },
    ]
    opp = SimpleNamespace(first_seen_at="2026-10-10T05:00:00+00:00")
    tl = rv.history_timeline(items, opp, "Indeed", 1, None)
    assert [t["title"] for t in tl] == [
        "You rated the fit",
        "Scored 4.6 by Full review",
        "Passed the quick screen",
        "Found on Indeed and 1 other place",
    ]
    assert tl[1]["sub"] == "m · prompt version evaluate/v7"
