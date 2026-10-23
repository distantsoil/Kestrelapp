"""The found-board smart filter (D-SRC-8): pure decision and the learned words. No network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kestrel.db.models import Opportunity, Pursuit
from kestrel.ingest import smart_filter as sf


def _ctx(floor="director", targets=("IT Director",), learned=frozenset()):
    return sf.FilterContext(
        floor_rank=sf.helpers.seniority_rank(floor) if floor else -1,
        tokens=sf.profile_tokens(list(targets)) | learned,
    )


def test_field_tokens_drop_level_stopwords_digits():
    got = sf.field_tokens("Senior Director of IT Operations 2")
    assert "information_technology" in got and not ({"senior", "director", "of", "2"} & got)
    assert "of" not in sf.field_tokens("Head of Security")
    assert not sf.field_tokens("Remote Role")


def test_keep_matching_field_at_floor():
    assert sf.keep("Director of IT Services", _ctx())


def test_below_floor_is_not_kept():
    assert not sf.keep("IT Support Analyst", _ctx())


def test_unknown_level_is_kept_when_field_matches():
    assert sf.keep("IT Strategy Partner", _ctx())


def test_other_field_not_kept():
    assert not sf.keep("Director of Marketing", _ctx())


def test_learned_field_is_kept():
    ctx = _ctx(learned=frozenset({"marketing"}))
    assert sf.keep("Director of Marketing", ctx)


def test_blank_title_and_nothing_to_judge_keep():
    assert sf.keep(None, _ctx())
    assert sf.keep("   ", _ctx())
    empty = sf.FilterContext(floor_rank=-1, tokens=frozenset())
    assert sf.keep("Director of Anything", empty)


def test_no_floor_passes_level_test():
    assert sf.keep("IT Analyst", _ctx(floor=""))


async def test_learned_tokens_need_two_pursuits(sm):
    async with sm() as s:
        for i, t in enumerate(["Director of Logistics", "Logistics Manager", "Chef de Cuisine"]):
            o = Opportunity(
                track="employment", opp_type="job", title=t, external_id=f"x{i}", stage="new"
            )
            s.add(o)
            await s.flush()
            s.add(Pursuit(opportunity_id=o.id, track="employment", status="shortlisted"))
        await s.commit()
    async with sm() as s:
        got = await sf.learned_tokens(s, "employment")
        assert "logistic" in got
        assert not any(t in got for t in ("chef", "cuisine"))
        ctx = await sf.build_context(
            s, "employment",
            SimpleNamespace(employment_terms=SimpleNamespace(
                seniority_floor="director", target_titles=["IT Director"])),
        )
        assert ctx.floor_rank == sf.helpers.seniority_rank("director")
        assert "information_technology" in ctx.tokens


# ------------------------------------------------------------------ precision report (#22)
async def _seed_report(sm, fast_fits=5, fast_not=1, lane_fits=1, lane_not=4):
    """Found-board roles in both lanes with their final verdicts, plus rows the report must
    ignore: a hand-added board, a rules reject, and a role still waiting to be scored."""
    from kestrel.db.models import Evaluation, Source
    from kestrel.pipeline.lane import LANE_REASON

    async with sm() as s:
        found = Source(track="employment", kind="greenhouse", name="found", origin="discovered")
        hand = Source(track="employment", kind="greenhouse", name="hand", origin="manual")
        s.add_all([found, hand])
        await s.flush()

        async def role(src, n, lane, tier, verdict, stage="t2_done"):
            o = Opportunity(
                track="employment", opp_type="job", title=f"role {n}", external_id=f"r{n}",
                stage=stage, source_id=src.id,
                stage_reason=f"soft: none; {LANE_REASON}" if lane else "soft: none",
            )
            s.add(o)
            await s.flush()
            if verdict:
                s.add(Evaluation(opportunity_id=o.id, tier=1, track="employment",
                                 verdict="keep"))
                s.add(Evaluation(opportunity_id=o.id, tier=tier, track="employment",
                                 verdict=verdict))

        n = 0
        for lane, fits, nots in ((False, fast_fits, fast_not), (True, lane_fits, lane_not)):
            for _ in range(fits):
                n += 1
                await role(found, n, lane, 2, "consider")
            for i in range(nots):
                n += 1
                await role(found, n, lane, 1 if i % 2 else 2, "kill" if i % 2 else "pass")
        await role(hand, 900, False, 2, "apply")  # not a found board: ignored
        await role(found, 901, False, 0, None, stage="t0_reject")  # rules set it aside
        await role(found, 902, True, 0, None, stage="t1_pending")  # not scored yet
        await s.commit()


async def test_report_counts_per_lane_and_labels_small_samples(sm):
    from kestrel.evals import smart_filter_report as rep

    await _seed_report(sm)
    async with sm() as s:
        r = await rep.build_report(s, "employment")
    fast, lane = r["fast"], r["background"]
    assert (fast["roles"], fast["scored"], fast["fits"], fast["not_fits"]) == (7, 6, 5, 1)
    assert (lane["roles"], lane["scored"], lane["fits"], lane["not_fits"]) == (6, 5, 1, 4)
    assert fast["unscored"] == 1 and lane["unscored"] == 1  # the rules reject / the waiting one
    assert fast["precision"] == pytest.approx(5 / 6) and fast["precision_sufficient"]
    assert r["miss_rate"] == pytest.approx(1 / 6)  # fits in the lane / all fits
    assert r["miss_sufficient"] is True  # six fits in all
    text = rep.render(r)
    assert "83%" in text and "17%" in text and "—" not in text


async def test_report_below_five_scored_is_insufficient_not_a_percentage(sm):
    from kestrel.evals import smart_filter_report as rep

    await _seed_report(sm, fast_fits=2, fast_not=1, lane_fits=0, lane_not=0)
    async with sm() as s:
        r = await rep.build_report(s, "employment")
    assert r["fast"]["scored"] == 3 and not r["fast"]["precision_sufficient"]
    assert r["background"]["scored"] == 0 and not r["miss_sufficient"]
    out = rep.render(r)
    assert "insufficient (3 scored" in out and "67%" not in out
    assert "miss rate: insufficient" in out


async def test_report_is_read_only(sm):
    from sqlalchemy import event

    from kestrel.evals import smart_filter_report as rep

    await _seed_report(sm)
    writes: list[str] = []
    async with sm() as s:
        event.listen(
            s.sync_session.get_bind(), "before_cursor_execute",
            lambda c, cur, stmt, *a: writes.append(stmt)
            if stmt.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"} else None,
        )
        await rep.build_report(s, "employment")
    assert writes == []
