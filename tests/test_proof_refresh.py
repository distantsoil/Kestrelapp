"""Refresh proof points from my resume, and the narrative and proof point warnings (issue #208
items 6c and 6d; D-SCORE-26, D-SCORE-27).

INVARIANT (hard rule 2): a claim written by the refresh is one of the operator's own resume
bullets byte for byte, and only after the operator ticks it and presses Apply; a posted form
names rows by id and never carries text into profile.yml. Invented text only; offline.
"""

from __future__ import annotations

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from kestrel.services import profile_edit as pe
from kestrel.services import proof_refresh as pr
from tests import profile_seed

FRAG_A = "Led process improvement across two distribution centers,"
FRAG_B = "raising throughput 22%."
OLD = "Logged tide readings for a harbor survey team."


def _points() -> list[dict]:
    return [
        {"key": "p1", "claim": FRAG_A, "evidence": "ops", "metric": "22%"},
        {"key": "p2", "claim": FRAG_B, "evidence": "ops", "metric": None},
        {"key": "p3", "claim": OLD, "evidence": "lab", "metric": None},
    ]


def _bullets() -> list[pr.Bullet]:
    return [pr.Bullet(text=t, role="Operations Analyst", role_index=0)
            for t in profile_seed.BULLETS] + [
        pr.Bullet(text="Kept the stockroom count within 1%.", role="Clerk", role_index=3)]


def test_the_plan_fixes_a_cut_point_removes_strays_and_offers_new_lines():
    plan = pr.plan_refresh(_points(), _bullets())
    got = [(r.action, r.key, r.old, r.new, r.ticked) for r in plan.rows]
    assert got == [
        ("fix", "p1", FRAG_A, profile_seed.BULLETS[0], True),
        ("remove", "p2", FRAG_B, "", False),  # unchecked: the operator decides (review)
        ("remove", "p3", OLD, "", False),
        ("add", "", "", profile_seed.BULLETS[1], True),
        ("add", "", "", "Kept the stockroom count within 1%.", False),  # an older role
    ]
    assert plan.rows[1].reason == "Another proof point already has this whole line."
    assert plan.rows[2].reason == "This is not on your current resume."


def test_a_point_that_matches_a_line_is_kept_and_no_resume_means_no_plan():
    plan = pr.plan_refresh([{"key": "k", "claim": "  " + profile_seed.BULLETS[1].upper()}],
                           _bullets()[:2])
    assert plan.kept == 1 and [r.action for r in plan.rows] == ["add"]
    assert pr.plan_refresh(_points(), None).has_resume is False


def test_only_the_approved_rows_apply_and_text_is_the_bullet_byte_for_byte():
    odd = "Cut berth idle time 18%  across 3 ports – “quoted”."
    bullets = [pr.Bullet(text=odd, role="Lead", role_index=0)]
    points = [{"key": "p1", "claim": "Cut berth idle time 18% across 3 ports", "evidence": "x",
               "metric": "18%"}, {"key": "p2", "claim": OLD, "evidence": "y", "metric": None}]
    plan = pr.plan_refresh(points, bullets)
    fix = next(r for r in plan.rows if r.action == "fix")
    new, n = pr.apply_rows(points, plan, {fix.id})
    assert n == 1
    assert new == [{"key": "p1", "claim": odd, "evidence": "x", "metric": "18%"}, points[1]]
    same, n = pr.apply_rows(points, plan, {"not-a-row-id"})
    assert n == 0 and same == points


@pytest.mark.parametrize("points, narrative, n_warn", [
    ([], "", 2), ([{"claim": "x"}] * 5, "", 1), ([{"claim": "x"}] * 4, "Runs ports.", 1),
    ([{"claim": "x"}] * 5, "Runs ports.", 0),
])
def test_warnings_for_an_empty_narrative_and_too_few_points(points, narrative, n_warn):
    assert len(pr.warnings(points, narrative)) == n_warn


# ------------------------------------------------------------------------------ the routes
@pytest.fixture
def client():
    from kestrel.web.app import build_app

    profile_seed.seed()
    pe.save_section("employment", "proof_points", {"proof_points": _points()})
    with TestClient(build_app(worker=False)) as c:
        yield c


def _saved() -> dict:
    return yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))["employment"]


def test_documents_shows_the_card_with_its_warnings(client):
    soup = BeautifulSoup(client.get("/profile?tab=documents&track=employment").text,
                         "html.parser")
    card = soup.select_one('[data-testid="pf-proof-points"]')
    assert card is not None
    warns = [w.get_text(" ", strip=True) for w in card.select('[data-testid="pf-pp-warning"]')]
    assert warns == [
        "Your narrative is empty. Scoring reads it to understand what you do; add a few "
        "sentences below.",
        "You have 3 proof points. Scoring can only credit what they and your resume show; 5 or "
        "more works best.",
    ]
    assert card.select_one('[data-testid="pf-pp-refresh"]') is not None


def test_refresh_shows_the_diff_and_writes_nothing(client):
    before = pe.profile_path().read_bytes()
    soup = BeautifulSoup(client.get("/proof-points/refresh?track=employment").text,
                         "html.parser")
    rows = soup.select('[data-testid="pf-pp-row"]')
    assert [r["data-action"] for r in rows] == ["fix", "remove", "remove", "add"]
    assert rows[0].select_one('[data-testid="pf-pp-new"]').get_text(" ", strip=True) == (
        f"Becomes: {profile_seed.BULLETS[0]} (Operations Analyst)")
    assert pe.profile_path().read_bytes() == before


def test_apply_writes_only_the_ticked_rows_and_never_posted_text(client):
    soup = BeautifulSoup(client.get("/proof-points/refresh?track=employment").text,
                         "html.parser")
    ids = {r["data-action"] + str(i): r.select_one("input")["value"]
           for i, r in enumerate(soup.select('[data-testid="pf-pp-row"]'))}
    r = client.post("/proof-points/apply", data={
        "track": "employment", "row": [ids["fix0"], ids["add3"]],
        # A crafted field is ignored: only ids are read.
        "claim": "I invented this.", "new": "I invented this too.",
    })
    assert r.status_code == 200 and "Saved 2 changes to your proof points." in r.text
    claims = [p["claim"] for p in _saved()["proof_points"]]
    assert claims == [profile_seed.BULLETS[0], FRAG_B, OLD, profile_seed.BULLETS[1]]
    assert "invented" not in pe.profile_path().read_text(encoding="utf-8")


def test_apply_with_forged_ids_changes_nothing(client):
    before = pe.profile_path().read_bytes()
    r = client.post("/proof-points/apply", data={"track": "employment", "row": ["0" * 16]})
    assert "Nothing was checked, so nothing changed." in r.text
    assert pe.profile_path().read_bytes() == before


def test_the_narrative_saves_from_the_card(client):
    r = client.post("/proof-points/narrative",
                    data={"track": "employment", "narrative": "  Runs regional operations.  "})
    assert "Narrative saved." in r.text
    assert _saved()["narrative"] == "Runs regional operations."
    assert not BeautifulSoup(r.text, "html.parser").select(
        '[data-testid="pf-pp-warning"]')[0].get_text().startswith("Your narrative is empty")


def test_rows_name_points_by_position_so_repeated_or_blank_keys_change_one_point():
    points = [{"claim": OLD, "evidence": "", "metric": None},
              {"claim": OLD, "evidence": "", "metric": None}]
    plan = pr.plan_refresh(points, _bullets())
    first = next(r for r in plan.rows if r.action == "remove")
    new, n = pr.apply_rows(points, plan, {first.id})
    assert n == 1 and new == [points[1]]


def test_a_short_claim_is_never_matched_as_a_fragment_of_a_longer_line():
    # Below 20 characters a few words appear in many lines: Remove is offered, never Fix.
    plan = pr.plan_refresh([{"key": "s", "claim": "Led process"}], _bullets())
    assert [r.action for r in plan.rows if r.key == "s"] == ["remove"]
