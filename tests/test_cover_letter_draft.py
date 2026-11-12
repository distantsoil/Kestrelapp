"""The saved cover letter and engagement outline on the Role page (D-UI-239, SPEC-MODULES 1.6).

INVARIANTS each test pins: nothing is sent to an online model without the confirmed ask-first form,
including the plain-writing re-ask; the prompt carries only the person's proof points and chosen
resume lines (the model selects, it never invents); a reply that fails its schema is repaired once,
then kept in quarantine, and the person sees a fixed sentence; nothing is ever sent for the person;
a draft persists, saves, discards (after asking) and downloads. Only `LLMClient.complete` and the
browser PDF call are faked. Nothing reaches the network."""

from __future__ import annotations

import asyncio
import json
import re
from importlib import resources

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel.config import HostedMode
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import DocRender, Quarantine, Setting, Task
from kestrel.llm.client import ChatResult, LLMClient
from tests import role_seed
from tests.letter_helpers import form_fields, generate, letter_body

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

MODEL = "vendor/letter-model"
SECRET = "/tmp/secret-path-xyz"
BODY = (
    "Dear Hiring Team,\n\n"
    "Your three distribution centers report on three different calendars, and I have spent two "
    "years making them agree. Quartz lantern ledger is the name my team gave the weekly review "
    "that made it stick.\n\nI would like thirty minutes next week to walk through the forecast."
)
OUTLINE = (
    "## Situation\n\nThree centers, three calendars.\n\n## What we would do\n\n- Agree one "
    "calendar\n- Rebuild the forecast"
)


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False), raise_server_exceptions=False)


def _go_online() -> None:
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


class Model:
    """Replaces the model call; keeps every request it receives."""

    def __init__(self, monkeypatch, *, body=BODY, claims=None, raw=None, outline=False):
        self.requests: list[list[dict]] = []
        payload = {"body_md": body, "word_count": len(body.split()),
                   "claims_used": claims or [], "opening_alternatives": []}
        if outline:
            payload = {"body_md": OUTLINE, "word_count": 20, "claims_used": claims or [],
                       "assumptions": ["Data access is granted in week one."],
                       "exclusions": ["No data entry", "No hardware", "No training"],
                       "commercial_shape": {"model": "day_rate", "indicative_days": 20,
                                            "caveat": "Indicative only."}}
        outer = self

        async def complete(self_client, **kwargs):
            outer.requests.append(kwargs.get("messages") or [])
            return ChatResult(content=raw if raw is not None else json.dumps(payload), model=MODEL)

        monkeypatch.setattr(LLMClient, "complete", complete)

    @property
    def calls(self) -> int:
        return len(self.requests)

    def prompt(self) -> str:
        return "\n".join(m["content"] for m in self.requests[0])


def _confirm(c, opp_id) -> dict[str, str]:
    return form_fields(c.get(f"/opportunities/{opp_id}/role/cover-letter").text)  # box ticked


GEN = "/opportunities/{}/role/cover-letter/generate"


def _letter_row(opp_id):
    async def go():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(DocRender).where(
                DocRender.opportunity_id == opp_id,
                DocRender.template.in_(("cover_letter", "engagement_outline"))))).scalars().all()

    return asyncio.run(go())


# ------------------------------------------------------------------------------ the ask-first panel
def test_ask_first_panel_says_what_is_sent_and_hints_when_the_box_is_not_ticked(ids):
    _go_online()
    with _client() as c:
        html = c.get(f"/opportunities/{ids['strong']}/role/cover-letter").text
    soup = BeautifulSoup(html, "html.parser")
    send = soup.select_one('[data-testid="rl-send"]')
    assert send is not None and not send.has_attr("disabled"), "Send and draft is pressable"
    assert soup.select_one("[data-cl-tick]") is not None  # the hint for a press without the tick
    assert soup.select_one("[data-cl-ack]") is not None
    text = soup.get_text(" ", strip=True)
    assert "What will be sent" in text and "What is not sent" in text
    assert "Your name, contact details and your resume file" in text
    assert "asks the same model once more" in text  # the re-ask sentence
    assert MODEL in text and "OpenRouter" in text


def test_the_reask_goes_only_after_the_confirmed_form_and_to_the_same_model(ids, monkeypatch):
    _go_online()
    model = Model(monkeypatch, body=BODY + "\n\nI bring synergy to every team.")
    with _client() as c:
        refused = c.post(GEN.format(ids["strong"]), data={"confirmed": "1", "model": MODEL})
        assert model.calls == 0 and "Nothing was sent" in refused.headers["hx-trigger"]
        ok = generate(c, GEN.format(ids["strong"]), _confirm(c, ids["strong"]))
    assert ok.status_code == 200 and model.calls == 2  # the draft, then the one re-ask
    assert model.calls == 2


def test_a_failed_online_draft_asks_again_before_any_retry(ids, monkeypatch):
    _go_online()
    model = Model(monkeypatch, raw="not json at all")
    with _client() as c:
        failed = generate(c, GEN.format(ids["strong"]), _confirm(c, ids["strong"]))
        sent = model.calls
        retry = c.get(f"/opportunities/{ids['strong']}/role/cover-letter/start")
        assert 'data-testid="cover-letter-retry"' in failed.text
        assert 'data-testid="rl-send"' in retry.text and model.calls == sent
        # a bare retry post, with no confirmation, sends nothing
        again = generate(c, GEN.format(ids["strong"]), {})
    assert model.calls == sent and "Nothing was sent" in again.headers["hx-trigger"]


def test_local_mode_drafts_with_no_consent_and_sends_nothing_to_an_online_service(ids, monkeypatch):
    model = Model(monkeypatch)
    with _client() as c:
        start = c.get(f"/opportunities/{ids['strong']}/role/cover-letter").text
        assert "hx-trigger=\"load\"" in start and "rl-send" not in start
        r = generate(c, GEN.format(ids["strong"]), {})
    assert r.status_code == 200 and model.calls >= 1 and "Nothing was sent" not in r.text


# ----------------------------------------------------------------------- the model never invents
def test_the_prompt_carries_only_proof_points_and_chosen_resume_lines(ids, monkeypatch):
    from kestrel.services import profile_edit as pe

    pe.add_proof_points("employment", [{
        "key": "cut_late", "claim": "Cut late shipments by a fifth",
        "evidence": "Rebuilt the weekly forecast", "metric": "22 percent"}])
    model = Model(monkeypatch, claims=["cut_late"])
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
    prompt = model.prompt()
    assert "[cut_late] Cut late shipments by a fifth" in prompt  # the key the reply must cite
    assert re.search(r"- \[[^\]]+\] \S", prompt.split("SELECTED RESUME BULLETS")[1])
    for private in ("@", "linkedin", "Resume file"):
        assert private.lower() not in prompt.lower(), private


def test_claims_that_resolve_are_traced_and_ones_that_do_not_are_solid_underlined(ids, monkeypatch):
    from kestrel.services import profile_edit as pe

    pe.add_proof_points("employment", [{
        "key": "cut_late", "claim": "distribution centers report on three different calendars",
        "evidence": "agree", "metric": None}])
    Model(monkeypatch, claims=["cut_late", "proof:invented-key"])
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    soup = BeautifulSoup(r.text, "html.parser")
    assert soup.select_one('.cl-claim[data-cl-key="cut_late"]') is not None
    assert soup.select_one('[data-cl-src="cut_late"]') is not None  # the source, on a click
    alert = soup.select_one('[data-testid="cover-letter-unverified"]')
    assert alert is not None and "Unverified claims present" in alert.get_text()
    assert "proof:invented-key" not in alert.get_text()
    assert "matches none of your proof points or resume lines" in alert.get_text()
    assert re.search(r"add (it|them) as a proof point", alert.get_text())
    # the Preview carries the warning while the claim remains
    assert soup.select_one('[data-testid="cover-letter-preview-warning"]') is not None


def test_a_clean_draft_has_no_warning(ids, monkeypatch):
    Model(monkeypatch, body="Quartz lantern ledger is the name my team gave the weekly review.\n\n"
          "I would like a short call next week to walk through the forecast.")
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert 'data-testid="cover-letter-unverified"' not in r.text
    assert 'data-testid="cover-letter-preview-warning"' not in r.text


# ------------------------------------------------------------------- structured output or failure
def test_an_unreadable_reply_is_repaired_once_then_quarantined_and_shows_a_fixed_sentence(
    ids, monkeypatch
):
    model = Model(monkeypatch, raw=f"here is a letter {SECRET} but not json")
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert model.calls == 2  # the first reply and exactly one repair
    soup = BeautifulSoup(r.text, "html.parser")
    reason = soup.select_one('[data-testid="cover-letter-reason"]').get_text()
    assert "could not be read as a draft" in reason
    assert SECRET not in r.text and "here is a letter" not in r.text
    assert not _letter_row(ids["strong"]), "a failed draft saves nothing"

    async def rows():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(Quarantine))).scalars().all()

    kept = asyncio.run(rows())
    assert kept and SECRET in (kept[0].raw_response or ""), "the raw reply is preserved"


def test_the_request_exception_never_reaches_the_page(ids, monkeypatch):
    async def boom(self_client, **kwargs):
        raise RuntimeError(f"cannot open {SECRET}")

    monkeypatch.setattr(LLMClient, "complete", boom)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert r.status_code == 200 and SECRET not in r.text
    assert 'data-testid="cover-letter-failed"' in r.text


# ------------------------------------------------------------- the draft persists and is editable
def test_the_draft_persists_and_documents_offers_open_draft(ids, monkeypatch):
    Model(monkeypatch)
    with _client() as c:
        before = c.get(f"/opportunities/{ids['strong']}/documents").text
        assert "No draft yet" in before and ">Draft<" in before.replace(" ", "")
        generate(c, GEN.format(ids["strong"]), {})
        after = c.get(f"/opportunities/{ids['strong']}/documents").text
        reopened = c.get(f"/opportunities/{ids['strong']}/role/cover-letter").text
    assert "Open draft" in after and "Kestrel never sends it" in after
    assert after.count('data-testid="new-cover-letter"') == 1
    assert "Quartz lantern ledger" in reopened  # reload shows the saved draft, no new model call
    assert len(_letter_row(ids["strong"])) == 1


def test_a_draft_in_flight_is_shared_and_survives_the_requester_leaving(ids, monkeypatch):
    """INVARIANT: leaving the sheet never loses a draft, and a second request for the same role
    joins the one in flight instead of asking the model again."""
    import httpx

    from kestrel.web.app import build_app
    from kestrel.web.pages import role as role_page

    model = Model(monkeypatch, body="Quartz lantern ledger is the name my team gave the weekly "
                  "review.\n\nI would like a short call next week to walk through the forecast.")
    started: list[int] = []
    real = role_page._make_draft

    async def main():
        gate = asyncio.Event()

        async def slow(opp_id):
            started.append(opp_id)
            await gate.wait()
            return await real(opp_id)

        monkeypatch.setattr(role_page, "_make_draft", slow)
        transport = httpx.ASGITransport(app=build_app(worker=False))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            url = GEN.format(ids["strong"])
            await c.post(url, data={})  # the first requester queues it, then leaves the sheet
            worker = asyncio.create_task(role_page.run_letter_drafts())  # the serve worker
            await asyncio.sleep(0.5)
            async with get_sessionmaker()() as s:
                (task,) = (await s.execute(select(Task))).scalars().all()
                assert task.status == "leased", "the worker, not a request, is drafting"
            second = await c.post(url, data={})
            assert "data-watching" in second.text  # joined: no second task, no second call
            gate.set()
            await worker
            return await c.get(f"/opportunities/{ids['strong']}/role/cover-letter/status")

    r = asyncio.run(main())
    assert started == [ids["strong"]], "one draft in flight, joined by the second request"
    assert "Quartz lantern ledger" in r.text and model.calls == 1
    assert len(_letter_row(ids["strong"])) == 1


def test_a_second_generate_for_a_saved_draft_makes_no_second_model_call(ids, monkeypatch):
    model = Model(monkeypatch)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        calls = model.calls
        generate(c, GEN.format(ids["strong"]), {})
    assert model.calls == calls and len(_letter_row(ids["strong"])) == 1


def test_save_stores_the_text_confirms_and_rechecks_the_words(ids, monkeypatch):
    Model(monkeypatch)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        rid = _letter_row(ids["strong"])[0].id
        url = f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/save"
        r = c.post(url, data={"body_md": "A shorter letter of my own words.", "tab": "edit"})
        empty = c.post(url, data={"body_md": "   "})
    toast = json.loads(r.headers["hx-trigger"])["k-toast"]
    assert toast["message"] == "Saved."
    soup = BeautifulSoup(r.text, "html.parser")
    assert soup.find("textarea").get_text() == "A shorter letter of my own words."
    assert soup.select_one("[data-cl-saved].is-on") is not None
    assert soup.select_one("[data-cl-count]").get("data-state") == "off"  # 6 words, not 250-350
    assert "cannot be empty" in json.loads(empty.headers["hx-trigger"])["k-toast"]["message"]
    assert letter_body(_letter_row(ids["strong"])[0].id) == "A shorter letter of my own words."


def test_a_banned_phrase_typed_by_the_person_is_still_pointed_out_after_a_save(ids, monkeypatch):
    Model(monkeypatch)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        rid = _letter_row(ids["strong"])[0].id
        r = c.post(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/save",
                   data={"body_md": "I am a team player who brings synergy."})
    assert "synergy" in r.text and "team player" in r.text


def test_discard_asks_first_and_then_deletes(ids, monkeypatch):
    Model(monkeypatch)
    with _client() as c:
        draft = generate(c, GEN.format(ids["strong"]), {}).text
        soup = BeautifulSoup(draft, "html.parser")
        # The first Discard click opens a question; only its Yes button posts the delete.
        first = soup.select_one('[data-testid="cover-letter-discard"]')
        assert first.get("hx-post") is None and first.has_attr("data-cl-discard")
        yes = soup.select_one('[data-testid="cover-letter-discard-yes"]')
        assert yes["hx-post"].endswith("/discard")
        assert _letter_row(ids["strong"]), "still there until the question is answered"
        r = c.post(yes["hx-post"])
        events = json.loads(r.headers["hx-trigger"])
        assert events["k-toast"]["message"] == "Draft discarded." and "rl-letter-close" in events
        after = c.get(f"/opportunities/{ids['strong']}/documents").text
    assert not _letter_row(ids["strong"]) and "No draft yet" in after


def test_discard_works_for_a_shortlisted_role_whose_pursuit_points_at_the_draft(ids, monkeypatch):
    from kestrel.db.models import Pursuit

    Model(monkeypatch)
    with _client() as c:
        c.post(f"/opportunities/{ids['strong']}/role/shortlist")
        generate(c, GEN.format(ids["strong"]), {})
        row = _letter_row(ids["strong"])[0]

        async def linked():
            async with get_sessionmaker()() as s:
                p = (await s.execute(select(Pursuit))).scalars().first()
                return p.cover_letter_id

        assert asyncio.run(linked()) == row.id
        r = c.post(f"/opportunities/{ids['strong']}/role/cover-letter/{row.id}/discard")
    assert r.status_code == 200 and not _letter_row(ids["strong"]) and asyncio.run(linked()) is None


def test_a_letter_belongs_to_its_role_only(ids, monkeypatch):
    Model(monkeypatch)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        rid = _letter_row(ids["strong"])[0].id
        other = ids["second"]
        for verb in ("save", "discard"):
            c.post(f"/opportunities/{other}/role/cover-letter/{rid}/{verb}", data={"body_md": "x"})
        got = c.get(f"/opportunities/{other}/role/cover-letter/{rid}/download")
    assert got.status_code == 404 and _letter_row(ids["strong"])


def test_a_letter_is_never_listed_as_a_resume_version(ids, monkeypatch):
    from kestrel.web.queries import opportunity_renders

    Model(monkeypatch)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})

    async def listed():
        async with get_sessionmaker()() as s:
            return [r["render"].template for r in await opportunity_renders(s, ids["strong"])]

    assert "cover_letter" not in asyncio.run(listed())


# ----------------------------------------------------------------- preview and download PDF
def test_the_preview_is_one_true_size_page_and_download_uses_the_pdf_plumbing(ids, monkeypatch):
    from kestrel.docs import render as docrender

    Model(monkeypatch)
    made: list[str] = []

    async def fake_pdf(html: str):
        made.append(html)
        return docrender.RenderedPdf(pdf=b"%PDF-1.4\n%%EOF\n", page_count=1)

    monkeypatch.setattr(docrender, "render_pdf", fake_pdf)
    with _client() as c:
        draft = generate(c, GEN.format(ids["strong"]), {}).text
        soup = BeautifulSoup(draft, "html.parser")
        frame = soup.select_one(".rl-pvpage iframe.rl-pvframe")
        assert frame is not None and "Quartz lantern ledger" in frame["srcdoc"]
        link = soup.select_one('[data-testid="cover-letter-download"]')
        assert link.has_attr("download")
        r = c.get(link["href"])
        r2 = c.get(link["href"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"] and r.content.startswith(b"%PDF")
    assert len(made) == 1, "the same text is not rendered twice"
    assert r2.status_code == 200
    assert "Quartz lantern ledger" in made[0] and "@page" in made[0]
    assert "Larkspur" not in made[0], "no organization name outside the person's own text"


def test_download_after_an_edit_makes_the_pdf_again_and_a_missing_browser_says_so(
    ids, monkeypatch
):
    from kestrel.docs import render as docrender

    Model(monkeypatch)
    made: list[str] = []

    async def fake_pdf(html: str):
        made.append(html)
        return docrender.RenderedPdf(pdf=b"%PDF-1.4\n%%EOF\n", page_count=1)

    monkeypatch.setattr(docrender, "render_pdf", fake_pdf)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        rid = _letter_row(ids["strong"])[0].id
        dl = f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/download"
        c.get(dl)
        c.post(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/save",
               data={"body_md": "Edited words only."})
        c.get(dl)

        async def no_edge(html):
            raise RuntimeError(f"no browser at {SECRET}")

        monkeypatch.setattr(docrender, "render_pdf", no_edge)
        c.post(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/save",
               data={"body_md": "Edited again."})
        broken = c.get(dl)
    assert len(made) == 2 and "Edited words only." in made[1]
    assert broken.status_code == 503 and SECRET not in broken.text


# ------------------------------------------------------------------------ consulting outline
def test_consulting_drafts_an_engagement_outline_not_a_letter(ids, monkeypatch):
    model = Model(monkeypatch, outline=True)
    with _client() as c:
        r = generate(c, GEN.format(ids["lead"]), {})
        row = _letter_row(ids["lead"])[0]
        docs = c.get(f"/opportunities/{ids['lead']}/documents").text
    assert row.template == "engagement_outline"
    assert "engagement outline" in model.prompt().lower()
    soup = BeautifulSoup(r.text, "html.parser")
    assert "An outline for a conversation, not a priced proposal." in soup.get_text()
    assert "No data entry" in soup.select_one('[data-testid="outline-extras"]').get_text()
    assert "Engagement outline" in docs and "Open draft" in docs


# ------------------------------------------------------------------ nothing is sent for the person
def test_the_draft_view_offers_no_way_to_send_anything(ids, monkeypatch):
    Model(monkeypatch)
    with _client() as c:
        draft = generate(c, GEN.format(ids["strong"]), {}).text
    low = draft.lower()
    assert "mailto:" not in low and "apply now" not in low and "submit application" not in low
    assert "Kestrel never sends it" in draft


# ---------------------------------------------------- motion, tokens and house style of the files
def _cover_css() -> str:
    from pathlib import Path

    from kestrel import paths

    css = (Path(paths.web_static_dir()) / "css" / "pages" / "role.css").read_text(
        encoding="utf-8")
    return css[css.index("cover letter and outline (D-UI-239)"):]


def test_cover_letter_css_uses_theme_tokens_only_and_turns_motion_off_when_asked():
    css = _cover_css()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "a hard-coded color"
    assert "rgb(" not in css and "rgba(" not in css
    tail = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    for name in (".cl-pane.is-on", ".cl-saved.is-on", ".cl-confirm", ".cl-tab"):
        assert name in tail, name
    assert "—" not in css


def test_cover_letter_files_have_no_em_dash_and_no_inline_style():
    from pathlib import Path

    from kestrel import paths

    static, templates = Path(paths.web_static_dir()), Path(paths.web_templates_dir())
    docs_templates = Path(str(resources.files("kestrel.docs") / "templates"))
    for path in (static / "js" / "role.js", docs_templates / "letter.html"):
        assert "—" not in path.read_text(encoding="utf-8"), path
    for name in ("role_letter", "role_letter_ask", "role_letter_draft", "role_letter_drafting",
                 "role_letter_failed", "role_tab_documents"):
        src = (templates / "partials" / f"{name}.html").read_text(encoding="utf-8")
        assert " style=" not in src and "—" not in src, name
