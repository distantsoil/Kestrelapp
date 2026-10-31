"""The Today page (docs/UI_PROPOSAL.html Screen 1) and its data functions: the review queue with
the disagreement flag, next actions bucketed by due date, the overnight counts from one Run and
the rows since it started, the system's asks in priority order, redaction under confidential
mode, and the page itself rendering the three cards plus the asks. Cloud-safe: a migrated SQLite
file and the test client only; no model, no network."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.analytics.today import (
    act_summary,
    actions_due,
    disagrees,
    due_label,
    honest_line,
    overnight,
    progress_view,
    review_queue,
    summary_line,
    system_asks,
    worth_a_look,
)
from kestrel.config import TierConfig, get_settings
from kestrel.db import migrate
from kestrel.db.base import iso
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Evaluation,
    Label,
    Opportunity,
    Org,
    Pursuit,
    Quarantine,
    Run,
    ScrapeRun,
    Setting,
    Source,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_EMPLOYER = "Northwind Freight"
_PLACEHOLDER = "[Confidential]"
# Read the clock when a test runs, never at import: a suite that starts before midnight and
# reaches these tests after it would seed yesterday's dates against today's app clock.
def _now() -> datetime:
    return datetime.now(UTC)


def _today() -> date:
    # LOCAL, matching what the operator's date input produces: `next_action_at` values here are
    # calendar dates with no timezone, so they are compared against the local day. The UTC
    # timestamps seeded through `_ago` are converted to a local date by the app before counting.
    return date.today()


def _ago(**kw) -> str:
    return iso(_now() - timedelta(**kw))


def _fresh_engine():
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    return make_async_engine(paths.db_path())


def _opp(org, src, i, **kw) -> Opportunity:
    base = dict(
        track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
        title=f"VP of IT {i}", org_name=_EMPLOYER, url=f"https://x/today-{i}",
        stage="t2_done", first_seen_at=_ago(hours=1),
    )
    base.update(kw)
    return Opportunity(**base)


def _ev(opp, **kw) -> Evaluation:
    base = dict(
        opportunity_id=opp.id, tier=2, track="employment", verdict="apply", verdict_model="apply",
        score=4.0, weighted_score=4.0, needs_review=False, created_at=_ago(hours=1),
        blocks={"a_role_summary": {"what_this_actually_is": f"Head of IT at {_EMPLOYER}."}},
    )
    base.update(kw)
    return Evaluation(**base)


async def _seed(*, confidential: bool = False, with_run: bool = True) -> dict[str, int]:
    """One Run (hosted, started 6 h ago) and everything since it: a scrape with 4 new postings,
    two shortlisted items (one where the model and the weights disagree), one shortlisted before
    the run, pursuits due today / tomorrow / overdue (the overdue one is 30 days silent) and one
    closed, three Tier 0 rejects (two stale, one geography), two Tier 1 verdicts, one
    quarantine row, and three labels (below the few-shot floor of 15)."""
    await reset_engine()
    engine = _fresh_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, int] = {}
    async with sm() as s:
        org = Org(name=_EMPLOYER, normalized_name=_EMPLOYER.lower())
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        if with_run:
            s.add(
                Run(
                    name="pipeline", status="done", mode="hosted", started_at=_ago(hours=6),
                    finished_at=_ago(hours=5),
                    counts={"tier0": {"seen": 5, "pass": 2, "reject": 3, "geo_uncertain": 0}},
                )
            )
        s.add(
            ScrapeRun(
                source_id=src.id, started_at=_ago(hours=5), finished_at=_ago(hours=5),
                status="ok", counts={"fetched": 6, "new": 4, "reseen": 2},
            )
        )
        a = _opp(org, src, "a")  # shortlisted, model and weights agree
        b = _opp(org, src, "b")  # shortlisted by the model, weights say consider
        c = _opp(org, src, "c")  # shortlisted but already a pursuit (due today)
        d = _opp(org, src, "d")  # shortlisted three days ago, before the run
        e = _opp(org, src, "e")  # pursuit due tomorrow
        f = _opp(org, src, "f")  # pursuit overdue and silent for 30 days
        g = _opp(org, src, "g")  # closed pursuit, must not show
        r1 = _opp(org, src, "r1", stage="t0_reject", stage_reason="blocker:stale: 60 days old")
        r2 = _opp(org, src, "r2", stage="t0_reject", stage_reason="blocker:stale: 70 days old")
        r3 = _opp(org, src, "r3", stage="t0_reject", stage_reason="blocker:geography: Berlin")
        s.add_all([a, b, c, d, e, f, g, r1, r2, r3])
        await s.flush()
        ids.update(a=a.id, b=b.id, c=c.id, d=d.id, e=e.id, f=f.id, g=g.id)
        s.add_all(
            [
                _ev(a, score=4.6),
                _ev(b, score=3.8, verdict="consider", verdict_model="apply", needs_review=True),
                _ev(c, score=4.1),
                _ev(d, score=4.4, created_at=_ago(days=3)),
                _ev(e, tier=1, verdict="keep", verdict_model="keep", score=None, blocks={}),
                _ev(f, tier=1, verdict="kill", verdict_model="kill", score=None, blocks={}),
            ]
        )
        s.add_all(
            [
                Pursuit(
                    opportunity_id=c.id, track="employment", status="applied",
                    next_action="Follow up with recruiter", next_action_at=_today().isoformat(),
                    last_activity_at=_ago(days=2),
                ),
                Pursuit(
                    opportunity_id=e.id, track="employment", status="interviewing",
                    next_action="Send capability statement",
                    next_action_at=(_today() + timedelta(days=1)).isoformat(),
                    last_activity_at=_ago(days=1),
                ),
                Pursuit(
                    opportunity_id=f.id, track="employment", status="applied",
                    next_action="Chase the recruiter",
                    next_action_at=(_today() - timedelta(days=2)).isoformat(),
                    last_activity_at=_ago(days=30),
                ),
                Pursuit(
                    opportunity_id=g.id, track="employment", status="rejected",
                    next_action="Nothing", next_action_at=_today().isoformat(),
                    closed_at=_ago(days=1), last_activity_at=_ago(days=40),
                ),
            ]
        )
        s.add(Quarantine(opportunity_id=b.id, tier=2, error="schema", raw_response="{"))
        for o in (a, b, c):
            s.add(Label(opportunity_id=o.id, track="employment", verdict="apply", score=4.5))
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
    return ids


async def _with_session(fn):
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            return await fn(s)
    finally:
        await engine.dispose()


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


# ------------------------------------------------------------------------------------ review
def test_review_queue_since_last_run_with_disagreement_flag():
    ids = asyncio.run(_seed())
    out = asyncio.run(_with_session(lambda s: review_queue(s, "employment")))
    # a and b are shortlisted since the run; c has a pursuit; d predates the run.
    assert out["n"] == 2 and out["needs_review"] == 1
    assert [r["id"] for r in out["items"]] == [ids["a"], ids["b"]]  # score desc
    top, second = out["items"]
    assert top["score"] == 4.6 and top["needs_review"] is False and top["verdict"] == "apply"
    assert top["summary"] == f"Head of IT at {_EMPLOYER}."
    assert second["needs_review"] is True and second["verdict_model"] == "apply"
    assert top["org"] == _EMPLOYER  # confidential mode off: nothing redacted
    assert asyncio.run(_with_session(lambda s: review_queue(s, "employment", limit=1)))["n"] == 2
    assert asyncio.run(_with_session(lambda s: review_queue(s, "consulting")))["n"] == 0


def test_review_queue_never_lists_a_hidden_opportunity():
    """Hide (archived_at) takes a row out of the pipeline everywhere, including Today's review
    queue; otherwise the operator is asked to decide on something they already set aside."""
    ids = asyncio.run(_seed())

    async def _hide(s):
        opp = await s.get(Opportunity, ids["a"])
        opp.archived_at = _ago(minutes=5)
        opp.archive_reason = "applied"
        await s.commit()

    asyncio.run(_with_session(_hide))
    out = asyncio.run(_with_session(lambda s: review_queue(s, "employment")))
    assert [r["id"] for r in out["items"]] == [ids["b"]]
    assert out["n"] == 1


def test_review_queue_falls_back_to_24h_without_a_run():
    asyncio.run(_seed(with_run=False))
    out = asyncio.run(_with_session(lambda s: review_queue(s, None)))
    assert out["n"] == 2  # d is three days old and still out of the window


def test_disagreement_and_summary_helpers():
    assert disagrees(Evaluation(verdict="consider", verdict_model="apply", needs_review=False))
    assert disagrees(Evaluation(verdict="apply", verdict_model="apply", needs_review=True))
    assert not disagrees(Evaluation(verdict="apply", verdict_model="apply", needs_review=False))
    assert summary_line({"a_situation": {"stated": "Post-close integration."}}) == (
        "Post-close integration."
    )
    assert summary_line({"b_fit": {}}) is None and summary_line(None) is None


# --------------------------------------------------------------------------------------- act
def test_actions_due_buckets_and_labels():
    ids = asyncio.run(_seed())
    out = asyncio.run(_with_session(lambda s: actions_due(s, "employment", today=_today())))
    assert out["n"] == 3 and out["overdue"] == 1 and out["today"] == 1 and out["tomorrow"] == 1
    by_opp = {a["opp_id"]: a for a in out["items"]}
    assert ids["g"] not in by_opp  # closed pursuit
    assert by_opp[ids["f"]]["bucket"] == "overdue" and by_opp[ids["f"]]["label"] == "overdue"
    assert by_opp[ids["c"]]["label"] == "today" and by_opp[ids["e"]]["label"] == "tomorrow"
    assert [a["opp_id"] for a in out["items"]][0] == ids["f"]  # overdue first
    assert by_opp[ids["c"]]["action"] == "Follow up with recruiter"
    assert due_label(date(2026, 10, 15), date(2026, 10, 12)) == ("week", "Thu")


# --------------------------------------------------------------------------------- overnight
def test_overnight_from_the_run_and_rows_since_it(monkeypatch):
    # A run that started before the 24 h floor is counted whole (here the floor is 4 h, so the
    # 6 h old run spans the window and its own Tier 0 counts are used).
    monkeypatch.setattr("kestrel.analytics.today._WINDOW_HOURS", 4)
    asyncio.run(_seed())
    out = asyncio.run(_with_session(lambda s: overnight(s, None, ghosting_days=21, today=_today())))
    assert out["run"]["status"] == "done" and out["mode"] == "hosted"
    assert out["sources_scanned"] == 1 and out["new_postings"] == 4
    assert out["tier0"]["in"] == 5 and out["tier0"]["kept"] == 2 and out["tier0"]["blocked"] == 3
    assert out["tier0"]["reasons"] == [{"rule": "stale", "n": 2}, {"rule": "geography", "n": 1}]
    assert out["tier1"] == {"in": 2, "kept": 1, "rejected": 1}
    # Tier 2 since the run: a, b, c evaluated; a and c shortlisted (b's weighted verdict is
    # consider), d is older than the run.
    assert out["tier2"] == {"evaluated": 3, "shortlisted": 2}
    assert out["quarantine_open"] == 1
    assert out["ghosting"] == {"n": 1, "days": 21}


def test_overnight_without_a_run_says_so_and_derives_tier0_from_rows():
    asyncio.run(_seed(with_run=False))
    out = asyncio.run(_with_session(lambda s: overnight(s, "employment", today=_today())))
    assert out["run"] is None and out["mode"] is None
    # Derived from opportunities first seen in the window: 3 rejects, 7 that went on.
    assert out["tier0"] == {
        "in": 10, "kept": 7, "blocked": 3,
        "reasons": [{"rule": "stale", "n": 2}, {"rule": "geography", "n": 1}],
    }


# ------------------------------------------------------------------------------------- honest
def test_honest_line_carries_n_and_insufficient_flag():
    asyncio.run(_seed())
    out = asyncio.run(_with_session(lambda s: honest_line(s, "employment")))
    assert out["n"] == 0 and out["insufficient"] is True and out["discriminating"] is None
    assert out["meter"] == 0 and "error" not in out


# --------------------------------------------------------------------------------------- asks
def _settings(**hosted):
    s = get_settings()
    if hosted:
        s.llm.hosted_mode.enabled = True
        s.llm.hosted_tiers = {
            t: TierConfig(backend="frontier", model="x/y") for t in ("tier1", "tier2")
        }
    return s


def test_system_asks_contents_and_order(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    asyncio.run(_seed(confidential=True))
    # D-PRIV-1: until the operator confirms the employer Kestrel hides, Today asks; once
    # confirmed (the stored flag), the ask goes.
    before = asyncio.run(_with_session(lambda s: system_asks(s, _settings(hosted=True), None)))
    assert "redaction_confirm" in [a["key"] for a in before]

    async def _confirm(s):
        from kestrel.docs.current_employer import set_confirmed

        await set_confirmed(s)
        await s.commit()

    asyncio.run(_with_session(_confirm))
    asks = asyncio.run(_with_session(lambda s: system_asks(s, _settings(hosted=True), None)))
    keys = [a["key"] for a in asks]
    # No profile.yml in a fresh home (the example is fiction), hosted on with no key, one
    # quarantine row, and labels short of the floor on both tracks. Confidential mode is on
    # WITH a redaction map here, so there is no redaction ask.
    assert keys == ["profile", "hosted_key", "quarantine", "labels:employment",
                    "labels:consulting"]
    assert [a["priority"] for a in asks] == sorted(a["priority"] for a in asks)
    hosted = next(a for a in asks if a["key"] == "hosted_key")
    assert "OPENROUTER_API_KEY" in hosted["line"] and hosted["href"] == "/settings"
    labels = next(a for a in asks if a["key"] == "labels:employment")
    assert labels["title"] == "12 more ratings" and "(3 of 15)" in labels["line"]
    assert labels["href"] == "/calibrate?track=employment"
    consulting = next(a for a in asks if a["key"] == "labels:consulting")
    assert "(0 of 15)" in consulting["line"]
    quarantine = next(a for a in asks if a["key"] == "quarantine")
    assert quarantine["title"] == "1 posting needs another try"
    assert "Retry them, or pick another model on Models" in quarantine["line"]
    for a in asks:
        assert a["title"] and a["line"] and a["href"] and a["button"]


def test_system_asks_redaction_map_and_track_scope(tmp_path, monkeypatch):
    asyncio.run(_seed(confidential=True))

    async def _drop_map(s):
        from sqlalchemy import update

        await s.execute(update(DocSection).values(org_label_redacted=None))
        await s.commit()

    asyncio.run(_with_session(_drop_map))
    asks = asyncio.run(_with_session(lambda s: system_asks(s, _settings(), "employment")))
    keys = [a["key"] for a in asks]
    assert "redaction" in keys and "hosted_key" not in keys
    assert "labels:consulting" not in keys and "labels:employment" in keys
    redaction = next(a for a in asks if a["key"] == "redaction")
    # D-PRIV-1: straight to a role's "This is my current employer" box.
    assert redaction["priority"] == 2 and redaction["href"].startswith("/documents")


def test_system_asks_reranker_floor_once_few_shot_is_met():
    asyncio.run(_seed())

    async def _more_labels(s):
        from sqlalchemy import select

        opps = (await s.execute(select(Opportunity))).scalars().all()
        for o in opps:  # ten opportunities in the seed; three already carry a label
            s.add(Label(opportunity_id=o.id, track="employment", verdict="apply", score=4.0))
        for i in range(10):
            o = Opportunity(track="employment", opp_type="job_posting", title=f"L{i}",
                            url=f"https://x/l{i}", stage="t2_done")
            s.add(o)
            await s.flush()
            s.add(Label(opportunity_id=o.id, track="employment", verdict="pass", score=2.0))
        await s.commit()

    asyncio.run(_with_session(_more_labels))
    asks = asyncio.run(_with_session(lambda s: system_asks(s, _settings(), "employment")))
    labels = next(a for a in asks if a["key"] == "labels:employment")
    assert "learn your ranking" in labels["line"] and "(20 of 200)" in labels["line"]
    assert labels["priority"] == 5


# --------------------------------------------------------------------------------- redaction
def test_confidential_mode_redacts_org_title_and_summary():
    asyncio.run(_seed(confidential=True))
    async def _both(s):
        return await review_queue(s, None), await actions_due(s, None, today=_today())

    review, act = asyncio.run(_with_session(_both))
    for r in review["items"]:
        assert r["org"] == _PLACEHOLDER
        assert _EMPLOYER not in (r["summary"] or "") and _PLACEHOLDER in r["summary"]
    for a in act["items"]:
        assert a["org"] == _PLACEHOLDER


# -------------------------------------------------------------------------------------- page
def test_today_page_renders_the_strip_matches_decisions_and_applications():
    """D-UI-92: the status strip, Top matches, Decisions waiting and Applications needing action
    replace the Review / Act / Schedule cards, the run panel and the asks. The queries the old
    cards printed are unchanged (tested above); the page prints the new sections."""
    asyncio.run(_seed())
    with _client() as c:
        r = c.get("/")
    assert r.status_code == 200
    body = r.text
    for hook in ("td-strip", "td-matches", "td-decisions", "td-apps"):
        assert f'data-testid="{hook}"' in body
    for gone in ("today-run", "today-review", "today-act", "today-schedule", "today-asks",
                 "Things to finish"):
        assert gone not in body
    assert body.index('data-testid="td-strip"') < body.index('data-testid="td-matches"') < \
        body.index('data-testid="td-decisions"')
    # The follow-up that is 30 days quiet is an application needing action.
    apps = body.split('data-testid="td-apps"')[1].split("</section>")[0]
    assert "VP of IT f" in apps and "ago" in apps
    assert "—" not in body.split("<main")[-1]
    assert "#D4622A" not in body.split("<main")[-1]  # theme variables only


def test_today_page_redacts_under_confidential_mode_and_takes_a_track():
    asyncio.run(_seed(confidential=True))
    with _client() as c:
        body = c.get("/?track=employment").text
        consulting = c.get("/?track=consulting").text
    assert _EMPLOYER not in body
    assert 'id="today" data-track="consulting"' in consulting
    assert 'data-testid="td-none"' in consulting  # no consulting roles found today


def test_today_page_with_an_empty_database():
    asyncio.run(reset_engine())
    engine = _fresh_engine()
    asyncio.run(engine.dispose())
    forget_engine()
    with _client() as c:
        r = c.get("/")
    assert r.status_code == 200
    strip = r.text.split('data-testid="td-strip"')[1].split('data-testid="td-matches"')[0]
    assert 'data-state="none"' in r.text and "No runs yet" in strip and "Run now" in strip
    assert 'data-testid="td-none"' in r.text and "No matches yet" in r.text


def test_a_failing_search_is_the_first_ask_with_its_last_error():
    """D-UI-33: a search that fails is named on Today with the reason from its last run."""
    asyncio.run(_seed())

    async def _break(s):
        from sqlalchemy import select

        src = (await s.execute(select(Source))).scalars().first()
        src.consecutive_failures = 1
        s.add(ScrapeRun(source_id=src.id, status="failed",
                        error="tls-client-64.dll could not be loaded"))
        await s.commit()

    asyncio.run(_with_session(_break))
    asks = asyncio.run(_with_session(lambda s: system_asks(s, _settings(), None)))
    first = asks[0]
    assert first["key"] == "searches_failing" and first["href"] == "/sources"
    assert first["title"] == "1 search failing" and "tls-client-64.dll" in first["line"]


def test_a_short_recent_run_never_narrows_overnight_below_24_hours():
    """D-UI-34 opens a desktop run per working cycle; the window must not shrink to it."""
    asyncio.run(_seed())
    out = asyncio.run(_with_session(lambda s: overnight(s, None, ghosting_days=21, today=_today())))
    # The 6 h old run's own Tier 0 counts cover only part of the window, so the rows decide.
    assert out["tier0"]["in"] >= 5
    assert out["window_hours"] == 24


# ------------------------------------------------------------------------- review and act
def _act(track=None):
    return asyncio.run(_with_session(lambda s: act_summary(s, track, today=_today())))


def _worth(track=None):
    return asyncio.run(_with_session(lambda s: worth_a_look(s, track)))


def test_review_and_act_counts():
    asyncio.run(_seed())
    # Strong is 4.5 and up (kestrel.web.fit.STRONG_FLOOR): a at 4.6 is, d at 4.4 is a good fit.
    # e is a Tier 1 keep with an open pursuit: still being scored, so not counted here yet. c and
    # f have open pursuits at `applied`: they sit in the Pipeline's Applied group, off the Review
    # count (D-UI-51 amended, #37).
    assert _worth("employment") == {"n": 3, "strong": 1, "your_call": 1}
    act = _act("employment")
    assert act["apply"] == 0 and act["follow_up"] == 2 and act["n"] == 2
    assert act["earliest"]["title"] == "VP of IT f" and act["earliest"]["days_quiet"] == 30
    assert act["earliest"]["days_overdue"] == 2


def test_shortlisting_keeps_a_role_in_review_and_adds_it_to_act():
    """D-UI-51: adding a role to the shortlist never drops it from the Review count."""
    ids = asyncio.run(_seed())
    before = _worth("employment")["n"]

    async def _shortlist(s):
        s.add(Pursuit(opportunity_id=ids["a"], track="employment", status="shortlisted"))
        await s.commit()

    asyncio.run(_with_session(_shortlist))
    assert _worth("employment")["n"] == before
    act = _act("employment")
    assert act["apply"] == 1 and act["n"] == 3


def test_worth_a_look_uses_the_highest_tier_evaluation_and_skips_hidden_roles():
    ids = asyncio.run(_seed())

    async def _more(s):
        opp_a = await s.get(Opportunity, ids["a"])
        opp_d = await s.get(Opportunity, ids["d"])
        opp_d.archived_at = _ago(hours=1)
        # A later Tier 1 row must not displace the Tier 2 verdict (highest tier, then id).
        s.add(_ev(opp_a, tier=1, verdict="keep", verdict_model="keep", score=None, blocks={}))
        await s.commit()

    asyncio.run(_with_session(_more))
    assert _worth("employment") == {"n": 2, "strong": 1, "your_call": 1}


def test_the_operator_label_moves_a_role_into_and_out_of_worth_a_look():
    """D-UI-51: the operator's latest label outranks the model for the group, so Today's Review
    count follows it: a consider labeled apply joins, an apply labeled pass leaves."""
    ids = asyncio.run(_seed())

    async def _label(s):
        from kestrel.db.models import VerdictOverride

        a = await s.get(Opportunity, ids["a"])
        org = await s.get(Org, a.org_id)
        src = await s.get(Source, a.source_id)
        m = _opp(org, src, "m")
        s.add(m)
        await s.flush()
        s.add(_ev(m, verdict="consider", verdict_model="consider", score=3.1))
        s.add(VerdictOverride(opportunity_id=m.id, operator_verdict="apply", operator_score=4.0))
        s.add(VerdictOverride(opportunity_id=ids["d"], operator_verdict="pass",
                              operator_score=1.5))
        await s.commit()

    asyncio.run(_with_session(_label))
    assert _worth("employment")["n"] == 3  # +m, -d (e is being scored; c, f are Applied)


def test_today_with_an_empty_database_says_nothing_is_waiting():
    asyncio.run(reset_engine())
    engine = _fresh_engine()
    asyncio.run(engine.dispose())
    forget_engine()
    assert _worth() == {"n": 0, "strong": 0, "your_call": 0}
    assert _act() == {"n": 0, "apply": 0, "follow_up": 0, "earliest": None}
    with _client() as c:
        body = c.get("/").text
    assert "No matches yet" in body
    assert "Nothing is waiting on you." in body and "Nothing is due this week." in body


def test_the_asks_are_off_the_page_but_the_failed_role_note_stays_in_the_retry_note():
    """D-UI-92: the page no longer carries the asks (the sidebar status and the nav badge do),
    and a failed role is the retry note's job. The ask lines themselves are unchanged."""
    asyncio.run(_seed())
    with _client() as c:
        body = c.get("/").text
    assert 'data-testid="today-asks"' not in body and 'data-testid="today-setup-rule"' not in body
    asks = asyncio.run(_with_session(lambda s: system_asks(s, _settings(), None)))
    assert any(a["key"] == "quarantine" and a["title"] == "1 posting needs another try"
               for a in asks)
    retry = body.split('data-testid="td-retry"')[1].split("</div>")[0]
    assert "1 role needs another try." in retry and 'href="/quarantine"' in retry


# ------------------------------------------------------------------- scoring progress panel
_ACTIVE = {
    "active": True, "total": 66, "scored": 38, "failed": 3, "running": 4, "waiting": 21,
    "model": "nvidia/nemotron (free, online)", "eta_minutes": 25, "last_run": None,
}


def _patch_progress(monkeypatch, value):
    async def fake(_s, _settings=None):
        return dict(value)

    from kestrel.web import routes_control

    monkeypatch.setattr(routes_control, "scoring_progress", fake)


def test_progress_view_states():
    from kestrel.analytics.progress import EMPTY

    assert progress_view(EMPTY)["state"] == "none"
    assert progress_view(None)["state"] == "none"
    v = progress_view(_ACTIVE)
    assert v["state"] == "active" and v["done"] == 41 and v["total"] == 66
    assert v["widths"] == {"scored": 57.6, "failed": 4.5, "running": 6.1}
    ended = iso(_now() - timedelta(hours=2))
    fin = progress_view(dict(EMPTY, last_run={"ended_at": ended, "status": "done", "scored": 41,
                                              "failed": 3}))
    assert fin["state"] == "finished" and fin["last_run"]["status"] == "finished"
    old = progress_view(dict(EMPTY, last_run={"ended_at": iso(_now() - timedelta(hours=13)),
                                              "status": "failed", "scored": 1, "failed": 0}))
    assert old["state"] == "none" and old["last_run"]["status"] == "failed"


def test_status_rail_says_scoring_and_how_the_run_ended(monkeypatch):
    from kestrel.analytics.progress import EMPTY

    asyncio.run(_seed())
    _patch_progress(monkeypatch, _ACTIVE)
    with _client() as c:
        assert "Scoring · 41 of 66" in c.get("/status/rail").text
    last = {"ended_at": iso(_now() - timedelta(hours=1)), "status": "done", "scored": 41,
            "failed": 3}
    _patch_progress(monkeypatch, dict(EMPTY, last_run=last))
    with _client() as c:
        rail = c.get("/status/rail").text
    assert "Scoring finished · 41 scored, 3 failed" in rail
    assert "Not scoring anything right now" not in rail
    _patch_progress(monkeypatch, EMPTY)
    with _client() as c:
        assert "Not scoring anything right now" in c.get("/status/rail").text


def test_open_failures_show_on_today_and_the_rail_whatever_the_last_run_said(monkeypatch):
    """D-UI-56: the latest small run read "1 scored, 0 failed" while 8 earlier failures still
    waited. open_failed is independent of the run: Today's run panel and the rail say "N roles
    need another try" with a link to the Scoring failed group whenever it is above zero."""
    from kestrel.analytics.progress import EMPTY
    from kestrel.web.pages import today as today_page

    assert progress_view(dict(EMPTY, open_failed=8))["state"] == "failed"
    assert progress_view(dict(EMPTY, open_failed=8))["open_failed"] == 8
    asyncio.run(_seed())
    last = {"ended_at": iso(_now() - timedelta(hours=1)), "status": "done", "scored": 1,
            "failed": 0}
    _patch_progress(monkeypatch, dict(EMPTY, last_run=last, open_failed=8))

    async def eight(_s, _track=None):
        return 8

    monkeypatch.setattr("kestrel.web.queries.open_quarantine_count", eight)
    with _client() as c:
        body = c.get("/").text
        panel = c.get("/today/run").text
        rail = c.get("/status/rail").text
    # Today says it in the retry note; the run-panel partial and the rail keep their wording.
    note = body.split('data-testid="td-retry"')[1].split("</div>")[0]
    assert 'href="/quarantine"' in note and "8 roles need another try." in note
    line = panel.split('data-testid="open-failed"')[1].split("</p>")[0]
    assert 'href="/quarantine"' in line and "8 roles need another try" in line
    assert 'data-rail="failed"' in rail and "8 roles need another try" in rail
    assert 'href="/quarantine"' in rail
    assert "—" not in body

    async def none(_s, _track=None):
        return 0

    monkeypatch.setattr("kestrel.web.queries.open_quarantine_count", none)
    _patch_progress(monkeypatch, dict(EMPTY, last_run=last))
    with _client() as c:
        assert 'data-testid="td-retry"' not in c.get("/").text
        assert 'data-testid="open-failed"' not in c.get("/today/run").text
        assert 'data-rail="failed"' not in c.get("/status/rail").text
    assert today_page.run_panel  # the panel is the Today surface now


# ---------------------------------------------------------------------------- calibrate ask
def _scored_roles(n: int) -> None:
    async def _add(s):
        org = Org(name="Ridgeline", normalized_name="ridgeline")
        src = Source(track="employment", kind="greenhouse", name="more", enabled=True)
        s.add_all([org, src])
        await s.flush()
        for i in range(n):
            o = _opp(org, src, f"cal{i}", url=f"https://x/cal-{i}")
            s.add(o)
            await s.flush()
            s.add(_ev(o, verdict="consider", verdict_model="consider", score=3.2))
        await s.commit()

    asyncio.run(_with_session(_add))


def _asks():
    return asyncio.run(_with_session(lambda s: system_asks(s, _settings(), "employment")))


def test_the_calibrate_ask_shows_once_after_a_finished_run_and_can_be_dismissed():
    """The ask is still computed (the notification and the nav badge read the same count); Today
    no longer prints it (D-UI-92), so the dismiss route and the ask text are checked directly."""
    asyncio.run(_seed())
    assert "taste" not in [a["key"] for a in _asks()]  # 4 scored roles: too few to rate
    _scored_roles(12)
    asks = _asks()
    taste = next(a for a in asks if a["key"] == "taste")
    assert taste["title"] == "Teach Kestrel your taste."
    assert taste["line"] == ("Rate 15 scored roles and it starts ranking the way you would. "
                             "Takes about five minutes.")
    assert taste["href"] == "/calibrate?track=employment"
    # It stands in for the bare label-count ask while it shows.
    assert "labels:employment" not in [a["key"] for a in asks]
    with _client() as c:
        assert c.post("/today/asks/nope/dismiss").status_code == 404
        assert c.post("/today/asks/taste/dismiss").status_code == 200
    after = _asks()
    assert "taste" not in [a["key"] for a in after]
    assert next(a for a in after if a["key"] == "labels:employment")["title"] == "12 more ratings"

    async def _row(s):
        return (await s.get(Setting, "ui.asks_dismissed")).value

    assert asyncio.run(_with_session(_row)) == ["taste"]


def test_the_calibrate_ask_waits_for_a_finished_run():
    asyncio.run(_seed(with_run=False))
    _scored_roles(15)
    assert "taste" not in [a["key"] for a in _asks()]


def test_worth_a_look_counts_tier3_and_your_call_and_the_nav_badge_shares_it():
    """One "worth a look" count for Today and the sidebar badge, by the Pipeline's rules: the
    latest evaluation (a Tier 3 apply over a Tier 2 consider counts), your call counts, and an
    opportunity with two Tier 2 apply rows counts once."""
    from kestrel.analytics.today import worth_a_look
    from kestrel.web.routes_control import _nav_counts

    ids = asyncio.run(_seed())

    async def _more(s):
        a = await s.get(Opportunity, ids["a"])
        org = await s.get(Org, a.org_id)
        src = await s.get(Source, a.source_id)
        x = _opp(org, src, "x")
        s.add(x)
        await s.flush()
        s.add(_ev(x, verdict="consider", verdict_model="consider", score=3.2))
        s.add(_ev(x, tier=3, verdict="apply", verdict_model="apply", score=4.7))
        s.add(_ev(a, score=4.2))  # a second Tier 2 apply for a: never a second count
        await s.commit()

    asyncio.run(_with_session(_more))

    async def _counts(s):
        return (await worth_a_look(s))["n"], (await _nav_counts(s))["shortlist"]

    # a, b (your call), d and x (D-UI-51); e is being scored, c and f are Applied (#37).
    assert asyncio.run(_with_session(_counts)) == (4, 4)


def test_a_past_interview_is_an_outcome_to_record_not_an_overdue_follow_up():
    ids = asyncio.run(_seed())

    async def _interview(s):
        p = (
            await s.execute(
                Pursuit.__table__.select().where(Pursuit.opportunity_id == ids["e"])
            )
        ).first()
        row = await s.get(Pursuit, p.id)
        row.next_action = "Interview"
        row.next_action_at = (_today() - timedelta(days=3)).isoformat()
        await s.commit()

    asyncio.run(_with_session(_interview))
    assert _act("employment")["follow_up"] == 2  # c and f, never the interview
    due = asyncio.run(_with_session(lambda s: actions_due(s, "employment")))
    (item,) = [i for i in due["items"] if i["opp_id"] == ids["e"]]
    assert item["label"] == "record the outcome" and item["action"].startswith("Interview was")
    assert due["overdue"] == 1 and due["outcome"] == 1


def test_the_act_card_counts_a_consulting_lead_being_researched():
    asyncio.run(_seed())

    async def _lead(s):
        o = Opportunity(track="consulting", opp_type="engagement", title="Fractional CIO",
                        org_name="Ridgeline", url="https://x/lead", stage="t2_done")
        s.add(o)
        await s.flush()
        s.add(Pursuit(opportunity_id=o.id, track="consulting", status="researching"))
        await s.commit()

    asyncio.run(_with_session(_lead))
    assert _act("consulting")["apply"] == 1


def test_quarantine_copy_follows_the_stored_error():
    from collections import Counter

    from kestrel.analytics.today import quarantine_kind, quarantine_line, quarantine_why

    assert quarantine_kind("the reply hit max_tokens 900 before the structured object was "
                           "complete") == "length"
    assert quarantine_kind("schema validation failed: verdict not in enum") == "schema"
    assert quarantine_kind("connection reset") == "other"
    assert quarantine_why(Counter(length=2)) == "The model ran out of room."
    assert quarantine_why(Counter(schema=1)) == (
        "The model's answer was not in the required format."
    )
    assert quarantine_why(Counter(length=1, schema=2)) == (
        "1 ran out of room and 2 came back in the wrong format."
    )
    assert "ran out of room" not in quarantine_line(1, Counter(schema=1))
    # The seed's one parked row failed on its schema, so Today never says "ran out of room".
    asyncio.run(_seed())
    asks = " ".join(f"{a['title']} {a['line']}" for a in _asks())
    assert "1 posting needs another try" in asks and "ran out of room" not in asks
    assert "not in the required format" in asks


def test_today_help_and_escalate_use_the_operator_words(monkeypatch):
    from kestrel.pipeline.tier3 import EscalationResult
    from kestrel.web.pages import escalate

    asyncio.run(_seed())
    with _client() as c:
        body = c.get("/").text
        help_page = c.get("/help").text
    assert "pursuits past" not in body and "pursuit past" not in body
    assert "VP of IT f" in body  # f, silent for 30 days, is an application needing action
    assert "becomes keep" not in help_page
    assert "When it is unsure, the role is kept" in help_page

    async def fake(_s, _c, _settings, _opp_id, *, confirmation_token):
        return EscalationResult(sent=True, transmission_id=7, evaluation_id=9, verdict="apply",
                                score=4.8)

    monkeypatch.setattr(escalate, "transmit_escalation", fake)
    with _client() as c:
        r = c.post("/opportunities/1/escalate/confirm", data={"confirmation_token": "t"})
    assert "<strong>strong fit</strong>" in r.text


def test_about_and_help_link_the_site_and_ko_fi():
    """D-UI-64, as the locked mockups settle it: About carries Check for updates and, in Source and
    support, the site and Ko-fi; Help's More help card carries the site."""
    asyncio.run(_seed())
    with _client() as c:
        about = c.get("/about").text
        helped = c.get("/help").text
    assert ">Check for updates</button>" in about
    support = about.split('data-testid="about-support"')[1].split("</article>")[0]
    assert 'href="https://ko-fi.com/distantsoil"' in support and ">Ko-fi " in support
    assert 'href="https://kestrelapp.co"' in support and ">Visit " in support
    more = helped.split('data-testid="hp-more-actions"')[1].split("</div>")[0]
    assert 'href="https://kestrelapp.co"' in more and ">kestrelapp.co</a>" in more


def test_the_speed_note_is_on_models_settings_and_onboarding():
    from pathlib import Path

    note = ("Free models, online or on your own computer, are often much slower than paid ones. "
            "A first scan can take an hour or more; Today shows the progress.")
    root = Path(paths.web_templates_dir())
    for name in ("models.html", "partials/st_mode.html", "partials/onboarding_body.html"):
        assert note in (root / name).read_text(encoding="utf-8"), name


def test_calibrate_explains_itself_in_the_how_it_works_flyout():
    # The old intro panel became the How it works flyout (D-UI-99): what it does, how many
    # ratings it needs, and the page's purpose line, with no jargon about "informative" roles.
    asyncio.run(_seed())
    with _client() as c:
        body = c.get("/calibrate").text
    fly = body.split('id="cal-how-fly"')[1]
    assert "What you do" in fly and "What your ratings change" in fly
    assert "At 15 ratings" in fly and "At 30" in fly
    assert 'data-testid="page-purpose"' in body and "does not review your resume" in body
    assert "ten most informative" not in body


def test_hx_trigger_filters_are_valid_htmx():
    """htmx ends a trigger filter at the FIRST `]`, so `[!q('dialog[open]')]` is cut mid-selector,
    and a filter must sit directly after its event name (`evt[filter] from:body`), not after the
    modifiers. Either mistake logs htmx:syntax:error on every load (the Today strip did both).
    `every 5s [filter]` is the one form that takes a space."""
    import re
    from pathlib import Path

    import kestrel.web

    root = Path(kestrel.web.__file__).parent / "templates"
    bad: list[str] = []
    for path in root.rglob("*.html"):
        for attr in re.findall(r'hx-trigger="([^"]*)"', path.read_text(encoding="utf-8")):
            for m in re.finditer(r"(\S*?)(\s*)\[(.*?)\]", attr):
                before, gap, inner = m.groups()
                every = r"every\s+(?:\d+(?:ms|s|m)|\{\{.*?\}\})\s*$"
                if "[" in inner:
                    bad.append(f"{path.name}: nested bracket in filter: {attr}")
                elif gap and not re.search(every, attr[: m.end(1)]):
                    bad.append(f"{path.name}: filter not directly after event name: {attr}")
    assert not bad, bad
