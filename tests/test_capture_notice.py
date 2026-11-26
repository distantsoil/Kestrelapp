"""The "role received and being scored" notice for a capture from the browser extension (issue
#267; owner, item 66, 2026-11-25: the text approved as written, the notice fades after a few
seconds, the bell row stays as the record).

What is proved here, against the real app with an isolated data home:
- A new role captured with the extension's key writes one bell row that names the role and opens it.
- The next status rail poll, which every page makes, carries it once as a toast linking to the role;
  the poll after that does not repeat it.
- A capture of a role Kestrel already has, and a capture without the key, write no notice.
- A notice older than the toast window stays a bell row and is never shown as a toast.
- Under Confidential mode the employer in the role's title is shown as its placeholder.
Nothing here reaches the network: the app answers in-process and the notice is local.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import DocMaster, DocSection, Notification, Setting
from kestrel.web import auth

KEY = "test-capture-notice-key-0123456789"
TEXT = "Depot Scheduler\n" + " ".join(
    ["Plan the weekly depot schedule and keep the drivers informed."] * 14)
URL = "https://jobs.example.test/depot-scheduler"
NOTICE = "Role received and being scored: Depot Scheduler"


@pytest.fixture(autouse=True)
def _fresh_db():
    auth.reset_cache()
    migrate.run_migrations()
    yield
    auth.reset_cache()
    forget_engine()


def _with_session(fn):
    async def go():
        eng = make_async_engine(paths.db_path())
        try:
            sm = async_sessionmaker(eng, expire_on_commit=False)
            async with sm() as s:
                return await fn(s)
        finally:
            await eng.dispose()

    return asyncio.run(go())


def _add(*rows) -> None:
    async def go(s):
        s.add_all(list(rows))
        await s.commit()

    _with_session(go)


def _notices() -> list[Notification]:
    async def go(s):
        return list((await s.execute(
            select(Notification).where(Notification.kind == "role_captured")
            .order_by(Notification.id))).scalars())

    return _with_session(go)


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False), base_url="http://127.0.0.1")


def _capture(c: TestClient, *, key: str | None = KEY, url: str = URL, title="Depot Scheduler"):
    headers = {"X-Kestrel-Key": key} if key else {}
    r = c.post("/api/opportunities/ingest", headers=headers,
               json={"url": url, "text": TEXT, "title": title, "score_now": True})
    assert r.status_code == 200, r.text
    return r.json()["id"]


CLAIM = "/notifications/captured/claim"


def _toast(c: TestClient) -> dict | None:
    """What one open page shows: the rail poll (a read) renders a claim element when a notice
    is waiting, and that element's POST claims it and answers with the toast."""
    r = c.get("/status/rail")
    assert r.status_code == 200
    if f'hx-post="{CLAIM}"' not in r.text:
        return None
    claimed = c.post(CLAIM)
    assert claimed.status_code == 200
    raw = claimed.headers.get("HX-Trigger")
    return json.loads(raw).get("k-toast") if raw else None


def test_a_new_capture_writes_one_bell_row_that_opens_the_role():
    _add(Setting(key=auth.SETTING_KEY, value=KEY))
    with _client() as c:
        opp_id = _capture(c)
    rows = _notices()
    assert [(n.title, n.opportunity_id, n.severity) for n in rows] == [(NOTICE, opp_id, "info")]
    from kestrel.web.notifications_view import context_action

    action = context_action("role_captured", opp_id)
    assert action == {"label": "Go to job detail", "href": f"/opportunities/{opp_id}",
                      "external": False}


def test_the_next_rail_poll_shows_it_once_as_a_toast_that_links_to_the_role():
    _add(Setting(key=auth.SETTING_KEY, value=KEY))
    with _client() as c:
        opp_id = _capture(c)
        first = _toast(c)
        assert first is not None, "the rail poll carried no toast"
        assert first["message"] == NOTICE
        assert first["href"] == f"/opportunities/{opp_id}"
        assert "timeout" not in first and "sticky" not in first  # the toast's normal fade
        assert _toast(c) is None, "the same notice was shown twice"
    assert len(_notices()) == 1  # the bell row stays as the record


def test_a_role_kestrel_already_has_writes_no_second_notice():
    _add(Setting(key=auth.SETTING_KEY, value=KEY))
    with _client() as c:
        first = _capture(c)
        again = _capture(c)
    assert again == first
    assert len(_notices()) == 1


def test_a_capture_without_the_key_writes_no_notice():
    _add(Setting(key=auth.SETTING_KEY, value=KEY))
    with _client() as c:
        _capture(c, key=None)
        assert _toast(c) is None
    assert _notices() == []


def test_an_old_notice_stays_in_the_bell_and_is_never_a_toast():
    _add(Setting(key=auth.SETTING_KEY, value=KEY))
    with _client() as c:
        _capture(c)

        async def age(s):
            for n in (await s.execute(select(Notification))).scalars():
                n.created_at = "2026-01-01T00:00:00.000000+00:00"
            await s.commit()

        _with_session(age)
        assert _toast(c) is None
    assert len(_notices()) == 1


def test_confidential_mode_shows_the_employer_as_its_placeholder():
    master = DocMaster(track="employment", name="Resume", doc_kind="resume")
    _add(Setting(key=auth.SETTING_KEY, value=KEY), Setting(key="confidential_mode", value=True),
         master)

    async def section(s):
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label="Harborline Freight", org_label_redacted="A logistics firm"))
        await s.commit()

    _with_session(section)
    with _client() as c:
        _capture(c, url="https://jobs.example.test/hf-1",
                 title="Depot Scheduler at Harborline Freight")
        shown = _toast(c)
        bell = c.get("/notifications/list").text
    assert len(_notices()) == 1
    # What the person sees: redacted when shown (the stored row keeps the title as captured).
    assert "Harborline Freight" not in bell and "A logistics firm" in bell
    assert shown is not None and "Harborline Freight" not in shown["message"]
    assert "A logistics firm" in shown["message"]


def _set_confidential(employer: str, placeholder: str, *, on: bool) -> None:
    master = DocMaster(track="employment", name="Resume", doc_kind="resume")
    rows = [master] + ([Setting(key="confidential_mode", value=True)] if on else [])
    _add(*rows)

    async def section(s):
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label=employer, org_label_redacted=placeholder))
        await s.commit()

    _with_session(section)


def _turn_confidential_on() -> None:
    _add(Setting(key="confidential_mode", value=True))


def test_confidential_turned_on_after_the_capture_hides_the_employer_everywhere():
    """Codex review 1: the employer is hidden by the mode in force when the notice is SHOWN, in
    the toast and in the bell, not only by the mode in force when it was captured."""
    _add(Setting(key=auth.SETTING_KEY, value=KEY))
    _set_confidential("Harborline Freight", "A logistics firm", on=False)
    with _client() as c:
        _capture(c, url="https://jobs.example.test/hf-2",
                 title="Depot Scheduler at Harborline Freight")
        _turn_confidential_on()
        shown = _toast(c)
        bell = c.get("/notifications/list").text
    assert shown is not None and "Harborline Freight" not in shown["message"], shown
    assert "A logistics firm" in shown["message"], shown
    assert "Harborline Freight" not in bell
    assert "A logistics firm" in bell


def test_a_failed_notice_write_never_breaks_the_capture(monkeypatch):
    """Codex review 2: a database error while recording the notice leaves the capture a success,
    with the stored role in the answer."""
    from kestrel.services import capture_notice

    async def broken(session, *_a, **_k):
        # A row the database refuses at commit (title is NOT NULL): the write fails at commit
        # time, after the role was stored, the way a busy or full database fails.
        session.add(Notification(kind="role_captured", title=None, severity="info"))

    monkeypatch.setattr(capture_notice, "record", broken)
    _add(Setting(key=auth.SETTING_KEY, value=KEY))
    with _client() as c:
        r = c.post("/api/opportunities/ingest", headers={"X-Kestrel-Key": KEY},
                   json={"url": URL, "text": TEXT, "title": "Depot Scheduler", "score_now": True})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Depot Scheduler" and isinstance(r.json()["id"], int)
    assert _notices() == []


def test_the_rail_poll_only_reads():
    """Codex review 4: the GET every page polls never writes. Polled three times, the notice is
    still waiting; only the claim (a POST) marks it shown."""
    _add(Setting(key=auth.SETTING_KEY, value=KEY))
    with _client() as c:
        _capture(c)
        for _ in range(3):
            assert f'hx-post="{CLAIM}"' in c.get("/status/rail").text
        assert [n.delivered_channels for n in _notices()] == [[]]
        assert c.get(CLAIM).status_code == 405
        assert c.post(CLAIM).status_code == 200
        assert [n.delivered_channels for n in _notices()] == [["toast"]]
        assert f'hx-post="{CLAIM}"' not in c.get("/status/rail").text
