"""Regression tests for the wording fixes in 0.6.0 (drift review A2, A3, A4,
A7, A9, A13). Each test renders the real page or fragment and asserts the NEW text is present and
the OLD text is absent. Offline: seeded SQLite file, the test client, no model, no network."""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from datetime import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from kestrel.db.models import MockSession, Opportunity, PrepPack
from tests.test_drift_fixes import _client, _fresh_sm, _seed_basic
from tests.test_onboarding import _app as _onboarding_app
from tests.test_onboarding import _jump, machine  # noqa: F401  (autouse-style fixture, used below)
from tests.test_pipeline_view import _client as _pipeline_client
from tests.test_pipeline_view import _get as _pipeline_get
from tests.test_pipeline_view import _seed as _pipeline_seed
from tests.test_stream_c_pages import _client as _render_client
from tests.test_stream_c_pages import _seed_render

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _visible_text(markup: str) -> str:
    """Text a person reads: tags stripped; attributes, comments and hidden inputs ignored."""
    soup = BeautifulSoup(markup, "html.parser")
    for el in soup(["script", "style", "template"]):
        el.decompose()
    for el in soup.select('input[type="hidden"]'):
        el.decompose()
    return soup.get_text(" ", strip=True)


# --------------------------------------------------------------------------------------- A2
def test_a2_one_page_render_says_1_page_not_1_pages():
    render_id, _ = _seed_render()  # page_count=1
    html = _render_client().get(f"/renders/{render_id}").text
    text = _visible_text(html)
    assert re.search(r"(?<![\w-])1 page(?!s)", text)
    assert "1 pages" not in text


# --------------------------------------------------------------------------------------- A3
def test_a3_deeper_read_confirmation_wording_and_full_payload(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")  # so the confirm form is offered
    opp_id, _ = asyncio.run(_seed_basic())
    c = _client()
    html = c.post(f"/opportunities/{opp_id}/escalate").text
    soup = BeautifulSoup(html, "html.parser")

    button = soup.select_one("form button[type=submit]")
    assert button is not None and button.get_text(strip=True) == "Send for a deeper read"
    assert "Send to frontier" not in html

    # The internal backend key ("frontier") appears nowhere in the visible text. The destination
    # is named by the service's own host instead. The <pre> is excluded: it is the user's own
    # payload, shown verbatim, and may legitimately contain any word.
    visible = BeautifulSoup(html, "html.parser")
    for pre in visible.select("pre"):
        pre.decompose()
    text = visible.get_text(" ", strip=True)
    assert "frontier" not in text.lower(), text
    assert "openrouter.ai" in text  # the service named, in the warning and the summary
    assert "current employer redacted" not in text  # redaction needs an employer chosen
    assert "This sends the job posting and your profile to openrouter.ai, using the model" in text
    assert "Confidential redaction:" in text

    # The page still shows exactly what will be sent: the whole payload, inside the <pre>.
    async def _expected() -> list[dict]:
        from kestrel.pipeline.tier3 import build_escalation_preview
        from kestrel.services.setting_overrides import load_settings_with_db

        sm = await _fresh_sm()
        async with sm() as s:
            settings = await load_settings_with_db(s)
            preview = await build_escalation_preview(s, settings, opp_id)
        return preview.messages

    messages = asyncio.run(_expected())
    pre = soup.select_one("pre").get_text()
    assert messages and all(m["content"] in pre for m in messages)
    assert "Own IT strategy and security for a services firm." in pre
    assert pre == "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)


# --------------------------------------------------------------------------------------- A4
def test_a4_mock_history_shows_a_local_date_not_raw_iso():
    opp_id, _ = asyncio.run(_seed_basic())

    async def _add() -> None:
        sm = await _fresh_sm()
        async with sm() as s:
            s.add(MockSession(
                opportunity_id=opp_id, mode="behavioral", question_count=3,
                started_at="2026-10-14T01:31:00+00:00", ended_at="2026-10-14T01:45:00+00:00",
                mean_scores={"structure": 4},
            ))
            await s.commit()

    asyncio.run(_add())
    html = _client().get(f"/mock/{opp_id}").text
    expect = datetime.fromisoformat("2026-10-14T01:31:00+00:00").astimezone()
    assert expect.strftime("%Y-%m-%d %H:%M") in html
    assert "2026-10-14T01:31" not in html
    assert "2026-10-14T01:45" not in html


# --------------------------------------------------------------------------------------- A7
@pytest.mark.usefixtures("machine")
def test_a7_skipped_label_shows_only_while_setup_is_unfinished():
    c = _onboarding_app()
    steps = ["welcome", "models", "about", "resume"]
    _jump("wants", visited=steps, skipped=["resume"], completed=False)
    unfinished = c.get("/onboarding").text
    assert re.search(r'data-ob-step="resume" data-status="skipped"', unfinished)
    assert ">Skipped<" in unfinished

    _jump("wants", visited=steps, skipped=["resume"], completed=True)
    finished = c.get("/onboarding").text
    assert re.search(r'data-ob-step="resume" data-status="skipped"', finished)
    assert ">Skipped<" not in finished


# --------------------------------------------------------------------------------------- A9
def test_a9_mock_interview_link_with_a_prep_pack():
    opp_id, _ = asyncio.run(_seed_basic())

    async def _add() -> None:
        sm = await _fresh_sm()
        async with sm() as s:
            assert (await s.execute(select(Opportunity))).scalars().first() is not None
            s.add(PrepPack(opportunity_id=opp_id, content_md="# Prep\n\nBe ready.",
                           stories={}, questions_to_ask=[], panel=[]))
            await s.commit()

    asyncio.run(_add())
    c = _client()
    link = re.compile(rf'<a[^>]*href="/mock/{opp_id}"[^>]*>\s*Mock interview\s*</a>')
    prep_page = c.get(f"/prep/{opp_id}").text
    assert f'href="/prep/{opp_id}?tab=practice"' in prep_page, "prep page has no Practice tab"
    # The role page's More menu is where the Mock interview link lives once a prep pack exists.
    panel = c.get(f"/opportunities/{opp_id}/role/side").text
    assert link.search(panel), "role page actions panel has no Mock interview link"


# -------------------------------------------------------------------------------------- A13
def test_a13_parked_scoring_chip_reads_failed_scoring_and_opens_needs_another_try():
    asyncio.run(_pipeline_seed())
    with _pipeline_client() as c:
        page = _pipeline_get(c, "/opportunities")
    chip = re.search(r'<a[^>]*data-chip="failed"[^>]*>.*?</a>', page, re.S)
    assert chip is not None
    label = htmllib.unescape(_visible_text(chip.group(0)))
    assert "Failed scoring" in label and 'href="/quarantine"' in chip.group(0)
    assert "Couldn't score" not in page and "Failed scoring (parked)" not in page
