"""The Models page and its analytics (kestrel/analytics/models_history.py): per-model summary with
agreement against operator labels, the insufficient label under five labeled evaluations (hard
rule 6), hosted cost from frontier transmissions versus "local", confidential redaction in the
recent history, the gold-set status in place of a stored bench run, and GET /models rendering."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.analytics.models_history import (
    bench_summary,
    best_by_tier,
    max_labeled_by_tier,
    model_summary,
    recent_history,
)
from kestrel.db import migrate
from kestrel.db.base import iso
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Evaluation,
    FrontierTransmission,
    Label,
    Opportunity,
    Org,
    Setting,
)

_EMPLOYER = "Northwind Freight"
_PLACEHOLDER = "[Confidential]"
_LOCAL = "kestrel-tier2"
_HOSTED = "openai/gpt-x"


def _fresh_engine():
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    return make_async_engine(paths.db_path())


async def _seed(*, confidential: bool = False) -> None:
    """Two models. The local tier-2 model has 8 evaluations, all labeled: 6 agree, 2 disagree, every
    score 0.5 off the operator's. The hosted tier-3 model has 4 evaluations, 3 labeled (all agree),
    and one priced frontier transmission. One extra old local evaluation sits outside a 30-day
    window."""
    await reset_engine()
    engine = _fresh_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        org = Org(name=_EMPLOYER, normalized_name=_EMPLOYER.lower())
        s.add(org)
        await s.flush()

        def opp(i: int) -> Opportunity:
            return Opportunity(
                track="employment", opp_type="job_posting", org_id=org.id,
                title=f"VP of IT at {_EMPLOYER} {i}", org_name=_EMPLOYER,
                url=f"https://x/models-{i}", stage="t2_done",
            )

        opps = [opp(i) for i in range(12)]
        s.add_all(opps)
        await s.flush()

        for i in range(8):
            s.add(
                Evaluation(
                    opportunity_id=opps[i].id, tier=2, track="employment", model=_LOCAL,
                    prompt_version="evaluate/v2", verdict="apply", score=4.0,
                    prompt_tokens=1000, completion_tokens=200, latency_ms=3000,
                )
            )
            s.add(
                Label(
                    opportunity_id=opps[i].id, track="employment",
                    verdict="apply" if i < 6 else "pass", score=4.5,
                )
            )
        for i in range(8, 12):
            ev = Evaluation(
                opportunity_id=opps[i].id, tier=3, track="employment", model=_HOSTED,
                prompt_version="evaluate/v2", verdict="consider", score=3.0,
                prompt_tokens=2000, completion_tokens=400, latency_ms=9000,
            )
            s.add(ev)
            if i < 11:
                s.add(Label(opportunity_id=opps[i].id, track="employment", verdict="consider",
                            score=3.0))
            if i == 8:
                await s.flush()
                s.add(
                    FrontierTransmission(
                        opportunity_id=opps[i].id, provider="frontier",
                        base_url="https://openrouter.ai/api/v1", model=_HOSTED,
                        payload={}, payload_sha256="abc", status="sent", evaluation_id=ev.id,
                        prompt_tokens=2000, completion_tokens=400, cost_usd=0.0123,
                    )
                )
        s.add(
            Evaluation(
                opportunity_id=opps[0].id, tier=2, track="employment", model=_LOCAL,
                prompt_version="evaluate/v2", verdict="apply", score=4.0,
                created_at=iso(datetime.now(UTC) - timedelta(days=100)),
            )
        )
        master = DocMaster(track="employment", name="Resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(
            DocSection(
                doc_master_id=master.id, section_kind="experience", sort_order=0,
                org_label=_EMPLOYER, org_label_redacted=_PLACEHOLDER,
            )
        )
        if confidential:
            s.add(Setting(key="confidential_mode", value=True))
        await s.commit()
    await engine.dispose()
    forget_engine()


async def _with_session(fn):
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            return await fn(s)
    finally:
        await engine.dispose()


# -------------------------------------------------------------------------------- the summary
def test_model_summary_agreement_math_and_insufficient_label():
    asyncio.run(_seed())
    rows = asyncio.run(_with_session(lambda s: model_summary(s)))
    by_model = {r.model: r for r in rows}

    local = by_model[_LOCAL]
    assert local.tier == 2 and local.prompt_version == "evaluate/v2"
    assert local.n == 9  # 8 recent plus the 100-day-old one
    # opps[0] carries two evaluations and one label: it is one labeled role (#122), so 8 roles
    # are compared and 6 of them agree.
    assert local.n_labeled == 8 and local.insufficient is False
    assert local.agreement_rate == 6 / 8
    assert local.mean_abs_score_error == 0.5
    assert local.verdicts == {"apply": 9}
    assert local.mean_score == 4.0
    assert local.hosted is False and local.cost_usd is None and local.cost_label == "local"

    hosted = by_model[_HOSTED]
    assert hosted.tier == 3 and hosted.n == 4
    assert hosted.n_labeled == 3 and hosted.insufficient is True
    assert hosted.agreement_rate is None and hosted.mean_abs_score_error is None
    assert hosted.hosted is True
    assert hosted.n_hosted_sends == 1 and hosted.n_costed_sends == 1
    assert hosted.cost_usd == 0.0123 and hosted.cost_label == "$0.0123"
    assert hosted.mean_latency_ms == 9000 and hosted.mean_prompt_tokens == 2000
    assert hosted.mean_completion_tokens == 400

    # Sorted by tier then n descending.
    assert [r.tier for r in rows] == [2, 3]


def test_since_window_and_track_filter():
    asyncio.run(_seed())
    rows = asyncio.run(_with_session(lambda s: model_summary(s, since_days=30)))
    local = next(r for r in rows if r.model == _LOCAL)
    assert local.n == 8 and local.n_labeled == 8
    assert local.agreement_rate == 0.75
    assert local.mean_latency_ms == 3000 and local.mean_prompt_tokens == 1000

    assert asyncio.run(_with_session(lambda s: model_summary(s, track="consulting"))) == []


def test_best_by_tier_requires_five_labels():
    asyncio.run(_seed())
    rows = asyncio.run(_with_session(lambda s: model_summary(s, since_days=30)))
    best = best_by_tier(rows)
    assert best[2] is not None and best[2].model == _LOCAL
    assert best[3] is None  # three labels is not enough to name a winner
    assert max_labeled_by_tier(rows) == {2: 8, 3: 3}


# --------------------------------------------------------------------------------- the history
def test_recent_history_redacts_the_employer_when_confidential():
    asyncio.run(_seed(confidential=True))
    history = asyncio.run(_with_session(lambda s: recent_history(s, limit=50)))
    assert len(history) == 13
    assert all(h.org == _PLACEHOLDER for h in history)
    assert not any(_EMPLOYER in (h.title or "") for h in history)
    assert all(_PLACEHOLDER in (h.title or "") for h in history)
    # Newest first; agreement and cost ride along.
    assert history[0].created_at >= history[-1].created_at
    assert history[-1].model == _LOCAL  # the 100-day-old row sorts last
    hosted = [h for h in history if h.hosted]
    assert len(hosted) == 1 and hosted[0].cost_usd == 0.0123 and hosted[0].cost_label == "$0.0123"
    local = [h for h in history if not h.hosted]
    assert all(h.cost_label == "local" for h in local)
    agreed = [h.agreed for h in history if h.model == _LOCAL]
    assert agreed.count(True) == 7 and agreed.count(False) == 2
    unlabeled = [h for h in history if h.agreed is None]
    assert len(unlabeled) == 1 and unlabeled[0].operator_verdict is None


def test_recent_history_shows_the_real_name_when_confidential_is_off():
    asyncio.run(_seed(confidential=False))
    history = asyncio.run(_with_session(lambda s: recent_history(s, limit=5)))
    assert len(history) == 5 and all(h.org == _EMPLOYER for h in history)


# ----------------------------------------------------------------------------------- the bench
def test_bench_summary_reports_goldset_state_not_a_stored_run(tmp_path):
    (tmp_path / "goldset.employment.jsonl").write_text('{"a":1}\n{"b":2}\n\n', encoding="utf-8")
    status = bench_summary(tmp_path)
    assert status.persisted is False and "stores no rows" in status.note
    by_track = {g.track: g for g in status.goldsets}
    emp = by_track["employment"]
    assert emp.present and emp.n_items == 2 and emp.insufficient and emp.placeholder
    con = by_track["consulting"]
    assert not con.present and con.n_items == 0 and con.insufficient

    (tmp_path / "goldset.employment.regenerated.jsonl").write_text("", encoding="utf-8")
    assert not bench_summary(tmp_path).goldsets[0].placeholder


# ------------------------------------------------------------------------------------ the page
def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_get_models_page_renders_the_comparison():
    asyncio.run(_seed())
    with _client() as c:
        r = c.get("/models?since=30")
    assert r.status_code == 200
    body = r.text
    assert _LOCAL in body and _HOSTED in body
    assert "75% of 8" in body
    assert "too few to judge (3 of 5 needed)" in body
    assert "Deeper read (Tier 3): too few to judge (3 of 5 needed)" in body
    assert "No stored benchmark run" in body
    assert 'href="/bench"' in body and "Settings &gt; Hosted" in body
    assert "local" in body and "$0.0123" in body
    assert 'href="/models"' in body  # the nav link
    assert "last 30 days" in body


def test_get_models_page_redacts_under_confidential_mode():
    asyncio.run(_seed(confidential=True))
    with _client() as c:
        body = c.get("/models").text
    assert _EMPLOYER not in body
    assert _PLACEHOLDER in body


def test_get_models_page_with_no_data():
    asyncio.run(reset_engine())
    engine = _fresh_engine()
    asyncio.run(engine.dispose())
    forget_engine()
    with _client() as c:
        r = c.get("/models?track=consulting&since=7")
    assert r.status_code == 200 and "No evaluations" in r.text


# --------------------------------------------------- #122: n_labeled counts duplicate groups
async def _seed_groups(group_sizes: list[int], solos: int) -> None:
    """One model rated every copy once; each group of copies has a label on every copy, the last
    copy's label (the newest) saying "pass" while the evaluation said "apply"."""
    await reset_engine()
    engine = _fresh_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    base = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)  # only compared with each other
    async with sm() as s:
        n = 0

        async def role(group: str | None) -> Opportunity:
            nonlocal n
            n += 1
            o = Opportunity(
                track="employment", opp_type="job_posting", title=f"Role {n}", org_name="Acme",
                url=f"https://x/groups-{n}", stage="t2_done", dedup_group_id=group,
            )
            s.add(o)
            await s.flush()
            s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment", model=_LOCAL,
                             prompt_version="evaluate/v2", verdict="apply", score=4.0))
            return o

        tick = 0
        for g, size in enumerate(group_sizes):
            for copy in range(size):
                o = await role(f"grp-{g}")
                tick += 1
                s.add(Label(opportunity_id=o.id, track="employment",
                            verdict="pass" if copy == size - 1 else "apply", score=2.0,
                            created_at=iso(base + timedelta(minutes=tick))))
        for _ in range(solos):
            o = await role(None)
            tick += 1
            s.add(Label(opportunity_id=o.id, track="employment", verdict="apply", score=4.0,
                        created_at=iso(base + timedelta(minutes=tick))))
        await s.commit()
    await engine.dispose()
    forget_engine()


def test_six_labeled_copies_of_two_postings_are_two_labeled_roles_and_insufficient():
    asyncio.run(_seed_groups([3, 3], solos=0))
    rows = asyncio.run(_with_session(lambda s: model_summary(s)))
    row = next(r for r in rows if r.model == _LOCAL)
    assert row.n == 6  # evaluations are still all counted
    assert row.n_labeled == 2 and row.insufficient is True  # hard rule 6
    assert row.agreement_rate is None


def test_labeled_roles_count_groups_and_a_role_without_a_group_is_its_own():
    asyncio.run(_seed_groups([3], solos=4))  # 7 labels, 5 distinct roles
    rows = asyncio.run(_with_session(lambda s: model_summary(s)))
    row = next(r for r in rows if r.model == _LOCAL)
    assert row.n_labeled == 5 and row.insufficient is False
    # The group's newest label wins ("pass" against the model's "apply"); 4 solos agree.
    assert row.agreement_rate == 0.8
