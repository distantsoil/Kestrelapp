"""Jobvite XML feed adapter. The feed (tests/fixtures/jobvite_feed.xml) is a shaped copy of a
captured reply with invented values; nothing here touches the network. Under test: field
mapping, resolving the company id from the board page, an explicit id, redirects as a gone board,
an empty feed, invalid XML characters, a DOCTYPE refused, a bad id or slug refused at config load
and at fetch, 429 as a partial run, link recognition, and a run that does not duplicate."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Source
from kestrel.ingest import board_discovery as bd
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters import jobvite as module
from kestrel.ingest.adapters.jobvite import JobviteAdapter
from kestrel.ingest.base import AdapterError, PostingBatch, SourceConfigError
from kestrel.ingest.http import HttpClient
from kestrel.ingest.runner import run_source

_XML = (Path(__file__).parent / "fixtures" / "jobvite_feed.xml").read_text(encoding="utf-8")
_FEED = "https://app.jobvite.com/CompanyJobs/Xml.aspx"
_BOARD = "https://jobs.jobvite.com/northwind"
_PAGE = "<html><script>var x = {\n  companyEId: 'aBcD1234',\n  other: 1 };</script></html>"


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    monkeypatch.setattr(module, "_RATE", 0)


def _http() -> HttpClient:
    return HttpClient(
        user_agent=get_settings().ingest.user_agent, respect_robots=False, max_attempts=1
    )


async def _fetch(config: dict | None = None):
    async with _http() as client:
        return await JobviteAdapter().fetch({"slug": "northwind", **(config or {})}, client)


def _serve(xml: str = _XML):
    respx.get(_BOARD).mock(return_value=Response(200, text=_PAGE))
    return respx.get(_FEED).mock(return_value=Response(200, text=xml))


@respx.mock
async def test_the_company_id_is_read_from_the_board_page_then_the_feed_is_mapped():
    feed = _serve()
    got = await _fetch({"company_name": "Northwind Inc"})
    assert isinstance(got, PostingBatch) and got.partial_error is None
    assert dict(feed.calls[0].request.url.params) == {"c": "aBcD1234"}
    assert [p.external_id for p in got] == ["oAAA0001", "oAAA0002", "oAAA0003"]
    a, b, c = got
    assert a.title == "Account Manager, Subscriptions" and a.org_name == "Northwind Inc"
    assert a.url == "https://app.jobvite.com/CompanyJobs/Job.aspx?c=aBcD1234&j=oAAA0001"
    assert a.apply_url.endswith("&k=Apply") and a.apply_url.startswith("https://")
    assert a.location_raw == "Vienna, Austria" and a.employment_type == "Full-Time"
    assert a.date_posted == "2026-10-21" and c.date_posted == "2026-09-05"
    assert "Invented text for a test fixture &amp; nothing else." in a.description_html
    assert a.description_html.startswith("<p><b>The Opportunity</b></p>")  # entities decoded
    assert a.is_remote is None and b.is_remote is True
    assert a.raw == {"category": "Sales", "region": "EMEA", "requisition_id": "10001"}
    assert "Invented Person" not in repr([p.raw for p in got])  # the hiring team is never read


@respx.mock
async def test_an_explicit_company_id_skips_the_board_page():
    board = respx.get(_BOARD).mock(return_value=Response(200, text=_PAGE))
    feed = respx.get(_FEED).mock(return_value=Response(200, text=_XML))
    got = await _fetch({"slug": None, "company_eid": "zZ99yY88"})
    assert len(got) == 3 and board.call_count == 0 and feed.call_count == 1
    assert dict(feed.calls[0].request.url.params) == {"c": "zZ99yY88"}
    assert got[0].org_name == "Jobvite company zZ99yY88"  # never the shared nameless org


@respx.mock
async def test_an_employers_own_domain_link_is_kept_and_a_missing_link_is_built():
    xml = _XML.replace(
        "http://app.jobvite.com/CompanyJobs/Job.aspx?c=aBcD1234&amp;j=oAAA0001",
        "https://careers.example.org/job/1").replace(
        "http://app.jobvite.com/CompanyJobs/Job.aspx?c=aBcD1234&j=oAAA0001",
        "https://careers.example.org/job/1").replace(
        "<apply-url><![CDATA[http://app.jobvite.com/CompanyJobs/Careers.aspx?c=aBcD1234&j=oAAA0002&k=Apply]]></apply-url>",
        "<apply-url>javascript:alert(1)</apply-url>")
    _serve(xml)
    got = await _fetch()
    assert got[0].url == "https://careers.example.org/job/1"
    assert got[1].apply_url == got[1].url and got[1].apply_url.startswith("https://")


@respx.mock
async def test_a_malformed_link_on_one_job_falls_back_to_the_built_link_and_keeps_the_run():
    xml = _XML.replace(
        "http://app.jobvite.com/CompanyJobs/Job.aspx?c=aBcD1234&amp;j=oAAA0001",
        "https://[bad/").replace(
        "http://app.jobvite.com/CompanyJobs/Job.aspx?c=aBcD1234&j=oAAA0001", "https://[bad/").replace(
        "<apply-url><![CDATA[http://app.jobvite.com/CompanyJobs/Careers.aspx?c=aBcD1234&j=oAAA0002&k=Apply]]></apply-url>",
        "<apply-url>https://[bad/</apply-url>")
    assert "https://[bad/" in xml
    _serve(xml)
    got = await _fetch()
    assert len(got) >= 2
    assert all("[" not in p.url and "[" not in p.apply_url for p in got)
    assert got[0].url.startswith("https://app.jobvite.com/CompanyJobs/Job.aspx?c=aBcD1234&j=")


@respx.mock
async def test_an_empty_tenant_is_an_empty_run():
    _serve('<?xml version="1.0" encoding="UTF-8"?><result />')
    got = await _fetch()
    assert list(got) == [] and got.partial_error is None


@respx.mock
async def test_invalid_xml_characters_are_removed_before_parsing():
    dirty = _XML.replace("Platform Engineer", "Platform\x0b Engi\x00neer&#11;").replace(
        "Talent Pool", "Talent&#x0B; Pool")
    _serve(dirty)
    got = await _fetch()
    assert [p.title for p in got][1:] == ["Platform Engineer (Remote)", "Talent Pool"]


@respx.mock
async def test_a_redirect_from_the_feed_means_the_board_is_gone():
    respx.get(_BOARD).mock(return_value=Response(200, text=_PAGE))
    respx.get(_FEED).mock(return_value=Response(
        302, headers={"location": "https://app.jobvite.com/Recruiting/Home.aspx"}))
    home = respx.get("https://app.jobvite.com/Recruiting/Home.aspx").mock(
        return_value=Response(200, text="<html/>"))
    with pytest.raises(AdapterError, match="not found"):
        await _fetch()
    assert home.call_count == 0  # never followed


@respx.mock
async def test_an_unknown_slug_or_a_page_without_the_id_is_a_bad_board():
    respx.get(_BOARD).mock(return_value=Response(302, headers={"location": "https://x.example/"}))
    with pytest.raises(AdapterError, match="board not found"):
        await _fetch()
    respx.get(_BOARD).mock(return_value=Response(200, text="<html>no id here</html>"))
    with pytest.raises(AdapterError, match="board not found"):
        await _fetch()


@respx.mock
async def test_a_429_is_a_partial_run_on_the_page_and_on_the_feed():
    respx.get(_BOARD).mock(return_value=Response(429))
    got = await _fetch()
    assert list(got) == [] and "rate limiting" in got.partial_error
    assert "—" not in got.partial_error
    respx.get(_BOARD).mock(return_value=Response(200, text=_PAGE))
    respx.get(_FEED).mock(return_value=Response(503))
    got = await _fetch()
    assert list(got) == [] and got.partial_error


@respx.mock
@pytest.mark.parametrize("body", [
    "<html>not xml", "<other/>", "",
    '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "b">]><result>&a;</result>',
])
async def test_bad_shapes_and_a_doctype_raise_adapter_error(body):
    _serve(body)
    with pytest.raises(AdapterError):
        await _fetch()


@respx.mock
async def test_other_statuses_raise_adapter_error():
    respx.get(_BOARD).mock(return_value=Response(200, text=_PAGE))
    respx.get(_FEED).mock(return_value=Response(404))
    with pytest.raises(AdapterError):
        await _fetch()


def test_validate_config_refuses_a_bad_slug_or_id():
    a = JobviteAdapter()
    a.validate_config({"slug": "north-wind_2"})
    a.validate_config({"company_eid": "aBcD1234", "company_name": "Northwind"})
    for bad in ({}, {"slug": ""}, {"slug": "a.b"}, {"slug": "-x"}, {"slug": 5}, {"slug": "a/b"},
                {"slug": "x" * 101}, {"company_eid": "ab"}, {"company_eid": "a b c d"},
                {"company_eid": "x" * 33}, {"company_eid": 1234}, {"company_eid": "a&j=1b"}):
        with pytest.raises(SourceConfigError):
            a.validate_config(bad)
    assert get_adapter("jobvite") is not None


@respx.mock
async def test_a_bad_id_or_slug_is_refused_at_fetch_before_any_request():
    async with _http() as client:
        for bad in ({"slug": "../x"}, {"slug": None, "company_eid": "a&j=1"}):
            with pytest.raises(SourceConfigError):
                await JobviteAdapter().fetch(bad, client)
    assert respx.calls.call_count == 0


def test_board_discovery_recognizes_jobvite_links():
    ref = bd.board_from_url("https://jobs.jobvite.com/northwind/jobs")
    assert (ref.kind, ref.config) == ("jobvite", {"slug": "northwind"})
    assert bd.board_from_url("https://jobs.jobvite.com/Northwind").key == ("jobvite", "northwind")
    feed = bd.board_from_url("https://app.jobvite.com/CompanyJobs/Xml.aspx?c=aBcD1234")
    assert (feed.kind, feed.config) == ("jobvite", {"company_eid": "aBcD1234"})
    assert feed.key == ("jobvite", "c:aBcD1234")
    for url in (
        "https://jobs.jobvite.com/", "https://www.jobvite.com/northwind",
        "https://app.jobvite.com/CompanyJobs/Xml.aspx", "https://app.jobvite.com/CompanyJobs/Xml.aspx?c=a",
        "https://app.jobvite.com/Other.aspx?c=aBcD1234", "https://jobs.jobvite.com.evil.x/northwind",
        "https://example.com/jobs.jobvite.com/northwind",
    ):
        assert bd.board_from_url(url) is None


@respx.mock
async def test_run_stores_roles_and_a_second_run_does_not_duplicate(sm):
    _serve()
    async with sm() as s:
        src = Source(track="employment", kind="jobvite", name="Northwind (Jobvite)",
                     config={"slug": "northwind"}, enabled=True, cadence_hours=24)
        s.add(src)
        await s.commit()
        await s.refresh(src)
    async with _http() as http:
        out = await run_source(sm, src.id, http=http)
        again = await run_source(sm, src.id, http=http)
    assert out.status == "ok", out.error
    assert out.counts["fetched"] == 3 and again.status == "ok"
    async with sm() as s:
        opps = (await s.execute(select(Opportunity))).scalars().all()
    assert len(opps) == 3 and opps[0].source_id == src.id


@respx.mock
@pytest.mark.parametrize("body", [
    # a control character or a bad character reference hides the keyword from a raw-text check
    '<?xml version="1.0"?><!DOC\x00TYPE result [<!ENT\x00ITY a "EXPANDED">]>'
    "<result><job><id>1</id><title>&a;&a;</title></job></result>",
    '<?xml version="1.0"?><!DOC&#0;TYPE result [<!ENT&#x1;ITY a "EXPANDED">]>'
    "<result><job><id>1</id><title>&a;</title></job></result>",
    '<?xml version="1.0"?><!ENT&#x0B;ITY a "b"><result/>',
])
async def test_a_doctype_hidden_behind_a_control_character_is_still_refused(body):
    _serve(body)
    with pytest.raises(AdapterError):
        await _fetch()


@respx.mock
async def test_a_doctype_written_inside_a_description_does_not_refuse_the_feed():
    body = ('<?xml version="1.0"?><result><job><id>1</id><title>Analyst</title>'
            "<description><![CDATA[<!DOCTYPE html><p>About the role</p>]]></description>"
            "</job><job><id>2</id><title>Clerk</title>"
            "<description>&lt;!DOCTYPE html&gt; and &lt;!ENTITY x&gt;</description></job></result>")
    _serve(body)
    got = await _fetch()
    assert [p.title for p in got] == ["Analyst", "Clerk"]


@respx.mock
async def test_two_companies_given_only_by_company_id_are_not_one_organization():
    feed = ('<?xml version="1.0"?><result><job><id>1</id><title>Software Engineer</title>'
            "</job></result>")
    respx.get(_FEED).mock(return_value=Response(200, text=feed))
    async with _http() as client:
        a = await JobviteAdapter().fetch({"company_eid": "AAAA1111"}, client)
        b = await JobviteAdapter().fetch({"company_eid": "BBBB2222"}, client)
    assert a[0].org_name and b[0].org_name and a[0].org_name != b[0].org_name
