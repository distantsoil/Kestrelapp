"""Find a role by its ID in the command palette and the Roles list filter (issue 213, D-UI-770).

One parser serves both. Every spelling, a miss falling back to the ordinary title search, a
duplicate opening its main copy, a hidden role, a missing ID, and confidential redaction."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import DocMaster, DocSection, Opportunity, Org, Setting, Source
from kestrel.services.role_id_query import parse_role_id
from kestrel.web.queries import OppFilters, list_opportunities

_EMPLOYER = "Quillfeather Haulage"


def _fresh() -> None:
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


async def _seed(*, confidential: bool = False) -> dict[str, int]:
    """Ordinary role, a duplicate pair, a hidden role, a role of a hidden company, and a role at
    the operator's current employer."""
    engine = make_async_engine(paths.db_path())
    async with async_sessionmaker(engine)() as s:
        src = Source(track="employment", kind="greenhouse", name="b", enabled=True)
        plain = Org(name="Marlowe Ironworks", normalized_name="marlowe ironworks")
        shut = Org(name="Dunmore Closed", normalized_name="dunmore closed", hidden_at=utcnow_iso())
        boss = Org(name=_EMPLOYER, normalized_name=_EMPLOYER.lower())
        s.add_all([src, plain, shut, boss])
        await s.flush()

        def opp(org, title, url, **kw):
            o = Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                            source_id=src.id, title=title, org_name=org.name, url=url, **kw)
            s.add(o)
            return o

        active = opp(plain, "Quarry Safety Planner", "https://x/1")
        main = opp(plain, "Kiln Scheduler", "https://x/2", dedup_group_id="g1")
        dup = opp(plain, "Kiln Scheduler", "https://x/3", dedup_group_id="g1", is_canonical=False)
        orphan = opp(plain, "Lone Copy", "https://x/4", is_canonical=False)
        hidden = opp(plain, "Rivet Counter", "https://x/5", archived_at=utcnow_iso(),
                     archive_reason="later")
        closed_co = opp(shut, "Furnace Tender", "https://x/6")
        mine = opp(boss, f"Dispatcher at {_EMPLOYER}", "https://x/7")
        tagged = opp(plain, "Analyst #9999", "https://x/8")
        await s.flush()
        if confidential:
            master = DocMaster(track="employment", name="Resume", doc_kind="resume")
            s.add(master)
            await s.flush()
            s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                             org_label=_EMPLOYER, org_label_redacted="[Confidential]"))
            s.add(Setting(key="confidential_mode", value=True))
        ids = {"active": active.id, "main": main.id, "dup": dup.id, "orphan": orphan.id,
               "hidden": hidden.id, "closed_co": closed_co.id, "mine": mine.id,
               "tagged": tagged.id}
        await s.commit()
    await engine.dispose()
    forget_engine()
    return ids


def _palette(q: str) -> dict:
    with _client() as c:
        return c.get("/palette/search", params={"q": q}).json()


def _list(q: str, **kw) -> list[int]:
    async def run():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as s:
            page = await list_opportunities(s, OppFilters(q=q, id_lookup=True, **kw))
        await engine.dispose()
        forget_engine()
        return [r.opp.id for r in page.rows]

    return asyncio.run(run())


# ------------------------------------------------------------------------------ the parser


@pytest.mark.parametrize("q", [":jobid-434", ":JobID-434", "  :jobid-434  ", "#434", "#434 ",
                               "role id 434", "Role ID 434", "  ROLE   ID  434 "])
def test_every_spelling_parses_to_the_id(q):
    assert parse_role_id(q) == 434


@pytest.mark.parametrize("q", ["", None, "434", "#", "#abc", "# 434", ":jobid-", ":jobid 434",
                               "jobid-434", "role 434", "role id: 434", "role id 434 analyst",
                               "#434x", "id 434", "#\u0664\u0663\u0664", "#\uff14\uff13\uff14",
                               "role id \u0664", "#-5", "#1.5", "role id 12345678901"])
def test_anything_else_is_not_an_id_query(q):
    assert parse_role_id(q) is None


# ------------------------------------------------------------------------------ the palette


def test_palette_finds_a_role_by_each_syntax_and_skips_the_two_character_minimum():
    _fresh()
    ids = asyncio.run(_seed())
    n = ids["active"]
    for q in (f":jobid-{n}", f":JOBID-{n} ", f"#{n}", f"role id {n}", f"Role ID {n}"):
        body = _palette(q)
        assert [r["href"] for r in body["roles"]] == [f"/opportunities/{n}"], q
        assert body["companies"] == []
        assert f"Role {n}" in body["roles"][0]["s"]
    # Two characters is the shortest ID query ("#7"); an ordinary one-character query is empty.
    assert [r["href"] for r in _palette("#1")["roles"]] == ["/opportunities/1"]
    assert _palette("q") == {"roles": [], "companies": []}


def test_palette_title_search_is_unchanged_for_ordinary_queries():
    _fresh()
    ids = asyncio.run(_seed())
    body = _palette("kiln")
    assert [r["href"] for r in body["roles"]] == [f"/opportunities/{ids['main']}"]
    assert body["roles"][0]["s"] == "Marlowe Ironworks"  # no Role, Hidden or note text added
    assert [c["t"] for c in _palette("marlowe")["companies"]] == ["Marlowe Ironworks"]
    # A number alone is title text, not an ID query.
    assert _palette(str(ids["active"])) == {"roles": [], "companies": []}
    assert _palette("role id") == {"roles": [], "companies": []}


def test_palette_id_with_no_role_falls_back_to_title_search():
    _fresh()
    asyncio.run(_seed())
    assert _palette("#99999") == {"roles": [], "companies": []}
    assert _palette(":jobid-0") == {"roles": [], "companies": []}


def test_palette_duplicate_opens_the_main_copy_with_the_note():
    _fresh()
    ids = asyncio.run(_seed())
    body = _palette(f"#{ids['dup']}")
    assert [r["href"] for r in body["roles"]] == [f"/opportunities/{ids['main']}"]
    assert f"Shown as role {ids['main']}, the main copy." in body["roles"][0]["s"]
    # The main copy itself carries no such note.
    own = _palette(f"#{ids['main']}")["roles"][0]["s"]
    assert "main copy" not in own
    # A copy with no main copy to open is shown as itself.
    assert [r["href"] for r in _palette(f"#{ids['orphan']}")["roles"]] == [
        f"/opportunities/{ids['orphan']}"]


def test_palette_hidden_roles_are_found_and_labeled():
    _fresh()
    ids = asyncio.run(_seed())
    hidden = _palette(f"#{ids['hidden']}")["roles"]
    assert [r["href"] for r in hidden] == [f"/opportunities/{ids['hidden']}"]
    assert "Hidden" in hidden[0]["s"].split(" · ")
    # An ordinary title search is unchanged: no label or ID text is added to its rows.
    assert [r["s"] for r in _palette("rivet")["roles"]] == ["Marlowe Ironworks"]
    closed = _palette(f"#{ids['closed_co']}")["roles"]
    assert [r["href"] for r in closed] == [f"/opportunities/{ids['closed_co']}"]
    assert "Hidden company" in closed[0]["s"]
    assert _palette("furnace")["roles"] == []  # a hidden company stays out of title search


def test_palette_confidential_mode_redacts_the_employer_on_id_and_title_results():
    _fresh()
    ids = asyncio.run(_seed(confidential=True))
    by_id = _palette(f"#{ids['mine']}")["roles"]
    by_title = _palette("dispatcher")["roles"]
    assert by_id and by_title
    assert _EMPLOYER not in str(by_id) and _EMPLOYER not in str(by_title)
    assert "[Confidential]" in by_id[0]["t"] + by_id[0]["s"]


def test_palette_without_confidential_mode_shows_the_real_employer():
    _fresh()
    ids = asyncio.run(_seed(confidential=False))
    assert _EMPLOYER in str(_palette(f"#{ids['mine']}")["roles"])


# ------------------------------------------------------------------------------ the Roles list


def test_list_finds_a_role_by_each_syntax():
    _fresh()
    ids = asyncio.run(_seed())
    n = ids["active"]
    for q in (f":jobid-{n}", f"#{n}", f"role id {n}", f"  ROLE ID {n}  "):
        assert _list(q) == [n], q


def test_list_title_filter_is_unchanged_for_ordinary_queries():
    _fresh()
    ids = asyncio.run(_seed())
    assert _list("kiln") == [ids["main"]]
    assert _list("marlowe") != []
    assert ids["hidden"] not in _list("marlowe")  # hidden roles stay out of an ordinary search
    assert ids["dup"] not in _list("marlowe")
    assert _list("#99999") == []
    assert _list(str(ids["active"])) == []


def test_list_duplicate_shows_the_main_copy_and_hidden_roles_are_found():
    _fresh()
    ids = asyncio.run(_seed())
    assert _list(f"#{ids['dup']}") == [ids["main"]]
    assert _list(f"#{ids['orphan']}") == [ids["orphan"]]
    assert _list(f"#{ids['hidden']}") == [ids["hidden"]]
    assert _list(f"#{ids['closed_co']}") == [ids["closed_co"]]
    assert _list("#424242") == []


def test_list_hidden_chip_still_means_hidden_only_for_an_id_query():
    _fresh()
    ids = asyncio.run(_seed())
    assert _list(f"#{ids['active']}", hidden_only=True) == []
    assert _list(f"#{ids['hidden']}", hidden_only=True) == [ids["hidden"]]


def test_list_page_labels_a_hidden_role_and_redacts_the_employer():
    _fresh()
    ids = asyncio.run(_seed(confidential=True))
    with _client() as c:
        page = c.get("/opportunities", params={"q": f"#{ids['hidden']}"}).text
        assert "Rivet Counter" in page and ">hidden" in page
        mine = c.get("/opportunities", params={"q": f"role id {ids['mine']}"}).text
        assert "Dispatcher" in mine and _EMPLOYER not in mine


# ------------------------------------------------- a miss falls back to title search (issue 213)


def test_an_id_that_finds_no_role_is_searched_as_title_text_on_both_surfaces():
    """Corrects the first round, which dropped the text for any query shaped like an ID: the
    issue says anything that does not match falls back to the normal title search."""
    _fresh()
    ids = asyncio.run(_seed())
    tagged = ids["tagged"]
    assert [r["href"] for r in _palette("#9999")["roles"]] == [f"/opportunities/{tagged}"]
    assert _list("#9999") == [tagged]
    # The fallback rows carry no ID labels or notes.
    assert _palette("#9999")["roles"][0]["s"] == "Marlowe Ironworks"


def test_an_id_that_finds_a_role_wins_over_title_text():
    _fresh()
    ids = asyncio.run(_seed())
    n = ids["active"]
    assert _list(f"#{n}") == [n]
    assert [r["href"] for r in _palette(f"#{n}")["roles"]] == [f"/opportunities/{n}"]


def test_unicode_digits_are_not_an_id():
    _fresh()
    asyncio.run(_seed())
    assert _palette("#\u0661") == {"roles": [], "companies": []}
    assert _list("#\uff11") == []


def test_list_page_prints_the_main_copy_note_and_the_hidden_company_label():
    _fresh()
    ids = asyncio.run(_seed())
    with _client() as c:
        dup = c.get("/opportunities", params={"q": f"#{ids['dup']}"}).text
        assert f"Shown as role {ids['main']}, the main copy." in dup
        assert 'data-testid="row-id-note"' in dup
        shut = c.get("/opportunities", params={"q": f"#{ids['closed_co']}"}).text
        assert "Hidden company" in shut and 'data-testid="row-id-label"' in shut
        plain = c.get("/opportunities", params={"q": "kiln"}).text
        assert "row-id-note" not in plain and "row-id-label" not in plain
        # The hidden role keeps its one existing tag; no second Hidden label.
        hid = c.get("/opportunities", params={"q": f"#{ids['hidden']}"}).text
        assert "row-id-label" not in hid and ">hidden" in hid


def test_palette_and_list_take_the_same_labels_from_one_lookup():
    from kestrel.services.role_id_query import role_id_hits

    _fresh()
    ids = asyncio.run(_seed())

    async def hits(n):
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as s:
            out = await role_id_hits(s, n)
        await engine.dispose()
        forget_engine()
        return out

    dup = asyncio.run(hits(ids["dup"]))
    assert [(h.opp_id, h.note) for h in dup] == [
        (ids["main"], f"Shown as role {ids['main']}, the main copy.")]
    assert asyncio.run(hits(424242)) == []
    shut = asyncio.run(hits(ids["closed_co"]))
    assert shut[0].label == "Hidden company"
    assert shut[0].label in _palette(f"#{ids['closed_co']}")["roles"][0]["s"]


# ------------------------------------------- the lookup is the Pipeline page's alone (round 3)


def test_the_json_api_never_returns_hidden_roles_for_an_id_query():
    _fresh()
    ids = asyncio.run(_seed())
    with _client() as c:
        for key in ("hidden", "closed_co", "dup"):
            body = c.get("/api/opportunities", params={"q": f"#{ids[key]}"}).json()
            assert [r["id"] for r in body["rows"]] == [], key
        # The same query on the page still finds them, and an ordinary API search is unchanged.
        assert [r["id"] for r in c.get(
            "/api/opportunities", params={"q": "kiln"}).json()["rows"]] == [ids["main"]]
    assert _list(f"#{ids['hidden']}") == [ids["hidden"]]


def test_an_id_query_without_the_page_flag_is_title_text():
    _fresh()
    ids = asyncio.run(_seed())

    async def run():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as s:
            page = await list_opportunities(s, OppFilters(q=f"#{ids['hidden']}"))
        await engine.dispose()
        forget_engine()
        return [r.opp.id for r in page.rows]

    assert asyncio.run(run()) == []


def test_the_not_a_fit_loader_prints_the_same_labels():
    _fresh()
    ids = asyncio.run(_seed())

    async def reject():
        engine = make_async_engine(paths.db_path())
        async with async_sessionmaker(engine)() as s:
            for oid in (ids["main"], ids["closed_co"]):
                o = await s.get(Opportunity, oid)
                o.stage, o.stage_reason = "t0_reject", "blocker:anti_title: help desk"
            await s.commit()
        await engine.dispose()
        forget_engine()

    asyncio.run(reject())
    with _client() as c:
        dup = c.get("/opportunities/not-fit", params={"q": f"#{ids['dup']}"}).text
        assert f"Shown as role {ids['main']}, the main copy." in dup
        shut = c.get("/opportunities/not-fit", params={"q": f"#{ids['closed_co']}"}).text
        assert "Hidden company" in shut


# ------------------------------------- an ID finds its role on either tab (D-UI-773, round 5)


async def _add_consulting_role():
    engine = make_async_engine(paths.db_path())
    async with async_sessionmaker(engine)() as s:
        org = Org(name="Harbor Lane", normalized_name="harbor lane")
        src = Source(track="consulting", kind="greenhouse", name="c", enabled=True)
        s.add_all([org, src])
        await s.flush()
        other = Opportunity(track="consulting", opp_type="job_posting", org_id=org.id,
                            source_id=src.id, title="Plant Audit", org_name=org.name,
                            url="https://x/c1")
        s.add(other)
        await s.flush()
        titled = Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                             source_id=src.id, title=f"Analyst #{other.id}",
                             org_name=org.name, url="https://x/c2")
        s.add(titled)
        await s.flush()
        out = (other.id, titled.id)
        await s.commit()
    await engine.dispose()
    forget_engine()
    return out


def test_an_id_finds_a_role_of_the_other_track_and_says_which_tab_it_is_on():
    """The owner-approved behavior: an exact ID finds that role whatever tab is active; it is
    never searched as title text because the role is on the other track."""
    _fresh()
    asyncio.run(_seed())
    other, titled = asyncio.run(_add_consulting_role())
    assert other in _list(f"#{other}", track="employment")
    assert other in _list(f"#{other}", track="consulting")
    with _client() as c:
        page = c.get("/opportunities", params={"q": f"#{other}", "track": "employment"}).text
        assert "Plant Audit" in page
        assert f'href="/opportunities?track=consulting&amp;q=%23{other}"' in page
        assert "On the Consulting tab" in page
        # On its own tab the role carries no tab label.
        own = c.get("/opportunities", params={"q": f"#{other}", "track": "consulting"}).text
        assert "Plant Audit" in own and "row-id-track" not in own


def test_palette_names_the_track_only_when_two_are_in_use(monkeypatch):
    from kestrel.web.pages import chrome

    _fresh()
    asyncio.run(_seed())
    other, _titled = asyncio.run(_add_consulting_role())
    monkeypatch.setattr(chrome, "enabled_tracks_cached", lambda: ("employment",))
    assert "Consulting role" not in _palette(f"#{other}")["roles"][0]["s"]
    monkeypatch.setattr(chrome, "enabled_tracks_cached", lambda: ("employment", "consulting"))
    assert "Consulting role" in _palette(f"#{other}")["roles"][0]["s"]
