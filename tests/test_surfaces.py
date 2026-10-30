"""Small surfaces (D-UI-152): Score again and Run now with friendly names and the ask-first step,
the bell panel, the update and what is new cards, announcements, toasts and the error page.
No network, no live model: fixtures and a throwaway data directory (the autouse isolation)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Notification, Opportunity, Pursuit, Source, Task

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "kestrel" / "web" / "static"
TEMPLATES = ROOT / "kestrel" / "web" / "templates"
HX = {"HX-Request": "true"}


async def _fresh() -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


def _client(monkeypatch=None, *, raise_server_exceptions: bool = True) -> TestClient:
    from kestrel.web.app import build_app

    asyncio.run(_fresh())
    if monkeypatch is not None:
        monkeypatch.setattr("kestrel.web.pages.rescore._local_models",
                            lambda settings: (["kestrel-tier3:latest"], None))
    return TestClient(build_app(worker=False), raise_server_exceptions=raise_server_exceptions)


def _run(coro):
    out = asyncio.run(coro)
    forget_engine()
    return out


# ------------------------------------------------------------------------------- Score again
def test_score_again_names_the_model_and_where_it_runs_and_asks_before_an_online_send(
        monkeypatch):
    client = _client(monkeypatch)

    async def seed() -> int:
        async with get_sessionmaker()() as s:
            o = Opportunity(track="employment", opp_type="job_posting",
                            title="Senior Ops Analyst", org_name="Pinecrest", stage="t2_done")
            s.add(o)
            await s.commit()
            return o.id

    oid = _run(seed())
    body = client.get(f"/rescore/choose?opp_ids={oid}").text
    assert "Score this role again?" in body and "Senior Ops Analyst" in body
    assert re.search(r"<b>Same model</b><span>[^<]+, on this computer\.", body)
    assert "kestrel-tier" not in body.split("<script>")[0].replace(
        'value="kestrel-tier3:latest"', "").replace('data-value="kestrel-tier3:latest"', "")
    assert "Your usual model stays the same afterward." in body
    # The ask-first step is in the dialog, hidden until an online model is picked.
    assert 'data-testid="score-again-confirm"' in body and "Send this to" in body
    assert "Not sent: your name, contact details and your resume file." in body
    assert "Send and score" in body and "—" not in body
    # The no-script page carries a plain consent box instead of the second step.
    page = client.get(f"/rescore/choose?opp_ids={oid}&page=1").text
    assert "<dialog" in page and 'name="confirmed"' not in page.split("</dialog>")[1]


def test_rescore_refuses_an_online_pick_that_was_not_confirmed(monkeypatch):
    client = _client(monkeypatch)

    async def seed() -> int:
        async with get_sessionmaker()() as s:
            o = Opportunity(track="employment", opp_type="job_posting", title="VP",
                            org_name="Acme", stage="t2_done")
            s.add(o)
            await s.commit()
            return o.id

    oid = _run(seed())
    data = {"opp_ids": str(oid), "model_choice": "other", "model": "hosted:x/y:free"}
    r = client.post("/rescore", data=data, headers=HX)
    assert "Confirm the online send first" in r.text and "nothing was sent" in r.text
    r = client.post("/rescore", data={**data, "confirmed": "1"}, headers=HX)  # Hosted mode is off
    assert "Hosted mode, which is off" in r.text
    # No-script: a fixed outcome code, back to the page it came from.
    r = client.post("/rescore", data={**data, "next": "/quarantine"}, follow_redirects=False)
    assert r.status_code == 303 and "outcome=not_confirmed" in r.headers["location"]
    assert "Confirm the online send first" in client.get(r.headers["location"]).text
    assert _run(_count_tasks()) == 0


async def _count_tasks() -> int:
    async with get_sessionmaker()() as s:
        return len((await s.execute(select(Task))).scalars().all())


def test_history_shows_friendly_names_never_a_raw_kestrel_tag():
    from kestrel.web.pages.rescore import comparison, friendly_name, short_model

    bases = {"tier2": "qwen3:14b"}
    assert friendly_name("kestrel-tier2:latest", bases) == "qwen3:14b"
    assert friendly_name("kestrel-tier2", {}) == "Kestrel's model for this step"
    assert friendly_name("openai/gpt-4o-mini", bases) == "gpt-4o-mini"
    rows = [{"model": "kestrel-tier2:latest", "score": 4.0},
            {"model": "google/gemma-4-31b:free", "score": 3.5}]
    line = comparison(rows, bases)
    assert line == "qwen3:14b scored 0.5 higher than Gemma." and "kestrel" not in line
    assert short_model("kestrel-tier2:latest") == "kestrel-tier2"  # the unknown-base fallback


def test_history_partial_has_no_raw_id():
    from jinja2 import Environment, FileSystemLoader

    from kestrel.web.deps import static_url

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    env.globals["static_url"] = static_url
    rows = [
        {"date": "2026-10-02", "model": "kestrel-tier2:latest", "name": "qwen3:14b",
         "fit": "good fit", "tone": "good", "score": 4.1},
        {"date": "2026-10-01", "model": "kestrel-tier3:latest", "name": "qwen3:30b",
         "fit": "maybe", "tone": "warn", "score": 3.0},
    ]
    html = env.get_template("partials/score_history_list.html").render(rows=rows, compare="x")
    assert "qwen3:14b" in html and "kestrel-tier" not in html


# --------------------------------------------------------------------------------- Run now
def _sources() -> list[int]:
    async def seed() -> list[int]:
        async with get_sessionmaker()() as s:
            rows = [Source(track="employment", kind="greenhouse", name=n, enabled=True)
                    for n in ("Meridian board", "Harbor board")]
            s.add_all(rows)
            await s.commit()
            return [r.id for r in rows]

    return _run(seed())


async def _scrape_ids() -> list[int]:
    async with get_sessionmaker()() as s:
        tasks = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all()
        return sorted(t.payload["source_id"] for t in tasks)




def test_run_now_refuses_an_online_pick_that_was_not_confirmed(monkeypatch):
    client = _client(monkeypatch)
    sid = _sources()[0]
    data = {"model_choice": "other", "model": "hosted:x/y:free"}
    r = client.post(f"/sources/{sid}/run-now", data=data, headers=HX)
    assert "Confirm the online send first" in r.text
    r = client.post("/scan/start", data=data, headers=HX)
    assert "Confirm the online send first" in r.text
    r = client.post(f"/sources/{sid}/run-now", data={**data, "confirmed": "1"}, headers=HX)
    assert "Hosted mode, which is off" in r.text
    assert _run(_scrape_ids()) == []


# --------------------------------------------------------------------------- bell panel
def _notes() -> dict[str, int]:
    async def seed() -> dict[str, int]:
        now = datetime.now(UTC)
        async with get_sessionmaker()() as s:
            o = Opportunity(track="employment", opp_type="job_posting", title="Ops lead",
                            org_name="Pinecrest", stage="t2_done")
            s.add(o)
            await s.flush()
            p = Pursuit(opportunity_id=o.id, track="employment", status="applied",
                        next_action="Send a thank you",
                        next_action_at=(now - timedelta(days=1)).date().isoformat())
            s.add(p)
            await s.flush()
            rows = {
                "due": Notification(kind="next_action_due", severity="info", pursuit_id=p.id,
                                    title="Next action due: Send a thank you"),
                "src": Notification(kind="source_disabled", severity="warning",
                                    title="Harbor board stopped after three failures"),
                "done": Notification(kind="run_finished", severity="success",
                                     title="Run finished: 4 new",
                                     created_at=(now - timedelta(days=3)).isoformat()),
            }
            s.add_all(rows.values())
            await s.commit()
            return {k: v.id for k, v in rows.items()}

    return _run(seed())


def test_bell_panel_tabs_groups_human_words_and_actions():
    client = _client()
    ids = _notes()
    body = client.get("/notifications/list").text
    assert 'data-testid="notifications-panel"' in body
    for label in ("All", "Needs attention", "Dismissed"):  # Dismissed is not recorded yet
        assert label in body
    assert "Today" in body and "Earlier" in body
    # A human severity, never the raw word; a plain relative time, never a raw stamp.
    assert "Needs attention" in body and ">warning<" not in body and ">info<" not in body
    assert "just now" in body and "3 days ago" in body
    # A due follow-up opens Applications; there is no Draft follow-up (D-UI-148).
    assert "Open in Applications" in body and "Draft follow-up" not in body
    assert 'href="/pursuits"' in body and "Open Sources" in body
    assert 'data-testid="notif-tab-dismissed"' in body and "disabled" in body and "—" not in body
    # The unresolved problem is badged and listed under Needs attention.
    attention = client.get("/notifications/list?tab=attention").text
    assert "Unresolved" in attention and "Harbor board stopped" in attention
    assert "Run finished" not in attention
    assert ids["src"] > 0










def test_relative_time_and_groups():
    from kestrel.web import notifications_view as nv

    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    iso = lambda **k: (now - timedelta(**k)).isoformat()  # noqa: E731
    assert nv.relative_time(iso(seconds=10), now) == "just now"
    assert nv.relative_time(iso(minutes=5), now) == "5 minutes ago"
    assert nv.relative_time(iso(hours=1), now) == "1 hour ago"
    assert nv.relative_time(iso(days=1), now) == "Yesterday"
    assert nv.relative_time(iso(days=4), now) == "4 days ago"
    assert nv.relative_time(iso(days=40), now).startswith("Sep")
    assert nv.relative_time("junk", now) == ""
    assert nv.day_group(iso(hours=2), now) == "Today"
    assert nv.day_group(iso(days=1), now) == "Yesterday"
    assert nv.day_group(iso(days=9), now) == "Earlier"
    assert nv.context_action("next_action_due", None)["label"] == "Open in Applications"
    assert nv.context_action("no_such_kind", None) is None




# ----------------------------------------------------------------- update and announcements
def test_update_cards_say_get_it_from_the_site_and_never_offer_to_restart_or_install():
    for name in ("update_banner", "update_details", "update_whats_new", "update_check_result",
                 "update_disclosure"):
        text = (TEMPLATES / "partials" / f"{name}.html").read_text(encoding="utf-8").lower()
        assert "restart to update" not in text and "install now" not in text, name
        assert "—" not in text, name
    banner = (TEMPLATES / "partials" / "update_banner.html").read_text(encoding="utf-8")
    assert "Get it from kestrelapp.co" in banner and "is available." in banner


# ----------------------------------------------------------------------------------- toasts
def test_toasts_use_the_shared_region_and_carry_undo_through():
    text = (TEMPLATES / "partials" / "toast.html").read_text(encoding="utf-8")
    assert "window.KUI.toast(" in text and "undoUrl: opts.undoUrl" in text
    assert "undo: opts.undo" in text
    # The persistent scan toast keeps its own box (Phase 1 decision).
    assert 'id="kestrel-toast"' in text


# ---------------------------------------------------------------------------------- error page
def test_a_failing_page_shows_the_calm_error_page_with_plain_details(monkeypatch):
    from fastapi import APIRouter

    from kestrel.web.app import build_app

    asyncio.run(_fresh())
    app = build_app(worker=False)
    router = APIRouter()

    @router.get("/boom")
    async def boom():
        raise RuntimeError("secret /home/user/key=abc123 must not be shown")

    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/boom", headers={"accept": "text/html"})
    assert r.status_code == 500
    assert "Kestrel couldn’t load this page" in r.text
    assert "Your roles and settings are safe on this computer." in r.text
    assert "Try again" in r.text and "Show details" in r.text and "Copy details" in r.text
    assert "What happened: an unexpected problem (RuntimeError)" in r.text
    assert "secret" not in r.text and "abc123" not in r.text and "/home/user" not in r.text
    assert "Kestrel " in r.text and "—" not in r.text
    # Not a page navigation: Starlette's own answer, so a fragment swap or a client is unchanged.
    assert client.get("/boom", headers=HX).text == "Internal Server Error"
    assert client.get("/boom", headers={"accept": "application/json"}).text == (
        "Internal Server Error")


# ---------------------------------------------------------------------------------- the sheet
def test_surfaces_css_is_token_only_and_prefixed():
    css = (STATIC / "css" / "pages" / "surfaces.css").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", body)
    assert "—" not in css
    for selector in re.findall(r"\.([a-zA-Z][\w-]*)", body):
        assert selector.startswith(
            ("sf-", "k-", "ke-", "score-", "badge", "model-picker", "is-")), selector


def test_new_templates_have_no_em_dash():
    for f in [TEMPLATES / "error.html", *(TEMPLATES / "partials").glob("update_*.html"),
              TEMPLATES / "partials" / "notifications_list.html",
              TEMPLATES / "partials" / "score_again_dialog.html",
              TEMPLATES / "partials" / "run_now_dialog.html",
              TEMPLATES / "partials" / "score_history_list.html",
              TEMPLATES / "partials" / "announcement.html"]:
        assert "—" not in f.read_text(encoding="utf-8"), f.name


def test_bell_count_is_unread_and_read_toggles_in_place():
    client = _client()
    ids = _notes()
    assert client.get("/notifications/bell").text == "3"
    r = client.post(f"/notifications/{ids['src']}/read?tab=all")
    assert 'id="bellcount"' in r.text and ">2<" in r.text  # the count swaps out of band
    assert client.get("/notifications/bell").text == "2"
    client.post(f"/notifications/{ids['src']}/unread?tab=all")
    assert client.get("/notifications/bell").text == "3"
    assert client.post("/notifications/99999/read").status_code == 200
    assert client.post(f"/notifications/{ids['due']}/bogus").status_code == 200


def test_run_a_search_now_lists_all_and_marks_pick_as_not_recorded(monkeypatch):
    client = _client(monkeypatch)
    _sources()
    body = client.get("/scan/choose").text
    assert "Run a search now" in body and "All my searches (2)" in body
    assert "Pick which ones" in body and "Not recorded yet" in body and "—" not in body
    assert 'data-testid="run-now-confirm"' in body


def test_online_refusal_covers_confirm_hosted_mode_and_confidential_hold(monkeypatch):
    from types import SimpleNamespace

    from kestrel.services import online_consent
    from kestrel.web.pages import rescore

    def settings(active: bool):
        return SimpleNamespace(llm=SimpleNamespace(hosted_mode=SimpleNamespace(active=active)))

    # The send names its destination (D-UI-178): the dialog showed "d" and the form posts it back.
    monkeypatch.setattr(rescore, "_approval_for_override", lambda _s, _o: {"tier2": ["d"]})
    shown = {"online_approval": '{"tier2":["d"]}'}

    async def state(value):
        async def fake(_s, _settings):
            return value
        monkeypatch.setattr(online_consent, "employer_state", fake)

    async def go() -> list:
        out = []
        await state("off")
        out.append(await rescore.online_refusal(None, settings(True), {}))
        out.append(await rescore.online_refusal(None, settings(True), {"confirmed": "1"}))
        out.append(await rescore.online_refusal(None, settings(False), {"confirmed": "1", **shown}))
        await state("held")
        out.append(await rescore.online_refusal(None, settings(True), {"confirmed": "1", **shown}))
        await state("hidden")
        out.append(await rescore.online_refusal(None, settings(True), {"confirmed": "1", **shown}))
        return out

    assert asyncio.run(go()) == ["not_confirmed", "not_confirmed", "hosted_off", "held", None]
    assert "held" in rescore.OUTCOMES


def test_the_dialogs_disable_send_while_a_confidential_send_would_be_held():
    for name, tid in (("score_again_dialog", "score-again-send"),
                      ("run_now_dialog", "run-now-send")):
        src = (TEMPLATES / "partials" / f"{name}.html").read_text(encoding="utf-8")
        assert f'data-testid="{tid}"{{% if consent.employer == \'held\' %}} disabled' in src
