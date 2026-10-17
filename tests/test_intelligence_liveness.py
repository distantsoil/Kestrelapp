"""Liveness: a 404 (or a "position filled" page) marks a shortlisted opportunity closed and writes
a notification; a transient error leaves it unchanged. Offline (a fake HTTP client, no network)."""

from __future__ import annotations

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


async def test_404_marks_closed_and_notifies(sm):
    opp_id = await _seed(sm)
    async with sm() as s:
        opp = await s.get(Opportunity, opp_id)
        state = await check_liveness(s, _Status(404), opp)
        await s.commit()
    assert state == "closed"
    async with sm() as s:
        opp = await s.get(Opportunity, opp_id)
        notes = (await s.execute(select(Notification))).scalars().all()
    assert opp.liveness == "closed"
    assert opp.liveness_checked_at is not None
    assert len(notes) == 1
    assert notes[0].kind == "liveness_closed"
    assert notes[0].opportunity_id == opp_id


async def test_position_filled_text_marks_closed(sm):
    opp_id = await _seed(sm)
    async with sm() as s:
        opp = await s.get(Opportunity, opp_id)
        state = await check_liveness(s, _Body("Sorry, this position has been filled."), opp)
        await s.commit()
    assert state == "closed"


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
