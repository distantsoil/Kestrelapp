"""Issue #66: Tier 2 does not score a stub posting as if complete (and never rejects it), and a
Glassdoor pay estimate is labelled an estimate in what the model reads as on the page.
The model is faked; nothing reaches a network."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity, Quarantine, Source, Task
from kestrel.ingest.adapters.jobspy import JobSpyAdapter
from kestrel.ingest.base import RawPosting
from kestrel.ingest.normalize import _resolve_comp
from kestrel.pipeline.context import comp_display
from kestrel.pipeline.profile import load_profile
from kestrel.pipeline.tier2 import handle_t2, is_stub_posting
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON, EMPTY_POSTING_WHY, hidden_unscored_why
from kestrel.web.pipeline_view import is_estimate, pay_text
from tests.pipeline_fakes import FakeLLM

_BODY = "Lead the IT organization, own the budget, vendors and security. " * 8


class _CountingLLM(FakeLLM):
    calls = 0

    async def complete(self, **kw):
        type(self).calls += 1
        return await super().complete(**kw)


async def _seed(sm, description: str | None, **extra) -> int:
    async with sm() as s:
        o = Opportunity(**{
            "track": "employment", "opp_type": "job_posting", "title": "VP of IT",
            "org_name": "Acme", "comp_max": 250000, "comp_source": "structured",
            "seen_count": 1, "ghost_score": 0.1, "stage": "t1_pass",
            "description_md": description, **extra,
        })
        s.add(o)
        await s.commit()
        return o.id


async def _run(sm, oid: int, llm_cls=_CountingLLM):
    settings = get_settings()
    llm_cls.calls = 0
    profiles = {t: load_profile(t) for t in ("employment", "consulting")}
    out = await handle_t2(sm, llm_cls(settings), settings, opportunity_id=oid, profiles=profiles)
    return out, llm_cls.calls


async def _quarantine_rows(sm, oid):
    async with sm() as s:
        return (await s.execute(select(Quarantine).where(Quarantine.opportunity_id == oid))).all()


async def test_empty_description_is_not_scored_and_is_not_a_failure(sm):
    oid = await _seed(sm, "")
    for _ in range(3):  # a Retry or a re-leased task must not pile up anything
        out, calls = await _run(sm, oid)
        assert out.action == "skip" and calls == 0  # the model was never asked
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        evals = (await s.execute(select(Evaluation))).all()
    assert await _quarantine_rows(sm, oid) == []  # not Needs another try, nothing to Retry
    assert opp.stage == "t1_pass" and opp.archived_at is None  # kept, visible, not rejected
    assert opp.stage_reason == EMPTY_POSTING_STAGE_REASON
    assert hidden_unscored_why(opp) == EMPTY_POSTING_WHY
    assert evals == []


async def test_a_stub_under_the_word_floor_is_not_scored_too(sm):
    oid = await _seed(sm, "Great team.\n\n\n  Apply now.")
    out, calls = await _run(sm, oid)
    assert out.action == "skip" and calls == 0


async def test_a_manual_paste_is_scored_however_short(sm):
    async with sm() as s:
        src = Source(track="employment", kind="manual", name="Manual capture (employment)",
                     config={}, enabled=False)
        s.add(src)
        await s.commit()
        sid = src.id
    oid = await _seed(sm, "VP of IT at Acme, $250k, remote.", source_id=sid)
    out, calls = await _run(sm, oid)
    assert out.action == "t2_done" and calls == 1


async def test_non_posting_types_are_scored_without_a_body(sm):
    oid = await _seed(sm, "", opp_type="rfp")
    out, calls = await _run(sm, oid)
    assert out.action == "t2_done" and calls == 1


def test_unspaced_text_is_counted_by_characters():
    long_cjk = "我们正在寻找一位信息技术副总裁负责预算供应商和安全。" * 8  # no spaces, 200+ chars
    assert not is_stub_posting(SimpleNamespace(
        trigger_data=None, event_date=None, opp_type="job_posting", description_md=long_cjk))
    assert is_stub_posting(SimpleNamespace(
        trigger_data=None, event_date=None, opp_type="job_posting", description_md="招聘副总裁"))


async def test_a_re_scrape_to_a_stub_keeps_an_existing_score_and_stage(sm):
    oid = await _seed(sm, _BODY)
    await _run(sm, oid)
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        assert opp.stage == "t2_done"
        opp.description_md = "Great team."
        opp.version += 1
        await s.commit()
    settings = get_settings()
    profiles = {t: load_profile(t) for t in ("employment", "consulting")}
    _CountingLLM.calls = 0
    out = await handle_t2(sm, _CountingLLM(settings), settings, opportunity_id=oid,
                          profiles=profiles, changed=True)
    assert out.action == "skip" and _CountingLLM.calls == 0
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t2_done"
        assert len((await s.execute(select(Evaluation))).all()) == 1
    assert await _quarantine_rows(sm, oid) == []


async def test_an_automatic_rescore_still_skips_a_stub(sm):
    oid = await _seed(sm, "")
    settings = get_settings()
    profiles = {t: load_profile(t) for t in ("employment", "consulting")}
    _CountingLLM.calls = 0
    out = await handle_t2(sm, _CountingLLM(settings), settings, opportunity_id=oid,
                          profiles=profiles, rescore=True)
    assert out.action == "skip" and _CountingLLM.calls == 0


async def test_the_operator_score_again_scores_a_stub_through_the_worker(sm):
    from kestrel.pipeline.worker import TierStats, Worker
    from kestrel.queue.rescore import enqueue_rescore

    oid = await _seed(sm, "Great team.")
    await _run(sm, oid)  # the normal pass leaves it Not scored
    async with sm() as s:
        await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
        task = (await s.execute(select(Task).where(Task.status == "pending"))).scalars().first()
    assert task.payload["score_stub"] is True
    async with sm() as s:  # an automatic bulk rescore does not carry the flag
        await s.delete(await s.get(Task, task.id))
        await s.commit()
        await enqueue_rescore(s, [oid])
        await s.commit()
        plain = (await s.execute(select(Task).where(Task.status == "pending"))).scalars().first()
        assert "score_stub" not in plain.payload
        await s.delete(plain)
        await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    settings = get_settings()
    _CountingLLM.calls = 0
    await Worker(sm, _CountingLLM(settings), settings).drain_tier(
        "t2", 1, handle_t2, TierStats()
    )
    assert _CountingLLM.calls == 1
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t2_done"


async def test_a_re_scrape_that_brings_a_body_scores_it(sm):
    oid = await _seed(sm, "")
    await _run(sm, oid)
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        opp.description_md = _BODY
        opp.version += 1
        await s.commit()
    settings = get_settings()
    profiles = {t: load_profile(t) for t in ("employment", "consulting")}
    _CountingLLM.calls = 0
    out = await handle_t2(sm, _CountingLLM(settings), settings, opportunity_id=oid,
                          profiles=profiles, changed=True)
    assert out.action == "t2_done" and _CountingLLM.calls == 1


async def test_a_normal_description_still_scores(sm):
    oid = await _seed(sm, _BODY)
    out, calls = await _run(sm, oid)
    assert out.action == "t2_done" and calls == 1
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t2_done"


async def test_a_trigger_role_without_a_body_is_still_scored(sm):
    oid = await _seed(sm, "", trigger_data={"event_type": "funding", "evidence_quote": "q"})
    out, calls = await _run(sm, oid)
    assert out.action == "t2_done" and calls == 1


def test_glassdoor_pay_is_an_estimate_everywhere():
    row = {"site": "glassdoor", "min_amount": 228800.0, "max_amount": 364000.0,
           "currency": "USD", "interval": "yearly", "title": "VP", "job_url": "u"}
    posting = JobSpyAdapter()._row_to_posting(row)
    assert posting.comp_is_estimate is True
    comp, source = _resolve_comp(posting, None, "employment")
    assert source == "inferred" and comp.max == 364000.0
    opp = SimpleNamespace(comp_min=comp.min, comp_max=comp.max, comp_currency="USD",
                          comp_interval="year", comp_source=source)
    # The page says est.; the model is told it is an estimate, with the same figures.
    assert is_estimate(opp) and pay_text(opp).endswith("est.")
    shown = comp_display(opp)
    assert "$228,800 - $364,000 / year" in shown and "estimate" in shown


def test_indeed_pay_and_posted_pay_stay_posted():
    row = {"site": "indeed", "min_amount": 100000.0, "max_amount": 120000.0,
           "currency": "USD", "interval": "yearly", "title": "VP", "job_url": "u"}
    posting = JobSpyAdapter()._row_to_posting(row)
    assert posting.comp_is_estimate is False
    assert _resolve_comp(posting, None, "employment")[1] == "structured"
    assert _resolve_comp(RawPosting(comp_min=1, comp_max=2), None, "employment")[1] == "structured"
    opp = SimpleNamespace(comp_min=100000, comp_max=120000, comp_currency="USD",
                          comp_interval="year", comp_source="structured")
    assert comp_display(opp) == "$100,000 - $120,000 / year" and not is_estimate(opp)


def test_an_estimate_never_triggers_a_pay_floor_rejection():
    from kestrel.rules import helpers as h

    est = SimpleNamespace(comp_max=100000, comp_source="inferred")
    posted = SimpleNamespace(comp_max=100000, comp_source="structured")
    assert h.comp_max_below(est, 200000) is False and h.comp_max_below(posted, 200000) is True
    assert h.comp_max_at_least(est, 50000) is False and h.comp_max_at_least(posted, 50000) is True
    from kestrel.rules.margin import comp_max_near_floor

    assert comp_max_near_floor(est, 105000, 10) is False
    assert comp_max_near_floor(posted, 105000, 10) is True


def test_the_floor_reason_never_calls_an_estimate_posted():
    from kestrel.web.fit import _detail

    profile = {"employment_terms": {"comp_floor_usd": 200000}}
    est = SimpleNamespace(comp_max=100000, comp_source="inferred")
    text = _detail("comp_floor", est, profile, None, None)
    assert "estimate" in text and "posted pay is" not in text
    posted = SimpleNamespace(comp_max=100000, comp_source="structured")
    assert "top of the posted pay" in _detail("comp_floor", posted, profile, None, None)


def test_company_pay_view_leaves_estimates_out():
    from kestrel.web.company_view import pay_view

    def opp(i, src):
        return SimpleNamespace(title=f"t{i}", comp_interval="year", comp_min=100000 + i,
                               comp_max=120000 + i, comp_currency="USD", comp_source=src)

    assert pay_view([opp(i, "structured") for i in range(5)])["enough"] is True
    assert pay_view([opp(i, "inferred") for i in range(5)]) == {"n": 0, "enough": False}


async def test_comp_intel_ignores_estimates(sm):
    from kestrel.db.models import CompObservation
    from kestrel.pipeline.context import comp_intel_count, comp_intel_stats

    async with sm() as s:
        for i in range(6):
            s.add(CompObservation(normalized_title="vp of it", geo_bucket="unknown",
                                  min=100000 + i, max=120000 + i, currency="USD",
                                  interval="year", source="inferred"))
        await s.commit()
        opp = SimpleNamespace(title="VP of IT", state=None, country=None, city=None)
        assert await comp_intel_stats(s, opp) is None and await comp_intel_count(s, opp) == 0
