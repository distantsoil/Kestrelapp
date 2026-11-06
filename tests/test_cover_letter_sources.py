"""Hard rule 2 on a letter's claims (issue #124, D-LETTER-8): the check view shows the exact text
each claim key carried in the prompt, never a later edit of it, and says when a source changed or
went since; one kind of source can never overwrite another under the same key; and a real proof
point key or resume-line key that was not sent is flagged unverified. Invented text only; nothing
reaches the network."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select, update

from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import DocVariant, Letter
from kestrel.llm.client import ChatResult, LLMClient
from kestrel.services import profile_edit as pe
from tests import role_seed
from tests.letter_helpers import generate
from tests.test_cover_letter_guards import GEN, MODEL, SHORT, URL, _client

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

POINT = {"key": "pp_forecast", "claim": "Rebuilt the invented demand forecast",
         "evidence": "Weekly review with each invented center lead",
         "metric": "Late shipments fell 22 percent"}


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _db(fn):
    async def go():
        async with get_sessionmaker()() as s:
            out = await fn(s)
            await s.commit()
            return out

    return asyncio.run(go())


class _Model:
    """The model call. `claims` is a list, or a function of the prompt the model was sent (a
    model guessing real keys it was not given). Records each prompt."""

    def __init__(self, monkeypatch, claims):
        self.prompts: list[str] = []
        outer = self

        async def complete(self_client, **kwargs):
            prompt = "\n".join(str(m.get("content") or "") for m in kwargs.get("messages") or [])
            outer.prompts.append(prompt)
            cited = claims(prompt) if callable(claims) else list(claims)
            payload = {"body_md": SHORT, "word_count": len(SHORT.split()),
                       "claims_used": cited, "opening_alternatives": []}
            return ChatResult(content=json.dumps(payload), model=MODEL)

        monkeypatch.setattr(LLMClient, "complete", complete)


def _source(html: str, key: str):
    return BeautifulSoup(html, "html.parser").select_one(f'.cl-src[data-cl-src="{key}"]')


def _lines(src) -> list[str]:
    return [p.get_text(" ", strip=True) for p in src.find_all("p")]


def _view(c, opp_id: int) -> str:
    return c.get(URL.format(opp_id) + "/status").text


def _resume_keys() -> dict[str, str]:
    """Every resume line a prompt may carry, by the key the prompt shows it under."""
    async def go(s):
        rows = (await s.execute(select(DocVariant.key, DocVariant.text))).all()
        return {k: t for k, t in rows}

    return _db(go)


# ============================================== (a) the view shows what was sent, not an edit
def test_a_proof_point_edited_after_drafting_shows_the_sent_text_and_a_changed_note(
    ids, monkeypatch
):
    pe.add_proof_points("employment", [POINT])
    model = _Model(monkeypatch, ["pp_forecast"])
    with _client() as c:
        first = generate(c, GEN.format(ids["strong"]), {}).text
        assert "[pp_forecast]" in model.prompts[0]
        assert _lines(_source(first, "pp_forecast")) == [
            POINT["claim"], POINT["evidence"], POINT["metric"]]
        pe.save_section("employment", "proof_points", {"proof_points": [
            {**POINT, "claim": "Invented claim written after the draft"}]}, keep_undo=False)
        after = _view(c, ids["strong"])
        src = _source(after, "pp_forecast")
        assert src is not None
        assert POINT["claim"] in _lines(src)  # the words the draft was written from
        assert "Invented claim written after the draft" not in after
        assert src.select_one('[data-testid="cover-letter-source-changed"]') is not None
        assert BeautifulSoup(after, "html.parser").select_one(
            '[data-testid="cover-letter-unresolved"]') is None  # still traced, not flagged
        pe.save_section("employment", "proof_points", {"proof_points": []}, keep_undo=False)
        gone = _view(c, ids["strong"])
        src = _source(gone, "pp_forecast")
        assert src is not None and POINT["claim"] in _lines(src)
        assert src.select_one('[data-testid="cover-letter-source-gone"]') is not None


def test_a_resume_line_edited_after_drafting_shows_the_sent_text(ids, monkeypatch):
    keys = _resume_keys()

    def cite_first_sent(prompt: str) -> list[str]:
        return [next(k for k in keys if f"[{k}]" in prompt)]

    model = _Model(monkeypatch, cite_first_sent)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        key = cite_first_sent(model.prompts[0])[0]
        sent_text = re.search(rf"^- \[{key}\] (.+)$", model.prompts[0], re.M).group(1)

        async def edit(s):
            await s.execute(update(DocVariant).where(DocVariant.key == key)
                            .values(text="Invented line rewritten after the draft."))

        _db(edit)
        html = _view(c, ids["strong"])
    src = _source(html, key)
    assert src is not None and sent_text in _lines(src)
    assert "Invented line rewritten after the draft." not in html
    assert src.select_one('[data-testid="cover-letter-source-changed"]') is not None


def test_the_snapshot_holds_the_exact_text_sent_per_key(ids, monkeypatch):
    pe.add_proof_points("employment", [POINT])
    _Model(monkeypatch, ["pp_forecast"])
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})

    async def stored(s):
        return (await s.execute(select(Letter))).scalars().one().extras

    extras = _db(stored)
    sent = extras["sent_sources"]
    assert sorted(sent) == extras["sent_keys"]
    assert sent["pp_forecast"]["parts"] == [POINT["claim"], POINT["evidence"], POINT["metric"]]


# ======================================================= (b) one kind never overwrites another
def test_a_proof_point_keyed_like_a_resume_line_refuses_the_draft_before_any_send(
    ids, monkeypatch
):
    keys = _resume_keys()
    # Only lines that are sent can collide; with every resume line shadowed, one surely is.
    pe.save_section("employment", "proof_points", {"proof_points": [
        {"key": k, "claim": f"Invented forecast planning claim {n}", "evidence": "",
         "metric": None} for n, k in enumerate(keys)]}, keep_undo=False)
    model = _Model(monkeypatch, [])
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert model.prompts == []  # refused before anything was sent
    assert "two of your sources use the same key" in r.text.lower()

    async def letters(s):
        return (await s.execute(select(Letter))).scalars().all()

    assert _db(letters) == []


def test_the_view_never_shows_one_kind_under_another_kinds_key():
    """The view's map is built with strict=False: a key used twice is left out, so a claim that
    names it is flagged, never shown with the other source's words."""
    from kestrel.docs.letters import SourceKeyCollision, source_map

    proofs = [{"key": "r1b1", "claim": "Invented proof words", "evidence": "", "metric": ""}]
    variants = [("r1b1", "Invented resume words"), ("r1b2", "Other invented line")]
    stories = [{"key": "r1b2", "title": "t", "action": "Invented story action", "result": ""}]
    with pytest.raises(SourceKeyCollision):
        source_map(proofs, variants, stories)
    loose = source_map(proofs, variants, stories, strict=False)
    assert loose == {}


# ======================================= (c) real keys that were not sent are flagged unverified
def test_a_real_proof_point_key_and_resume_line_key_not_sent_are_unverified(ids, monkeypatch):
    pe.add_proof_points("employment", [
        {"key": f"pp_garden_{i}", "claim": f"Invented tulip bed number {i} grew", "evidence": "",
         "metric": None} for i in range(4)
    ] + [{"key": "pp_hit", "claim": "Rebuilt the demand forecast for distribution centers",
          "evidence": "", "metric": None}])
    resume = _resume_keys()
    real = [f"pp_garden_{i}" for i in range(4)] + ["pp_hit", *resume]

    def guess_unsent(prompt: str) -> list[str]:
        unsent = [k for k in real if f"[{k}]" not in prompt]
        proof = next(k for k in unsent if k.startswith("pp_"))
        line = next(k for k in unsent if k in resume)
        return [proof, line]

    model = _Model(monkeypatch, guess_unsent)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    proof, line = guess_unsent(model.prompts[0])
    assert f"[{proof}]" not in model.prompts[0] and f"[{line}]" not in model.prompts[0]
    assert "unverified claims present" in r.text.lower()
    soup = BeautifulSoup(r.text, "html.parser")
    unresolved = soup.select_one('[data-testid="cover-letter-unresolved"]')
    assert unresolved is not None
    assert _source(r.text, proof) is None and _source(r.text, line) is None

    async def stored(s):
        return (await s.execute(select(Letter))).scalars().one().claims_used

    assert _db(stored) == [proof, line]  # flagged and stored, never dropped
    assert re.search(r"2 claims .* match none", unresolved.get_text())


# ============ 0.7.6 review item 5: a key that collides after the draft was not deleted
def test_a_source_added_after_the_draft_under_the_same_key_is_not_called_deleted(
    ids, monkeypatch
):
    """The view's map leaves out a key two sources now share (strict=False). The resume line
    the draft cited still exists, so the page must never say it was deleted: it says another
    source now uses the same key, and still shows the words the draft was written from."""
    keys = _resume_keys()

    def cite_first_sent(prompt: str) -> list[str]:
        return [next(k for k in keys if f"[{k}]" in prompt)]

    model = _Model(monkeypatch, cite_first_sent)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
        key = cite_first_sent(model.prompts[0])[0]
        sent_text = re.search(rf"^- \[{key}\] (.+)$", model.prompts[0], re.M).group(1)
        pe.save_section("employment", "proof_points", {"proof_points": [
            {"key": key, "claim": "Invented proof point added later", "evidence": "",
             "metric": None}]}, keep_undo=False)
        html = _view(c, ids["strong"])
    src = _source(html, key)
    assert src is not None and sent_text in _lines(src)
    assert src.select_one('[data-testid="cover-letter-source-gone"]') is None
    assert "Deleted since this draft" not in html
    note = src.select_one('[data-testid="cover-letter-source-collided"]')
    assert note is not None and "same key" in note.get_text()
