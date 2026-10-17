"""Adversarial, fully offline tests for P5 company research (kestrel/research/company.py). No
live model call is ever made: the LLM is a deterministic fake. Covers the three code-enforced
rules from SPEC-MODULES 2.1 / hard rule 2:

  1. A signal whose source_index is out of range is dropped, never written as an unattributed
     claim, both in the returned OrgResearch and in what gets appended to org.signals.
  2. Fewer than two supplied excerpts never reaches the model at all -- the fake LLM raises if
     called, and the result is insufficient_sources=True.
  3. Enriching org.size_band/sector/ownership from the research summary fills only what was None;
     a specific existing value (sector="healthcare") is never overwritten by a vaguer inference.
"""

from __future__ import annotations

import json

import respx
from httpx import Response
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Org, OrgResearch
from kestrel.llm.client import ChatResult
from kestrel.research.company import research_org


class _FakeP5LLM:
    """Returns a fixed P5Research-shaped payload regardless of the rendered prompt, so tests
    exercise company.py's validation/enrichment logic without any HTTP call."""

    def __init__(self, settings, payload: dict) -> None:
        self.settings = settings
        self._payload = payload

    async def complete(self, *, tier, messages, schema=None, **_):
        return ChatResult(
            content=json.dumps(self._payload), model=self.settings.llm.tiers[tier].model
        )


class _NoCallLLM:
    """complete() must never be invoked -- proves the <2-source path never reaches the model."""

    def __init__(self, settings) -> None:
        self.settings = settings

    async def complete(self, *, tier, messages, schema=None, **_):
        raise AssertionError("the model must not be called with fewer than two sources")


def _payload(**overrides) -> dict:
    base = {
        "summary_md": "Acme Corp is scaling its engineering org quickly.",
        "signals": [
            {
                "category": "growth",
                "statement": "Acme Corp grew revenue significantly last year.",
                "source_index": 1,
                "implication": "Budget likely available for new headcount.",
            },
            {
                "category": "risk",
                "statement": "A claim attributed to a source that was never supplied.",
                "source_index": 99,
                "implication": "n/a",
            },
        ],
        "unknowns": ["Exact headcount is not stated."],
        "angle_for_candidate": "Lead with scaling experience.",
        "sources_used": [1],
    }
    base.update(overrides)
    return base


async def _make_org(sm, **attrs) -> int:
    async with sm() as s:
        org = Org(name="Acme Corp", normalized_name="acme corp", **attrs)
        s.add(org)
        await s.flush()
        await s.commit()
        return org.id


# ------------------------------------------------------ excerpt acquisition, tier 1 and tier 3
_ABOUT = "<html><body><h1>About Acme Corp</h1><p>" + ("Acme builds hospital software. " * 20) + (
    "</p></body></html>"
)


@respx.mock
async def test_company_pages_fall_back_to_org_domain(sm):
    """Ingestion only ever sets org.domain (resolve_org), never org.url; tier 1 must still run."""
    from kestrel.ingest.http import HttpClient
    from kestrel.research.excerpts import acquire_excerpts

    org_id = await _make_org(sm, domain="acme.example")
    respx.get("https://acme.example/robots.txt").mock(return_value=Response(404))
    respx.get("https://acme.example/").mock(return_value=Response(200, text=_ABOUT))
    respx.get(url__regex=r"https://acme\.example/.+").mock(return_value=Response(404))
    respx.get(url__regex=r"https://efts\.sec\.gov/.*").mock(
        return_value=Response(200, json={"hits": {"hits": []}})
    )
    settings = get_settings()
    async with sm() as s:
        org = await s.get(Org, org_id)
        assert org.url is None
        async with HttpClient(
            user_agent="Kestrel/1.0 (ops@kestrel-test.org)", default_rate_per_sec=0
        ) as http:
            excerpts = await acquire_excerpts(s, http, org, settings=settings)
    kinds = [e.kind for e in excerpts.excerpts]
    assert "company_page" in kinds
    page = next(e for e in excerpts.excerpts if e.kind == "company_page")
    assert page.url.rstrip("/") == "https://acme.example"
    assert "hospital software" in page.text


@respx.mock
async def test_edgar_tier_is_skipped_without_a_real_contact_user_agent(sm):
    from kestrel.ingest.http import HttpClient
    from kestrel.research.excerpts import acquire_excerpts

    org_id = await _make_org(sm)  # no domain: tier 1 has nothing to fetch
    edgar = respx.get(url__regex=r"https://efts\.sec\.gov/.*").mock(
        return_value=Response(200, json={"hits": {"hits": []}})
    )
    settings = get_settings()
    async with sm() as s:
        org = await s.get(Org, org_id)
        placeholder = "Kestrel/1.0 (+https://github.com/kestrel-app; contact: op@example.com)"
        async with HttpClient(user_agent=placeholder, default_rate_per_sec=0) as http:
            await acquire_excerpts(s, http, org, settings=settings)
        assert edgar.called is False  # never sent with a fabricated contact (hard rule 7)
        async with HttpClient(
            user_agent="Kestrel/1.0 (ops@kestrel-test.org)", default_rate_per_sec=0
        ) as http:
            await acquire_excerpts(s, http, org, settings=settings)
        assert edgar.called is True
        assert edgar.calls.last.request.headers["User-Agent"] == (
            "Kestrel/1.0 (ops@kestrel-test.org)"
        )


async def test_out_of_range_source_index_is_dropped(sm):
    org_id = await _make_org(sm)
    settings = get_settings()
    paste = (
        "Acme Corp grew revenue significantly last year.\n\n"
        "Acme Corp has an experienced leadership team."
    )
    async with sm() as s:
        org = await s.get(Org, org_id)
        client = _FakeP5LLM(settings, _payload())
        record = await research_org(s, client, org, settings=settings, operator_paste=paste)
        await s.commit()

        assert record.insufficient_sources is False
        assert len(record.signals) == 1  # the source_index=99 signal was dropped
        assert record.signals[0]["source_index"] == 1
        assert record.signals[0]["category"] == "growth"

    async with sm() as s:
        org = await s.get(Org, org_id)
        assert len(org.signals) == 1  # org.signals gained only the valid signal
        assert org.signals[0]["category"] == "growth"
        assert org.signals[0]["statement"] == "Acme Corp grew revenue significantly last year."

        rows = (await s.execute(select(OrgResearch))).scalars().all()
        assert len(rows) == 1


async def test_fewer_than_two_sources_is_insufficient_and_skips_model(sm):
    org_id = await _make_org(sm)
    settings = get_settings()
    async with sm() as s:
        org = await s.get(Org, org_id)
        client = _NoCallLLM(settings)  # raises if complete() is ever called
        record = await research_org(
            s, client, org, settings=settings, operator_paste="Only one note here."
        )
        await s.commit()

        assert record.insufficient_sources is True


async def test_enrichment_fills_none_but_preserves_specific_value(sm):
    org_id = await _make_org(sm, sector="healthcare", size_band=None, ownership=None)
    settings = get_settings()
    paste = (
        "Acme Corp is a private equity portfolio company.\n\n"
        "Acme Corp is undergoing rapid manufacturing expansion."
    )
    summary = (
        "Acme Corp is a private equity-backed portfolio company undergoing rapid "
        "manufacturing expansion."
    )
    async with sm() as s:
        org = await s.get(Org, org_id)
        client = _FakeP5LLM(settings, _payload(summary_md=summary))
        await research_org(s, client, org, settings=settings, operator_paste=paste)
        await s.commit()

    async with sm() as s:
        org = await s.get(Org, org_id)
        # the pre-existing, more specific value is preserved despite the summary implying
        # "manufacturing" -- never overwritten by a vaguer keyword-inferred sector.
        assert org.sector == "healthcare"
        # ownership WAS None and IS inferable from the summary text -> filled.
        assert org.ownership == "pe_backed"
        # infer_org_attrs has no keyword-inference path for size_band (only structured hints),
        # so it stays None here -- the key assertion above is that "healthcare" was preserved.
        assert org.size_band is None
