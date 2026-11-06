"""Issue #143 (owner-approved wording, 2026-11-05): the "Local AI and Ollama" statement.

The wording is in docs/OUTBOUND-CALLS.md (the source of the website page) and on the Models
page "On this computer" card behind Show details. Two of its sentences are claims about the code,
so they are pinned here against the real client: the request a local model receives carries no
tools, no web access and no files, and a reply that is not the fixed format is never accepted.
Offline: respx-mocked, files read only."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from kestrel.config import get_settings
from kestrel.llm.client import LLMClient
from kestrel.llm.guard import run_guarded
from kestrel.llm.schemas import P1Triage
from kestrel.web.help_articles import TOPICS

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "OUTBOUND-CALLS.md"
_URL = "http://127.0.0.1:11434/v1/chat/completions"
_LEADS = ("What Kestrel does.", "What Kestrel cannot control.", "How to keep it local.")
_VALID = '{"verdict": "keep", "confidence": 0.8, "reason": "plausible", "signals": ["level_match"]}'
# Everything a request can carry that would give a model reach beyond the prompt.
_REACH = {"tools", "tool_choice", "functions", "function_call", "plugins", "web_search_options",
          "files", "file_ids", "attachments", "images", "tool_resources"}


def _section() -> str:
    text = DOC.read_text(encoding="utf-8")
    assert "\n## Local AI and Ollama\n" in text, "docs/OUTBOUND-CALLS.md lost its Local AI section"
    return text.split("\n## Local AI and Ollama\n", 1)[1].split("\n## ", 1)[0]


def _plain(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html).replace("&#39;", "'").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def test_the_outbound_calls_document_has_the_local_ai_and_ollama_section():
    section = _plain(_section())
    for lead in _LEADS:
        assert f"**{lead}**" in _section()
    assert "It gives the model no tools, no web access and no files." in section
    assert "Every answer is checked against a fixed format." in section
    assert "block Ollama's internet access in your firewall" in section
    assert "—" not in section and "–" not in section


@pytest.mark.usefixtures("_isolate_data_home")
def test_the_models_card_is_qualified_and_its_details_hold_the_approved_paragraphs():
    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.web.app import build_app

    migrate.run_migrations(paths.db_path())
    with TestClient(build_app(worker=False)) as c:
        body = c.get("/models").text
    assert "Works offline" not in body and "Private. " not in body
    card = body.split('id="mo-computer"', 1)[1].split("</section>", 1)[0]
    assert "Private when Ollama is set up as shipped" in card
    details = card.split('data-testid="mo-privacy-details"', 1)[1].split("</details>", 1)[0]
    assert "<summary>Show details</summary>" in details
    assert not re.search(r'mo-privacy-details"[^>]*\sopen', card), "the details start closed"
    shown = _plain(details.split("</summary>", 1)[1])
    approved = _plain(_section().replace("**", ""))
    assert shown == approved, "the card's details and the document must say the same words"


@pytest.mark.usefixtures("_isolate_data_home")
def test_the_help_article_on_models_no_longer_says_data_never_leaves():
    article = next(a for t in TOPICS for a in t.articles if (t.key, a.key) == ("models", "local"))
    text = _plain(article.body)
    assert "your data never leaves" not in text
    assert "as long as Ollama is set up as shipped" in text
    assert "Ollama's own settings" in text


@respx.mock
async def test_a_local_model_gets_a_prompt_and_nothing_else_and_prose_is_never_accepted():
    sent: list[dict] = []

    def reply(request):
        sent.append(json.loads(request.content))
        return Response(200, json={"choices": [{"message": {"content": "Sure, it is fine."}}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    respx.post(_URL).mock(side_effect=reply)
    msgs = [{"role": "user", "content": "triage this"}]
    res = await run_guarded(LLMClient(get_settings()), tier="tier1", messages=msgs,
                            model_cls=P1Triage)
    assert sent, "no request reached the local model"
    for body in sent:
        assert not (set(body) & _REACH), sorted(set(body) & _REACH)
        assert all(isinstance(m["content"], str) for m in body["messages"])
        assert "response_format" in body  # the fixed format travels with the request
    assert not res.ok and res.quarantined and res.value is None

    respx.post(_URL).mock(return_value=Response(
        200, json={"choices": [{"message": {"content": _VALID}}], "usage": {}}))
    ok = await run_guarded(LLMClient(get_settings()), tier="tier1", messages=msgs,
                           model_cls=P1Triage)
    assert ok.ok and ok.value.verdict == "keep"
