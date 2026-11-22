"""The extension's document routes and its I've applied edges (issue #186, D-EXT-748, D-EXT-749).

Invariant: Copy text returns the person's own saved text and nothing else: the resume's lines
come from their catalog unchanged (hard rule 2) and the letter is the saved letter body. Documents
are listed only for the role they belong to.
"""

from __future__ import annotations

from types import SimpleNamespace

from kestrel.db.models import DocMaster, DocRender, Letter
from kestrel.web.pages.extension_api import resume_text
from tests.test_extension_api import (  # noqa: F401 - _fresh_db is an autouse fixture
    _client,
    _fresh_db,
    _headers,
    _seed_role,
    _store_key,
    _with_session,
)

LETTER = "Dear hiring team,\n\nI plan delivery routes for a regional depot every week."


def _seed_documents(opp_id: int) -> tuple[int, int]:
    async def go(s):
        master = DocMaster(
            track="employment",
            name="Main",
            doc_kind="resume",
            header={"name": "Sam Rivera", "headline": "Route planner"},
        )
        s.add(master)
        await s.flush()
        resume = DocRender(
            opportunity_id=opp_id,
            doc_master_id=master.id,
            template="classic",
            config={},
            created_at="2026-11-10T09:00:00+00:00",
        )
        letter_render = DocRender(
            opportunity_id=opp_id,
            doc_master_id=master.id,
            template="cover_letter",
            config={},
            created_at="2026-11-10T10:00:00+00:00",
        )
        s.add_all([resume, letter_render])
        await s.flush()
        s.add(
            Letter(
                render_id=letter_render.id,
                opportunity_id=opp_id,
                kind="cover_letter",
                body_md=LETTER,
            )
        )
        await s.commit()
        return resume.id, letter_render.id

    return _with_session(go)


def test_summary_lists_the_roles_documents_and_copy_text_returns_them():
    _store_key()
    opp_id = _seed_role(stage="t2_done", stage_reason=None, description_md="Plans routes.")
    other = _seed_role(title="Other role", url="https://example.org/other")
    resume_id, letter_id = _seed_documents(opp_id)
    with _client() as c:
        docs = c.get(f"/api/extension/roles/{opp_id}/summary", headers=_headers()).json()
        docs = docs["documents"]
        assert docs["resume"] == {
            "available": True,
            "updated": "2026-11-10",
            "render_id": resume_id,
            "pdf_path": f"/renders/{resume_id}/download",
        }
        assert docs["cover_letter"]["render_id"] == letter_id
        assert docs["cover_letter"]["pdf_path"] == (
            f"/opportunities/{opp_id}/role/cover-letter/{letter_id}/download"
        )
        r = c.get(f"/api/extension/roles/{opp_id}/documents/cover_letter.txt", headers=_headers())
        assert r.status_code == 200 and r.text == LETTER
        assert r.headers["cache-control"] == "no-store"
        r = c.get(f"/api/extension/roles/{opp_id}/documents/resume.txt", headers=_headers())
        assert r.status_code == 200 and r.text.startswith("Sam Rivera\nRoute planner")
        # Another role sees none of them.
        r = c.get(f"/api/extension/roles/{other}/documents/cover_letter.txt", headers=_headers())
        assert r.status_code == 404


def test_resume_text_keeps_every_line_as_written():
    bullet = SimpleNamespace(text="Cut late deliveries by a fifth, across 3 depots.")
    role = SimpleNamespace(
        title="Route Planner", org="Northwind", dates="2021 to 2026", bullets=[bullet]
    )
    doc = SimpleNamespace(
        header={"name": "Sam Rivera", "headline": ""},
        blocks=[
            SimpleNamespace(
                heading="Summary", text="Plans routes.", roles=[], skills=[], entries=[]
            ),
            SimpleNamespace(heading="Experience", text=None, roles=[role], skills=[], entries=[]),
            SimpleNamespace(
                heading="Skills",
                text=None,
                roles=[],
                skills=[
                    {"name": "Tools", "items": ["Route maps", "Spreadsheets"]},
                    {"items": ["Scheduling"]},
                    "not a group",
                ],
                entries=[],
            ),
            SimpleNamespace(
                heading="Education",
                text=None,
                roles=[],
                skills=[],
                entries=[
                    "Certificate in Logistics",
                    {"title": "Forklift course", "year": "2024", "n": 3},
                ],
            ),
        ],
    )
    assert resume_text(doc) == (
        "Sam Rivera\n\nSummary\nPlans routes.\n\nExperience\n"
        "Route Planner · Northwind · 2021 to 2026\n"
        "- Cut late deliveries by a fifth, across 3 depots.\n\nSkills\n"
        "Tools: Route maps, Spreadsheets\nScheduling\n\nEducation\n"
        "Certificate in Logistics\nForklift course · 2024\n"
    )


def test_applied_refuses_a_hidden_role_and_changes_nothing():
    _store_key()
    opp_id = _seed_role(
        stage="t2_done",
        stage_reason=None,
        description_md="Plans routes.",
        archived_at="2026-11-01T00:00:00+00:00",
    )
    with _client() as c:
        r = c.post(f"/api/extension/roles/{opp_id}/applied", headers=_headers())
        assert r.status_code == 409 and r.json()["error"] == "hidden"

    async def go(s):
        from sqlalchemy import func, select

        from kestrel.db.models import Pursuit

        return (await s.execute(select(func.count(Pursuit.id)))).scalar_one()

    assert _with_session(go) == 0


def test_summary_of_a_waiting_role_says_so():
    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        body = c.get(f"/api/extension/roles/{opp_id}/summary", headers=_headers()).json()
    assert body["waiting"] is True and body["score"] is None
    assert body["documents"]["resume"]["available"] is False


# ------------------------------------------------------------------ review round 1 (D-EXT-768)
EMPLOYER = "Harborline Logistics"
PLACEHOLDER = "A regional logistics firm"


def _confidential(on: bool) -> None:
    from kestrel.db.models import DocSection, Setting

    async def go(s):
        master = DocMaster(track="employment", name="Resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label=EMPLOYER, org_label_redacted=PLACEHOLDER))
        if on:
            s.add(Setting(key="confidential_mode", value=True))
        await s.commit()

    _with_session(go)


def _letter_for(opp_id: int, body: str) -> None:
    async def go(s):
        master = DocMaster(track="employment", name="Letters", doc_kind="resume")
        s.add(master)
        await s.flush()
        render = DocRender(opportunity_id=opp_id, doc_master_id=master.id,
                           template="cover_letter", config={})
        s.add(render)
        await s.flush()
        s.add(Letter(render_id=render.id, opportunity_id=opp_id, kind="cover_letter",
                     body_md=body))
        await s.commit()

    _with_session(go)


def test_confidential_mode_redacts_copy_text_and_the_waiting_list():
    _store_key()
    _confidential(True)
    opp_id = _seed_role(stage="t2_done", stage_reason=None, description_md="Plans routes.")
    waiting = _seed_role(title=f"Planner at {EMPLOYER}", org_name=EMPLOYER,
                         url="https://example.org/w1")
    _letter_for(opp_id, f"I plan routes at {EMPLOYER} today.")
    with _client() as c:
        r = c.get(f"/api/extension/roles/{opp_id}/documents/cover_letter.txt", headers=_headers())
        assert r.status_code == 200 and EMPLOYER not in r.text and PLACEHOLDER in r.text
        items = c.get("/api/extension/waiting?limit=20", headers=_headers()).json()["items"]
        row = next(it for it in items if it["id"] == waiting)
        assert EMPLOYER not in row["title"] and EMPLOYER not in row["company"]
        # The search matches only what the list shows.
        hits = c.get("/api/extension/waiting?q=Harborline", headers=_headers()).json()
        assert hits["total"] == 0


def test_confidential_mode_off_copies_the_real_text():
    _store_key()
    _confidential(False)
    opp_id = _seed_role(stage="t2_done", stage_reason=None, description_md="Plans routes.")
    _letter_for(opp_id, f"I plan routes at {EMPLOYER} today.")
    with _client() as c:
        r = c.get(f"/api/extension/roles/{opp_id}/documents/cover_letter.txt", headers=_headers())
    assert r.text == f"I plan routes at {EMPLOYER} today."


def test_a_resume_with_an_unresolved_pick_is_not_copied():
    _store_key()
    opp_id = _seed_role(stage="t2_done", stage_reason=None, description_md="Plans routes.")

    async def go(s):
        from kestrel.db.models import DocSection, DocSlot, DocVariant

        master = DocMaster(track="employment", name="Main", doc_kind="resume")
        s.add(master)
        await s.flush()
        sec = DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label="Northwind")
        s.add(sec)
        await s.flush()
        slot = DocSlot(doc_section_id=sec.id, key="b1", sort_order=0, optional=False)
        s.add(slot)
        await s.flush()
        s.add(DocVariant(doc_slot_id=slot.id, key="v1", text="Planned routes."))
        s.add(DocRender(opportunity_id=opp_id, doc_master_id=master.id, template="classic",
                        config={"slots": {"b1": "invented-missing-key"},
                                "summary": "invented-missing-summary"}))
        await s.commit()

    _with_session(go)
    with _client() as c:
        r = c.get(f"/api/extension/roles/{opp_id}/documents/resume.txt", headers=_headers())
    assert r.status_code == 409 and "Planned routes." not in r.text


def test_a_capability_statement_is_not_offered_as_the_resume():
    _store_key()
    opp_id = _seed_role(stage="t2_done", stage_reason=None, description_md="Plans routes.")

    async def go(s):
        master = DocMaster(track="consulting", name="Cap", doc_kind="capability")
        s.add(master)
        await s.flush()
        s.add(DocRender(opportunity_id=opp_id, doc_master_id=master.id, template="classic",
                        config={}))
        await s.commit()

    _with_session(go)
    with _client() as c:
        body = c.get(f"/api/extension/roles/{opp_id}/summary", headers=_headers()).json()
    assert body["documents"]["resume"]["available"] is False


def test_a_long_posting_that_names_a_captcha_is_saved():
    from tests.test_extension_api import LONG_TEXT

    _store_key()
    opp_id = _seed_role()
    text = " ".join([LONG_TEXT] * 3) + " This site is protected by reCAPTCHA."
    with _client() as c:
        r = c.post(f"/api/extension/roles/{opp_id}/description", headers=_headers(),
                   json={"text": text, "page_title": "Senior Data Analyst", "dry_run": False})
    assert r.status_code == 200 and r.json()["saved"] is True


def test_linkedin_is_never_read_or_captured_from_the_extension():
    from tests.test_extension_api import EXT_ORIGIN, LONG_TEXT

    _store_key()
    opp_id = _seed_role()
    with _client() as c:
        r = c.post(f"/api/extension/roles/{opp_id}/description", headers=_headers(),
                   json={"text": LONG_TEXT, "page_url": "https://www.linkedin.com/jobs/view/1",
                         "dry_run": False})
        assert r.status_code == 422 and r.json()["error"] == "linkedin"
        r = c.post("/api/opportunities/ingest", headers=_headers(origin=EXT_ORIGIN),
                   json={"url": "https://www.linkedin.com/jobs/view/2", "text": LONG_TEXT})
        assert r.status_code == 422
        # The same refusal when a browser sends no extension Origin: the key header alone
        # marks the extension (review round 3, D-EXT-772).
        r = c.post("/api/opportunities/ingest", headers=_headers(),
                   json={"url": "https://www.linkedin.com/jobs/view/3", "text": LONG_TEXT})
        assert r.status_code == 422 and r.json()["error"] == "linkedin"


def test_every_extension_route_is_in_the_key_test():
    """A new route under /api/extension/ must be added to the key test's list (§8a.6)."""
    from kestrel.web.pages.extension_api import router
    from tests.test_extension_api import _route_calls

    listed = {(m, p.replace("/1/", "/{opp_id}/")) for m, p, _b in _route_calls(1)}
    listed |= {("GET", "/api/extension/roles/{opp_id}/documents/{kind}.txt")}
    for route in router.routes:
        for method in route.methods:
            assert (method, route.path) in listed, (method, route.path)


def test_a_resume_waiting_for_pick_again_is_not_copied():
    """Pick again's needs_pick blocks Copy text exactly as it blocks the PDF (review round 2)."""
    _store_key()
    opp_id = _seed_role(stage="t2_done", stage_reason=None, description_md="Plans routes.")

    async def go(s):
        master = DocMaster(track="employment", name="Main", doc_kind="resume",
                           header={"name": "Sam Rivera"})
        s.add(master)
        await s.flush()
        s.add(DocRender(opportunity_id=opp_id, doc_master_id=master.id, template="classic",
                        config={"needs_pick": {"b1": "invented-model-key"}}))
        await s.commit()

    _with_session(go)
    with _client() as c:
        r = c.get(f"/api/extension/roles/{opp_id}/documents/resume.txt", headers=_headers())
    assert r.status_code == 409


def test_linkedin_hosts_with_a_trailing_dot_or_other_domains_are_refused():
    from kestrel.services.extension import is_linkedin

    for url in ("https://www.linkedin.com./jobs/view/1", "https://uk.linkedin.com/jobs/1",
                "https://www.linkedin.cn/jobs/1", "https://lnkd.in/abc"):
        assert is_linkedin(url), url
    for url in ("https://linkedinjobs.example.org/1", "https://example.org/linkedin.com"):
        assert not is_linkedin(url), url


def test_confidential_mode_keeps_the_employer_out_of_the_role_summary():
    """The popup's role summary (title, company, fits, gaps, pay, level) never names the current
    employer under confidential mode (review round 3)."""
    import json

    from kestrel.db.models import Evaluation

    _store_key()
    _confidential(True)
    opp_id = _seed_role(title=f"Route Planner, {EMPLOYER}", org_name=EMPLOYER, stage="t2_done",
                        stage_reason=None, description_md=f"Join {EMPLOYER} as a planner.")

    async def go(s):
        s.add(Evaluation(
            opportunity_id=opp_id, tier=2, track="employment", verdict="apply",
            verdict_model="apply", score=4.1, weighted_score=4.0, needs_review=False,
            dimensions={"role_fit": {"score": 4.2}},
            blocks={"b_fit": {
                "matches": [{"requirement": f"Planned routes at {EMPLOYER}",
                             "strength": "strong"}],
                "gaps": [{"requirement": f"Knows {EMPLOYER} systems", "severity": "minor",
                          "mitigation": f"Mention your {EMPLOYER} tools."}]},
                "c_level": {"relative_level": "at", "scope_read": f"Same scope as {EMPLOYER}"}},
            blockers=[], legitimacy={"status": "clean", "signals": []}))
        await s.commit()

    _with_session(go)
    with _client() as c:
        r = c.get(f"/api/extension/roles/{opp_id}/summary", headers=_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["fits"] and body["gaps"], body  # the parts that could carry the name are there
    assert EMPLOYER not in json.dumps(body), body


# ------------------------------------------------------------------ review round 5 (D-EXT-777)
def test_capture_body_is_an_allowlist_with_cut_lengths():
    """Invariant: a captured page is imported through the allowlisted body only; text is cut to
    40,000 characters, and a field of the wrong type is refused, never a server error."""
    from sqlalchemy import select

    from kestrel.db.models import Opportunity

    _store_key()
    big = ("Plan routes for the depot every week. " * 40000)[:1_500_000]
    with _client() as c:
        r = c.post("/api/opportunities/ingest", headers=_headers(),
                   json={"url": "https://jobs.example.org/big", "text": big,
                         "title": "Route Planner", "extra_field": "ignored"})
        assert r.status_code == 200
        opp_id = r.json()["id"]
        for bad in ({"url": ["https://jobs.example.org/a"], "text": "Planner"},
                    {"url": "https://jobs.example.org/b", "text": "Planner", "title": {"a": 1}},
                    {"text": 12345},
                    {"text": "Planner", "track": "invented-track"},
                    {"text": "Planner", "score_now": "yes"},
                    ["not", "an", "object"]):
            r = c.post("/api/opportunities/ingest", headers=_headers(), json=bad)
            assert r.status_code == 400, (bad, r.status_code, r.text[:200])

    async def go(s):
        return (await s.execute(select(Opportunity.description_md)
                                .where(Opportunity.id == opp_id))).scalar_one()

    stored = _with_session(go)
    assert stored is not None and len(stored) <= 40000


# ------------------------------------------------------------------ merge-gate round (D-EXT-777)
def test_every_ingest_door_cuts_text_and_names():
    """The cut lives in manual_ingest, so the MCP ingest tool (which skips the HTTP route) is held
    to the same limits as the extension's Capture."""
    import asyncio

    from fastmcp import Client
    from sqlalchemy import select

    from kestrel.db.models import Opportunity
    from kestrel.ingest.manual import NAME_MAX, TEXT_MAX
    from kestrel.mcp.server import build_mcp

    async def run():
        async with Client(build_mcp()) as c:
            return (await c.call_tool("ingest_opportunity", {
                "url": "https://jobs.example.org/mcp-big",
                "text": "Plan depot routes every week. " * 40000,
                "title": "Route Planner " * 400,
                "org_name": "Northwind " * 100,
            })).data

    out = asyncio.run(run())

    async def go(s):
        query = select(Opportunity).where(Opportunity.id == out["id"])
        return (await s.execute(query)).scalar_one()

    opp = _with_session(go)
    assert len(opp.description_md) <= TEXT_MAX
    assert len(opp.title) <= NAME_MAX and len(opp.org_name or "") <= NAME_MAX


def test_lookup_cuts_each_url():
    """A board's lookup sends at most 100 addresses, each cut to the URL limit."""
    from kestrel.ingest.manual import URL_MAX

    _store_key()
    long_url = "https://jobs.example.org/" + "a" * 10000
    opp_id = _seed_role(url=long_url[:URL_MAX], stage="t2_done", stage_reason=None,
                        description_md="Plans routes.")
    with _client() as c:
        r = c.post("/api/opportunities/lookup", headers=_headers(), json={"urls": [long_url]})
    assert r.status_code == 200
    assert [v["id"] for v in r.json().values()] == [opp_id]
