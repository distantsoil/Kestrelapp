"""D-PERF-2: the grouped counts behind the nav badge, the status rail and Today's Review card,
and the role page's Previous and Next, load no full row and answer exactly what the old code did.
The old implementations are kept here as the oracle. Cloud-safe: a migrated SQLite file, no model,
no network."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import iso
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import (
    Evaluation,
    Opportunity,
    Org,
    Pursuit,
    Quarantine,
    Setting,
    Source,
    Task,
    VerdictOverride,
)
from kestrel.services import count_cache, tracks
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON, GROUP_NEW, GROUP_WORTH, STRONG, YOUR_CALL
from kestrel.web.queries import (
    GROUP_ORDER,
    OppFilters,
    _base_stmt,
    _group_expr,
    _with_operator,
    list_opportunities,
    row_fit,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _now() -> datetime:
    return datetime.now(UTC)


def _fresh_engine():
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    return make_async_engine(paths.db_path())


# --------------------------------------------------------------------------- the old code (oracle)
async def old_worth_a_look(session, track=None, *, top: int = 0) -> dict:
    from kestrel.analytics.today import _track_scope
    from kestrel.db.models import Org as OrgM
    from kestrel.web.redact import confidential_once

    track = _track_scope(track)
    stmt = _with_operator(
        _base_stmt(OppFilters(track=track), Opportunity, Evaluation, OrgM.name)
    ).where(_group_expr() == GROUP_ORDER.index(GROUP_WORTH))
    n = strong = your_call = 0
    ranked = []
    for opp, ev, org_name in (await session.execute(stmt)).all():
        n += 1
        fit = row_fit(opp, ev)
        strong += int(fit.word == STRONG)
        your_call += int(fit.word == YOUR_CALL)
        if top:
            score = fit.score if fit.score is not None else -1.0
            ranked.append((score, opp.id, opp, fit, org_name or opp.org_name))
    out = {"n": n, "strong": strong, "your_call": your_call}
    if top:
        conf, mapping = await confidential_once(session)
        assert not conf  # the oracle is compared unredacted; redaction is tested separately
        ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
        out["top"] = [
            {"id": opp.id, "title": opp.title or "Untitled", "org": org, "word": fit.word,
             "tone": fit.tone, "score": fit.score}
            for _s, _i, opp, fit, org in ranked[:top]
        ]
    return out


async def old_unrated(session, track=None) -> int:
    from sqlalchemy import func

    return (
        await session.execute(
            _with_operator(_base_stmt(OppFilters(track=track), func.count())).where(
                _group_expr() == GROUP_ORDER.index(GROUP_NEW)
            )
        )
    ).scalar_one()


async def old_sequence(session, f: OppFilters) -> list[int]:
    f.grouped = True
    f.page_size = 200
    f.limits = {k: 200 for k in ("worth", "maybe", "applied", "new", "failed")}
    page = await list_opportunities(session, f)
    return [r.opp.id for r in page.rows]


# --------------------------------------------------------------------------------- the fixture
def _opp(org, src, name, **kw) -> Opportunity:
    base = dict(
        track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
        title=f"Role {name}", org_name=org.name, url=f"https://example.test/{name}",
        stage="t2_done", description_md="Own the work.",
    )
    base.update(kw)
    return Opportunity(**base)


def _ev(opp, **kw) -> Evaluation:
    base = dict(opportunity_id=opp.id, tier=2, track=opp.track, verdict="apply", score=4.6,
                weighted_score=4.5, needs_review=False, model="m")
    base.update(kw)
    return Evaluation(**base)


# name -> (opportunity kwargs, evaluation kwargs or None, extras)
SPECS: dict[str, tuple[dict, dict | None, dict]] = {
    "strong": ({}, {"score": 4.8, "weighted_score": 4.7}, {}),
    "good": ({}, {"score": 4.1, "weighted_score": 4.0}, {}),
    "your_call": ({}, {"verdict": "consider", "score": 3.8, "weighted_score": 3.3,
                       "needs_review": True}, {}),
    "review_strong": ({}, {"score": 4.9, "weighted_score": 4.8, "needs_review": True}, {}),
    "review_below": ({}, {"verdict": "pass", "score": 1.5, "weighted_score": 1.2,
                          "needs_review": True}, {}),
    "maybe": ({}, {"verdict": "consider", "score": 3.2, "weighted_score": 3.1}, {}),
    "pass": ({}, {"verdict": "pass", "score": 2.0, "weighted_score": 2.0}, {}),
    "kill": ({}, {"verdict": "kill", "score": None, "weighted_score": None}, {}),
    "t0_reject": ({"stage": "t0_reject", "stage_reason": "blocker:salary_floor: low"}, None, {}),
    "t1_reject": ({"stage": "t1_reject"}, {"tier": 1, "verdict": "kill"}, {}),
    "new": ({"stage": "new"}, None, {}),
    "t1_keep": ({"stage": "t1_pass"}, {"tier": 1, "verdict": "keep", "score": None,
                                     "weighted_score": None}, {}),
    "failed": ({"stage": "quarantined"}, None, {"quarantine": True}),
    "error_live": ({"stage": "error"}, None, {"task": {"status": "pending"}}),
    "failed_leased": ({"stage": "quarantined"}, None,
                      {"task": {"status": "leased", "lease": 600}}),
    "failed_backoff": ({"stage": "quarantined"}, None, {"task": {"status": "pending",
                                                                  "retry": 600}}),
    "failed_deferred": ({"stage": "quarantined"}, None, {"task": {"status": "pending",
                                                                   "deferred": True}}),
    "failed_lane": ({"stage": "quarantined"}, None, {"task": {"status": "pending", "lane": 1}}),
    "failed_old_ev": ({"stage": "quarantined"}, {}, {}),
    "empty_posting": ({"stage": "t1_pass", "stage_reason": EMPTY_POSTING_STAGE_REASON}, None, {}),
    "hidden_applied": ({"stage": "new", "archive_reason": "applied"}, None, {}),
    "removed": ({"stage": "quarantined"}, None, {"removed": True}),
    "listed_new": ({"stage": "new"}, None, {"pursuit": "shortlisted"}),
    "listed_strong": ({}, {"score": 4.9}, {"pursuit": "preparing"}),
    "applied": ({}, {"verdict": "pass", "score": 2.0}, {"pursuit": "applied"}),
    "interview": ({}, {}, {"pursuit": "interviewing"}),
    "closed_pursuit": ({}, {"verdict": "consider", "score": 3.0}, {"pursuit": "shortlisted",
                                                                    "closed": True}),
    "label_apply": ({}, {"verdict": "consider", "score": 3.1}, {"label": "apply"}),
    "label_pass": ({}, {"score": 4.7}, {"label": "pass"}),
    "label_consider": ({}, {"verdict": "pass", "score": 2.0}, {"label": "consider"}),
    "label_on_failed": ({"stage": "quarantined"}, None, {"label": "apply"}),
    "label_twice": ({}, {"verdict": "consider", "score": 3.1}, {"label": "pass",
                                                                 "label2": "pursue"}),
    "tier3": ({"stage": "t3_done"}, {"tier": 3, "score": 4.4}, {}),
    "tier1_forced": ({}, {"tier": 1, "verdict": "apply", "score": 4.4}, {}),
    "two_evals": ({}, {"tier": 2, "score": 3.0, "verdict": "consider"},
                  {"extra_eval": {"tier": 2, "score": 4.9, "verdict": "apply"}}),
    "hidden_org": ({}, {}, {"org": "hidden"}),
    "hidden_org_pursued": ({}, {}, {"org": "hidden", "pursuit": "shortlisted"}),
    "duplicate": ({"is_canonical": False}, {}, {}),
    "duplicate_saved": ({"is_canonical": False}, {}, {"saved": True}),
    "archived": ({"archived_at": "2026-01-01T00:00:00+00:00"}, {}, {}),
    "saved": ({}, {"verdict": "consider", "score": 3.3}, {"saved": True}),
}


async def _seed(sm) -> dict[str, int]:
    ids: dict[str, int] = {}
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        orgs = {
            "plain": Org(name="Plain Co", normalized_name="plain co"),
            "hidden": Org(name="Hidden Co", normalized_name="hidden co",
                          hidden_at=iso(_now())),
        }
        s.add_all([src, *orgs.values()])
        await s.flush()
        for track in ("employment", "consulting"):
            for name, (okw, ekw, extra) in SPECS.items():
                key = f"{track}:{name}"
                opp = _opp(orgs[extra.get("org", "plain")], src, key, track=track, **okw)
                s.add(opp)
                await s.flush()
                ids[key] = opp.id
                if ekw is not None:
                    s.add(_ev(opp, **ekw))
                if "extra_eval" in extra:
                    s.add(_ev(opp, **extra["extra_eval"]))
                if extra.get("quarantine"):
                    s.add(Quarantine(opportunity_id=opp.id, tier=2, error="bad"))
                if "task" in extra:
                    t = extra["task"]
                    payload = {"opportunity_id": opp.id}
                    if t.get("deferred"):
                        payload["deferred"] = 1
                    if t.get("lane"):
                        payload["lane"] = 1
                    s.add(Task(
                        type="t2", payload=payload, status=t["status"],
                        lease_until=iso(_now() + timedelta(seconds=t["lease"]))
                        if t.get("lease") else None,
                        retry_at=iso(_now() + timedelta(seconds=t["retry"]))
                        if t.get("retry") else None,
                    ))
                if "pursuit" in extra:
                    s.add(Pursuit(
                        opportunity_id=opp.id, track=track, status=extra["pursuit"],
                        closed_at=iso(_now()) if extra.get("closed") else None,
                        last_activity_at=iso(_now()),
                    ))
                for k in ("label", "label2"):
                    if extra.get(k):
                        s.add(VerdictOverride(opportunity_id=opp.id, operator_verdict=extra[k],
                                              operator_score=4.0))
                if extra.get("saved"):
                    s.add(Setting(key=f"saved_role.{opp.id}", value=True))
                if extra.get("removed"):
                    s.add(Setting(key=f"retry_removed.{opp.id}", value=True))
        await s.commit()
    return ids


@pytest.fixture
def sm():
    asyncio.run(reset_engine())
    engine = _fresh_engine()
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _tracks_back():
    before = tracks._cache["value"]
    yield
    tracks._cache["value"] = before
    count_cache.clear()


@contextlib.contextmanager
def _watch_loads(into: list[object]):
    """Record every Opportunity or Evaluation the ORM builds while the block runs."""

    def note(target, _ctx) -> None:
        into.append(target)

    for model in (Opportunity, Evaluation):
        event.listen(model, "load", note)
    try:
        yield
    finally:
        for model in (Opportunity, Evaluation):
            event.remove(model, "load", note)


def _run(sm, fn):
    async def go():
        async with sm() as s:
            return await fn(s)

    return asyncio.run(go())


# ---------------------------------------------------------------------------------------- parity
@pytest.mark.parametrize("enabled", [("employment", "consulting"), ("employment",)])
def test_counts_equal_the_old_code_for_every_track_group_and_state(sm, enabled):
    from kestrel.analytics.today import group_counts, worth_a_look, worth_count

    asyncio.run(_seed(sm))
    tracks._cache["value"] = enabled  # a switched-off track holds its tasks (D-UI-68)
    count_cache.clear()

    async def check(s):
        seen_groups: set[str] = set()
        for track in (None, "employment", "consulting", "nonsense"):
            old = await old_worth_a_look(s, track, top=4)
            new = await worth_a_look(s, track, top=4)
            assert new == old, track
            assert await worth_count(s, track) == old["n"]
            scope = track if track in ("employment", "consulting") else None
            from kestrel.web.queries import group_total

            assert group_total(await group_counts(s), GROUP_NEW, scope) == await old_unrated(
                s, scope
            )
            f = OppFilters(track=scope, grouped=True)
            page = await list_opportunities(s, f)
            for key in GROUP_ORDER:
                assert group_total(await group_counts(s), key, scope) == page.group_counts[key]
                if page.group_counts[key]:
                    seen_groups.add(key)
        assert seen_groups == set(GROUP_ORDER)  # the fixture reaches every group

    _run(sm, check)


def test_the_fixture_reaches_strong_and_your_call(sm):
    asyncio.run(_seed(sm))

    async def check(s):
        out = await old_worth_a_look(s, None)
        assert out["strong"] >= 4 and out["your_call"] >= 2 and out["n"] > out["strong"]

    _run(sm, check)


def test_no_orm_row_is_loaded_for_the_counts(sm):
    from kestrel.analytics.today import worth_a_look, worth_count

    asyncio.run(_seed(sm))
    loaded: list[object] = []

    async def go(s):
        with _watch_loads(loaded):
            await worth_count(s, "employment")
            await worth_a_look(s, "employment", top=3)
            await worth_a_look(s, None)

    _run(sm, go)
    assert loaded == []


def test_the_nav_counts_and_badges_match_the_old_numbers(sm):
    from kestrel.web.routes_control import _nav_counts

    asyncio.run(_seed(sm))
    tracks._cache["value"] = ("employment", "consulting")
    count_cache.clear()

    async def check(s):
        for track in (None, "employment", "consulting"):
            got = await _nav_counts(s, track)
            assert got["shortlist"] == (await old_worth_a_look(s, track))["n"]
            assert got["unrated"] == await old_unrated(s, track)
        got = await _nav_counts(s, "employment")
        for t in ("employment", "consulting"):
            assert got[f"track-{t}"] >= (await old_worth_a_look(s, t))["n"]

    _run(sm, check)


# -------------------------------------------------------------------------------------- sequence
def test_previous_and_next_equal_the_old_sequence_for_every_role(sm):
    from kestrel.web.pages.role import _sequence

    ids = asyncio.run(_seed(sm))

    async def old(s, opp):
        f = OppFilters(track=opp.track, kind="opportunities")
        seq = await old_sequence(s, f)
        if opp.id not in seq:
            return None
        i = seq.index(opp.id)
        return {"pos": i + 1, "total": len(seq), "prev": seq[i - 1] if i > 0 else None,
                "next": seq[i + 1] if i + 1 < len(seq) else None}

    async def check(s):
        seen = 0
        for oid in ids.values():
            opp = await s.get(Opportunity, oid)
            want = await old(s, opp)
            assert await _sequence(s, opp) == want, oid
            seen += want is not None
        assert seen > 10

    _run(sm, check)


def test_sequence_honors_the_sort_and_filters_of_the_default_view(sm):
    from kestrel.web.queries import sequence_ids

    asyncio.run(_seed(sm))

    async def check(s):
        for sort in ("score", "date", "first_seen", "oldest", "company", "pre_score", "ghost"):
            for q in (None, "Role employment:s"):
                f = OppFilters(track="employment", kind="opportunities", sort=sort, q=q)
                assert await sequence_ids(s, f) == await old_sequence(s, f), (sort, q)

    _run(sm, check)


def test_sequence_loads_no_full_row(sm):
    from kestrel.web.pages.role import _sequence

    ids = asyncio.run(_seed(sm))
    loaded: list[object] = []

    async def go(s):
        opp = await s.get(Opportunity, ids["employment:strong"])
        with _watch_loads(loaded):
            assert await _sequence(s, opp) is not None

    _run(sm, go)
    assert loaded == []


# ----------------------------------------------------------------------------------- invalidation
def test_the_cache_is_used_while_nothing_changes_and_dropped_on_every_watched_write(sm):
    from kestrel.analytics.today import group_counts

    asyncio.run(_seed(sm))
    calls: list[str] = []

    async def go():
        async with sm() as s:
            engine = s.get_bind()
            event.listen(
                engine, "before_cursor_execute",
                lambda *a: calls.append(a[2]) if a[2].lstrip().upper().startswith("SELECT")
                else None,
            )
            first = await group_counts(s)
            n = len(calls)
            assert await group_counts(s) == first
            assert len(calls) == n  # served from the cache: no statement ran
            # a write to each watched table, committed, shows on the very next read
            opp = (await s.execute(select(Opportunity).where(
                Opportunity.title == "Role employment:good"))).scalar_one()
            s.add(VerdictOverride(opportunity_id=opp.id, operator_verdict="pass",
                                  operator_score=1.0))
            await s.commit()
            changed = await group_counts(s)
            assert changed != first
            await s.execute(update(Task).where(Task.id == -1).values(status="done"))
            await s.commit()
            n = len(calls)
            await group_counts(s)
            assert len(calls) > n  # even a no-row write to a watched table drops it
            return first, changed

    first, changed = asyncio.run(go())
    assert first["employment"]["worth"] == changed["employment"]["worth"] + 1


def test_a_write_that_rolls_back_never_leaves_its_numbers_behind(sm):
    from kestrel.analytics.today import group_counts

    ids = asyncio.run(_seed(sm))

    async def go():
        async with sm() as writer, sm() as reader:
            before = await group_counts(reader)
            await writer.execute(update(Opportunity).where(
                Opportunity.id == ids["employment:strong"]).values(stage="t0_reject"))
            # uncommitted: another session must see (and cache) nothing of it
            assert await group_counts(reader) == before
            await writer.rollback()
            assert await group_counts(reader) == before
            await writer.execute(update(Opportunity).where(
                Opportunity.id == ids["employment:strong"]).values(stage="t0_reject"))
            await writer.commit()
            after = await group_counts(reader)
            assert after != before

    asyncio.run(go())


def test_a_session_sees_its_own_unflushed_changes(sm):
    from kestrel.analytics.today import worth_count

    ids = asyncio.run(_seed(sm))

    async def go():
        async with sm() as s:
            before = await worth_count(s, "employment")
            opp = await s.get(Opportunity, ids["employment:good"])
            s.add(VerdictOverride(opportunity_id=opp.id, operator_verdict="pass"))
            assert await worth_count(s, "employment") == before - 1  # autoflush, not the cache
            await s.rollback()

    asyncio.run(go())


def test_the_cache_ages_out_and_follows_the_track_switch(sm, monkeypatch):
    from kestrel.analytics.today import group_counts

    asyncio.run(_seed(sm))

    async def go():
        async with sm() as s:
            await group_counts(s)
            key = next(iter(count_cache._entries))
            v, t, val = count_cache._entries[key]
            count_cache._entries[key] = (v, t - count_cache.TTL_SECONDS - 1, val)
            calls = []
            event.listen(s.get_bind(), "before_cursor_execute", lambda *a: calls.append(1))
            await group_counts(s)
            assert calls  # recomputed: a retry or lease coming due is not a write
            calls.clear()
            tracks._cache["value"] = ("consulting",)
            await group_counts(s)
            assert calls  # a different held-track set is a different key

    asyncio.run(go())


def test_every_table_the_group_statement_reads_is_watched(sm):
    from kestrel.analytics.today import _worth_rows
    from kestrel.web.queries import group_counts_by_track

    asyncio.run(_seed(sm))
    seen: list[str] = []

    async def go(s):
        event.listen(s.get_bind(), "before_cursor_execute", lambda *a: seen.append(a[2]))
        await group_counts_by_track(s)
        await _worth_rows(s, None)

    _run(sm, go)
    import re

    tables = {
        m.lower() for sql in seen for m in re.findall(r'(?:from|join)\s+"?(\w+)"?', sql, re.I)
    }
    assert tables - {"select", "sqlite_master"} <= count_cache.WATCHED
