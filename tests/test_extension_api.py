"""The browser extension's own API (issue #186), written from the spec without the implementation.

Invariant: the extension's routes only ever fill the description of the one waiting role named in
the path; they never create a role, never replace an existing description, refuse check pages
server-side, and refuse any request without the right X-Kestrel-Key.

"Waiting for a description" is a role in stage t1_pass whose stage_reason is the empty-posting
reason and that is not archived. All text below is invented. No test touches the network.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import (
    Evaluation,
    Opportunity,
    Pursuit,
    Setting,
    StatusEvent,
    Task,
)
from kestrel.web import auth
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON

KEY = "invented-extension-key-123"
EXT_ORIGIN = "chrome-extension://abcdefghijklmnop"
ORIGINS = [None, EXT_ORIGIN]

CHECK_PHRASES = [
    "verify you are human",
    "are you a robot",
    "checking your browser",
    "enable javascript and cookies",
    "just a moment",
    "access denied",
    "complete the security check",
    "press and hold",
    "captcha",
]

# Invented posting prose; repeated to reach a length well past the 80-word floor.
_SENTENCE = (
    "The team plans quarterly reports, cleans shared datasets, and explains trends to managers "
    "across the regional offices with clear charts and short written notes."
)
LONG_TEXT = " ".join([_SENTENCE] * 6)  # about 130 words
SHORT_TEXT = "Plan reports and clean shared datasets for the regional offices every week."


# ------------------------------------------------------------------ setup


@pytest.fixture(autouse=True)
def _fresh_db():
    auth.reset_cache()
    migrate.run_migrations()
    yield
    auth.reset_cache()
    forget_engine()


def _with_session(fn):
    """Run `fn(session)` on a private engine, so seeding never shares the app's cached engine."""

    async def go():
        eng = make_async_engine(paths.db_path())
        try:
            sm = async_sessionmaker(eng, expire_on_commit=False)
            async with sm() as s:
                return await fn(s)
        finally:
            await eng.dispose()

    return asyncio.run(go())


def _store_key(value: str = KEY) -> None:
    async def go(s):
        s.add(Setting(key=auth.SETTING_KEY, value=value))
        await s.commit()

    _with_session(go)
    auth.reset_cache()


def _seed_role(**fields) -> int:
    base = dict(
        track="employment", opp_type="job_posting", title="Senior Data Analyst",
        org_name="Bluefin Works", url="https://www.indeed.com/viewjob?jk=aaa111",
        stage="t1_pass", stage_reason=EMPTY_POSTING_STAGE_REASON,
        first_seen_at="2026-03-04T10:00:00.000000+00:00",
    )
    base.update(fields)

    async def go(s):
        opp = Opportunity(**base)
        s.add(opp)
        await s.commit()
        return opp.id

    return _with_session(go)


def _get_opp(opp_id: int) -> Opportunity | None:
    async def go(s):
        return await s.get(Opportunity, opp_id)

    return _with_session(go)


def _count(model) -> int:
    async def go(s):
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()

    return _with_session(go)


def _setting(key: str):
    async def go(s):
        row = await s.get(Setting, key)
        return None if row is None else row.value

    return _with_session(go)


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(
        build_app(worker=False),
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
        client=("127.0.0.1", 12345),
    )


def _headers(key: str | None = KEY, origin: str | None = None, **extra) -> dict[str, str]:
    h: dict[str, str] = {}
    if key is not None:
        h["X-Kestrel-Key"] = key
    if origin is not None:
        h["Origin"] = origin
    h.update(extra)
    return h


def _describe(c: TestClient, opp_id: int, text, *, dry_run: bool | None, page_title="", **more):
    body = {"text": text, "page_url": "https://www.indeed.com/viewjob?jk=aaa111",
            "page_title": page_title}
    if dry_run is not None:
        body["dry_run"] = dry_run
    body.update(more)
    return c.post(f"/api/extension/roles/{opp_id}/description", json=body, headers=_headers())


# ------------------------------------------------------------------ the key, on every route


def _route_calls(opp_id: int):
    return [
        ("GET", "/api/extension/config", None),
        ("GET", "/api/extension/waiting", None),
        ("POST", f"/api/extension/roles/{opp_id}/description",
         {"text": LONG_TEXT, "page_url": "", "page_title": "", "dry_run": True}),
        ("GET", f"/api/extension/roles/{opp_id}/summary", None),
        ("POST", f"/api/extension/roles/{opp_id}/applied", None),
        ("PUT", "/api/extension/features", {"badges": True}),
    ]


def _refusal(body: dict) -> str:
    """"guard" for the app-wide guard's {"detail"} body, else the router's error code, which
    must come with a plain sentence."""
    if body.get("detail") and "error" not in body:
        return "guard"
    if not (isinstance(body.get("message"), str) and body["message"]):
        return "no message"
    return str(body.get("error"))


def _guard_answers_first(method: str, origin: str | None) -> bool:
    return bool(origin) and method not in ("GET", "HEAD", "OPTIONS")


@pytest.mark.parametrize("origin", ORIGINS)
def test_every_route_refuses_a_missing_or_wrong_key_and_accepts_the_right_one(origin):
    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        for method, path, body in _route_calls(opp_id):
            for bad in (None, "not-the-key"):
                r = c.request(method, path, json=body, headers=_headers(bad, origin))
                assert r.status_code == 403, (method, path, bad, r.status_code)
                # The app-wide extension guard (app._extension_refusal) refuses an unsafe method
                # from an extension origin before the route runs, with its own body (D-EXT-742);
                # everywhere else the router's refusal answers.
                assert _refusal(r.json()) == (
                    "guard" if _guard_answers_first(method, origin) else "wrong_key"
                ), (method, path, r.text)
        # the refused description call did nothing, and the refused applied call did nothing
        assert _get_opp(opp_id).description_md in (None, "")
        assert _count(Pursuit) == 0
        for method, path, body in _route_calls(opp_id):
            r = c.request(method, path, json=body, headers=_headers(KEY, origin))
            assert 200 <= r.status_code < 300, (method, path, r.status_code, r.text)


@pytest.mark.parametrize("origin", ORIGINS)
def test_no_stored_key_answers_not_set_up(origin):
    opp_id = _seed_role()
    with _client() as c:
        for method, path, body in _route_calls(opp_id):
            for sent in (None, "any-guess"):
                r = c.request(method, path, json=body, headers=_headers(sent, origin))
                assert r.status_code == 403, (method, path, r.status_code)
                assert _refusal(r.json()) == (
                    "guard" if _guard_answers_first(method, origin) else "not_set_up"
                ), (method, path, r.text)
    assert _count(Pursuit) == 0


@pytest.mark.parametrize("origin", ORIGINS)
def test_document_routes_need_the_key(origin):
    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        for kind in ("resume.txt", "cover_letter.txt"):
            path = f"/api/extension/roles/{opp_id}/documents/{kind}"
            r = c.get(path, headers=_headers("not-the-key", origin))
            assert r.status_code == 403 and r.json().get("error") == "wrong_key"
            r = c.get(path, headers=_headers(None, origin))
            assert r.status_code == 403 and r.json().get("error") == "wrong_key"


# ------------------------------------------------------------------ config


def test_config_shape_and_waiting_hosts_count_only_waiting_roles():
    _store_key()
    _seed_role(url="https://www.indeed.com/viewjob?jk=1")
    _seed_role(url="https://WWW.Indeed.com/viewjob?jk=2")
    _seed_role(url="https://boards.example-ats.test/acme/jobs/3")
    _seed_role(url="https://www.indeed.com/viewjob?jk=4", archived_at="2026-03-05T00:00:00Z")
    _seed_role(url="https://www.indeed.com/viewjob?jk=5", stage="t2_done", stage_reason=None,
               description_md="Already described.")
    _seed_role(url="https://www.indeed.com/viewjob?jk=6", stage="t1_pass", stage_reason="other")
    with _client() as c:
        r = c.get("/api/extension/config", headers=_headers())
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["version"], str) and data["version"]
    assert set(data["features"]) == {"badges", "waiting", "toolbar_count"}
    assert all(isinstance(v, bool) for v in data["features"].values())
    hosts = {h["host"]: h["count"] for h in data["waiting_hosts"]}
    assert hosts == {"indeed.com": 2, "boards.example-ats.test": 1}


# ------------------------------------------------------------------ waiting list


def test_waiting_lists_only_waiting_roles_with_the_item_shape():
    _store_key()
    keep = _seed_role(title="Inventory Planner", org_name="Harbor Lane",
                      first_seen_at="2026-05-06T08:00:00.000000+00:00")
    _seed_role(title="Archived Planner", archived_at="2026-05-07T00:00:00Z")
    _seed_role(title="Scored Planner", stage="t2_done", stage_reason=None,
               description_md="Body.")
    _seed_role(title="New Planner", stage="new", stage_reason=None)
    with _client() as c:
        r = c.get("/api/extension/waiting", headers=_headers())
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    (item,) = data["items"]
    assert item["id"] == keep
    assert item["title"] == "Inventory Planner"
    assert item["company"] == "Harbor Lane"
    assert item["found"] == "2026-05-06"
    assert "source_label" in item
    assert isinstance(item["host_match"], bool)


def test_waiting_orders_host_matches_first_then_newest():
    _store_key()
    old_match = _seed_role(title="Old Match", url="https://www.indeed.com/viewjob?jk=1",
                           first_seen_at="2026-01-01T00:00:00.000000+00:00")
    new_other = _seed_role(title="New Other", url="https://jobs.elsewhere.test/r/2",
                           first_seen_at="2026-06-01T00:00:00.000000+00:00")
    sub_match = _seed_role(title="Sub Match", url="https://uk.indeed.com/viewjob?jk=3",
                           first_seen_at="2026-02-01T00:00:00.000000+00:00")
    mid_other = _seed_role(title="Mid Other", url="https://jobs.elsewhere.test/r/4",
                           first_seen_at="2026-04-01T00:00:00.000000+00:00")
    with _client() as c:
        r = c.get("/api/extension/waiting", params={"host": "www.indeed.com", "limit": 20},
                  headers=_headers())
    assert r.status_code == 200
    items = r.json()["items"]
    # One rule for "this site" (D-EXT-774): the exact host, www. aside. uk.indeed.com is another
    # site, so it sorts with the others, newest first.
    assert [i["id"] for i in items] == [old_match, new_other, mid_other, sub_match]
    assert [i["host_match"] for i in items] == [True, False, False, False]


def test_waiting_q_matches_title_or_company_and_total_ignores_limit():
    _store_key()
    for n in range(7):
        _seed_role(title=f"Route Planner {n}", org_name="Quiet Hills",
                   first_seen_at=f"2026-03-0{n + 1}T00:00:00.000000+00:00")
    _seed_role(title="Lab Technician", org_name="PLANNERS United")
    _seed_role(title="Lab Technician", org_name="Other Co")
    with _client() as c:
        r = c.get("/api/extension/waiting", params={"q": "planner"}, headers=_headers())
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 8
        assert len(data["items"]) == 5  # the default limit
        r = c.get("/api/extension/waiting", params={"q": "QUIET", "limit": 2},
                  headers=_headers())
        assert r.json()["total"] == 7 and len(r.json()["items"]) == 2
        r = c.get("/api/extension/waiting", params={"limit": 20}, headers=_headers())
        assert r.status_code == 200
        r = c.get("/api/extension/waiting", params={"limit": 21}, headers=_headers())
        assert r.status_code == 422


# ------------------------------------------------------------------ description: dry run


def test_dry_run_is_the_default_and_stores_nothing():
    _store_key()
    opp_id = _seed_role()
    tasks_before = _count(Task)
    with _client() as c:
        r = _describe(c, opp_id, LONG_TEXT, dry_run=None,
                      page_title="Senior Data Analyst | Indeed")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["words"], int) and data["words"] >= 120
    assert isinstance(data["first_lines"], str) and data["first_lines"]
    assert data["looks_like_check_page"] is False
    assert data["too_short"] is False
    assert data["title_match"] is True
    opp = _get_opp(opp_id)
    assert opp.description_md in (None, "")
    assert opp.stage == "t1_pass"
    assert _count(Task) == tasks_before


def test_dry_run_flags_a_short_text():
    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        r = _describe(c, opp_id, SHORT_TEXT, dry_run=True)
    assert r.status_code == 200 and r.json()["too_short"] is True
    assert _get_opp(opp_id).description_md in (None, "")


def test_title_match_and_miss():
    _store_key()
    opp_id = _seed_role(title="Senior Data Analyst")
    with _client() as c:
        hit = _describe(c, opp_id, LONG_TEXT, dry_run=True,
                        page_title="Senior Data Analyst | Indeed")
        miss = _describe(c, opp_id, LONG_TEXT, dry_run=True,
                         page_title="Warehouse Associate - Night Shift")
    assert hit.json()["title_match"] is True
    assert miss.json()["title_match"] is False


@pytest.mark.parametrize("phrase", CHECK_PHRASES)
def test_check_page_phrase_is_flagged_on_dry_run_and_refused_on_save(phrase):
    _store_key()
    opp_id = _seed_role()
    text = f"{LONG_TEXT} {phrase.upper()} {LONG_TEXT}"
    with _client() as c:
        dry = _describe(c, opp_id, text, dry_run=True)
        assert dry.status_code == 200
        assert dry.json()["looks_like_check_page"] is True
        save = _describe(c, opp_id, text, dry_run=False)
    assert save.status_code == 422
    assert save.json()["error"] == "check_page"
    assert isinstance(save.json()["message"], str) and save.json()["message"]
    opp = _get_opp(opp_id)
    assert opp.description_md in (None, "")
    assert opp.stage == "t1_pass"


# ------------------------------------------------------------------ description: save


def test_save_fills_the_waiting_role_and_queues_an_embed():
    _store_key()
    opp_id = _seed_role()
    roles_before = _count(Opportunity)
    with _client() as c:
        r = _describe(c, opp_id, f"\n\n  {LONG_TEXT}  \n", dry_run=False,
                      page_title="Senior Data Analyst | Indeed")
    assert r.status_code == 200
    assert r.json() == {"saved": True, "stage": "new"}
    opp = _get_opp(opp_id)
    assert opp.description_md == LONG_TEXT
    assert opp.stage == "new"
    assert _count(Opportunity) == roles_before

    async def embeds(s):
        rows = (await s.execute(select(Task).where(Task.type == "embed"))).scalars().all()
        return [t.payload for t in rows]

    assert {"opportunity_id": opp_id} in _with_session(embeds)


def test_save_to_one_waiting_role_leaves_the_other_waiting():
    _store_key()
    first = _seed_role(url="https://www.indeed.com/viewjob?jk=one")
    second = _seed_role(url="https://www.indeed.com/viewjob?jk=two", title="Junior Data Analyst")
    with _client() as c:
        r = _describe(c, first, LONG_TEXT, dry_run=False)
        assert r.status_code == 200
        waiting = c.get("/api/extension/waiting", headers=_headers()).json()
    other = _get_opp(second)
    assert other.description_md in (None, "")
    assert other.stage == "t1_pass" and other.stage_reason == EMPTY_POSTING_STAGE_REASON
    assert [i["id"] for i in waiting["items"]] == [second]


def test_save_refuses_a_short_text():
    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        r = _describe(c, opp_id, SHORT_TEXT, dry_run=False)
    assert r.status_code == 422 and r.json()["error"] == "too_short"
    assert _get_opp(opp_id).description_md in (None, "")


def test_unknown_role_is_404_and_creates_nothing():
    _store_key()
    _seed_role()
    roles_before = _count(Opportunity)
    with _client() as c:
        for dry in (True, False):
            r = _describe(c, 9999, LONG_TEXT, dry_run=dry)
            assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert _count(Opportunity) == roles_before


def test_a_role_that_already_has_a_description_is_never_replaced():
    _store_key()
    opp_id = _seed_role(stage="t2_done", stage_reason=None,
                        description_md="The original invented description.")
    with _client() as c:
        r = _describe(c, opp_id, LONG_TEXT, dry_run=False)
    assert r.status_code == 409 and r.json()["error"] == "has_description"
    opp = _get_opp(opp_id)
    assert opp.description_md == "The original invented description."
    assert opp.stage == "t2_done"


def test_an_archived_waiting_role_is_not_filled():
    _store_key()
    opp_id = _seed_role(archived_at="2026-03-05T00:00:00Z")
    with _client() as c:
        r = _describe(c, opp_id, LONG_TEXT, dry_run=False)
    assert r.status_code == 409 and r.json()["error"] == "has_description"
    assert _get_opp(opp_id).description_md in (None, "")


def test_extra_fields_are_ignored_and_never_stored():
    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        r = _describe(c, opp_id, LONG_TEXT, dry_run=False, title="Injected Title",
                      stage="t2_done", org_name="Injected Org", secret_marker="zz-marker-zz")
    assert r.status_code == 200
    opp = _get_opp(opp_id)
    assert opp.title == "Senior Data Analyst"
    assert opp.org_name == "Bluefin Works"
    assert opp.stage == "new"
    assert "zz-marker-zz" not in json.dumps(
        {"d": opp.description_md, "raw": opp.raw, "r": opp.stage_reason}, default=str
    )


@pytest.mark.parametrize("bad", [123, None, ["a list"], {"an": "object"}])
def test_a_non_string_text_is_400(bad):
    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        r = _describe(c, opp_id, bad, dry_run=False)
    assert r.status_code == 400
    assert isinstance(r.json().get("error"), str)
    assert _get_opp(opp_id).description_md in (None, "")


# ------------------------------------------------------------------ summary and documents


def test_summary_of_a_scored_role():
    _store_key()
    opp_id = _seed_role(stage="t2_done", stage_reason=None, description_md="Invented body.")

    async def add_eval(s):
        s.add(Evaluation(opportunity_id=opp_id, tier=2, track="employment", verdict="apply",
                         verdict_model="apply", score=4.1, weighted_score=4.0,
                         needs_review=False, dimensions={}, blocks={}, blockers=[],
                         legitimacy={"status": "clean", "signals": []}))
        await s.commit()

    _with_session(add_eval)
    with _client() as c:
        r = c.get(f"/api/extension/roles/{opp_id}/summary", headers=_headers())
    assert r.status_code == 200
    data = r.json()
    for key in ("title", "company", "place", "score", "verdict", "dimensions",
                "dimension_count", "pay", "level", "fits", "gaps", "documents", "applied_at"):
        assert key in data, key
    assert data["title"] == "Senior Data Analyst"
    assert data["company"] == "Bluefin Works"
    assert data["verdict"]  # the spec names the key, not its shape
    assert data["score"] is not None
    assert len(data["dimensions"]) <= 3
    for kind in ("resume", "cover_letter"):
        assert isinstance(data["documents"][kind]["available"], bool)
    assert data["applied_at"] is None


def test_summary_of_a_waiting_role_and_an_unknown_one():
    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        ok = c.get(f"/api/extension/roles/{opp_id}/summary", headers=_headers())
        missing = c.get("/api/extension/roles/9999/summary", headers=_headers())
    assert ok.status_code == 200 and ok.json()["title"] == "Senior Data Analyst"
    assert missing.status_code == 404


def test_documents_404_without_a_document_and_422_for_an_unknown_kind():
    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        for kind in ("resume.txt", "cover_letter.txt"):
            r = c.get(f"/api/extension/roles/{opp_id}/documents/{kind}", headers=_headers())
            assert r.status_code == 404, (kind, r.status_code)
        r = c.get(f"/api/extension/roles/{opp_id}/documents/portfolio.txt", headers=_headers())
        assert r.status_code == 422


# ------------------------------------------------------------------ applied


def test_applied_is_recorded_once():
    _store_key()
    opp_id = _seed_role(stage="t2_done", stage_reason=None, description_md="Invented body.")
    with _client() as c:
        first = c.post(f"/api/extension/roles/{opp_id}/applied", headers=_headers())
        second = c.post(f"/api/extension/roles/{opp_id}/applied", headers=_headers())
    assert first.status_code == 200
    one = first.json()
    assert one["changed"] is True
    assert isinstance(one["applied_at"], str) and one["applied_at"]
    assert "stage" in one
    two = second.json()
    assert second.status_code == 200
    assert two["changed"] is False
    assert two["applied_at"] == one["applied_at"]

    async def state(s):
        p = (await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
             ).scalar_one()
        events = (await s.execute(
            select(StatusEvent).where(StatusEvent.pursuit_id == p.id,
                                      StatusEvent.to_status == "applied")
        )).scalars().all()
        return p.applied_at, len(events)

    applied_at, n_events = _with_session(state)
    assert applied_at
    assert n_events == 1


def test_applied_unknown_role_is_404():
    _store_key()
    with _client() as c:
        r = c.post("/api/extension/roles/9999/applied", headers=_headers())
    assert r.status_code == 404
    assert _count(Pursuit) == 0


# ------------------------------------------------------------------ features


def test_features_default_true_update_and_reflect_in_config():
    _store_key()
    with _client() as c:
        before = c.get("/api/extension/config", headers=_headers()).json()["features"]
        assert before == {"badges": True, "waiting": True, "toolbar_count": True}
        r = c.put("/api/extension/features", json={"badges": False}, headers=_headers())
        assert r.status_code == 200
        assert r.json() == {"features": {"badges": False, "waiting": True,
                                         "toolbar_count": True}}
        after = c.get("/api/extension/config", headers=_headers()).json()["features"]
    assert after == {"badges": False, "waiting": True, "toolbar_count": True}
    assert _setting("extension.badges") is False


@pytest.mark.parametrize("body", [{"sparkles": True}, {"badges": "yes"}, {"waiting": 1},
                                  {"toolbar_count": None}])
def test_features_refuse_unknown_fields_and_non_booleans(body):
    _store_key()
    with _client() as c:
        r = c.put("/api/extension/features", json=body, headers=_headers())
        assert r.status_code == 400
        features = c.get("/api/extension/config", headers=_headers()).json()["features"]
    assert features == {"badges": True, "waiting": True, "toolbar_count": True}
    for name in ("badges", "waiting", "toolbar_count"):
        assert _setting(f"extension.{name}") in (None, True)


# ------------------------------------------------------------------ last seen


_UAS = {
    "Edge": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Firefox": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Chrome": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36",
    "Safari": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like "
              "Gecko) Version/17.0 Safari/605.1.15",
    "Other": "InventedAgent/1.0",
}


@pytest.mark.parametrize("family", list(_UAS))
def test_last_seen_records_the_browser_family_not_the_user_agent(family):
    from kestrel.services.extension import reset_seen_throttle

    reset_seen_throttle()
    _store_key()
    with _client() as c:
        r = c.get("/api/extension/config", headers=_headers(**{"User-Agent": _UAS[family]}))
    assert r.status_code == 200
    seen = _setting("extension.last_seen")
    assert isinstance(seen, dict)
    assert seen["browser"] == family
    assert isinstance(seen["at"], str) and seen["at"]
    assert _UAS[family] not in json.dumps(seen)


# ------------------------------------------------------------------ changed existing routes


def test_lookup_reports_liveness_waiting_and_applied():
    _store_key()
    closed = _seed_role(url="https://jobs.example.test/closed", stage="t2_done",
                        stage_reason=None, description_md="Body.", liveness="closed")
    waiting = _seed_role(url="https://jobs.example.test/waiting")

    async def apply(s):
        s.add(Pursuit(opportunity_id=closed, track="employment", status="applied",
                      applied_at="2026-04-02T09:00:00.000000+00:00"))
        await s.commit()

    _with_session(apply)
    with _client() as c:
        r = c.post("/api/opportunities/lookup",
                   json={"urls": ["https://jobs.example.test/closed",
                                  "https://jobs.example.test/waiting"]},
                   headers=_headers())
    assert r.status_code == 200
    data = r.json()
    a, b = data["https://jobs.example.test/closed"], data["https://jobs.example.test/waiting"]
    assert a["id"] == closed and b["id"] == waiting
    assert a["liveness"] == "closed"
    assert a["needs_description"] is False and b["needs_description"] is True
    assert a["applied_at"] == "2026-04-02T09:00:00.000000+00:00"
    assert b["applied_at"] is None


def _embed_priority_after_ingest(body: dict) -> int:
    _store_key()
    with _client() as c:
        r = c.post("/api/opportunities/ingest", json=body, headers=_headers())
    assert r.status_code == 200, r.text
    opp_id = r.json()["id"]

    async def prios(s):
        rows = (await s.execute(select(Task).where(Task.type == "embed"))).scalars().all()
        return [t.priority for t in rows if (t.payload or {}).get("opportunity_id") == opp_id]

    found = _with_session(prios)
    assert len(found) == 1, found
    return found[0]


def test_ingest_score_now_raises_the_embed_priority():
    body = {"url": "https://jobs.example.test/fresh-1",
            "text": "Logistics Coordinator\n" + LONG_TEXT, "score_now": True}
    assert _embed_priority_after_ingest(body) > 0


def test_ingest_without_score_now_keeps_priority_zero():
    body = {"url": "https://jobs.example.test/fresh-2",
            "text": "Logistics Coordinator\n" + LONG_TEXT}
    assert _embed_priority_after_ingest(body) == 0


# ------------------------------------------------------------------ where a captured role came from


def _facts_after_capture(body: dict) -> list[tuple[str, str]]:
    import re

    _store_key()
    with _client() as c:
        r = c.post("/api/opportunities/ingest", json=body, headers=_headers())
        assert r.status_code == 200, r.text
        page = c.get(f"/opportunities/{r.json()['id']}")
    assert page.status_code == 200
    block = re.search(r'<div class="rl-facts".*?</div>', page.text, re.S).group(0)
    block = re.sub(r"<svg.*?</svg>", "", block, flags=re.S)
    return re.findall(r'data-kind="(\w+)">([^<]*)<', block)


def test_an_extension_capture_names_its_origin_in_the_role_header():
    facts = dict(_facts_after_capture({
        "url": "https://www.jobs.example.test/post/9",
        "text": "Logistics Coordinator\n" + LONG_TEXT, "title": "Logistics Coordinator"}))
    assert facts["source"] == "From the browser extension (jobs.example.test)"
    assert facts["org"] == "Unknown company"


def test_an_extension_capture_without_a_link_still_says_it_came_from_the_extension():
    facts = dict(_facts_after_capture({"text": "Logistics Coordinator\n" + LONG_TEXT}))
    assert facts["source"] == "From the browser extension"


def test_a_role_from_another_path_has_no_extension_label():
    _store_key()
    rid = _seed_role()
    with _client() as c:
        page = c.get(f"/opportunities/{rid}")
    assert page.status_code == 200
    assert "From the browser extension" not in page.text


def _facts_of(opp_id: int) -> dict[str, str]:
    import re

    with _client() as c:
        page = c.get(f"/opportunities/{opp_id}")
    assert page.status_code == 200
    block = re.search(r'<div class="rl-facts".*?</div>', page.text, re.S).group(0)
    block = re.sub(r"<svg.*?</svg>", "", block, flags=re.S)
    return dict(re.findall(r'data-kind="(\w+)">([^<]*)<', block))


_NO_EXT_LINE = "From the browser extension"


def test_a_plain_api_ingest_without_the_key_or_an_extension_origin_has_no_extension_line():
    with _client() as c:
        r = c.post("/api/opportunities/ingest", json={
            "url": "https://jobs.example.test/api-1",
            "text": "Logistics Coordinator\n" + LONG_TEXT})
    assert r.status_code == 200, r.text
    assert "source" not in _facts_of(r.json()["id"])
    assert _get_opp(r.json()["id"]).raw in (None, {})


def test_an_mcp_ingest_has_no_extension_line():
    from fastmcp import Client

    from kestrel.mcp.server import build_mcp

    async def go():
        async with Client(build_mcp()) as c:
            return (await c.call_tool("ingest_opportunity", {
                "url": "https://jobs.example.test/mcp-1",
                "text": "Logistics Coordinator\n" + LONG_TEXT})).data

    out = asyncio.run(go())
    assert "source" not in _facts_of(out["id"])
    assert _get_opp(out["id"]).raw in (None, {})


def test_a_role_added_by_hand_in_the_app_has_no_extension_line():
    from kestrel.services.applied_import import add_found_role

    async def go(s):
        res = await add_found_role(
            s, track="employment", url="https://jobs.example.test/hand-1",
            text="Logistics Coordinator\n" + LONG_TEXT, title=None, org_name=None)
        await s.commit()
        return res

    res = _with_session(go)
    assert res.ok, res
    assert "source" not in _facts_of(res.opportunity_id)
    assert _get_opp(res.opportunity_id).raw in (None, {})


def test_confidential_mode_drops_the_host_from_the_extension_line():
    async def on(s):
        s.add(Setting(key="confidential_mode", value=True))
        await s.commit()

    _with_session(on)
    facts = dict(_facts_after_capture({
        "url": "https://careers.employer-example.test/post/9",
        "text": "Logistics Coordinator\n" + LONG_TEXT}))
    assert facts["source"] == "From the browser extension"
    assert "employer-example" not in facts["source"]


def _label_after_ingest(headers: dict[str, str]) -> tuple[int, object]:
    _store_key()
    with _client() as c:
        r = c.post("/api/opportunities/ingest", headers=headers, json={
            "url": "https://jobs.example.test/auth-1",
            "text": "Logistics Coordinator\n" + LONG_TEXT})
    if r.status_code != 200:
        return r.status_code, None
    return r.status_code, (_get_opp(r.json()["id"]).raw or {}).get("captured_via")


def test_an_extension_origin_without_the_key_does_not_label_a_capture():
    status, label = _label_after_ingest({"Origin": EXT_ORIGIN})
    assert label is None, (status, label)


def test_a_wrong_key_does_not_label_a_capture():
    status, label = _label_after_ingest({"X-Kestrel-Key": "not-the-key"})
    assert label is None, (status, label)


def test_the_right_key_labels_a_capture_as_the_extension():
    status, label = _label_after_ingest({"X-Kestrel-Key": KEY})
    assert (status, label) == (200, "extension")
