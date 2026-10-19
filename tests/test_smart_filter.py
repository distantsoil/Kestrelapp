"""The found-board smart filter (D-SRC-8): pure decision and the learned words. No network."""

from __future__ import annotations

from types import SimpleNamespace

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
