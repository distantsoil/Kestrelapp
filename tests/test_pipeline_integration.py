"""GATE (any environment): 30 fixture opportunities per track through Tiers 0, 1, 2 with a mocked
LLM, asserting stage transitions, rule attribution, weighted-score computation, needs_review
flagging, blocker and legitimacy forcing, quarantine promotion of a failed Tier 1, and that
consulting evaluations write signals back to the org."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity, Org, Quarantine, Run, Source
from kestrel.pipeline.profile import load_profile
from kestrel.pipeline.tier2 import handle_t2
from kestrel.pipeline.worker import Worker
from tests.pipeline_fakes import FakeLLM


# A posting date that is always inside max_posting_age_days, so the stale blocker never
# time-bombs this gate (it did: a hardcoded July date went red on 2026-09-13).
def _recent() -> str:
    return (datetime.now(UTC) - timedelta(days=3)).date().isoformat()

# index -> marker; the rest are plain keep/apply survivors. 30 per track.
_MARKS = {
    22: "LOWCONF", 23: "KILL", 24: "QUAR1", 25: "QUAR2",
    26: "BLOCK", 27: "REJECT", 28: "SUSPECT", 29: "DIVERGE",
}


def _title(i: int, base: str) -> str:
    mark = _MARKS.get(i)
    return f"{base} #{i}" + (f" [{mark}]" if mark else "")


async def _seed(sm) -> tuple[int, int]:
    async with sm() as s:
        emp = Source(track="employment", kind="greenhouse", name="emp", enabled=True)
        con = Source(track="consulting", kind="greenhouse", name="con", enabled=True)
        s.add_all([emp, con])
        await s.flush()
        emp_id, con_id = emp.id, con.id
        for i in range(30):
            s.add(
                Opportunity(
                    track="employment", opp_type="job_posting", source_id=emp_id,
                    title=_title(i, "VP of IT"), comp_max=250000, comp_source="parsed",
                    remote_type="hybrid", state="OH", city="Columbus", location_raw="Columbus, OH",
                    date_posted=_recent(), seen_count=1, ghost_score=0.1, stage="new",
                    description_md="Own IT leadership, budget and vendor accountability, security.",
                )
            )
        for i in range(30):
            org = Org(name=f"Beacon {i}", normalized_name=f"beacon {i}", sector="healthcare",
                      ownership="pe_backed", size_band="500-1000", signals=[])
            s.add(org)
            await s.flush()
            s.add(
                Opportunity(
                    track="consulting", opp_type="contract_posting", source_id=con_id,
                    org_id=org.id, title=_title(i, "Fractional CIO"), comp_max=2500,
                    comp_interval="day", comp_source="parsed", remote_type="remote", country="US",
                    location_raw="Remote (US)", seen_count=1, ghost_score=0.1, stage="new",
                    description_md="Interim IT leadership after an acquisition; consolidate.",
                )
            )
        await s.commit()
    return emp_id, con_id


async def _by_title(sm) -> dict[str, Opportunity]:
    async with sm() as s:
        return {o.title: o for o in (await s.execute(select(Opportunity))).scalars().all()}


async def _evals(sm, opp_id: int, tier: int) -> list[Evaluation]:
    async with sm() as s:
        return list(
            (
                await s.execute(
                    select(Evaluation).where(
                        Evaluation.opportunity_id == opp_id, Evaluation.tier == tier
                    )
                )
            ).scalars()
        )


async def test_full_pipeline_both_tracks(sm):
    from kestrel.services.tracks import BOTH, save_enabled

    async with sm() as s:  # a new install is employment-only; this test runs both
        await save_enabled(s, BOTH)
        await s.commit()
    await _seed(sm)
    settings = get_settings()
    worker = Worker(sm, FakeLLM(settings), settings)
    stats = await worker.run()

    assert stats.tier0["pass"] == 60  # every seeded opportunity survives Tier 0
    assert stats.tier1.completed > 0 and stats.tier2.completed > 0

    opps = await _by_title(sm)

    # --- Tier 1 behaviors -----------------------------------------------------------------
    kill = opps["VP of IT #23 [KILL]"]
    assert kill.stage == "t1_reject"
    assert not await _evals(sm, kill.id, 2)  # never reached Tier 2

    lowconf = opps["VP of IT #22 [LOWCONF]"]
    assert lowconf.stage == "t2_done"  # forced keep escalated it
    t1 = (await _evals(sm, lowconf.id, 1))[0]
    assert t1.verdict == "keep" and t1.forced_by == "low_confidence_forced_keep"

    # --- Quarantine promotion (hard rule 4) -----------------------------------------------
    quar1 = opps["VP of IT #24 [QUAR1]"]
    assert quar1.stage == "t2_done"  # promoted past a Tier 1 structural failure
    t1q = (await _evals(sm, quar1.id, 1))[0]
    assert t1q.forced_by == "t1_quarantine_promoted"
    async with sm() as s:
        qrows = (
            await s.execute(select(Quarantine).where(Quarantine.opportunity_id == quar1.id))
        ).scalars().all()
    assert any(q.tier == 1 for q in qrows)

    # --- Tier 2 quarantine terminal -------------------------------------------------------
    quar2 = opps["VP of IT #25 [QUAR2]"]
    assert quar2.stage == "quarantined"
    async with sm() as s:
        q2 = (
            await s.execute(select(Quarantine).where(Quarantine.opportunity_id == quar2.id))
        ).scalars().all()
    assert any(q.tier == 2 for q in q2)

    # --- Tier 2 overrides -----------------------------------------------------------------
    block = (await _evals(sm, opps["VP of IT #26 [BLOCK]"].id, 2))[0]
    assert block.verdict == "pass" and "blocker" in block.forced_by

    reject = (await _evals(sm, opps["VP of IT #27 [REJECT]"].id, 2))[0]
    assert reject.verdict == "pass" and "legitimacy_reject" in reject.forced_by

    suspect = (await _evals(sm, opps["VP of IT #28 [SUSPECT]"].id, 2))[0]
    assert suspect.score == 3.5 and suspect.verdict == "consider"

    diverge = (await _evals(sm, opps["VP of IT #29 [DIVERGE]"].id, 2))[0]
    assert diverge.needs_review is True

    # --- weighted-score computation on a clean survivor -----------------------------------
    clean = (await _evals(sm, opps["VP of IT #0"].id, 2))[0]
    assert clean.weighted_score == 4.2  # dims all 4.2, employment weights sum to 1.0
    assert clean.verdict == "apply" and clean.verdict_model == "apply"

    # --- consulting writes situation + buyers back to the org -----------------------------
    con0 = opps["Fractional CIO #0"]
    assert con0.stage == "t2_done"
    async with sm() as s:
        org = await s.get(Org, con0.org_id)
        cats = [sig["category"] for sig in (org.signals or [])]
    assert "situation" in cats and cats.count("buyer") >= 2  # economic_buyer + influencer

    # --- the run recorded per-tier throughput --------------------------------------------
    async with sm() as s:
        run = (await s.execute(select(Run).order_by(Run.id.desc()))).scalars().first()
    assert run.status == "done"
    assert "per_minute" in run.counts["tier1"] and "per_minute" in run.counts["tier2"]


class _RecordingLLM(FakeLLM):
    """Captures every payload per tier so the test can inspect what a hosted tier would send."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.sent: list[tuple[str, str]] = []

    async def complete(self, *, tier, messages, **kw):
        self.sent.append((tier, "\n".join(m.get("content", "") for m in messages)))
        return await super().complete(tier=tier, messages=messages, **kw)


async def test_hosted_tiers_redact_the_employer_in_every_worker_payload(sm):
    """Hosted mode (SPEC 6.4): with Tier 1 and Tier 2 on a hosted backend, confidential mode on,
    and a redaction map, the worker's full run never puts the current employer in any payload,
    and the run still completes end to end (nothing is refused when a map exists)."""
    from kestrel.db.models import DocMaster, DocSection

    await _seed(sm)
    employer = "Beacon 3"  # the org of one consulting opportunity is the operator's employer
    async with sm() as s:
        dm = DocMaster(track="employment", name="resume", doc_kind="resume")
        s.add(dm)
        await s.flush()
        s.add(DocSection(doc_master_id=dm.id, section_kind="role", org_label=employer,
                         org_label_redacted="[Confidential]"))
        await s.commit()
    settings = get_settings()
    settings.confidential_mode = True
    settings.llm.hosted_mode.enabled = True
    settings.llm.hosted_mode.acknowledged_at = "2026-10-12T10:00:00+00:00"
    settings.llm.hosted_mode.acknowledged_terms = settings.llm.hosted_mode.disclosure_terms()
    for tier in ("tier1", "tier2"):
        settings.llm.tiers[tier].backend = "frontier"
        settings.llm.tiers[tier].model = "qwen/qwen3-8b:free"
    fake = _RecordingLLM(settings)
    stats = await Worker(sm, fake, settings).run(track="consulting")

    assert stats.tier1.completed == 30 and stats.tier2.completed > 0
    hosted_payloads = [text for tier, text in fake.sent if tier in ("tier1", "tier2")]
    assert hosted_payloads
    assert all(employer.lower() not in text.lower() for text in hosted_payloads)
    from kestrel.privacy import _LIVE

    assert any(any(c in text for c in _LIVE) for text in hosted_payloads)
    assert (await _by_title(sm))["Fractional CIO #3"].stage == "t2_done"


async def test_changed_reeval_is_idempotent_on_the_same_version(sm):
    """A `changed` Tier 2 task that finds an evaluation already made against the opportunity's
    current content version must skip, so a re-leased/duplicate task cannot double-evaluate."""
    await _seed(sm)
    settings = get_settings()
    fake = FakeLLM(settings)
    worker = Worker(sm, fake, settings)
    await worker.run(track="consulting")

    con0 = (await _by_title(sm))["Fractional CIO #0"]
    async with sm() as s:
        org_before = await s.get(Org, con0.org_id)
        signals_before = len(org_before.signals or [])
        n_evals_before = len(await _evals(sm, con0.id, 2))

    # Re-deliver the SAME-version changed task twice (simulating a crash/redo and a duplicate).
    profiles = {t: load_profile(t) for t in ("employment", "consulting")}
    for _ in range(2):
        out = await handle_t2(
            sm, fake, settings, opportunity_id=con0.id, profiles=profiles, changed=True
        )
        assert out.action == "skip"

    async with sm() as s:
        org_after = await s.get(Org, con0.org_id)
        assert len(org_after.signals or []) == signals_before  # no signal inflation
    assert len(await _evals(sm, con0.id, 2)) == n_evals_before  # no duplicate evaluation


async def test_resume_does_not_duplicate_or_lose_work(sm):
    """A crash between a handler's commit and its task-completion must resume cleanly: the re-leased
    task sees the advanced stage, skips the model call, and no second Tier 2 task is created."""
    await _seed(sm)
    settings = get_settings()
    worker = Worker(sm, FakeLLM(settings), settings)
    await worker.run(track="employment", limit=5)

    # Every t1/t2 task should be terminal (done); re-running produces no new work and no dup evals.
    async with sm() as s:
        clean = (
            await s.execute(
                select(Opportunity).where(Opportunity.title == "VP of IT #0")
            )
        ).scalar_one()
        before = len(
            (
                await s.execute(select(Evaluation).where(Evaluation.opportunity_id == clean.id))
            ).scalars().all()
        )
    # A second full run over the same data must not re-evaluate the already-finished opportunity.
    worker2 = Worker(sm, FakeLLM(settings), settings)
    await worker2.run(track="employment", limit=5)
    async with sm() as s:
        after = len(
            (
                await s.execute(select(Evaluation).where(Evaluation.opportunity_id == clean.id))
            ).scalars().all()
        )
    assert after == before  # no duplicate evaluation on resume/re-run
