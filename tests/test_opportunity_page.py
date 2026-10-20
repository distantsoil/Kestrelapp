"""The rebuilt opportunity detail page (docs/UI_PROPOSAL.html Screen 3): the decision at the top,
five HTMX-loaded tabs, and a right column with Your call, Documents and the Tier 3 card.

Pattern: tests/test_confidential_api_mcp.py. A throwaway engine seeds a per-test sqlite file, the
cached engine is forgotten so TestClient's app builds its own on its own loop, and post-request DB
assertions open a fresh engine. Nothing here reaches the network: the Tier 3 card is built from
the read-only preview (kestrel.pipeline.tier3.build_escalation_preview) and a cached price, never
from a provider call."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    Contact,
    DocMaster,
    DocRender,
    DocSection,
    Evaluation,
    FrontierTransmission,
    Opportunity,
    Org,
    OrgResearch,
    Pursuit,
    Setting,
    Source,
    StatusEvent,
    VerdictOverride,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_EMPLOYER = "Northwind Freight"
_PLACEHOLDER = "[Confidential]"


_DOMAIN = "northwindfreight.com"


async def _seed(
    *, confidential: bool = False, master: bool = True, employer_web: bool = False
) -> dict[str, int]:
    """`employer_web`: the org carries its website, the posting sits on a board slug that names
    the employer, the source is named after it, and a dedup copy on the careers host exists."""
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        org = Org(
            name=_EMPLOYER, normalized_name=_EMPLOYER.lower(), sector="logistics",
            ownership="pe_backed", size_band="500-1000",
            signals=[{"category": "funding", "statement": f"{_EMPLOYER} raised a round."}],
            url=f"https://www.{_DOMAIN}" if employer_web else None,
            domain=_DOMAIN if employer_web else None,
        )
        src = Source(
            track="employment", kind="greenhouse", enabled=True,
            name=f"{_EMPLOYER} careers" if employer_web else "Greenhouse board",
        )
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title=f"VP of Information Technology at {_EMPLOYER}", org_name=_EMPLOYER,
            url=(
                "https://boards.greenhouse.io/northwind-freight/jobs/1"
                if employer_web
                else "https://boards.example.com/northwind/jobs/1"
            ),
            stage="t2_done", dedup_group_id="grp-1" if employer_web else None,
            description_md=f"{_EMPLOYER} is hiring a VP of IT. Reports to the CFO.",
            location_raw="Austin, TX", remote_type="hybrid", comp_min=230000, comp_max=260000,
            comp_currency="USD", comp_interval="year", seen_count=3, version=1,
            liveness="live", liveness_checked_at="2026-10-11T08:00:00+00:00",
        )
        s.add(opp)
        if employer_web:
            s.add(
                Opportunity(
                    track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
                    title=f"VP IT, {_EMPLOYER}", org_name=_EMPLOYER, stage="t2_done",
                    url=f"https://careers.{_DOMAIN}/jobs/1", dedup_group_id="grp-1",
                )
            )
        await s.flush()
        ev = Evaluation(
            opportunity_id=opp.id, tier=2, track="employment", verdict="apply",
            verdict_model="apply", score=4.6, weighted_score=4.5, needs_review=False,
            model="kestrel-tier2", prompt_version="evaluate/v1", prompt_tokens=3100,
            created_at="2026-10-12T05:52:00+00:00",
            dimensions={
                "role_fit": {"score": 4.7, "why": f"Head of IT at {_EMPLOYER} maps directly."},
                "domain_match": {"score": 4.5, "why": "Integration work is the stated priority."},
            },
            blocks={
                "a_role_summary": {
                    "what_this_actually_is": f"Head of IT for {_EMPLOYER}, an acquired carrier.",
                    "title_body_mismatch": None,
                },
                "b_fit": {
                    "matches": [{"requirement": "M&A integration", "evidence": "two integrations",
                                 "strength": "strong"}],
                    "gaps": [{"requirement": "freight vertical", "severity": "addressable",
                              "mitigation": "lead with the regional rollout"}],
                },
                "e_angle": {"lead_with": "Vendor consolidation numbers.", "jd_citations": []},
            },
            blockers=[], legitimacy={"status": "clean", "confidence": "high", "signals": []},
        )
        s.add(ev)
        pursuit = Pursuit(
            opportunity_id=opp.id, track="employment", status="applied",
            last_activity_at="2026-10-12T09:00:00+00:00",
        )
        s.add(pursuit)
        await s.flush()
        s.add(
            StatusEvent(
                pursuit_id=pursuit.id, from_status="shortlisted", to_status="applied",
                note="Applied through the careers page.", occurred_at="2026-10-12T09:00:00+00:00",
            )
        )
        s.add(
            FrontierTransmission(
                opportunity_id=opp.id, created_at="2026-10-12T10:00:00+00:00",
                provider="frontier", base_url="https://openrouter.ai/api/v1",
                model="anthropic/claude-sonnet-4.6", confidential=True, payload=[],
                payload_sha256="abc", prompt_tokens_estimate=3400, status="sent",
                prompt_tokens=3350, completion_tokens=900, cost_usd=0.0213,
            )
        )
        s.add(
            OrgResearch(
                org_id=org.id, summary_md=f"{_EMPLOYER} is eighteen months into a roll-up.",
                signals=[], unknowns=[], angle="Integration", insufficient_sources=False,
            )
        )
        s.add(Contact(org_id=org.id, name="Jane Doe", title=f"CFO, {_EMPLOYER}",
                      contact_type="hiring_manager"))
        ids = {"opp": opp.id, "org": org.id}
        if master:
            m = DocMaster(track="employment", name="Executive resume", doc_kind="resume")
            s.add(m)
            await s.flush()
            s.add(
                DocSection(
                    doc_master_id=m.id, section_kind="role", sort_order=0,
                    org_label=_EMPLOYER, org_label_redacted=_PLACEHOLDER,
                )
            )
            s.add(
                DocRender(
                    opportunity_id=opp.id, doc_master_id=m.id, filename="resume.pdf",
                    page_count=2, created_at="2026-10-12T11:00:00+00:00",
                )
            )
            ids["master"] = m.id
        else:
            # The redaction map comes from doc sections, so keep one on an inactive master.
            m = DocMaster(track="employment", name="Old resume", doc_kind="resume",
                          is_active=False)
            s.add(m)
            await s.flush()
            s.add(
                DocSection(
                    doc_master_id=m.id, section_kind="role", sort_order=0,
                    org_label=_EMPLOYER, org_label_redacted=_PLACEHOLDER,
                )
            )
        if confidential:
            s.add(Setting(key="confidential_mode", value=True))
        await s.commit()
    await engine.dispose()
    forget_engine()
    return ids


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


async def _override_count(opp_id: int) -> int:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(VerdictOverride)
                .where(VerdictOverride.opportunity_id == opp_id)
            )
        ).scalar_one()
    await engine.dispose()
    return n


# ------------------------------------------------------------------------------- the page
def test_page_renders_header_tabs_and_right_column():
    ids = asyncio.run(_seed())
    with _client() as c:
        r = c.get(f"/opportunities/{ids['opp']}")
    assert r.status_code == 200
    html = r.text
    # header: breadcrumb, title, the fit word (D-UI-45); the actions are in the decision panel
    assert "Pipeline ›" in html
    assert "VP of Information Technology" in html
    assert 'data-testid="verdict-pill"' in html and 'data-fit="strong fit">strong fit<' in html
    assert ">apply<" not in html and "Start pursuit" not in html
    assert "On my shortlist · applied" in html  # the seeded pursuit shows as the state
    assert (
        f"/renders/new?master_id={ids['master']}&amp;opportunity_id={ids['opp']}"
        f"&amp;from=/opportunities/{ids['opp']}"
    ) in html
    assert ">Deeper read with a stronger model<" in html
    assert f"/opportunities/{ids['opp']}/escalate" in html and "Escalate" not in html
    # the fact row
    facts = html.split('data-testid="facts"')[1].split("</div>")[0]
    for expected in (_EMPLOYER, "Austin, TX, hybrid", "$230k to $260k", "via Greenhouse",
                     "seen 3 times, unchanged"):
        assert expected in facts, expected
    # five tabs, each loading its partial
    for t in ("evaluation", "posting", "organization", "documents", "history"):
        assert f"/opportunities/{ids['opp']}/tab/{t}" in html, t
    # the evaluation tab is rendered inline on the first paint
    assert "The model and the scoring rules agree: strong fit." in html
    # 4.6 and 4.5 differ, so both show, named for what they are (D-UI-45)
    assert "4.6<small>model's score</small>" in html
    assert "4.5<small>weighted score</small>" in html and "<small>holistic" not in html
    assert "A · What this actually is" in html and "B · Why you" in html
    assert "G · Legitimacy" in html and "Compensation corpus" in html
    assert 'class="dimrow"' in html and "Role fit" in html
    # right column
    assert ">Disagree with the fit?<" in html and 'name="fit" value="maybe"' in html
    assert 'id="tier3-card"' in html and "Nothing is sent until you confirm" in html
    assert 'id="documents-card"' in html and "1 render for this role" in html


def test_tab_partials_return_their_content():
    ids = asyncio.run(_seed())
    with _client() as c:
        ev = c.get(f"/opportunities/{ids['opp']}/tab/evaluation")
        assert ev.status_code == 200
        assert 'data-tab="evaluation"' in ev.text
        assert "The model and the scoring rules agree" in ev.text
        assert "What would worry a hiring committee" in ev.text  # the gaps list under B
        assert "3,100 tokens in" in ev.text and "a local model" in ev.text

        posting = c.get(f"/opportunities/{ids['opp']}/tab/posting")
        assert posting.status_code == 200
        assert "Reports to the CFO" in posting.text
        assert "boards.example.com/northwind/jobs/1" in posting.text
        assert ">live<" in posting.text and "checked 2026-10-11" in posting.text

        org = c.get(f"/opportunities/{ids['opp']}/tab/organization")
        assert org.status_code == 200
        assert "eighteen months into a roll-up" in org.text
        assert "Jane Doe" in org.text and "raised a round" in org.text
        assert f"/orgs/{ids['org']}" in org.text

        docs = c.get(f"/opportunities/{ids['opp']}/tab/documents")
        assert docs.status_code == 200
        assert "resume.pdf" in docs.text and "Executive resume" in docs.text
        assert "No draft yet" in docs.text

        hist = c.get(f"/opportunities/{ids['opp']}/tab/history")
        assert hist.status_code == 200
        assert "Tier 2 evaluation" in hist.text and "kestrel-tier2" in hist.text
        assert "shortlisted to applied" in hist.text
        assert "Deeper read sent to OpenRouter" in hist.text and "$0.0213" in hist.text
        assert "Tier 3 transmission" not in hist.text

        assert c.get(f"/opportunities/{ids['opp']}/tab/nope").status_code == 404
        assert c.get("/opportunities/999999/tab/posting").status_code == 404
        assert c.get("/opportunities/999999").status_code == 404


def test_tier3_card_states_destination_and_estimate_and_sends_nothing():
    ids = asyncio.run(_seed())
    with _client() as c:
        html = c.get(f"/opportunities/{ids['opp']}").text
    card = html.split('id="tier3-card"')[1].split('id="t3-escalate"')[0]
    # destination: the configured backend and model (packaged settings.yml: OpenRouter)
    assert "OpenRouter" in card
    assert "anthropic/claude-sonnet-4.6" in card
    # the token estimate from the read-only preview, and the cost line
    assert "tokens in" in card
    assert "cost depends on the model" in card or "about $" in card or "under $" in card
    assert "Nothing is sent until you confirm" in card
    # no key configured in tests: preview only, and the card says so
    assert "Preview only" in card
    # the card itself carries no send: no confirm form, no transmit button
    assert "escalate/confirm" not in card
    assert "Transmit" not in card
    assert "confirmation_token" not in card


def test_tier3_cost_uses_a_cached_price_and_never_the_network(monkeypatch):
    from kestrel.llm import providers
    from kestrel.web import routes_pages

    async def _no_network(*a, **k):
        raise AssertionError("the page must not list models from the provider")

    monkeypatch.setattr(providers, "list_models", _no_network)
    assert routes_pages._cost_line("anthropic/claude-sonnet-4.6", 3400) == (
        "cost depends on the model's price"
    )
    monkeypatch.setattr(
        providers, "MODEL_CATALOG_CACHE",
        {"frontier": [providers.ModelInfo(id="anthropic/claude-sonnet-4.6", name="Sonnet",
                                           prompt_usd_per_m=3.0)]},
        raising=False,
    )
    assert routes_pages._cost_line("anthropic/claude-sonnet-4.6", 3400) == "about $0.01"
    assert routes_pages._cost_line("anthropic/claude-sonnet-4.6", 340_000) == "about $1.02"
    assert routes_pages._cost_line("other/model", 3400) == "cost depends on the model's price"


# -------------------------------------------------------------------------- confidential
def test_confidential_mode_redacts_the_employer_everywhere_on_the_page():
    ids = asyncio.run(_seed(confidential=True))
    with _client() as c:
        page = c.get(f"/opportunities/{ids['opp']}").text
        posting = c.get(f"/opportunities/{ids['opp']}/tab/posting").text
        org = c.get(f"/opportunities/{ids['opp']}/tab/organization").text
        hist = c.get(f"/opportunities/{ids['opp']}/tab/history").text
    for name, html in (("page", page), ("posting", posting), ("organization", org)):
        assert _EMPLOYER not in html, f"{name} leaked the employer"
        assert _PLACEHOLDER in html, f"{name} did not show the placeholder"
    assert _EMPLOYER not in hist, "history leaked the employer"
    # the header title and the evaluation prose (inline on the page) are redacted too
    assert f"VP of Information Technology at {_PLACEHOLDER}" in page
    assert f"Head of IT for {_PLACEHOLDER}" in page
    # the Tier 3 card says the employer is redacted before any send
    assert "Employer redacted" in page.split('id="tier3-card"')[1]


def test_confidential_mode_redacts_urls_source_and_dedup_copies_in_the_tabs():
    """The org website, the posting URL, the source name and the dedup copies name the employer
    as readily as the prose: none of them, nor the domain, may reach the two tabs."""
    ids = asyncio.run(_seed(confidential=True, employer_web=True))
    with _client() as c:
        posting = c.get(f"/opportunities/{ids['opp']}/tab/posting").text
        org = c.get(f"/opportunities/{ids['opp']}/tab/organization").text
    for name, html in (("posting", posting), ("organization", org)):
        low = html.lower()
        assert _EMPLOYER.lower() not in low, f"{name} leaked the employer name"
        assert _DOMAIN not in low and "northwindfreight" not in low, f"{name} leaked the domain"
        assert "northwind-freight" not in low, f"{name} leaked the board slug"
        assert "greenhouse.io" not in low, f"{name} kept a link that reveals the employer"
    assert "source posting" in posting and f"{_PLACEHOLDER} careers" in posting
    assert "Also seen (1)" in posting and f"VP IT, {_PLACEHOLDER}" in posting
    assert "website redacted" in org and "https://" not in org.split("Website")[1][:200]


def test_confidential_mode_off_keeps_urls_source_and_dedup_links():
    ids = asyncio.run(_seed(confidential=False, employer_web=True))
    with _client() as c:
        posting = c.get(f"/opportunities/{ids['opp']}/tab/posting").text
        org = c.get(f"/opportunities/{ids['opp']}/tab/organization").text
    assert 'href="https://boards.greenhouse.io/northwind-freight/jobs/1"' in posting
    assert f"{_EMPLOYER} careers" in posting and f"careers.{_DOMAIN}/jobs/1" in posting
    assert f'href="https://www.{_DOMAIN}"' in org and f">{_DOMAIN} " in org


def test_confidential_mode_off_shows_the_real_name():
    ids = asyncio.run(_seed(confidential=False))
    with _client() as c:
        page = c.get(f"/opportunities/{ids['opp']}").text
    assert _EMPLOYER in page and _PLACEHOLDER not in page


# ------------------------------------------------------------------------------ your call
def test_override_from_the_new_page_still_records_a_label():
    ids = asyncio.run(_seed())
    with _client() as c:
        page = c.get(f"/opportunities/{ids['opp']}").text
        # the control posts to the same route it always did
        assert f'hx-post="/api/opportunities/{ids["opp"]}/override"' in page
        r = c.post(
            f"/api/opportunities/{ids['opp']}/override",
            data={"mode": "agree"}, headers={"HX-Request": "true"},
        )
        assert r.status_code == 200 and "You said strong fit." in r.text
        r2 = c.post(
            f"/api/opportunities/{ids['opp']}/override",
            data={"mode": "override", "operator_verdict": "pass", "operator_score": "2.5",
                  "reason": "Wrong vertical."},
            headers={"HX-Request": "true"},
        )
        assert r2.status_code == 200 and "You said not a fit." in r2.text
        # the page reflects the latest call against the fit
        page2 = c.get(f"/opportunities/{ids['opp']}").text
        assert "You said <b>not a fit</b>; the fit says strong fit." in page2
        hist = c.get(f"/opportunities/{ids['opp']}/tab/history").text
        assert "Wrong vertical." in hist and "Agreed with the system" in hist
    assert asyncio.run(_override_count(ids["opp"])) == 2


# --------------------------------------------------------------------------------- tailor
def test_tailor_is_disabled_with_a_reason_when_no_master_exists():
    ids = asyncio.run(_seed(master=False))
    with _client() as c:
        page = c.get(f"/opportunities/{ids['opp']}").text
        docs = c.get(f"/opportunities/{ids['opp']}/tab/documents").text
    header = page.split('data-testid="tailor"')[0][-200:]
    assert "disabled" in header
    assert "no active document master for the employment track" in page
    assert "/renders/new?master_id=" not in page
    assert "disabled" in docs and "No active document master" in docs


def test_posting_tab_states_why_a_posting_closed_and_when_it_looked_closed_once():
    """D-INTEL-1: a closed posting says why; one expired read says it will be checked again."""
    from sqlalchemy import update

    ids = asyncio.run(_seed())

    async def _set(**values):
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await s.execute(
                update(Opportunity).where(Opportunity.id == ids["opp"]).values(**values)
            )
            await s.commit()
        await engine.dispose()

    asyncio.run(_set(liveness="live", liveness_strikes=1))
    with _client() as c:
        once = c.get(f"/opportunities/{ids['opp']}/tab/posting").text
    assert 'data-testid="liveness-strike"' in once and "Looked closed once" in once
    assert 'data-testid="closed-reason"' not in once

    asyncio.run(_set(liveness="closed", liveness_strikes=2, closed_reason="http_404"))
    with _client() as c:
        closed = c.get(f"/opportunities/{ids['opp']}/tab/posting").text
    assert "The posting page returned not found twice." in closed
    assert 'data-testid="liveness-strike"' not in closed
    assert "\u2014" not in closed.split('data-testid="liveness-pill"')[1][:400]
