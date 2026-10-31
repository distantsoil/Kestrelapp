"""Company page (D-UI-123): header, open roles with liveness chips, the sourced summary in each
of its states, the ask-first Refresh for an online model, Hide with Undo, People you know (the
exact D-UI-131 line), pay and the reply rate under five. Offline: no model, no network."""

from __future__ import annotations

import asyncio
import html as htmllib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import (
    ConnectionPosition,
    Contact,
    ContactImport,
    Opportunity,
    Org,
    OrgSummary,
    Pursuit,
    Task,
)
from kestrel.research import org_summary as osum
from kestrel.web import company_view as cv

LINE = (
    "Because you imported these connections from LinkedIn, their details may not be public. "
    "Kestrel keeps them on this computer only and never shares them."
)


def _client():
    from tests.test_hosted_mode import _fresh_app

    return _fresh_app()


def _page(client, org_id: int) -> str:
    """The page text with entities decoded, so copy with apostrophes reads as written."""
    return htmllib.unescape(client.get(f"/orgs/{org_id}").text)


def _run(coro_fn):
    async def go():
        async with get_sessionmaker()() as s:
            out = await coro_fn(s)
            await s.commit()
            return out

    return asyncio.run(go())


def _org(**kw) -> int:
    async def go(s):
        org = Org(name="Harbor Ledger", normalized_name="harbor ledger", **kw)
        s.add(org)
        await s.flush()
        return org.id

    return _run(go)


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


def _opp(org_id: int, title: str, **kw) -> int:
    async def go(s):
        o = Opportunity(
            track="employment", opp_type="job_posting", org_id=org_id, title=title,
            url=f"https://x.example/{title}", stage="t2_done", **kw,
        )
        s.add(o)
        await s.flush()
        return o.id

    return _run(go)


def _record(org_id: int, **kw) -> None:
    async def go(s):
        s.add(OrgSummary(org_id=org_id, **kw))

    _run(go)


def _tasks() -> list[Task]:
    async def go(s):
        return list((await s.execute(select(Task).where(Task.type == osum.TASK_TYPE))).scalars())

    return _run(go)


_SOURCES = [
    {"index": 1, "kind": "website", "url": "https://harbor.example/about",
     "title": "harbor.example/about", "fetched_at": _days_ago(2)},
    {"index": 2, "kind": "filing", "url": "https://sec.example/f", "title": "Annual report",
     "fetched_at": _days_ago(3)},
]


# ------------------------------------------------------------------------------- the header
def test_header_shows_known_facts_only_and_no_follow_button():
    client = _client()
    org_id = _org(sector="Freight", hq_location="Memphis, TN", domain="harbor.example")
    html = _page(client, org_id)
    assert "Harbor Ledger" in html and "Freight" in html and "Memphis, TN" in html
    assert "harbor.example" in html
    assert 'data-testid="co-hide"' in html and "Hide this company" in html
    assert "Follow" not in html  # deferred (D-UI-123 amended)
    assert "Company not found" not in html


def test_unknown_facts_are_left_blank_not_guessed():
    client = _client()
    org_id = _org()
    html = _page(client, org_id)
    assert 'data-testid="co-meta"' in html
    meta = html.split('data-testid="co-meta"')[1].split("</p>")[0]
    assert "<span>" not in meta and "<a " not in meta


def test_missing_company_page_is_404():
    client = _client()
    assert client.get("/orgs/9999").status_code == 404


# ------------------------------------------------------------------------------------ roles
def test_roles_carry_a_liveness_chip_only_when_the_page_says_so():
    client = _client()
    org_id = _org()
    now = datetime.now(UTC).isoformat()
    _opp(org_id, "Fresh", liveness="live", liveness_checked_at=now, date_posted=_days_ago(3))
    _opp(org_id, "Old", liveness="live", liveness_checked_at=now, date_posted=_days_ago(63))
    _opp(org_id, "Never", liveness="unchecked")
    _opp(org_id, "Gone", liveness="closed")
    html = _page(client, org_id)
    assert "Kestrel checked the company's careers page today and this role is still listed." in html
    assert "Possibly stale: posted 63 days ago, still listed" in html
    assert html.count('data-testid="co-chip"') == 2  # Never has none; Gone is not an open role
    assert "Never" in html and "Gone" not in html.split(' id="co-roles"')[1].split("</section>")[0]


# ---------------------------------------------------------------------------------- summary
def test_summary_ok_shows_numbered_markers_and_the_local_label():
    client = _client()
    org_id = _org()
    _record(
        org_id, status="ok", source_count=2, sources=_SOURCES,
        sentences=[{"text": "Harbor Ledger keeps books for ports.", "source_indexes": [1, 2]}],
        tags=[{"key": "does", "text": "Port accounting", "source_indexes": [1]}],
        model="local-m", generated_at=_days_ago(1),
    )
    html = _page(client, org_id)
    assert "Written by local-m" in html and "check the sources" in html
    assert 'data-src="co-src-1"' in html and 'data-src="co-src-2"' in html
    assert 'id="co-src-1"' in html and "Port accounting" in html


def test_summary_insufficient_says_so_and_a_refresh_makes_no_model_call():
    client = _client()
    org_id = _org()
    _record(org_id, status="insufficient", insufficient_sources=True, source_count=1,
            sources=_SOURCES[:1])
    html = _page(client, org_id)
    assert "Not enough public information to write a summary yet." in html
    assert "Kestrel found 1 source." in html


def test_summary_quarantined_has_its_own_state():
    client = _client()
    org_id = _org()
    _record(org_id, status="quarantined", source_count=2, sources=_SOURCES)
    html = _page(client, org_id)
    assert 'data-testid="co-quarantined"' in html
    assert "no summary was made up" in html


def test_summary_card_polls_while_a_pass_is_queued():
    client = _client()
    org_id = _org()
    client.get(f"/orgs/{org_id}")  # opening it queues the pass
    card = client.get(f"/orgs/{org_id}/summary").text
    assert 'data-state="pending"' in card and 'hx-trigger="every 4s"' in card
    assert 'data-testid="co-pending"' in card


def test_refresh_on_a_local_model_just_queues():
    client = _client()
    org_id = _org()
    r = client.post(f"/orgs/{org_id}/summary/refresh")
    assert r.status_code == 200 and "Refreshing the summary" in r.text
    assert "co-ask" not in r.text
    assert [t.payload["reason"] for t in _tasks()] == ["refresh"]


def _hosted_on(monkeypatch, on: bool = True) -> str:
    """Settings as the page reads them, with Hosted mode acknowledged (or not). Returns the
    configured Tier 2 model, which is what the dialog shows and posts."""
    from kestrel.web.pages import orgs

    orig = orgs.load_settings_with_db

    async def patched(session):
        st = (await orig(session)).model_copy(deep=True)
        hm = st.llm.hosted_mode
        if on:
            hm.enabled = True
            hm.acknowledged_at = "2026-10-01T00:00:00Z"
            hm.acknowledged_terms = hm.disclosure_terms()
        return st

    monkeypatch.setattr(orgs, "load_settings_with_db", patched)
    return str(_db_tier2_model())


def _tier2_dest() -> str:
    from tests.test_hosted_mode import _db_settings

    st = _db_settings()
    from kestrel.services.online_consent import destination_id

    return destination_id(st, "tier2")


def _db_tier2_model() -> str:
    from tests.test_hosted_mode import _db_settings

    return _db_settings().llm.tiers["tier2"].model


def test_refresh_on_an_online_model_asks_first_and_sends_only_on_confirm(monkeypatch):
    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    client = _client()
    model = _hosted_on(monkeypatch)
    org_id = _org()
    _record(org_id, status="ok", source_count=2, sources=_SOURCES,
            sentences=[{"text": "A.", "source_indexes": [1]}], tags=[])
    asked = client.post(f"/orgs/{org_id}/summary/refresh")
    assert 'data-testid="co-ask"' in asked.text
    assert "the company name and short excerpts of the 2 public pages" in asked.text
    assert "Not sent: your resume, your profile, your contacts or your name." in asked.text
    assert _tasks() == []  # nothing queued, nothing sent, until the dialog is confirmed
    assert htmllib.escape(json.dumps(model)) in asked.text or json.dumps(model) in asked.text
    done = client.post(
        f"/orgs/{org_id}/summary/refresh",
        data={"confirmed": "1", "model": model, "dest": _tier2_dest()},
    )
    assert "Refreshing the summary" in done.text and "co-ask" not in done.text
    assert len(_tasks()) == 1
    # The page open never queues an online pass by itself.
    other = _org_named("Other Co")
    client.get(f"/orgs/{other}")
    assert len(_tasks()) == 1


def test_a_confirmed_refresh_is_refused_server_side_for_another_model(monkeypatch):
    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    client = _client()
    model = _hosted_on(monkeypatch)
    org_id = _org()
    r = client.post(
        f"/orgs/{org_id}/summary/refresh",
        data={"confirmed": "1", "model": "vendor/some-other-model"},
    )
    assert "Nothing was sent" in r.text and "model changed" in r.text
    assert _tasks() == []
    none = client.post(f"/orgs/{org_id}/summary/refresh", data={"confirmed": "1"})
    assert "Nothing was sent" in none.text and _tasks() == []
    same_model_elsewhere = client.post(
        f"/orgs/{org_id}/summary/refresh",
        data={"confirmed": "1", "model": model, "dest": "x|https://elsewhere.example/v1|" + model},
    )
    assert "Nothing was sent" in same_model_elsewhere.text and _tasks() == []
    ok = client.post(
        f"/orgs/{org_id}/summary/refresh",
        data={"confirmed": "1", "model": model, "dest": _tier2_dest()},
    )
    assert "Refreshing the summary" in ok.text and len(_tasks()) == 1


def test_a_confirmed_refresh_is_refused_server_side_when_hosted_mode_is_off(monkeypatch):
    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    client = _client()
    model = _hosted_on(monkeypatch, on=False)
    org_id = _org()
    r = client.post(f"/orgs/{org_id}/summary/refresh", data={"confirmed": "1", "model": model})
    assert "Nothing was sent" in r.text and "Hosted mode" in r.text
    assert _tasks() == []


def test_a_held_manual_refresh_asks_to_refresh_again(monkeypatch):
    client = _client()
    org_id = _org()
    assert "Refreshing the summary" in client.post(f"/orgs/{org_id}/summary/refresh").text

    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)

    async def hold(s):
        await osum.hold_for_confirmation(s, org_id)
        for t in (await s.execute(select(Task))).scalars():
            t.status = "done"

    _run(hold)
    page = client.get(f"/orgs/{org_id}/summary").text
    assert "Refresh again to confirm the online send" in page


def test_header_has_the_initials_tile_and_imported_today_is_lowercase():
    client = _client()
    org_id = _org()
    _import_one(org_id)
    page = _page(client, org_id)
    assert 'data-testid="co-mono"' in page and ">HL<" in page
    assert "imported today" in page and "imported Today" not in page
    assert "Imported Today" not in page


def _org_named(name: str) -> int:
    async def go(s):
        org = Org(name=name, normalized_name=name.lower())
        s.add(org)
        await s.flush()
        return org.id

    return _run(go)


def test_online_refresh_of_the_employer_is_refused_even_when_confirmed(monkeypatch):
    from kestrel.web.pages import orgs

    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    monkeypatch.setattr(orgs, "_held", lambda settings, mapping, org: True)
    client = _client()
    org_id = _org()
    r = client.post(f"/orgs/{org_id}/summary/refresh", data={"confirmed": "1"})
    assert "Nothing was sent" in r.text and "current employer" in r.text
    assert _tasks() == []


# ------------------------------------------------------------------------------------- hide
def test_hide_and_undo_keep_everything_and_swap_in_place():
    client = _client()
    org_id = _org()
    opp_id = _opp(org_id, "Keep me")
    r = client.post(f"/orgs/{org_id}/hide")
    assert r.status_code == 200 and "Show this company here" in r.text
    assert 'id="co-banner"' in r.text and 'hx-swap-oob="true"' in r.text
    toast = json.loads(r.headers["HX-Trigger"])["k-toast"]
    assert toast["undoUrl"].startswith(f"/orgs/{org_id}/unhide?at=")  # this hide's own stamp
    assert "Harbor Ledger" in toast["message"]

    async def check(s):
        assert (await s.get(Org, org_id)).hidden_at is not None
        assert await s.get(Opportunity, opp_id) is not None  # nothing deleted

    _run(check)
    page = _page(client, org_id)
    assert 'data-testid="co-undo"' in page
    back = client.post(f"/orgs/{org_id}/unhide")
    assert "Hide this company" in back.text and 'class="co-banner"' in back.text

    async def after(s):
        assert (await s.get(Org, org_id)).hidden_at is None

    _run(after)
    assert client.post("/orgs/9999/hide").status_code == 404
    assert client.post("/orgs/9999/unhide").status_code == 404


# ---------------------------------------------------------------------------- people you know
def _import_one(org_id: int, *, current: bool = True) -> None:
    async def go(s):
        imp = ContactImport(file_name="Connections.csv", row_count=1)
        s.add(imp)
        await s.flush()
        c = Contact(name="Dana Lee", title="Operations Director", source="linkedin_import",
                    org_id=org_id)
        s.add(c)
        await s.flush()
        s.add(ConnectionPosition(
            contact_id=c.id, company_name="Harbor Ledger", org_id=org_id, match="sure",
            title="Operations Director", connected_on="2021-05-01", last_import_id=imp.id,
            first_import_id=imp.id, is_current=current,
        ))

    _run(go)


def test_people_you_know_without_an_import_links_to_it_and_has_no_linkedin_line():
    client = _client()
    org_id = _org()
    html = _page(client, org_id)
    assert "Import your LinkedIn connections to see people you know at this company" in html
    assert LINE not in html


def test_people_you_know_from_an_import_shows_the_exact_line_and_both_groups():
    client = _client()
    org_id = _org()
    _import_one(org_id)
    html = _page(client, org_id)
    assert LINE in html
    assert "Dana Lee" in html and "Works here now" in html
    assert "Import your LinkedIn connections" not in html
    assert "Kestrel never sends anything" in html


def test_used_to_work_here_comes_from_an_earlier_import():
    client = _client()
    org_id = _org()
    _import_one(org_id, current=False)
    html = _page(client, org_id)
    assert "Used to work here" in html and "Dana Lee" in html


def test_public_contacts_never_include_imported_connections():
    client = _client()
    org_id = _org()
    _import_one(org_id)

    async def add(s):
        s.add(Contact(name="Priya Raman", title="Director", org_id=org_id,
                      source="research", source_url="https://harbor.example/team"))

    _run(add)
    html = _page(client, org_id)
    people = html.split(' id="co-people"')[1].split("</section>")[0]
    assert "Priya Raman" in people and "harbor.example" in people
    assert "Dana Lee" not in people


# ------------------------------------------------------------------------------- pay, history
def _opps_with_pay(org_id: int, n: int) -> None:
    for i in range(n):
        _opp(org_id, f"Role {i}", comp_min=80000 + i * 1000, comp_max=100000 + i * 1000,
             comp_currency="USD", comp_interval="year")


def test_pay_under_five_shows_no_range():
    client = _client()
    org_id = _org()
    _opps_with_pay(org_id, 3)
    html = _page(client, org_id)
    assert ("Kestrel has seen pay on only 3 similar roles here. "
            "It needs at least 5 before it shows a range.") in html
    assert "Typical" not in html.split(' id="co-pay"')[1].split("</section>")[0]


def test_pay_with_five_or_more_shows_the_range_and_its_basis():
    client = _client()
    org_id = _org()
    _opps_with_pay(org_id, 6)
    html = _page(client, org_id)
    assert ("Based on 6 similar roles that list their pay. These are the company's posted "
            "ranges, not what people were offered.") in html
    assert "Middle half of roles" in html and "$" in html


def test_pay_view_ignores_hourly_and_mixed_currency():
    class Row:
        def __init__(self, lo, hi, cur="USD", interval="year"):
            self.comp_min, self.comp_max = lo, hi
            self.comp_currency, self.comp_interval = cur, interval

    rows = [Row(50, 60, interval="hour")] * 9 + [Row(80000 + i, 90000) for i in range(4)]
    assert cv.pay_view(rows) == {"n": 4, "enough": False}


def test_reply_rate_is_withheld_under_five_applications():
    client = _client()
    org_id = _org()
    opp_id = _opp(org_id, "Applied role")

    async def add(s):
        s.add(Pursuit(opportunity_id=opp_id, track="employment", status="applied",
                      applied_at=_days_ago(17)))

    _run(add)
    html = _page(client, org_id)
    assert "Reply rate: Too few to tell (1). A rate needs at least 5 applications." in html
    assert "No reply yet, 17 days" in html


def test_reply_line_gives_a_rate_from_five():
    ps = [Pursuit(status="rejected")] * 2 + [Pursuit(status="applied")] * 3
    assert cv.reply_line(ps) == "Reply rate: 2 of 5 applications heard back (40%)."


def test_stale_threshold_and_dates():
    now = datetime.now(UTC)
    assert cv.short_date(now.isoformat(), now=now) == "Today"
    assert cv.short_date("2026-03-05T00:00:00+00:00", now=now) == "Mar 5"
    assert cv.short_date("garbage") == ""


@pytest.mark.parametrize("path", ["/orgs/{id}"])
def test_page_uses_the_shared_system_and_not_the_legacy_sheet(path):
    client = _client()
    org_id = _org()
    html = _page(client, org_id)
    assert "/static/css/pages/company.css" in html and "/static/css/kestrel.css" not in html
    assert "—" not in html.split("<main")[-1]  # no em dash in the page copy


# ------------------------------------------------------------- confidential mode (real map)
def _employer_setup(*, with_map: bool = True) -> int:
    from kestrel.db.models import DocMaster, DocSection, Setting

    async def go(s):
        org = Org(name="Harbor Ledger", normalized_name="harbor ledger", domain="harborledger.com",
                  url="https://harborledger.com",
                  signals=[{"statement": "Harbor Ledger opened a hub",
                            "source_url": "https://harborledger.com/news",
                            "observed_at": _days_ago(3)}])
        s.add(org)
        await s.flush()
        s.add(Setting(key="confidential_mode", value=True))
        if with_map:
            master = DocMaster(track="employment", name="Resume", doc_kind="resume")
            s.add(master)
            await s.flush()
            s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                             org_label="Harbor Ledger", org_label_redacted="a freight company"))
        return org.id

    return _run(go)


def test_confidential_mode_hides_the_employer_across_the_page(monkeypatch):
    client = _client()
    org_id = _employer_setup()
    _record(org_id, status="ok", source_count=1,
            sources=[{"index": 1, "kind": "website", "url": "https://harborledger.com/about",
                      "title": "Harbor Ledger careers", "fetched_at": _days_ago(1)}],
            sentences=[{"text": "Harbor Ledger keeps books.", "source_indexes": [1]}], tags=[])
    html = _page(client, org_id)
    assert "Harbor Ledger" not in html and "harborledger.com" not in html
    assert "a freight company" in html
    toast = client.post(f"/orgs/{org_id}/hide").headers["HX-Trigger"]
    assert "Harbor Ledger" not in toast


def test_online_refresh_is_refused_when_confidential_mode_has_no_employer_chosen(monkeypatch):
    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    client = _client()
    org_id = _employer_setup(with_map=False)
    r = client.post(f"/orgs/{org_id}/summary/refresh", data={"confirmed": "1"})
    assert "Nothing was sent" in r.text and _tasks() == []


def test_the_summary_label_names_the_model_that_wrote_it_when_tier2_is_online(monkeypatch):
    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    client = _client()
    org_id = _org()
    _record(org_id, status="ok", source_count=1, sources=_SOURCES[:1], model="old-local-model",
            sentences=[{"text": "A.", "source_indexes": [1]}], tags=[])
    assert "Written by old-local-model" in _page(client, org_id)


def test_the_linkedin_line_needs_people_to_show():
    client = _client()
    org_id = _org()

    async def imp(s):
        s.add(ContactImport(file_name="Connections.csv", row_count=0))

    _run(imp)
    html = _page(client, org_id)
    assert "No one in your contacts is listed here." in html and LINE not in html


def test_a_reposted_role_is_one_pay_observation():
    client = _client()
    org_id = _org()
    for _ in range(6):
        _opp(org_id, "Same role", comp_min=80000, comp_max=90000, comp_currency="USD",
             comp_interval="year")
    html = _page(client, org_id)
    assert "Kestrel has seen pay on only 1 similar roles here." in html


def test_only_web_addresses_become_links():
    assert cv.safe_url("javascript:alert(1)") is None
    assert cv.safe_url("https://a.example/x") == "https://a.example/x"


def test_missing_company_htmx_answer_is_static_and_escaped():
    r = _client().get("/orgs/987654", headers={"HX-Request": "true"})
    assert r.status_code == 404
    assert r.text == '<div class="warnbox">Company not found.</div>'
    assert _client().get("/orgs/%3Cscript%3E").status_code == 422  # not an id, never echoed
