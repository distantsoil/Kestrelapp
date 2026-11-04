"""The review fix pass on the cover letter and outline (D-UI-239 additions).

INVARIANTS each test pins:
- Nothing is sent to an online model without the ticked confirmation, including retries, a re-ask,
  joining a draft in flight, resuming after a restart, and Draft again. A draft is a `task` row;
  one whose confirmed model or destination no longer matches is held and sends nothing.
- A file path read from the database is never used without confinement to the data folder: a
  restored or edited backup cannot make a download serve, or a delete remove, another file.
- The posting never grounds a number or name; the unverified check runs again on edited text;
  confidential mode hides the current employer in everything shown and in the PDF.

Only `LLMClient.complete` and the browser PDF call are faked. Nothing reaches the network."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import DocRender, DocSection, Opportunity, Setting, Task
from kestrel.docs import render as docrender
from kestrel.services.online_consent import APPROVAL_HOLD, CONSENT_DEST_KEY, CONSENT_KEY
from tests import role_seed
from tests.letter_helpers import form_fields, generate
from tests.test_cover_letter_draft import (
    GEN,
    MODEL,
    Model,
    _client,
    _confirm,
    _go_online,
    _letter_row,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

OTHER_MODEL = "vendor/other-model"
FAKE_PDF = b"%PDF-1.4\nfake made by the test\n%%EOF\n"
CLEAN = (
    "Quartz lantern ledger is the name my team gave the weekly review.\n\n"
    "I would like a short call next week to walk through the forecast."
)


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


@pytest.fixture
def fake_pdf(monkeypatch):
    made: list[str] = []

    async def fake(html: str):
        made.append(html)
        return docrender.RenderedPdf(pdf=FAKE_PDF, page_count=1)

    monkeypatch.setattr(docrender, "render_pdf", fake)
    import kestrel.docs.service as service

    monkeypatch.setattr(service, "render_pdf", fake)
    return made


def _db(coro_fn):
    async def go():
        async with get_sessionmaker()() as s:
            return await coro_fn(s)

    return asyncio.run(go())


async def _poll() -> list[Task]:
    async with get_sessionmaker()() as s:
        return (await s.execute(
            select(Task).where(Task.type == "letter_draft"))).scalars().all()


def _tasks() -> list[Task]:
    return _db(lambda s: _all(s, select(Task).where(Task.type == "letter_draft")))


async def _all(s, stmt):
    return (await s.execute(stmt)).scalars().all()


def _set(key: str, value) -> None:
    async def go(s):
        row = await s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value
        await s.commit()

    _db(go)


def _outside(tmp_path, name="not_kestrels.pdf"):
    """A file that is not Kestrel's, outside the data folder, that a poisoned path points at."""
    f = tmp_path / "elsewhere" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"%PDF-1.4\nSOMEONE ELSES FILE\n%%EOF\n")
    return f


# ------------------------------------------------------------ 1. a stored path is confined
def test_a_poisoned_letter_pdf_path_is_neither_served_nor_deleted(ids, monkeypatch, fake_pdf,
                                                                  tmp_path):
    """INVARIANT: a file path read from the database is never used without confinement."""
    Model(monkeypatch, body=CLEAN)
    outside = _outside(tmp_path)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        row = _letter_row(ids["strong"])[0]
        body = row.config["body_md"]
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

        async def poison(s):
            r = await s.get(DocRender, row.id)
            r.pdf_path = str(outside)
            r.config = {**r.config, "pdf_digest": digest}  # the "up to date" mark: old code serves
            await s.commit()

        _db(poison)
        got = c.get(f"/opportunities/{ids['strong']}/role/cover-letter/{row.id}/download")
        assert got.status_code == 200 and b"SOMEONE ELSES FILE" not in got.content
        assert got.content == FAKE_PDF, "treated as missing: the letter is made again"
        assert outside.exists()

        _db(poison)
        c.post(f"/opportunities/{ids['strong']}/role/cover-letter/{row.id}/discard")
    assert outside.exists() and not _letter_row(ids["strong"])


def test_a_legitimate_letter_pdf_is_still_served_and_deleted(ids, monkeypatch, fake_pdf):
    from kestrel import paths

    Model(monkeypatch, body=CLEAN)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        rid = _letter_row(ids["strong"])[0].id
        got = c.get(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/download")
        stored = _letter_row(ids["strong"])[0].pdf_path
        assert got.content == FAKE_PDF and stored.startswith(str(paths.documents_dir()))
        again = c.get(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/download")
        assert again.content == FAKE_PDF and len(fake_pdf) == 1, "the saved file is reused"
        c.post(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/discard")
    from pathlib import Path

    assert not Path(stored).exists()


def test_a_poisoned_resume_pdf_path_is_neither_served_nor_deleted(ids, fake_pdf, tmp_path):
    outside = _outside(tmp_path, "resume_elsewhere.pdf")

    async def poison(s):
        r = await s.get(DocRender, ids["render"])
        r.pdf_path = str(outside)
        await s.commit()

    _db(poison)
    with _client() as c:
        got = c.get(f"/renders/{ids['render']}/download")
        assert b"SOMEONE ELSES FILE" not in got.content
        assert got.status_code == 200 and got.content == FAKE_PDF  # made again, inside the folder
        assert outside.exists()
        _db(poison)
        c.post(f"/documents/{ids['master']}/delete", data={})
    assert outside.exists(), "deleting a document never removes a file outside the data folder"


def test_a_legitimate_resume_pdf_still_downloads_and_deletes(ids):
    from pathlib import Path

    stored = _db(lambda s: _first(s, ids["render"]))
    with _client() as c:
        got = c.get(f"/renders/{ids['render']}/download")
        assert got.status_code == 200 and got.content.startswith(b"%PDF")
        c.post(f"/documents/{ids['master']}/delete", data={})
    assert not Path(stored).exists()


async def _first(s, render_id):
    return (await s.get(DocRender, render_id)).pdf_path


def test_a_poisoned_prep_pack_pdf_path_is_not_served(ids, tmp_path):
    from kestrel.db.models import PrepPack

    outside = _outside(tmp_path, "pack_elsewhere.pdf")

    async def add(s):
        pack = PrepPack(opportunity_id=ids["strong"], content_md="x", pdf_path=str(outside))
        s.add(pack)
        await s.commit()
        return pack.id

    pack_id = _db(add)
    with _client() as c:
        got = c.get(f"/prep/{pack_id}/download")
    assert got.status_code == 404 and b"SOMEONE ELSES FILE" not in got.content


# --------------------------------------------------- 2. the draft is a task row (hard rule 5)
def _consent_of(c, opp_id) -> dict[str, str]:
    form = _confirm(c, opp_id)
    return {CONSENT_KEY: form["model"], CONSENT_DEST_KEY: form["dest"]}


def test_generate_queues_one_task_row_that_finishes_with_the_letter(ids, monkeypatch):
    Model(monkeypatch, body=CLEAN)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
    (task,) = _tasks()
    assert task.status == "done" and task.payload["opportunity_id"] == ids["strong"]
    assert task.payload["render_id"] == _letter_row(ids["strong"])[0].id


def test_a_draft_cut_short_by_a_quit_resumes_from_its_task_row(ids, monkeypatch):
    """Kill and resume: the task row survives the quit and a fresh worker completes it."""
    import httpx

    from kestrel.queue.queue import TaskQueue
    from kestrel.web.app import build_app
    from kestrel.web.pages import role as role_page

    model = Model(monkeypatch, body=CLEAN)
    real = role_page._make_draft

    async def life():
        async def hang(opp_id):
            await asyncio.Event().wait()

        monkeypatch.setattr(role_page, "_make_draft", hang)
        transport = httpx.ASGITransport(app=build_app(worker=False))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            queued = await c.post(GEN.format(ids["strong"]), data={})
            assert "data-watching" in queued.text, "the request only queues the draft"
            worker = asyncio.create_task(role_page.run_letter_drafts())
            for _ in range(100):
                await asyncio.sleep(0.1)
                if any(t.status == "leased" for t in await _poll()):
                    break
            worker.cancel()  # the app quits: the worker stops with the loop, the row stays

    asyncio.run(life())
    (left,) = _tasks()
    assert left.status == "leased" and not _letter_row(ids["strong"]) and model.calls == 0

    monkeypatch.setattr(role_page, "_make_draft", real)
    released = asyncio.run(TaskQueue(get_sessionmaker()).release_all_leased())  # what a quit does
    assert released == 1
    assert asyncio.run(role_page.run_letter_drafts()) == 1  # the next start's worker
    (after,) = _tasks()
    assert after.id == left.id and after.status == "done" and model.calls == 1
    assert len(_letter_row(ids["strong"])) == 1


def test_a_stale_online_confirmation_is_held_and_nothing_is_sent(ids, monkeypatch):
    """INVARIANT: the model changed after the dialog: the task is held, the worker sends nothing,
    and the sheet shows the ask-first panel again."""
    from kestrel.web.pages import role as role_page

    _go_online()
    model = Model(monkeypatch, body=CLEAN)
    with _client() as c:
        consent = _consent_of(c, ids["strong"])

        async def queue(s):
            from kestrel.docs import letter_task

            await letter_task.enqueue_or_join(
                s, ids["strong"], kind="cover_letter", consent=consent, replace_id=None)

        _db(queue)
        _set("llm.tiers.tier3.model", OTHER_MODEL)  # the person switched models since
        assert asyncio.run(role_page.run_letter_drafts()) == 0
        assert asyncio.run(role_page.run_letter_drafts()) == 0, "a held task is left alone"
        (task,) = _tasks()
        assert task.status == "pending" and task.last_error == APPROVAL_HOLD
        assert model.calls == 0, "nothing was sent on the stale confirmation"

        sheet = BeautifulSoup(c.get(f"/opportunities/{ids['strong']}/role/cover-letter").text,
                              "html.parser")
        assert sheet.select_one('[data-testid="cover-letter-ask"]') is not None
        assert sheet.select_one('[data-testid="cover-letter-held"]') is not None
        assert OTHER_MODEL in sheet.get_text(" ")
        docs = c.get(f"/opportunities/{ids['strong']}/documents").text
        assert "On hold" in docs and "nothing was sent" in docs.lower()

        # a bare post is still refused; only the ticked, current confirmation lets it run
        assert model.calls == 0
        generate(c, GEN.format(ids["strong"]), {})
        assert model.calls == 0
        ok = generate(c, GEN.format(ids["strong"]), _confirm(c, ids["strong"]))
    assert ok.status_code == 200 and model.calls >= 1
    assert len(_tasks()) == 1 and _tasks()[0].status == "done"
    assert _tasks()[0].payload[CONSENT_KEY] == OTHER_MODEL


def test_a_change_after_the_lease_holds_the_task_before_the_send(ids, monkeypatch):
    """The settings move between the worker's check and the send: still nothing is sent."""
    import httpx

    from kestrel.web.app import build_app
    from kestrel.web.pages import role as role_page

    _go_online()
    model = Model(monkeypatch, body=CLEAN)
    real = role_page._make_draft

    async def main():
        gate = asyncio.Event()

        async def slow(opp_id):
            await gate.wait()
            return await real(opp_id)

        monkeypatch.setattr(role_page, "_make_draft", slow)
        transport = httpx.ASGITransport(app=build_app(worker=False))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            html = (await c.get(f"/opportunities/{ids['strong']}/role/cover-letter")).text
            form = form_fields(html)
            await c.post(GEN.format(ids["strong"]), data=form)  # queues the draft
            worker = asyncio.create_task(role_page.run_letter_drafts())
            await asyncio.sleep(0.6)
            async with get_sessionmaker()() as s:
                (await s.get(Setting, "llm.tiers.tier3.model")).value = OTHER_MODEL
                await s.commit()
            gate.set()
            await worker
            return await c.get(f"/opportunities/{ids['strong']}/role/cover-letter/status")

    r = asyncio.run(main())
    assert model.calls == 0, "the model changed before the send, so nothing was sent"
    assert 'data-testid="cover-letter-ask"' in r.text and OTHER_MODEL in r.text
    (task,) = _tasks()
    assert task.status == "pending" and task.last_error == APPROVAL_HOLD


def test_joining_a_draft_in_flight_queues_no_second_task_and_no_second_call(ids, monkeypatch):
    import httpx

    from kestrel.web.app import build_app
    from kestrel.web.pages import role as role_page

    model = Model(monkeypatch, body=CLEAN)
    real = role_page._make_draft
    seen: list[str] = []

    async def main():
        gate = asyncio.Event()

        async def slow(opp_id):
            await gate.wait()
            return await real(opp_id)

        monkeypatch.setattr(role_page, "_make_draft", slow)
        transport = httpx.ASGITransport(app=build_app(worker=False))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            url = GEN.format(ids["strong"])
            await c.post(url, data={})  # queues the draft
            worker = asyncio.create_task(role_page.run_letter_drafts())  # the serve worker
            await asyncio.sleep(0.6)
            sheet = await c.get(f"/opportunities/{ids['strong']}/role/cover-letter")
            seen.append(sheet.text)  # reopening while drafting: the drafting state, not a start
            second = await c.post(url, data={})
            seen.append(second.text)  # a second click joins; it neither queues nor sends
            tasks_now = len(await _poll())
            gate.set()
            await worker
            status = f"/opportunities/{ids['strong']}/role/cover-letter/status"
            return tasks_now, await c.get(status)

    mid_tasks, r = asyncio.run(main())
    assert mid_tasks == 1 and len(_tasks()) == 1
    assert model.calls == 1 and "Quartz lantern ledger" in r.text
    soup = BeautifulSoup(seen[0], "html.parser")
    card = soup.select_one('[data-testid="cover-letter-drafting"]')
    assert card is not None and card.has_attr("data-watching") and card.get("hx-post") is None
    joined = BeautifulSoup(seen[1], "html.parser").select_one(
        '[data-testid="cover-letter-drafting"]')
    assert joined is not None and joined.has_attr("data-watching")


def test_an_online_join_with_a_different_confirmation_is_refused(ids, monkeypatch):
    """INVARIANT: a second click cannot ride on, or replace, the confirmation the task holds."""
    import httpx

    from kestrel.web.app import build_app
    from kestrel.web.pages import role as role_page

    _go_online()
    model = Model(monkeypatch, body=CLEAN)
    real = role_page._make_draft

    async def form_for(c):
        html = (await c.get(f"/opportunities/{ids['strong']}/role/cover-letter")).text
        return form_fields(html)

    async def main():
        gate = asyncio.Event()

        async def slow(opp_id):
            await gate.wait()
            return await real(opp_id)

        monkeypatch.setattr(role_page, "_make_draft", slow)
        transport = httpx.ASGITransport(app=build_app(worker=False))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            form = await form_for(c)
            await c.post(GEN.format(ids["strong"]), data=form)  # queues the draft
            worker = asyncio.create_task(role_page.run_letter_drafts())
            await asyncio.sleep(0.6)
            async with get_sessionmaker()() as s:  # the person changes the model, confirms again
                (await s.get(Setting, "llm.tiers.tier3.model")).value = OTHER_MODEL
                await s.commit()
            newer = {**form, "model": OTHER_MODEL, "dest": form["dest"].replace(MODEL, OTHER_MODEL)}
            join = await c.post(GEN.format(ids["strong"]), data=newer)
            rows = await _poll()
            gate.set()
            await worker
            return join, rows

    join, rows = asyncio.run(main())
    assert len(rows) == 1 and rows[0].payload[CONSENT_KEY] == MODEL, "the task is unchanged"
    toast = json.loads(join.headers["hx-trigger"])["k-toast"]["message"]
    assert "Nothing was sent" in toast and "different confirmation" in toast
    assert model.calls == 0, "and the stale task did not send either (held at the send)"


def test_a_failed_draft_leaves_its_reason_on_the_documents_row(ids, monkeypatch):
    Model(monkeypatch, raw="this is not json")
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        docs = BeautifulSoup(c.get(f"/opportunities/{ids['strong']}/documents").text,
                             "html.parser")
        opened = c.get(f"/opportunities/{ids['strong']}/role/cover-letter").text
    row = docs.select_one('[data-testid="docrow"]:has([data-testid="new-cover-letter"])')
    assert "could not be read as a draft" in row.get_text(" ")
    assert row.select_one('[data-testid="new-cover-letter"]').get_text(strip=True) == "Try again"
    assert 'data-testid="cover-letter-failed"' in opened, "the sheet shows it too"


# ------------------------------------------------------ 3. a refusal shows its reason
def test_a_hosted_refusal_shows_the_refusal_not_an_unreadable_reply(ids, monkeypatch):
    from kestrel.services import online_consent
    from kestrel.services.online_consent import HELD

    _go_online()
    _set("confidential_mode", True)

    from kestrel.docs import current_employer
    from kestrel.privacy import EmployerMap

    async def nobody(session, **kwargs):  # confidential mode with no employer to hide
        return EmployerMap()

    monkeypatch.setattr(current_employer, "load_employer_map", nobody)
    model = Model(monkeypatch, body=CLEAN)
    # The settings moved after the dialog's checks; the send's own plan is the last guard.
    monkeypatch.setattr(online_consent, "consent_problem", lambda route, form: None)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {"confirmed": "1", "model": MODEL,
                                                    "dest": "frontier|x|" + MODEL})
    soup = BeautifulSoup(r.text, "html.parser")
    reason = soup.select_one('[data-testid="cover-letter-reason"]').get_text(" ", strip=True)
    assert model.calls == 0, "refused before anything was sent"
    assert reason == HELD
    assert "could not be read" not in r.text
    assert "See the reply" not in r.text and "/quarantine" not in r.text


# ------------------------------------------- 4. the posting is not a grounding source
def test_a_number_from_the_posting_restated_as_the_persons_claim_is_unverified(ids, monkeypatch):
    async def add(s):
        opp = await s.get(Opportunity, ids["strong"])
        opp.description_md += "\n\nYou will own a 4,712 SKU catalog and a $83 million budget."
        await s.commit()

    _db(add)
    Model(monkeypatch, body="I have run a $83 million budget and a 4,712 SKU catalog.\n\n"
          "I would like a short call next week.")
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    soup = BeautifulSoup(r.text, "html.parser")
    alert = soup.select_one('[data-testid="cover-letter-unverified"]')
    assert alert is not None
    text = alert.get_text(" ")
    assert "$83 million" in text
    assert soup.select_one('[data-testid="cover-letter-preview-warning"]') is not None


# ------------------------------------------------- 5. rechecked after an edit; tighter dotted line
def test_a_number_added_by_hand_is_flagged_after_the_edit_and_before_download(ids, monkeypatch):
    Model(monkeypatch, body=CLEAN)
    with _client() as c:
        first = generate(c, GEN.format(ids["strong"]), {}).text
        assert 'data-testid="cover-letter-unverified"' not in first
        rid = _letter_row(ids["strong"])[0].id
        saved = c.post(
            f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/save",
            data={"body_md": CLEAN + "\n\nI cut costs by 73 percent at my last company.",
                  "tab": "edit"})
        reopened = c.get(f"/opportunities/{ids['strong']}/role/cover-letter").text
    for html in (saved.text, reopened):
        soup = BeautifulSoup(html, "html.parser")
        alert = soup.select_one('[data-testid="cover-letter-unverified"]')
        assert alert is not None and "73" in alert.get_text()
        assert any("73" in u.get_text() for u in soup.select("u.cl-unv"))
        assert soup.select_one('[data-testid="cover-letter-preview-warning"]') is not None


def test_an_edit_that_adds_a_detail_from_the_persons_own_proof_stops_being_flagged(
    ids, monkeypatch
):
    from kestrel.services import profile_edit as pe

    pe.add_proof_points("employment", [{
        "key": "cut_late", "claim": "Cut late shipments by 22 percent",
        "evidence": "Rebuilt the weekly forecast", "metric": "22 percent"}])
    Model(monkeypatch, body=CLEAN)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        rid = _letter_row(ids["strong"])[0].id
        r = c.post(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/save",
                   data={"body_md": CLEAN + "\n\nI cut late shipments by 22 percent."})
    assert 'data-testid="cover-letter-unverified"' not in r.text


def test_a_sentence_is_traced_only_when_it_restates_a_cited_source():
    from kestrel.docs.letters import annotate, traced

    sources = {"k": {"label": "Your proof point", "parts": [],
                     "text": "Rebuilt the weekly distribution centers review calendar"}}
    restates = "I rebuilt the weekly review calendar for the distribution centers."
    incidental = (
        "The weekly review of the distribution centers is held on Fridays by a rotating group "
        "of managers drawn from every region of the company."
    )
    para = annotate(restates, sources, ["k"], [], [])[0]
    assert para[0]["kind"] == "claim" and para[0]["key"] == "k"
    para = annotate(incidental, sources, ["k"], [], [])[0]
    assert all(seg["kind"] == "plain" for seg in para), "three shared words do not trace it"
    uncited = annotate(restates, sources, [], [], [])[0]
    assert uncited[0]["kind"] == "plain", "a source the draft did not cite never traces"
    assert traced(3, 6) and not traced(3, 7) and not traced(2, 2)


# ------------------------------------------------- 6. confidential mode hides the employer
def test_confidential_mode_hides_the_employer_in_the_letter_everywhere(ids, monkeypatch,
                                                                       fake_pdf):
    from kestrel.db.models import DocMaster
    from kestrel.services import profile_edit as pe

    async def employer(s):
        s.add(Setting(key="confidential_mode", value=True))
        master = DocMaster(track="employment", name="Old resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label="Harbor Ledger", org_label_redacted="a freight company"))
        await s.commit()

    _db(employer)
    pe.add_proof_points("employment", [{
        "key": "cut_late", "claim": "Cut late shipments at Harbor Ledger by a fifth",
        "evidence": "Rebuilt the weekly forecast for Harbor Ledger", "metric": None}])
    Model(monkeypatch, claims=["cut_late"], body=(
        "At Harbor Ledger I cut late shipments by a fifth and rebuilt the weekly forecast.\n\n"
        "I would like a short call next week."))
    with _client() as c:
        draft = generate(c, GEN.format(ids["strong"]), {}).text
        rid = _letter_row(ids["strong"])[0].id
        edit = c.post(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/save",
                      data={"body_md": "Unchanged-looking text.", "tab": "edit"}).text
        reopened = c.get(f"/opportunities/{ids['strong']}/role/cover-letter").text
        c.get(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/download")
    for html in (draft, reopened):
        assert "Harbor Ledger" not in html, "check view, edit text, preview and sources"
        assert "a freight company" in html
        soup = BeautifulSoup(html, "html.parser")
        assert "Harbor Ledger" not in soup.select_one("iframe.rl-pvframe")["srcdoc"]
        src = soup.select_one('[data-cl-src="cut_late"]')
        assert src is not None and "Harbor Ledger" not in src.get_text()
    assert "Harbor Ledger" not in edit
    assert fake_pdf, "a PDF was made, so the check below looks at something"
    assert all("Harbor Ledger" not in h for h in fake_pdf)


def test_the_confidential_pdf_is_made_from_the_hidden_text(ids, monkeypatch, fake_pdf):
    from kestrel.db.models import DocMaster

    async def employer(s):
        s.add(Setting(key="confidential_mode", value=True))
        master = DocMaster(track="employment", name="Old resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label="Harbor Ledger", org_label_redacted="a freight company"))
        await s.commit()

    _db(employer)
    Model(monkeypatch, body="At Harbor Ledger I ran the weekly forecast review.\n\n"
          "I would like a short call next week.")
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        rid = _letter_row(ids["strong"])[0].id
        r = c.get(f"/opportunities/{ids['strong']}/role/cover-letter/{rid}/download")
    assert r.status_code == 200 and len(fake_pdf) == 1
    assert "Harbor Ledger" not in fake_pdf[0] and "a freight company" in fake_pdf[0]


# --------------------------------------------------------------------------- 7. a test path
def test_the_css_the_cover_letter_tests_read_is_found_from_the_package_not_the_cwd(
    monkeypatch, tmp_path
):
    from tests.test_cover_letter_draft import _cover_css

    monkeypatch.chdir(tmp_path)  # a path relative to the working directory would not resolve here
    assert _cover_css().startswith("cover letter and outline (D-UI-239)")


# ============================================================== review round 3 (D-TEST-6)
SHEET = "/opportunities/{}/role/cover-letter"


def test_an_unticked_box_posts_no_confirmation_and_the_server_refuses(ids, monkeypatch):
    """INVARIANT: the server refuses any online send whose form did not carry the ticked box;
    JavaScript is a convenience, never the guard. The panel used to post a hidden confirmed=1
    whatever the box said, so a press that got past the script sent."""
    _go_online()
    model = Model(monkeypatch, body=CLEAN)
    with _client() as c:
        html = c.get(SHEET.format(ids["strong"])).text
        soup = BeautifulSoup(html, "html.parser")
        assert soup.select('input[type="hidden"][name="confirmed"]') == [], "no hidden stand-in"
        box = soup.select_one('[data-testid="cover-letter-ack"]')
        assert box["name"] == "confirmed" and box["value"] == "1"

        unticked = form_fields(html, ticked=False)  # what the browser posts, box left empty
        assert "confirmed" not in unticked and unticked["model"] == MODEL and unticked["dest"]
        refused = generate(c, GEN.format(ids["strong"]), unticked)
        assert model.calls == 0 and _tasks() == [], "nothing sent, no task row"
        assert 'data-testid="cover-letter-ask"' in refused.text
        assert "Nothing was sent" in refused.text + refused.headers.get("hx-trigger", "")

        ticked = form_fields(html)
        assert ticked["confirmed"] == "1"
        ok = generate(c, GEN.format(ids["strong"]), ticked)
    assert model.calls >= 1 and 'data-testid="cover-letter-draft"' in ok.text
    assert [t.status for t in _tasks()] == ["done"]


def test_a_request_only_queues_the_draft_the_worker_runs_it(ids, monkeypatch):
    """The Role page used to start its own runner from the request, outside the worker's
    drain-one-tier-then-the-next order and its pause. A request now queues and answers."""
    import time

    model = Model(monkeypatch, body=CLEAN)
    with _client() as c:
        r = c.post(GEN.format(ids["strong"]), data={})
        assert "data-watching" in r.text
        time.sleep(1.0)  # a request-side runner would have drafted by now
        assert model.calls == 0 and [t.status for t in _tasks()] == ["pending"]
        from tests.letter_helpers import drain_letters

        assert drain_letters() == 1
    assert model.calls == 1 and [t.status for t in _tasks()] == ["done"]


def test_a_paused_worker_drains_no_letter_and_resuming_drains_it(ids, monkeypatch):
    from kestrel.web import app as app_mod

    model = Model(monkeypatch, body=CLEAN)
    with _client() as c:
        c.post(GEN.format(ids["strong"]), data={})
        app_mod.set_paused(True)
        try:
            from tests.letter_helpers import drain_letters

            assert drain_letters() == 0 and model.calls == 0
            assert [t.status for t in _tasks()] == ["pending"]
        finally:
            app_mod.set_paused(False)
        assert drain_letters() == 1
    assert model.calls == 1


def test_the_worker_loop_drains_letters_after_both_tiers(ids, monkeypatch):
    """Model swap thrash: the loop finishes tier 1, then tier 2, and only then the letters."""
    from kestrel.pipeline.worker import Worker
    from kestrel.web import app as app_mod
    from kestrel.web.pages import role as role_page

    order: list[str] = []
    stop = asyncio.Event()

    async def tier(self, name, *a, **k):
        order.append(name)

    async def letters(*, stop=None, **k):
        order.append("letters")
        stop.set()
        return 0

    async def embed(self, stats, *a, **k):
        pass

    monkeypatch.setattr(Worker, "drain_tier", tier)
    monkeypatch.setattr(Worker, "drain_embed", embed)
    monkeypatch.setattr(role_page, "run_letter_drafts", letters)
    asyncio.run(asyncio.wait_for(app_mod._worker_loop(stop), timeout=60.0))
    assert order == ["t1", "t2", "letters"], order


def test_a_crashed_local_draft_waits_for_the_next_pass_instead_of_spinning(ids, monkeypatch):
    from kestrel.web.pages import role as role_page

    seen: list[int] = []

    async def boom(opp_id):
        seen.append(opp_id)
        raise RuntimeError("the model process died")

    monkeypatch.setattr(role_page, "_make_draft", boom)
    with _client() as c:
        c.post(GEN.format(ids["strong"]), data={})
        asyncio.run(role_page.run_letter_drafts())
        assert len(seen) == 1, "one attempt per pass, not three in a row"
        (task,) = _tasks()
        assert task.status == "pending" and task.attempts == 1
        asyncio.run(role_page.run_letter_drafts())  # the next worker cycle tries again
    assert len(seen) == 2


def test_a_crashed_online_draft_is_failed_never_sent_again(ids, monkeypatch):
    """INVARIANT: an online prompt that may have been sent is never sent again by itself. The
    person sees why on the Documents row and drafts again through the ask-first panel."""
    from kestrel.docs.letter_task import STOPPED_ONLINE
    from kestrel.web.pages import role as role_page

    _go_online()
    seen: list[int] = []

    async def boom(opp_id):
        seen.append(opp_id)
        raise RuntimeError("the connection dropped after the request left")

    monkeypatch.setattr(role_page, "_make_draft", boom)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), _confirm(c, ids["strong"]))
        asyncio.run(role_page.run_letter_drafts())
        asyncio.run(role_page.run_letter_drafts())
        assert len(seen) == 1, "no automatic second send"
        (task,) = _tasks()
        assert task.status == "dead" and task.last_error == STOPPED_ONLINE
        docs = c.get(f"/opportunities/{ids['strong']}/documents").text
        sheet = c.get(SHEET.format(ids["strong"])).text
    assert STOPPED_ONLINE == "The draft stopped partway. Draft again to try once more."
    assert STOPPED_ONLINE in docs and STOPPED_ONLINE in sheet


def test_an_online_draft_cut_off_mid_send_is_failed_after_the_restart(ids, monkeypatch):
    """Quit while the request is out: the row says the send started, so the restart fails it
    with the reason instead of sending the same material twice."""
    from kestrel.docs.letter_task import SENDING_KEY, STOPPED_ONLINE
    from kestrel.queue.queue import TaskQueue
    from kestrel.web.pages import role as role_page

    _go_online()
    model = Model(monkeypatch, body=CLEAN)
    real = role_page._make_draft

    async def life():
        async def hang(opp_id):
            await asyncio.Event().wait()

        monkeypatch.setattr(role_page, "_make_draft", hang)
        import httpx

        from kestrel.web.app import build_app

        transport = httpx.ASGITransport(app=build_app(worker=False))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            html = (await c.get(SHEET.format(ids["strong"]))).text
            await c.post(GEN.format(ids["strong"]), data=form_fields(html))
            worker = asyncio.create_task(role_page.run_letter_drafts())
            for _ in range(100):
                await asyncio.sleep(0.1)
                if any(t.status == "leased" for t in await _poll()):
                    break
            worker.cancel()

    asyncio.run(life())
    (left,) = _tasks()
    assert left.status == "leased" and left.payload.get(SENDING_KEY) is True

    monkeypatch.setattr(role_page, "_make_draft", real)
    asyncio.run(TaskQueue(get_sessionmaker()).release_all_leased())
    assert asyncio.run(role_page.run_letter_drafts()) == 0
    (after,) = _tasks()
    assert after.status == "dead" and after.last_error == STOPPED_ONLINE and model.calls == 0


def test_an_online_draft_that_never_started_resumes_and_clears_its_marker(ids, monkeypatch):
    from kestrel.docs.letter_task import SENDING_KEY
    from kestrel.web.pages import role as role_page

    _go_online()
    model = Model(monkeypatch, body=CLEAN)
    with _client() as c:
        c.post(GEN.format(ids["strong"]), data=_confirm(c, ids["strong"]))  # queued, not run
        assert asyncio.run(role_page.run_letter_drafts()) == 1
    (task,) = _tasks()
    assert task.status == "done" and SENDING_KEY not in task.payload and model.calls == 1


def test_saving_an_edit_deletes_the_old_pdf_and_discard_leaves_none(ids, monkeypatch, fake_pdf):
    from kestrel import paths

    Model(monkeypatch, body=CLEAN)
    base = SHEET.format(ids["strong"])
    seeded = set(paths.documents_dir().rglob("*.pdf"))  # the seeded resume's file is not ours
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        rid = _letter_row(ids["strong"])[0].id
        c.get(f"{base}/{rid}/download")
        stale = _letter_row(ids["strong"])[0].pdf_path
        from pathlib import Path

        assert Path(stale).is_file()
        c.post(f"{base}/{rid}/save", data={"body_md": "A shorter letter of my own words.",
                                          "tab": "edit"})
        assert not Path(stale).exists(), "the PDF of the old text is gone, not orphaned"
        assert _letter_row(ids["strong"])[0].pdf_path is None
        c.get(f"{base}/{rid}/download")  # a new PDF for the new text
        c.post(f"{base}/{rid}/discard")
    assert set(paths.documents_dir().rglob("*.pdf")) == seeded, "no letter PDF remains"


def test_a_held_letter_task_is_not_live_work_for_polling_or_the_rail(ids, monkeypatch):
    """A draft waiting for the person's confirmation is not something the worker is doing: the
    Today list must not keep refreshing for it."""
    from kestrel.db.base import utcnow_iso
    from kestrel.queue.queue import in_flight

    _go_online()
    Model(monkeypatch, body=CLEAN)
    with _client() as c:
        consent = {CONSENT_KEY: MODEL, CONSENT_DEST_KEY: "stale|x|" + MODEL}

        async def queue(s):
            from kestrel.docs import letter_task

            await letter_task.enqueue_or_join(
                s, ids["strong"], kind="cover_letter", consent=consent, replace_id=None)

        _db(queue)
        live = _db(lambda s: _all(s, select(Task.id).where(in_flight(utcnow_iso()))))
        assert live == [1], "a queued draft is live work"
        assert "every 10s" in _list_trigger(c.get("/opportunities").text), "a live task polls"
        _set("llm.tiers.tier3.model", OTHER_MODEL)  # the confirmation is now stale
        assert asyncio.run(__import__("kestrel.web.pages.role", fromlist=["x"])
                           .run_letter_drafts()) == 0
        (task,) = _tasks()
        assert task.last_error == APPROVAL_HOLD
        assert _db(lambda s: _all(s, select(Task.id).where(in_flight(utcnow_iso())))) == []
        page = c.get("/opportunities")
        assert page.status_code == 200
        assert "every 10s" not in _list_trigger(page.text), "the held draft keeps nothing polling"


def _list_trigger(html: str) -> str:
    """When the roles list refreshes itself (its own hx-trigger, not the page's status chip)."""
    return BeautifulSoup(html, "html.parser").select_one("#pipeline-list")["hx-trigger"]


def test_every_occurrence_of_an_unverified_phrase_is_marked():
    from kestrel.docs.letters import _mark

    out = _mark("We cut 40% of delays and then 40% again.", "plain", "", ["40%"], [])
    marked = [seg["text"] for seg in out if seg["kind"] == "unverified"]
    assert marked == ["40%", "40%"]
    assert "".join(seg["text"] for seg in out) == "We cut 40% of delays and then 40% again."


def test_a_stored_path_is_compared_without_regard_to_case_where_the_disk_does_not(
    monkeypatch, tmp_path
):
    """macOS and Windows volumes ignore case: the same folder written two ways must be accepted,
    and a sibling that merely starts with the same letters must still be refused."""
    from kestrel import paths

    root = tmp_path / "Documents"
    root.mkdir()
    monkeypatch.setattr(paths.os.path, "normcase", lambda p: p.lower())  # a case-blind volume
    assert paths.confined_file(str(tmp_path / "DOCUMENTS" / "a.pdf"), root) is not None
    assert paths.confined_file(str(tmp_path / "documents-other" / "a.pdf"), root) is None
    assert paths.confined_file(str(root), root) is not None


def test_a_failing_letter_pass_does_not_stop_the_rest_of_the_cycle(ids, monkeypatch):
    """The letter step is isolated: when it raises (say a table is missing), training and the
    other steps after it still run in the same cycle."""
    from kestrel.learning import training
    from kestrel.pipeline.worker import Worker
    from kestrel.web import app as app_mod
    from kestrel.web.pages import role as role_page

    stop = asyncio.Event()
    reached: list[str] = []

    async def noop(self, *a, **k):
        pass

    async def letters(*, stop=None, **k):
        raise RuntimeError("no such table: task")

    async def train(*a, **k):
        reached.append("training")
        stop.set()

    monkeypatch.setattr(Worker, "drain_tier", noop)
    monkeypatch.setattr(Worker, "drain_embed", noop)
    monkeypatch.setattr(role_page, "run_letter_drafts", letters)
    monkeypatch.setattr(training, "run_cycle", train)
    asyncio.run(asyncio.wait_for(app_mod._worker_loop(stop), timeout=60.0))
    assert reached == ["training"]


def test_a_lost_lease_means_the_online_draft_is_not_sent(ids, monkeypatch):
    """INVARIANT: an online draft whose lease is no longer ours is never sent. The send marker
    cannot be written, so the pass leaves the row to its owner and the model is not called."""
    from kestrel.docs import letter_task
    from kestrel.queue.queue import TaskQueue

    runs: list[int] = []

    async def run(task):
        runs.append(task.id)
        return {"ok": True, "render_id": 1}

    async def check(task):
        # Between the lease and the send marker another worker takes the row.
        async with get_sessionmaker()() as s:
            row = await s.get(Task, task.id)
            row.worker_id = "another-worker"
            await s.commit()
        return None

    async def go():
        sm = get_sessionmaker()
        tid = await TaskQueue(sm).enqueue(
            letter_task.TASK_TYPE,
            {"opportunity_id": ids["strong"], CONSENT_KEY: "vendor/letter-model"},
        )
        done = await letter_task.drain(sm, run, check)
        async with sm() as s:
            return done, await s.get(Task, tid)

    done, row = asyncio.run(go())
    assert runs == [] and done == 0, "the draft was sent although the lease was lost"
    assert row.worker_id == "another-worker" and letter_task.SENDING_KEY not in (row.payload or {})
