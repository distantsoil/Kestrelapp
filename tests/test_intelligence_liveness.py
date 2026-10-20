"""Liveness (D-INTEL-1): a posting closes only after two consecutive expired reads (a 404/410 or a
"position filled" page), with the reason stored and one notification; a live read resets the
count; a transient error touches nothing. Offline (a fake HTTP client, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Notification, Opportunity, Org, Pursuit, Source
from kestrel.intelligence.liveness import check_liveness, due_for_liveness, run_liveness_cycle


class _Status:
    def __init__(self, status: int) -> None:
        self.status = status

    async def get_text(self, url: str, **kw) -> str:
        req = httpx.Request("GET", url)
        resp = httpx.Response(self.status, request=req)
        raise httpx.HTTPStatusError(f"{self.status}", request=req, response=resp)


class _Body:
    def __init__(self, body: str) -> None:
        self._body = body

    async def get_text(self, url: str, **kw) -> str:
        return self._body


async def _seed(sm, *, url="https://x/1", checked_at=None) -> int:
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP IT", org_name="Acme", url=url, liveness="live",
            liveness_checked_at=checked_at,
        )
        s.add(opp)
        await s.flush()
        s.add(Pursuit(opportunity_id=opp.id, track="employment", status="shortlisted"))
        await s.commit()
        return opp.id


async def _check(sm, opp_id, http):
    async with sm() as s:
        opp = await s.get(Opportunity, opp_id)
        state = await check_liveness(s, http, opp)
        await s.commit()
    return state


async def _row(sm, opp_id):
    async with sm() as s:
        return await s.get(Opportunity, opp_id)


async def _notes(sm):
    async with sm() as s:
        return (await s.execute(select(Notification))).scalars().all()


async def test_one_404_is_a_strike_not_a_close(sm):
    opp_id = await _seed(sm)
    assert await _check(sm, opp_id, _Status(404)) == "live"
    opp = await _row(sm, opp_id)
    assert opp.liveness == "live" and opp.liveness_strikes == 1 and opp.closed_reason is None
    assert opp.liveness_checked_at is not None
    assert await _notes(sm) == []


async def test_two_consecutive_404s_close_with_reason_and_one_notification(sm):
    opp_id = await _seed(sm)
    await _check(sm, opp_id, _Status(404))
    assert await _check(sm, opp_id, _Status(404)) == "closed"
    opp = await _row(sm, opp_id)
    assert opp.liveness == "closed" and opp.closed_reason == "http_404"
    assert opp.liveness_strikes == 2
    notes = await _notes(sm)
    assert len(notes) == 1 and notes[0].kind == "liveness_closed"
    assert notes[0].opportunity_id == opp_id
    assert "not found twice" in notes[0].body and "\u2014" not in notes[0].body
    # A third expired read on a closed row neither re-notifies nor changes the reason.
    await _check(sm, opp_id, _Status(410))
    assert len(await _notes(sm)) == 1
    assert (await _row(sm, opp_id)).closed_reason == "http_404"


async def test_410_reason_is_recorded(sm):
    opp_id = await _seed(sm)
    await _check(sm, opp_id, _Status(410))
    await _check(sm, opp_id, _Status(410))
    assert (await _row(sm, opp_id)).closed_reason == "http_410"


async def test_position_filled_text_needs_two_reads_and_records_page_text(sm):
    opp_id = await _seed(sm)
    page = _Body("Sorry, this position has been filled.")
    assert await _check(sm, opp_id, page) == "live"
    assert (await _row(sm, opp_id)).liveness_strikes == 1
    assert await _check(sm, opp_id, page) == "closed"
    opp = await _row(sm, opp_id)
    assert opp.closed_reason == "page_text"
    assert "page said the role is filled twice" in (await _notes(sm))[0].body


async def test_expired_live_expired_does_not_close(sm):
    opp_id = await _seed(sm)
    await _check(sm, opp_id, _Status(404))
    await _check(sm, opp_id, _Body("We are actively hiring for this role."))
    opp = await _row(sm, opp_id)
    assert opp.liveness_strikes == 0 and opp.closed_reason is None
    await _check(sm, opp_id, _Status(404))
    opp = await _row(sm, opp_id)
    assert opp.liveness == "live" and opp.liveness_strikes == 1
    assert await _notes(sm) == []


async def test_transient_error_between_two_expired_reads_does_not_reset(sm):
    opp_id = await _seed(sm)
    await _check(sm, opp_id, _Status(404))
    await _check(sm, opp_id, _Status(503))
    assert (await _row(sm, opp_id)).liveness_strikes == 1
    assert await _check(sm, opp_id, _Status(404)) == "closed"


async def test_a_live_read_clears_a_closed_reason(sm):
    opp_id = await _seed(sm)
    await _check(sm, opp_id, _Status(404))
    await _check(sm, opp_id, _Status(404))
    await _check(sm, opp_id, _Body("We are actively hiring for this role."))
    opp = await _row(sm, opp_id)
    assert opp.liveness == "live" and opp.closed_reason is None and opp.liveness_strikes == 0


async def test_a_one_strike_row_is_not_closed_for_the_ghost_model(sm):
    """req_reappeared counts a DIFFERENT posting of the same requisition that is closed. One strike
    is not closed, so it must not raise the signal; the second strike does."""
    from kestrel.intelligence import ghost

    first, second = await _seed_many(sm, [None, None])
    async with sm() as s:
        for oid in (first, second):
            opp = await s.get(Opportunity, oid)
            opp.external_id = "REQ-1"
        await s.commit()
    await _check(sm, second, _Status(404))
    async with sm() as s:
        assert await ghost.req_reappeared(s, await s.get(Opportunity, first)) is False
    await _check(sm, second, _Status(404))
    async with sm() as s:
        assert await ghost.req_reappeared(s, await s.get(Opportunity, first)) is True


async def test_due_for_liveness_rechecks_a_strike_row_after_recheck_hours(sm):
    ids = await _seed_many(sm, [None, None])
    now = datetime.now(UTC)
    two_days = (now - timedelta(days=2)).isoformat()
    ten_hours = (now - timedelta(hours=10)).isoformat()
    thirty_hours = (now - timedelta(hours=30)).isoformat()
    async with sm() as s:
        a, b = await s.get(Opportunity, ids[0]), await s.get(Opportunity, ids[1])
        a.liveness_strikes, a.liveness_checked_at = 1, thirty_hours  # one strike, past 24 h
        b.liveness_strikes, b.liveness_checked_at = 1, ten_hours  # one strike, too soon
        await s.commit()
    async with sm() as s:
        due = await due_for_liveness(s, cadence_days=3, recheck_hours=24)
    assert [o.id for o in due] == [ids[0]]
    # A clean row checked two days ago is not due on a three-day cadence, strike or no strike.
    async with sm() as s:
        a = await s.get(Opportunity, ids[0])
        a.liveness_strikes, a.liveness_checked_at = 0, two_days
        await s.commit()
        assert await due_for_liveness(s, cadence_days=3, recheck_hours=24) == []


async def test_cycle_gives_a_strike_row_its_second_read_and_counts_the_close(sm):
    from kestrel.config import get_settings

    stale = "2026-01-01T00:00:00.000000+00:00"
    (oid,) = await _seed_many(sm, [stale])
    settings = get_settings()
    counts = await run_liveness_cycle(sm, settings, _Status(404))
    assert counts == {"due": 1, "checked": 1, "closed": 0, "errors": 0}
    async with sm() as s:  # age the strike so it is due again
        opp = await s.get(Opportunity, oid)
        opp.liveness_checked_at = stale
        await s.commit()
    counts = await run_liveness_cycle(sm, settings, _Status(404))
    assert counts == {"due": 1, "checked": 1, "closed": 1, "errors": 0}


async def test_transient_error_leaves_liveness_unchanged(sm):
    opp_id = await _seed(sm)
    async with sm() as s:
        opp = await s.get(Opportunity, opp_id)
        state = await check_liveness(s, _Status(503), opp)
        await s.commit()
    assert state == "live"  # a 503 is transient, not a closed role
    async with sm() as s:
        opp = await s.get(Opportunity, opp_id)
        notes = (await s.execute(select(Notification))).scalars().all()
    assert opp.liveness == "live"
    assert notes == []


async def test_live_page_stays_live_no_notification(sm):
    opp_id = await _seed(sm)
    async with sm() as s:
        opp = await s.get(Opportunity, opp_id)
        state = await check_liveness(s, _Body("We are actively hiring for this role."), opp)
        await s.commit()
    assert state == "live"
    async with sm() as s:
        notes = (await s.execute(select(Notification))).scalars().all()
    assert notes == []


async def _seed_many(sm, checked_ats: list[str | None], *, url="https://x/1") -> list[int]:
    """Several pursued opportunities under one org, in rowid order of `checked_ats`."""
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        ids = []
        for i, checked in enumerate(checked_ats):
            opp = Opportunity(
                track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
                title=f"Role {i}", org_name="Acme", url=f"{url}/{i}", liveness="live",
                liveness_checked_at=checked,
            )
            s.add(opp)
            await s.flush()
            s.add(Pursuit(opportunity_id=opp.id, track="employment", status="shortlisted"))
            ids.append(opp.id)
        await s.commit()
        return ids


async def test_due_filter_runs_before_the_limit(sm):
    """A fresh row on a low rowid must not shadow a stale row beyond the limit: the due filter is
    applied in SQL, before the limit, oldest check first."""
    fresh = utcnow_iso()
    stale = "2026-01-01T00:00:00.000000+00:00"
    ids = await _seed_many(sm, [fresh, fresh, stale, None])
    async with sm() as s:
        due = await due_for_liveness(s, cadence_days=3, limit=1)
        assert [o.id for o in due] == [ids[3]]  # never checked comes first
        due = await due_for_liveness(s, cadence_days=3, limit=2)
        assert [o.id for o in due] == [ids[3], ids[2]]
        assert ids[0] not in {o.id for o in due} and ids[1] not in {o.id for o in due}


class _Raising:
    async def get_text(self, url: str, **kw) -> str:
        raise RuntimeError("unexpected failure, not an httpx error")


async def test_run_liveness_cycle_checks_everything_due_and_survives_one_failure(sm):
    from kestrel.config import get_settings

    stale = "2026-01-01T00:00:00.000000+00:00"
    ids = await _seed_many(sm, [stale, None, utcnow_iso()])
    settings = get_settings()
    assert (await run_liveness_cycle(sm, settings, _Status(404)))["closed"] == 0  # one strike each
    async with sm() as s:  # age the strikes so both are due for the second read
        for oid in ids[:2]:
            (await s.get(Opportunity, oid)).liveness_checked_at = stale
        await s.commit()
    counts = await run_liveness_cycle(sm, settings, _Status(404))
    assert counts == {"due": 2, "checked": 2, "closed": 2, "errors": 0}
    async with sm() as s:
        for oid in ids[:2]:
            opp = await s.get(Opportunity, oid)
            assert opp.liveness == "closed" and opp.liveness_checked_at is not None
        fresh = await s.get(Opportunity, ids[2])
        assert fresh.liveness == "live"  # not due: untouched
        notes = (await s.execute(select(Notification))).scalars().all()
        assert len(notes) == 2
    # A second pass finds nothing due (closed rows are excluded); it never re-notifies.
    assert (await run_liveness_cycle(sm, settings, _Status(404)))["due"] == 0


async def test_run_liveness_cycle_counts_an_item_error_and_continues(sm):
    from kestrel.config import get_settings

    stale = "2026-01-01T00:00:00.000000+00:00"
    await _seed_many(sm, [stale, stale])
    counts = await run_liveness_cycle(sm, get_settings(), _Raising())
    assert counts["due"] == 2 and counts["errors"] == 2 and counts["checked"] == 0


async def test_due_for_liveness_selects_stale_shortlisted(sm):
    # Checked long ago -> due; and one never checked -> due.
    old = "2026-01-01T00:00:00+00:00"
    opp_id = await _seed(sm, checked_at=old)
    async with sm() as s:
        due = await due_for_liveness(s, cadence_days=3, now=utcnow_iso())
    assert any(o.id == opp_id for o in due)


def test_migration_0020_adds_and_drops_the_liveness_columns(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine, inspect, text

    from kestrel import paths
    from kestrel.db import migrate

    db = tmp_path / "kestrel.db"
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        command.upgrade(cfg, "0019")
        engine = create_engine(f"sqlite:///{db.as_posix()}")
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO opportunity (id, track, opp_type, title, remote_type, "
                    "comp_source, description_tokens, first_seen_at, last_seen_at, seen_count, "
                    "ghost_score, version, is_canonical, liveness, stage, pre_score) VALUES "
                    "(1, 'employment', 'job_posting', 'VP IT', 'unknown', 'none', 0, "
                    "'2026-10-01', '2026-10-01', 1, 0.0, 1, 1, 'live', 'new', 0.0)"))
            command.upgrade(cfg, "0020")
            cols = {c["name"]: c for c in inspect(engine).get_columns("opportunity")}
            assert cols["liveness_strikes"]["nullable"] is False
            assert cols["closed_reason"]["nullable"] is True
            with engine.connect() as conn:  # an existing row backfills to zero strikes
                row = conn.execute(text(
                    "SELECT liveness_strikes, closed_reason FROM opportunity WHERE id = 1")).one()
            assert tuple(row) == (0, None)
            command.downgrade(cfg, "0019")
            cols = {c["name"] for c in inspect(engine).get_columns("opportunity")}
            assert "liveness_strikes" not in cols and "closed_reason" not in cols
        finally:
            engine.dispose()


def test_closed_reasons_are_the_documented_set():
    from kestrel.db.enums import CLOSED_REASONS

    assert CLOSED_REASONS == ("http_404", "http_410", "page_text", "ats_missing")
