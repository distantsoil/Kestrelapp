"""Issue #255 (owner item 39; SPEC-INTERFACES "The bell and its panel"): a notification can be
dismissed from its menu. A dismissed item leaves All and Needs attention, is no longer counted
on the bell, and is listed under the Dismissed tab, where Restore brings it back. Dismissing never
deletes the row. Invented data only; no network."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Notification
from tests import clock_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _client() -> TestClient:
    from kestrel.web.app import build_app

    async def fresh():
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()
        forget_engine()

    asyncio.run(fresh())
    return TestClient(build_app(worker=False))


def _seed() -> dict[str, int]:
    async def go():
        now = datetime.now(UTC)
        async with get_sessionmaker()() as s:
            rows = {
                "src": Notification(kind="source_disabled", severity="warning",
                                    title="Harbor board stopped after three failures"),
                "done": Notification(kind="run_finished", severity="success",
                                     title="Run finished: 4 new",
                                     created_at=(now - timedelta(days=3)).isoformat()),
            }
            s.add_all(rows.values())
            await s.commit()
            return {k: v.id for k, v in rows.items()}
    out = asyncio.run(go())
    forget_engine()
    return out


def _row(note_id: int) -> Notification | None:
    async def go():
        async with get_sessionmaker()() as s:
            return await s.get(Notification, note_id)
    out = asyncio.run(go())
    forget_engine()
    return out


def _item_ids(body: str) -> set[int]:
    return {int(m) for m in re.findall(r'data-note-id="(\d+)"', body)}


def test_each_item_offers_a_working_dismiss():
    client = _client()
    ids = _seed()
    body = client.get("/notifications/list").text
    assert f'hx-post="/notifications/{ids["src"]}/dismiss?tab=all"' in body
    button = re.search(r'<button[^>]*data-testid="notif-dismiss"[^>]*>', body)
    assert button is not None and "disabled" not in button.group(0)
    assert "Not recorded yet" not in body


def test_dismiss_moves_the_item_to_dismissed_and_off_the_bell():
    client = _client()
    ids = _seed()
    assert client.get("/notifications/bell").text == "2"
    r = client.post(f"/notifications/{ids['src']}/dismiss?tab=all")
    assert r.status_code == 200
    assert _item_ids(r.text) == {ids["done"]}  # the answer is the panel without it
    assert 'id="bellcount"' in r.text and ">1<" in r.text  # the count swaps out of band
    assert client.get("/notifications/bell").text == "1"
    attention = client.get("/notifications/list?tab=attention").text
    assert "Harbor board stopped" not in attention
    assert 'class="sf-tab-n"' not in attention  # nothing left needs attention
    dismissed = client.get("/notifications/list?tab=dismissed").text
    assert _item_ids(dismissed) == {ids["src"]}
    assert f'hx-post="/notifications/{ids["src"]}/restore?tab=dismissed"' in dismissed
    assert 'data-testid="notif-dismiss"' not in dismissed
    row = _row(ids["src"])
    assert row is not None and row.dismissed_at  # recorded, never deleted


def test_restore_brings_it_back_and_the_dismissed_tab_says_when_it_is_empty():
    from kestrel.web.notifications_view import NOTIF_DISMISSED_EMPTY

    assert NOTIF_DISMISSED_EMPTY == "No dismissed notifications."
    client = _client()
    ids = _seed()
    empty = client.get("/notifications/list?tab=dismissed").text
    assert NOTIF_DISMISSED_EMPTY in empty and _item_ids(empty) == set()
    tab = re.search(r'<button[^>]*data-testid="notif-tab-dismissed"[^>]*>', empty).group(0)
    assert "disabled" not in tab and 'aria-selected="true"' in tab
    client.post(f"/notifications/{ids['src']}/dismiss?tab=all")
    r = client.post(f"/notifications/{ids['src']}/restore?tab=dismissed")
    assert NOTIF_DISMISSED_EMPTY in r.text and _item_ids(r.text) == set()
    assert _item_ids(client.get("/notifications/list").text) == {ids["src"], ids["done"]}
    assert _row(ids["src"]).dismissed_at is None


def test_a_dismissed_item_is_not_counted_even_when_marked_unread():
    client = _client()
    ids = _seed()
    client.post(f"/notifications/{ids['src']}/dismiss?tab=all")
    client.post(f"/notifications/{ids['src']}/unread?tab=dismissed")
    assert _row(ids["src"]).read_at is None
    assert client.get("/notifications/bell").text == "1"
    from kestrel.web.deps import bell_unread

    assert bell_unread() == 1  # the badge printed by the page agrees


def test_an_unknown_id_or_action_changes_nothing():
    client = _client()
    ids = _seed()
    assert client.post("/notifications/99999/dismiss").status_code == 200
    assert client.post(f"/notifications/{ids['src']}/bogus").status_code == 200
    assert _row(ids["src"]).dismissed_at is None


# ------------------------------------------------- review round 1 (Codex): every reader of the row
def _dismiss(client, note_id: int) -> None:
    assert client.post(f"/notifications/{note_id}/dismiss?tab=all").status_code == 200


def test_a_dismissed_row_is_never_delivered_as_a_pop_up():
    from kestrel.config import get_settings
    from kestrel.services.delivery import deliver_pending

    client = _client()
    ids = _seed()  # both rows are still waiting to be delivered
    _dismiss(client, ids["src"])
    settings = get_settings()
    settings.notify.enabled = True
    settings.notify.toast = False  # no real pop-up in the test
    settings.notify.ntfy_topic = None
    settings.notify.webhook_url = None
    settings.notify.events = {}
    settings.notify.sound = False

    async def go():
        async with get_sessionmaker()() as s:
            return await deliver_pending(s, settings)
    result = asyncio.run(go())
    forget_engine()
    assert result.sent == 1 and result.collapsed == 1, result
    assert result.title == "Run finished: 4 new", result
    # Handled, so a later Restore never sends a late pop-up for it.
    assert _row(ids["src"]).delivered_channels


def test_a_dismissed_not_scored_note_does_not_reach_the_pipeline_strip():
    from kestrel.db.models import Opportunity
    from kestrel.services.score_again_notice import KIND, record_not_scored, take_notice

    client = _client()

    async def seed():
        async with get_sessionmaker()() as s:
            o = Opportunity(track="employment", opp_type="job_posting", title="Ops lead",
                            stage="t2_done", seen_count=1)
            s.add(o)
            await s.flush()
            await record_not_scored(s, o.id)
            await s.commit()
            from sqlalchemy import select
            return (await s.execute(select(Notification.id).where(Notification.kind == KIND))
                    ).scalar_one()
    note_id = asyncio.run(seed())
    forget_engine()
    _dismiss(client, note_id)

    async def strip():
        async with get_sessionmaker()() as s:
            return await take_notice(s)
    assert asyncio.run(strip()) is None
    forget_engine()


def test_restore_puts_an_item_back_at_its_own_time_and_answers_on_dismissed():
    client = _client()
    now = datetime.now(UTC)

    async def seed():
        async with get_sessionmaker()() as s:
            old = Notification(kind="run_finished", severity="success", title="Old run",
                               created_at=(now - timedelta(days=5)).isoformat())
            s.add(old)
            s.add_all([Notification(kind="run_finished", severity="success", title=f"Run {i}",
                                    created_at=clock_seed.ago(now, minutes=i).isoformat())
                       for i in range(20)])
            await s.commit()
            return old.id
    old = asyncio.run(seed())
    forget_engine()
    _dismiss(client, old)
    r = client.post(f"/notifications/{old}/restore?tab=dismissed")
    # The answer stays on Dismissed (the item left it); it does not claim the item is on All.
    assert 'data-tab="dismissed"' in r.text and old not in _item_ids(r.text)
    assert _row(old).dismissed_at is None
    # Back in All at its own time: older than the newest 20, so not on the panel's first 20,
    # like any other notification that old.
    shown = _item_ids(client.get("/notifications/list").text)
    assert len(shown) == 20 and old not in shown


def test_a_row_dismissed_then_restored_before_delivery_never_pops_up_later():
    from kestrel.config import get_settings
    from kestrel.services.delivery import deliver_pending

    client = _client()
    ids = _seed()  # both rows are still waiting to be delivered
    _dismiss(client, ids["src"])
    assert client.post(f"/notifications/{ids['src']}/restore?tab=dismissed").status_code == 200
    settings = get_settings()
    settings.notify.enabled = True
    settings.notify.toast = False
    settings.notify.ntfy_topic = None
    settings.notify.webhook_url = None
    settings.notify.events = {}
    settings.notify.sound = False

    async def go():
        async with get_sessionmaker()() as s:
            return await deliver_pending(s, settings)
    result = asyncio.run(go())
    forget_engine()
    # The restored row was already seen in the bell: only the other one is delivered.
    assert result.sent == 1 and result.collapsed == 1, result
    assert result.title == "Run finished: 4 new", result

