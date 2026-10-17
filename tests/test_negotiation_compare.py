"""Offer comparison matrix: quantitative axes come from valuation, qualitative axes from the
operator's ratings, each axis is normalized across the offers (risk inverted), and the weighted
total is computed. It is analysis, never a recommendation. Pure and offline."""

from __future__ import annotations

import pytest

from kestrel.negotiation.compare import AXES, OfferComp, compare_offers, offer_comp_from


def _a() -> OfferComp:
    return OfferComp(
        offer_id=1, label="Offer A", first_year=300000, four_year=1200000,
        qual={"level_scope": 5, "reporting_line": 4, "location_travel": 3,
              "org_risk": 1, "trajectory_fit": 5},
    )


def _b() -> OfferComp:
    return OfferComp(
        offer_id=2, label="Offer B", first_year=250000, four_year=1000000,
        qual={"level_scope": 4, "reporting_line": 5, "location_travel": 5,
              "org_risk": 3, "trajectory_fit": 3},
    )


def test_matrix_computes_normalized_and_weighted_totals():
    m = compare_offers([_a(), _b()])
    assert m.axes == list(AXES)
    a, b = m.rows

    # Quantitative axes scale to the max across offers (higher is better).
    assert a["normalized"]["total_first_year"] == 1.0
    assert b["normalized"]["total_first_year"] == pytest.approx(0.8333, abs=1e-4)
    assert a["normalized"]["four_year_value"] == 1.0

    # org_risk is inverted: A's risk 1 -> 0.8, B's risk 3 -> 0.4.
    assert a["normalized"]["org_risk"] == 0.8
    assert b["normalized"]["org_risk"] == 0.4

    # Equal default weights over all seven present axes.
    assert a["weighted_total"] == pytest.approx(6.2 / 7, abs=1e-4)
    assert b["weighted_total"] == pytest.approx(5.46667 / 7, abs=1e-3)
    assert a["weighted_total"] > b["weighted_total"]  # A wins on the numbers

    # It is analysis, never a recommendation: no accept/verdict field anywhere.
    assert "recommend" not in m.note.lower() or "not a recommendation" in m.note.lower()
    assert all("accept" not in k for row in m.rows for k in row)


def test_weights_shift_the_outcome():
    # Weight location_travel and reporting_line (where B leads) heavily; B should overtake.
    weights = {a: 0.01 for a in AXES}
    weights["location_travel"] = 5.0
    weights["reporting_line"] = 5.0
    m = compare_offers([_a(), _b()], weights)
    a, b = m.rows
    assert b["weighted_total"] > a["weighted_total"]


def test_missing_axis_is_excluded_not_penalized():
    a = OfferComp(offer_id=1, label="A", first_year=None, four_year=1200000,
                  qual={"level_scope": 5})
    m = compare_offers([a, _b()])
    row_a = m.rows[0]
    assert row_a["normalized"]["total_first_year"] is None  # missing, not zero
    # weighted_total averages only the present axes, so the missing one does not drag it to 0.
    assert row_a["weighted_total"] is not None and row_a["weighted_total"] > 0


def test_offer_comp_from_row_uses_valuation():
    class _Row:
        base = 200000
        bonus_target_pct = 10
        signing = 0
        equity_value_estimate = 0

    oc = offer_comp_from(_Row(), offer_id=7, label="row", qual={"org_risk": 2, "bogus": 9})
    assert oc.first_year == 220000  # 200k + 10% bonus
    assert oc.four_year == 880000  # (200k + 20k) * 4
    assert oc.qual == {"org_risk": 2}  # unknown axis dropped
