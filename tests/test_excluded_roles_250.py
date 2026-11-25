"""Issue #250: a role the person's filters exclude is never scored or surfaced.

The named cases, each a path that could send a role to a model:
  - a source run (search or company board): an unrelated title is dropped before storage;
  - a source run that sees a stored role again with materially changed text: Fit score runs
    again only for a role already past Quick sort, never for one the rules set aside, one Quick
    sort set aside, one not screened yet (the normal flow reads the new text anyway), or one
    whose scoring failed (the stage does not say which step failed; it waits for the person);
  - manual add, and the extension's capture (the same `manual_ingest` call from
    `opportunities_ingest`): the title filter is bypassed by design, the rules still run;
  - Score again: a role a rule set aside is refused unless the person chose Score anyway;
  - a retry of a failed row: refused for a role that is set aside.
Signal sources have no title drop by specification (SPEC.md, D-SRC-17), and the legacy lane's
recheck is covered by tests/test_lane.py. All text is invented; the model is faked."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import func, select

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity, Quarantine, Source, Task
from kestrel.ingest import runner
from kestrel.ingest.base import RawPosting
from kestrel.ingest.http import HttpClient
from kestrel.ingest.manual import manual_ingest
from kestrel.pipeline.profile import load_profile
from kestrel.pipeline.tier0 import run_tier0_batch
from kestrel.pipeline.tier2 import handle_t2
from kestrel.queue import row_retry
from kestrel.queue.rescore import enqueue_rescore, past_tier1
from tests.pipeline_fakes import FakeLLM

_BODY = "Lead the IT organization, own the budget, vendors and security. " * 8
_NEW_BODY = "Run platform planning, staffing, audits and supplier reviews across regions. " * 8

# Not past Quick sort for certain: a changed posting must not send these to Fit score. A failed
# role is here too: a Quick sort task that ran out of tries also parks a role at `quarantined`.
NOT_PAST = ("new", "t0_pass", "t0_reject", "t1_reject", "quarantined", "error")
# Past Quick sort: a changed posting is scored again (no regression).
PAST = ("t1_pass", "t2_done", "t3_done")


class _CountingLLM(FakeLLM):
    calls = 0

    async def complete(self, **kw):
        type(self).calls += 1
        return await super().complete(**kw)


def _profile(**terms) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        yaml.safe_dump({"employment": {
            "employment_terms": {"target_titles": ["Director of IT"], **terms},
            "geo_rules": {"remote_country_allowlist": ["US"]},
        }}),
        encoding="utf-8",
    )


async def _seed(sm, stage: str, **extra) -> int:
    async with sm() as s:
        o = Opportunity(**{
            "track": "employment", "opp_type": "job_posting", "title": "Director of IT",
            "org_name": "Fictional Org", "comp_max": 250000, "comp_source": "structured",
            "seen_count": 1, "ghost_score": 0.1, "stage": stage, "stage_reason": "seeded",
            "description_md": _BODY, **extra,
        })
        s.add(o)
        await s.commit()
        return o.id


async def _changed_t2(sm, oid: int):
    settings = get_settings()
    _CountingLLM.calls = 0
    profiles = {t: load_profile(t) for t in ("employment", "consulting")}
    out = await handle_t2(sm, _CountingLLM(settings), settings, opportunity_id=oid,
                          profiles=profiles, changed=True)
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        evals = (await s.execute(select(func.count()).select_from(Evaluation).where(
            Evaluation.opportunity_id == oid, Evaluation.tier == 2))).scalar_one()
    return out, _CountingLLM.calls, opp, evals


def test_one_definition_of_past_quick_sort():
    assert [s for s in NOT_PAST if past_tier1(s)] == []
    assert [s for s in PAST if not past_tier1(s)] == []
    assert not past_tier1(None)
    # The person's own retry of a failed Fit score row may also run from a failed role.
    assert row_retry.RUNS_FROM["t2"] == (*PAST, "quarantined", "error")


# ------------------------------------------------------------------ changed text: the handler
@pytest.mark.parametrize("stage", NOT_PAST)
async def test_changed_text_never_scores_a_role_not_past_quick_sort(sm, stage):
    oid = await _seed(sm, stage)
    out, calls, opp, evals = await _changed_t2(sm, oid)
    assert calls == 0  # the model was never asked
    assert out.action == "skip"
    assert (opp.stage, opp.stage_reason) == (stage, "seeded")  # not moved, not surfaced
    assert evals == 0


async def test_changed_text_scores_a_role_quick_sort_kept(sm):
    oid = await _seed(sm, "t1_pass")
    out, calls, opp, evals = await _changed_t2(sm, oid)
    assert (out.action, calls, opp.stage, evals) == ("t2_done", 1, "t2_done", 1)


async def test_changed_text_scores_an_already_scored_role_again(sm):
    oid = await _seed(sm, "t1_pass")
    await _changed_t2(sm, oid)
    async with sm() as s:  # the posting changed materially: new text, next version
        opp = await s.get(Opportunity, oid)
        assert opp.stage == "t2_done"
        opp.description_md = _NEW_BODY
        opp.version += 1
        await s.commit()
    out, calls, opp, evals = await _changed_t2(sm, oid)
    assert (out.action, calls, opp.stage, evals) == ("t2_done", 1, "t2_done", 2)


# ------------------------------------------------------------------ changed text: the source run
def _posting(body: str, title: str = "Director of IT") -> RawPosting:
    return RawPosting(external_id="r1", title=title, org_name="Fictional Org",
                      description_md=body, location_raw="Remote",
                      url="https://example.test/jobs/r1")


async def _source_run(sm, monkeypatch, sid: int | None, posting: RawPosting) -> int:
    class Fake:
        def validate_config(self, config: dict) -> None:
            pass

        async def fetch(self, config: dict, http: HttpClient) -> list[RawPosting]:
            return [posting]

    monkeypatch.setattr(runner, "get_adapter", lambda kind: Fake())
    monkeypatch.setattr(runner.board_discovery, "discover_boards", AsyncMock(return_value=0))
    if sid is None:
        async with sm() as s:
            src = Source(track="employment", kind="rss", name="Fake board", config={},
                         enabled=True)
            s.add(src)
            await s.commit()
            sid = src.id
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        await runner.run_source(sm, sid, http=http)
    return sid


async def _reseen_changed(sm, monkeypatch, stage: str) -> tuple[Opportunity, list[dict]]:
    """Store a role from a source, put it at `stage`, then see it again with new text."""
    _profile()
    sid = await _source_run(sm, monkeypatch, None, _posting(_BODY))
    async with sm() as s:
        opp = (await s.execute(select(Opportunity))).scalar_one()
        opp.stage, opp.stage_reason = stage, "seeded"
        await s.commit()
        oid, version = opp.id, opp.version
    await _source_run(sm, monkeypatch, sid, _posting(_NEW_BODY))
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        assert opp.version == version + 1  # the text did change materially
        assert opp.description_md.strip() == _NEW_BODY.strip()
        t2 = [dict(t.payload) for t in (await s.execute(
            select(Task).where(Task.type == "t2"))).scalars()]
    return opp, t2


@pytest.mark.parametrize("stage", NOT_PAST)
async def test_source_run_queues_no_fit_score_for_a_role_not_past_quick_sort(
        sm, monkeypatch, stage):
    opp, t2 = await _reseen_changed(sm, monkeypatch, stage)
    assert t2 == []
    assert (opp.stage, opp.stage_reason) == (stage, "seeded")


@pytest.mark.parametrize("stage", ("t1_pass", "t2_done"))
async def test_source_run_still_queues_fit_score_for_a_kept_role(sm, monkeypatch, stage):
    opp, t2 = await _reseen_changed(sm, monkeypatch, stage)
    assert [(p["opportunity_id"], p["changed"], p["version"]) for p in t2] == [
        (opp.id, True, opp.version)]


async def test_a_set_aside_role_seen_again_with_new_text_is_never_scored(sm, monkeypatch):
    """The whole path of the defect: source run, then whatever Fit score task exists runs."""
    opp, t2 = await _reseen_changed(sm, monkeypatch, "t0_reject")
    for payload in t2 or [{"changed": True}]:  # run the handler even when nothing was queued
        out, calls, after, evals = await _changed_t2(sm, opp.id)
        assert (calls, evals, after.stage) == (0, 0, "t0_reject")
        assert payload.get("changed") is True and out.action == "skip"


# ------------------------------------------------------------------ the other named cases
async def test_source_run_drops_an_unrelated_title_before_storage(sm, monkeypatch):
    _profile()
    await _source_run(sm, monkeypatch, None, _posting(_BODY, title="Pastry Chef"))
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Opportunity))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(Task))).scalar_one() == 0


async def test_manual_add_and_capture_still_meet_the_rules(sm):
    """Manual add and the extension capture share manual_ingest: the title filter is bypassed
    (the person chose the role), and an excluded title is still set aside with no model task."""
    _profile(anti_titles=["Pastry Chef"])
    async with sm() as s:
        opp = await manual_ingest(s, url="https://example.test/jobs/m1", text=_BODY,
                                  title="Pastry Chef", org_name="Fictional Org")
        oid = opp.id
    await run_tier0_batch(sm, settings=get_settings())
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        scoring = (await s.execute(select(func.count()).select_from(Task).where(
            Task.type.in_(("t1", "t2", "t3"))))).scalar_one()
    assert opp.stage == "t0_reject" and opp.stage_reason.startswith("blocker:anti_title")
    assert scoring == 0


async def test_score_again_refuses_a_set_aside_role(sm):
    oid = await _seed(sm, "t0_reject")
    async with sm() as s:
        result = await enqueue_rescore(s, [oid])
        await s.commit()
        tasks = (await s.execute(select(func.count()).select_from(Task))).scalar_one()
    assert (result.rules, result.queued, tasks) == ([oid], [], 0)


@pytest.mark.parametrize("stage", ("new", "t0_pass", "t0_reject", "t1_reject"))
async def test_a_row_retry_refuses_a_role_not_past_quick_sort(sm, stage):
    oid = await _seed(sm, stage)
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        plan = row_retry.plan(Quarantine(opportunity_id=oid, tier=2), opp)
    assert plan.task is None and plan.why == row_retry.MOVED_ON
