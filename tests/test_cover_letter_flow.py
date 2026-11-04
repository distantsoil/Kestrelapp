"""The cover letter flow on the Role page (SPEC-MODULES 1.6, PROMPTS P4, hard rule 1 and the Privacy
section): a local route drafts without any online step; an online route names what is sent and
sends nothing until the shared consent is confirmed; an error from the model never shows the
person a raw message. Every test drives the real routes and fakes only the model call
(`LLMClient.complete`), counting calls. Nothing reaches the network."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from kestrel.config import HostedMode
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting
from kestrel.llm.client import ChatResult, LLMClient
from tests import role_seed
from tests.letter_helpers import form_fields, generate

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

MODEL = "vendor/letter-model"
LETTER = (
    "Dear Hiring Team,\n\n"
    "Your three distribution centers report on three different calendars, and I have spent two "
    "years making them agree. At Harbor Ridge Supply I rebuilt the demand forecast and late "
    "shipments fell 22 percent. Quartz lantern ledger is the name my team gave the weekly review "
    "that made it stick.\n\nThe gap I would name is tooling for freight bidding. I have not run "
    "a bidding desk. I have run the forecasts that feed one, and I can learn the rest in a "
    "quarter.\n\nI would like thirty minutes next week to walk through the forecast model."
)


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False), raise_server_exceptions=False)


def _go_online() -> None:
    """Hosted mode accepted, and the cover letter's tier pointed at an online service."""

    async def seed() -> None:
        async with get_sessionmaker()() as session:
            session.add_all([
                Setting(key="llm.hosted_mode.enabled", value=True),
                Setting(key="llm.hosted_mode.acknowledged_at", value="2026-10-15T00:00:00Z"),
                Setting(key="llm.hosted_mode.acknowledged_terms",
                        value=HostedMode().disclosure_terms()),
                Setting(key="llm.tiers.tier3.backend", value="frontier"),
                Setting(key="llm.tiers.tier3.model", value=MODEL),
            ])
            await session.commit()

    asyncio.run(seed())


class _FakeModel:
    """Stands in for the model call and counts every request (including a re-ask)."""

    def __init__(
        self, monkeypatch, *, body: str = LETTER, claims=None, error: Exception | None = None
    ):
        self.calls = 0
        payload = {
            "body_md": body,
            "word_count": len(body.split()),
            "claims_used": claims or [],
            "opening_alternatives": [],
        }
        outer = self

        async def complete(self_client, **kwargs):
            outer.calls += 1
            if error is not None:
                raise error
            return ChatResult(content=json.dumps(payload), model=MODEL)

        monkeypatch.setattr(LLMClient, "complete", complete)


def _seen(r) -> str:
    """Everything the person is shown by a response: its body and any toast it raises."""
    return r.text + " " + r.headers.get("hx-trigger", "")


def _dialog_form(html: str) -> dict[str, str]:
    """The fields the browser posts from the ask-first panel with its box ticked."""
    return form_fields(html)


URL = "/opportunities/{}/role/cover-letter"
GEN = "/opportunities/{}/role/cover-letter/generate"


def test_local_route_dialog_offers_to_draft_and_does_not_ask_for_a_hosted_provider(ids):
    with _client() as c:
        r = c.get(URL.format(ids["strong"]))
    assert r.status_code == 200
    low = r.text.lower()
    assert "cover letter" in low
    assert "hosted" not in low and "openrouter" not in low and "api key" not in low
    assert "online model" not in low


def test_online_route_dialog_names_the_company_the_model_and_the_provider(ids):
    _go_online()
    with _client() as c:
        r = c.get(URL.format(ids["strong"]))
    assert r.status_code == 200
    assert "Larkspur Logistics" in r.text
    assert MODEL in r.text
    assert "OpenRouter" in r.text


def test_online_generate_without_confirmation_never_calls_the_model(ids, monkeypatch):
    _go_online()
    fake = _FakeModel(monkeypatch)
    with _client() as c:
        none = generate(c, GEN.format(ids["strong"]), {})
        wrong_model = c.post(GEN.format(ids["strong"]),
                             data={"confirmed": "1", "model": "other/model"})
        no_dest = c.post(GEN.format(ids["strong"]), data={"confirmed": "1", "model": MODEL})
        stale = c.post(GEN.format(ids["strong"]), data={
            "confirmed": "1", "model": MODEL, "dest": "stale|https://old.example/v1|" + MODEL})
    assert fake.calls == 0
    for r in (none, wrong_model, no_dest, stale):
        assert r.status_code < 500
        assert LETTER[:40] not in r.text
    assert "Nothing was sent" in _seen(wrong_model)
    assert "Nothing was sent" in _seen(no_dest)
    assert "Nothing was sent" in _seen(stale)
    # With no confirmation at all the person is either refused or shown the ask-first dialog.
    assert "Nothing was sent" in _seen(none) or 'data-testid="rl-send"' in none.text


def test_online_generate_with_the_dialogs_confirmation_shows_the_letter_verbatim(ids, monkeypatch):
    _go_online()
    fake = _FakeModel(monkeypatch)
    with _client() as c:
        form = _dialog_form(c.get(URL.format(ids["strong"])).text)
        assert form.get("confirmed") == "1" and form.get("model") == MODEL and form.get("dest")
        r = generate(c, GEN.format(ids["strong"]), form)
    assert r.status_code == 200
    assert fake.calls >= 1
    assert "Quartz lantern ledger" in r.text
    assert "body_md=" not in r.text and "CoverLetter(" not in r.text
    assert "LetterDraft(" not in r.text and "P4Cover(" not in r.text


def test_a_model_error_shows_a_fixed_sentence_never_its_text(ids, monkeypatch):
    secret = "/tmp/secret-path-xyz"
    for online in (False, True):
        _FakeModel(monkeypatch, error=RuntimeError(f"cannot open {secret}"))
        with _client() as c:
            if online:
                _go_online()
                data = _dialog_form(c.get(URL.format(ids["strong"])).text)
            else:
                data = {}
            r = generate(c, GEN.format(ids["strong"]), data)
        assert r.status_code < 500, f"online={online}"
        assert secret not in _seen(r), f"online={online}"
        assert "secret-path" not in _seen(r)
        assert BeautifulSoup(r.text, "html.parser").get_text(strip=True) or r.headers.get(
            "hx-trigger"), "the person is told nothing"


def test_local_route_drafts_without_any_hosted_confirmation(ids, monkeypatch):
    fake = _FakeModel(monkeypatch)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert r.status_code == 200
    assert fake.calls >= 1
    assert "Quartz lantern ledger" in r.text
    assert "Nothing was sent" not in _seen(r)
    assert "body_md=" not in r.text and "CoverLetter(" not in r.text


def test_the_letter_is_shown_in_an_editor_so_the_person_edits_before_rendering(ids, monkeypatch):
    _FakeModel(monkeypatch)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    area = BeautifulSoup(r.text, "html.parser").find("textarea")
    assert area is not None and "Quartz lantern ledger" in area.get_text()


def test_a_claim_that_resolves_to_nothing_flags_the_draft_unverified(ids, monkeypatch):
    _FakeModel(monkeypatch, claims=["proof:no-such-key-anywhere"])
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert r.status_code == 200
    assert "unverified claims present" in r.text.lower()


def test_a_banned_phrase_in_the_body_is_pointed_out(ids, monkeypatch):
    _FakeModel(monkeypatch, body=LETTER + "\n\nI bring synergy to every team.")
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert r.status_code == 200
    assert "synergy" in r.text.lower()
    assert re.search(r"banned|avoid|overused|cliche|cliché", r.text, re.IGNORECASE)


# ----------------------------------------------------------------- polish and the action bar
def _letter_rows(opp_id: int):
    from sqlalchemy import select

    from kestrel.db.models import DocRender

    async def run():
        async with get_sessionmaker()() as s:
            return list((await s.execute(
                select(DocRender).where(DocRender.opportunity_id == opp_id,
                                        DocRender.template == "cover_letter")
            )).scalars())

    return asyncio.run(run())


def _text(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def test_the_ask_panel_lists_only_the_letters_own_line_and_the_button_is_pressable(ids):
    _go_online()
    with _client() as c:
        html = c.get(URL.format(ids["strong"])).text
    soup = BeautifulSoup(html, "html.parser")
    items = [li.get_text(" ", strip=True) for li in soup.select(".cl-panel li")]
    assert any("cover letter" in i for i in items)
    assert not any("engagement outlines, research summaries" in i for i in items)
    send = soup.select_one('[data-testid="rl-send"]')
    assert send is not None and send.get("disabled") is None
    hint = soup.select_one("[data-cl-tick]")
    assert hint is not None and hint.get_text(strip=True) == "Tick the box to send"


def test_an_unresolved_claim_is_described_in_words_never_by_its_key(ids, monkeypatch):
    _FakeModel(monkeypatch, claims=["invented_erp_claim"])
    with _client() as c:
        html = generate(c, GEN.format(ids["strong"]), {}).text
    assert "invented_erp_claim" not in html
    assert "matches none of your proof points or resume lines" in _text(html)


def test_a_proof_points_source_is_shown_as_separate_lines(ids, monkeypatch):
    from kestrel.services import profile_edit as pe

    pe.add_proof_points("employment", [{
        "key": "pp_forecast", "claim": "Rebuilt the demand forecast",
        "evidence": "Weekly review with each center lead",
        "metric": "Late shipments fell 22 percent"}])
    _FakeModel(monkeypatch, claims=["pp_forecast"])
    with _client() as c:
        html = generate(c, GEN.format(ids["strong"]), {}).text
    src = BeautifulSoup(html, "html.parser").select_one('.cl-src[data-cl-src="pp_forecast"]')
    assert [p.get_text(strip=True) for p in src.find_all("p")] == [
        "Rebuilt the demand forecast", "Weekly review with each center lead",
        "Late shipments fell 22 percent"]


def _bar(html: str) -> dict[str, bool]:
    soup = BeautifulSoup(html, "html.parser")
    bar = soup.select_one('[data-testid="cover-letter-actions"]')
    assert bar is not None
    return {b["data-testid"]: b.get("hidden") is None
            for b in bar.select(".cl-bar__btns > button, .cl-bar__btns > a")}


def test_the_action_bar_is_under_the_panes_with_the_right_buttons_for_each_tab(ids, monkeypatch):
    _FakeModel(monkeypatch)
    with _client() as c:
        check = generate(c, GEN.format(ids["strong"]), {}).text
        rid = _letter_rows(ids["strong"])[0].id
        edit = c.post(f"{URL.format(ids['strong'])}/{rid}/save",
                      data={"body_md": LETTER, "tab": "edit"}).text
    on_check = _bar(check)
    assert on_check == {"cover-letter-edit": True, "cover-letter-save": False,
                        "cover-letter-download": False, "cover-letter-preview-go": True,
                        "cover-letter-redraft": True, "cover-letter-discard": True}
    on_edit = _bar(edit)
    assert on_edit["cover-letter-save"] and not on_edit["cover-letter-edit"]
    assert on_edit["cover-letter-preview-go"] and on_edit["cover-letter-redraft"]
    soup = BeautifulSoup(check, "html.parser")
    assert soup.select_one('.cl-bar [data-testid="cover-letter-edit"].k-btn--primary')
    # the bar sits after the panes, and the form's Save is tied to the form by id
    assert soup.select_one('[data-testid="cover-letter-save"]')["form"] == "cl-edit-form"
    assert soup.select_one("#cl-edit-form") is not None
    assert "Your edits to this draft are replaced" in _text(check)


def test_draft_again_online_asks_first_and_the_model_is_not_called_until_confirmed(
    ids, monkeypatch
):
    _go_online()
    fake = _FakeModel(monkeypatch)
    base = URL.format(ids["strong"])
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), _dialog_form(c.get(base).text))
        first = _letter_rows(ids["strong"])
        assert len(first) == 1 and fake.calls >= 1
        before = fake.calls
        ask = c.post(f"{base}/{first[0].id}/redraft")
        form = _dialog_form(ask.text)
        assert 'data-testid="cover-letter-ask"' in ask.text and form["replace"] == str(first[0].id)
        assert fake.calls == before
        # posting the redraft without the confirmation sends nothing and keeps the draft
        refused = generate(c, GEN.format(ids["strong"]), {"replace": str(first[0].id)})
        assert fake.calls == before and 'data-testid="cover-letter-ask"' in refused.text
        assert [r.id for r in _letter_rows(ids["strong"])] == [first[0].id]
        done = generate(c, GEN.format(ids["strong"]), form)
    assert fake.calls > before and 'data-testid="cover-letter-draft"' in done.text
    rows = _letter_rows(ids["strong"])
    assert len(rows) == 1 and rows[0].id != first[0].id


def test_draft_again_on_this_computer_replaces_the_draft(ids, monkeypatch):
    fake = _FakeModel(monkeypatch)
    base = URL.format(ids["strong"])
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        old = _letter_rows(ids["strong"])[0].id
        card = c.post(f"{base}/{old}/redraft")
        assert 'data-testid="cover-letter-drafting"' in card.text
        form = _dialog_form(card.text)
        assert form["replace"] == str(old)
        calls = fake.calls
        generate(c, GEN.format(ids["strong"]), form)
    assert fake.calls > calls
    rows = _letter_rows(ids["strong"])
    assert [r.id for r in rows] != [old] and len(rows) == 1
