"""Analytics: the funnel computes correctly on seeded data, and the honest summary reports
non-discrimination when the score does not predict response, even if the model's prose is cheerful.
Fully offline (a fake P12 client, no live model)."""

from __future__ import annotations

import json

from kestrel.analytics.funnel import funnel
from kestrel.analytics.summary import honest_summary
from kestrel.config import get_settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    Evaluation,
    Opportunity,
    Org,
    Outcome,
    Pursuit,
    Source,
)
from kestrel.llm.client import ChatResult


async def _org_src(s):
    org = Org(name="Acme", normalized_name="acme")
    src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
    s.add_all([org, src])
    await s.flush()
    return org, src


def _opp(org, src, i, stage) -> Opportunity:
    return Opportunity(
        track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
        title="VP IT", org_name="Acme", url=f"https://x/{i}", stage=stage,
    )


async def test_funnel_computes_correctly(sm):
    async with sm() as s:
        org, src = await _org_src(s)
        # stages: 1 new, 1 t0_reject, 1 t1_pass, 2 t2_done
        opps = [
            _opp(org, src, 0, "new"),
            _opp(org, src, 1, "t0_reject"),
            _opp(org, src, 2, "t1_pass"),
            _opp(org, src, 3, "t2_done"),
            _opp(org, src, 4, "t2_done"),
        ]
        s.add_all(opps)
        await s.flush()
        # the two t2_done opps got a tier-2 evaluation
        for o in opps[3:]:
            s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment", verdict="apply",
                             verdict_model="apply", score=4.2, weighted_score=4.2,
                             needs_review=False, dimensions={}, blocks={}, blockers=[],
                             legitimacy={"status": "clean", "signals": []}))
        # pursuits: one applied, one interviewing
        s.add(Pursuit(opportunity_id=opps[3].id, track="employment", status="applied",
                      applied_at=utcnow_iso()))
        s.add(Pursuit(opportunity_id=opps[4].id, track="employment", status="interviewing"))
        await s.commit()

    async with sm() as s:
        fn = await funnel(s, "employment")
    steps = {st.name: st.count for st in fn.steps}
    assert steps["ingested"] == 5
    assert steps["tier0_pass"] == 3  # t1_pass + 2 t2_done
    assert steps["tier1_pass"] == 3
    assert steps["tier2_evaluated"] == 2
    assert steps["shortlisted"] == 2  # two pursuits
    assert steps["pursued"] == 2  # applied + interviewing both reached "pursued"
    assert steps["first_response"] == 1  # only the interviewing one
    assert steps["screening"] == 1
    assert steps["interview"] == 1
    assert steps["final"] == 0
    assert steps["offer"] == 0
    # conversion carries the sample size (previous step count)
    tier0 = next(st for st in fn.steps if st.name == "tier0_pass")
    assert tier0.sample_size == 5 and abs(tier0.conversion - 3 / 5) < 1e-9


async def test_quality_metrics_ignores_trackless_and_other_tier_quarantines(sm):
    from kestrel.analytics.metrics import quality_metrics
    from kestrel.db.models import Quarantine

    async with sm() as s:
        org, src = await _org_src(s)
        o = _opp(org, src, 0, "t2_done")
        s.add(o)
        await s.flush()
        for _ in range(10):  # ten clean employment tier-2 evaluations
            s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment", verdict="apply",
                             verdict_model="apply", score=4.0, weighted_score=4.0,
                             needs_review=False, dimensions={}, blocks={}, blockers=[],
                             legitimacy={"status": "clean", "signals": []}))
        # A track-less P12 summary quarantine (tier 12, opportunity_id None) must NOT count against
        # the employment eval validity rate.
        s.add(Quarantine(opportunity_id=None, tier=12, model="m", prompt="p", raw_response="r",
                         error="e", attempts=2))
        await s.commit()

    async with sm() as s:
        q = await quality_metrics(s, "employment")
    assert q["evaluations"] == 10
    assert q["quarantine_n"] == 0  # the track-less P12 quarantine is excluded
    assert q["json_validity_rate"] == 1.0  # not depressed to 10/11


async def test_applications_per_week_is_insufficient_below_five(sm):
    """Two applications a week apart is not a weekly rate (hard rule 6): the metric stays None and
    the n travels beside it so the page can say "insufficient (n=2)" rather than render 2.0."""
    from kestrel.analytics.metrics import time_metrics

    async with sm() as s:
        org, src = await _org_src(s)
        for i, day in enumerate(("2026-07-01", "2026-07-08")):
            o = _opp(org, src, i, "t2_done")
            s.add(o)
            await s.flush()
            s.add(Pursuit(opportunity_id=o.id, track="employment", status="applied",
                          applied_at=f"{day}T09:00:00.000000+00:00"))
        await s.commit()
    async with sm() as s:
        t = await time_metrics(s, "employment")
    assert t["applications"] == 2 and t["applications_n"] == 2
    assert t["applications_per_week"] is None
    assert t["applications_per_week_insufficient"] is True

    # At the floor the rate is computed, and the n is still carried beside it.
    async with sm() as s:
        for i, day in enumerate(("2026-07-15", "2026-07-22", "2026-07-29"), start=10):
            o = _opp(org, src, i, "t2_done")
            s.add(o)
            await s.flush()
            s.add(Pursuit(opportunity_id=o.id, track="employment", status="applied",
                          applied_at=f"{day}T09:00:00.000000+00:00"))
        await s.commit()
    async with sm() as s:
        t = await time_metrics(s, "employment")
    assert t["applications_n"] == 5
    assert t["applications_per_week"] == 1.25  # 5 over a 28-day span
    assert t["applications_per_week_insufficient"] is False


_P12 = {
    "summary_md": "Everything looks great, strong pipeline.",
    "headline_finding": "The pipeline is healthy.",
    "one_thing_to_change": "Keep going.",
    "evidence_for_it": "Vibes.",
    "sample_size_warnings": [],
    "insufficient_data": False,  # the model is cheerful; the CODE verdict must still win
}


class _FakeP12:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **_):
        return ChatResult(content=json.dumps(_P12), prompt_tokens=1, completion_tokens=1,
                          latency_ms=1, model=self.settings.llm.tiers[tier].model)


async def test_honest_summary_reports_non_discrimination(sm):
    async with sm() as s:
        org, src = await _org_src(s)
        # 30 outcomes: score does NOT predict response. High (4.6) and low (2.4) score bands
        # both respond at ~50%, so the score is not discriminating.
        for i in range(30):
            o = _opp(org, src, i, "t2_done")
            s.add(o)
            await s.flush()
            score = 4.6 if i % 2 == 0 else 2.4
            s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment", verdict="apply",
                             verdict_model="apply", score=score, weighted_score=score,
                             needs_review=False, dimensions={}, blocks={}, blockers=[],
                             legitimacy={"status": "clean", "signals": []}))
            p = Pursuit(opportunity_id=o.id, track="employment", status="applied",
                        applied_at=utcnow_iso())
            s.add(p)
            await s.flush()
            responded = (i % 4) in (0, 1)  # ~50%, independent of score parity
            s.add(Outcome(pursuit_id=p.id, opportunity_id=o.id, final_status="rejected",
                          furthest_stage="applied",
                          days_to_first_response=5 if responded else None))
        await s.commit()

    async with sm() as s:
        summary = await honest_summary(s, _FakeP12(), "employment")

    # The code-computed verdict wins over the model's cheerful prose.
    assert summary.discriminating is False
    assert summary.not_discriminating is True
    assert summary.metrics["score_vs_outcome"]["n_outcomes"] == 30
    # The NARRATIVE, not just the flag, must lead with the honest finding: the cheerful model prose
    # cannot be all the operator sees.
    assert "not discriminating" in summary.summary_md.lower()
    assert "not discriminating" in summary.headline_finding.lower()
    assert summary.summary_md != _P12["summary_md"]  # the cheerful line is not the whole story
