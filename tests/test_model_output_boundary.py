"""A model's reply is text Kestrel checks and shows. It is never an address Kestrel opens, a path
Kestrel reads or writes, or a tool the model is handed (the model-output audit).

Four guards, all offline with invented data:

1. Request shape. Every body the model client builds, for every backend kind and protocol, has no
   tool, function, search or agent key. The one exception is pinned exactly: the Messages protocol
   answers structured output through a single forced tool whose only content is the reply schema.
2. Fetch inventory. A static scan of `kestrel/` finds every place code makes a web request. Each
   file that does is listed below with where its address comes from. A new one fails until an owner
   lists it. The scan is by function name and receiver, because following a value through the
   program is not decidable; the second guard supplies the part a list cannot.
3. No fetch beside a model reply. A file that reads a model's reply may not make a web request,
   start a program or open a browser. So a reply can never reach one of those in the same module,
   and a new helper that does must be written in a file that never sees a reply.
4. Hostile replies. Replies carrying `http://attacker.invalid/x` and `../../secret` go through the
   research, contact, document and file-name paths: no request is made and no file outside the
   data folder is opened.
"""

from __future__ import annotations

import ast
import builtins
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from kestrel.config import BackendConfig, get_settings
from kestrel.llm.client import (
    build_messages_body,
    build_request_body,
    build_responses_body,
)

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "kestrel"

HOSTILE_URL = "http://attacker.invalid/x"
HOSTILE_PATH = "../../secret"


# ======================================================================== 1. request shape
# A key anywhere in a request body (any depth) that would give a model an action to take.
FORBIDDEN_KEYS = frozenset({
    "tools", "tool_choice", "functions", "function_call", "parallel_tool_calls",
    "web_search", "web_search_options", "web_search_preview", "search", "search_options",
    "search_parameters", "plugins", "mcp", "mcp_servers", "browser", "computer", "computer_use",
    "code_interpreter", "file_search", "agent", "agents", "container", "tool_resources",
})

_MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}


def _keys(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}/{k}", str(k)
            yield from _keys(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _keys(v, f"{path}[{i}]")


def _forbidden_in(body: dict) -> list[str]:
    # Keys inside the reply schema are the model's own field names, not request features.
    scrubbed = json.loads(json.dumps(body))
    for holder in _schema_holders(scrubbed):
        holder.clear()
    return [p for p, k in _keys(scrubbed) if k in FORBIDDEN_KEYS]


def _schema_holders(body: dict):
    """The dicts that hold a reply schema, wherever the protocol puts it."""
    rf = body.get("response_format")
    if isinstance(rf, dict) and isinstance(rf.get("json_schema"), dict):
        yield rf["json_schema"]
    txt = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(txt, dict):
        yield txt
    oc = (body.get("output_config") or {}).get("format")
    if isinstance(oc, dict):
        yield oc
    for t in body.get("tools") or []:
        if isinstance(t, dict) and isinstance(t.get("input_schema"), dict):
            yield t["input_schema"]


def _backends() -> list[BackendConfig]:
    out = []
    for mode in ("json_schema", "json_object"):
        for think in ("reasoning_effort", "chat_template_kwargs", "none"):
            out.append(
                BackendConfig(base_url="http://x/v1", json_mode=mode, thinking_control=think)
            )
    for provider in ("openrouter", "openai", "openmodel", "generic"):
        out.append(BackendConfig(
            base_url="https://x.example/v1", json_mode="json_schema", thinking_control="none",
            external=True, provider=provider,
        ))
    # Every backend the shipped settings name.
    out.extend(get_settings().llm.backends.values())
    return out


@pytest.mark.parametrize("schema", [None, _SCHEMA])
@pytest.mark.parametrize("repair", [False, True])
def test_chat_completions_body_carries_no_tool_for_any_backend(schema, repair):
    for backend in _backends():
        body = build_request_body(
            model="m", messages=_MSGS, backend=backend, temperature=0.1, max_tokens=50,
            schema=schema, schema_name="resp", repair=repair,
        )
        assert _forbidden_in(body) == [], (backend.provider, backend.json_mode)


@pytest.mark.parametrize("schema", [None, _SCHEMA])
def test_responses_body_carries_no_tool(schema):
    body = build_responses_body(
        model="m", messages=_MSGS, temperature=0.1, max_tokens=50, schema=schema,
        schema_name="resp",
    )
    assert _forbidden_in(body) == []


def _messages_backend(provider: str) -> BackendConfig:
    return BackendConfig(
        base_url="https://x.example/v1", protocol="messages", provider=provider, external=True,
    )


def _messages_body(provider: str, schema):
    return build_messages_body(
        model="m", messages=_MSGS, temperature=0.1, max_tokens=50, schema=schema,
        schema_name="resp", backend=_messages_backend(provider),
    )


@pytest.mark.parametrize("provider", ["anthropic", "openmodel", "generic"])
def test_messages_body_without_a_schema_carries_no_tool(provider):
    assert _forbidden_in(_messages_body(provider, None)) == []


def test_the_claude_api_messages_body_declares_no_tool():
    assert _forbidden_in(_messages_body("anthropic", _SCHEMA)) == []


@pytest.mark.parametrize("provider", ["openmodel", "generic"])
def test_messages_structured_output_is_the_one_pinned_reply_tool(provider):
    """The owner-allowlisted exception: structured output on the Messages protocol is a forced
    call to ONE tool that holds only the reply schema. It cannot read, fetch or run anything; the
    "call" is the reply, parsed and validated like every other reply. Any other shape, a second
    tool, an extra field or another choice, fails here."""
    body = _messages_body(provider, _SCHEMA)
    tools = body["tools"]
    assert len(tools) == 1
    assert set(tools[0]) == {"name", "description", "input_schema"}
    assert tools[0]["name"] == "resp"
    assert body["tool_choice"] == {"type": "tool", "name": "resp"}
    leftover = {k: v for k, v in body.items() if k not in ("tools", "tool_choice")}
    assert _forbidden_in(leftover) == []


def test_the_shape_guard_itself_catches_each_forbidden_key():
    for key in sorted(FORBIDDEN_KEYS):
        assert _forbidden_in({"model": "m", key: []}) == [f"/{key}"]
        assert _forbidden_in({"model": "m", "extra": {"deep": [{key: 1}]}}) == [
            f"/extra/deep[0]/{key}"
        ]
    # A reply schema may name a field "search" or "tools"; that is not a request feature.
    body = build_request_body(
        model="m", messages=_MSGS, backend=BackendConfig(base_url="http://x/v1"),
        temperature=0.0, max_tokens=5, schema_name="resp",
        schema={"type": "object", "properties": {"tools": {"type": "string"}}},
    )
    assert _forbidden_in(body) == []


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages"])
async def test_the_request_that_leaves_the_client_has_no_tool(protocol):
    """End to end through `LLMClient.complete`: the bytes sent, not only the builder's dict."""
    from kestrel.llm.client import LLMClient

    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}}], "usage": {},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
            "content": [{"type": "text", "text": "{}"}],
        })

    settings = get_settings()
    backend = settings.llm.backends["local"].model_copy(update={"protocol": protocol})
    settings.llm.backends["local"] = backend
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = LLMClient(settings, http=http, http_local=http)
        await client.complete(tier="tier1", messages=_MSGS, schema=_SCHEMA, schema_name="resp")
    assert len(seen) == 1
    url, body = seen[0]
    assert url.startswith("http://127.0.0.1:11434/v1/")
    if protocol == "messages":
        body = {k: v for k, v in body.items() if k not in ("tools", "tool_choice")}
    assert _forbidden_in(body) == []


# ======================================================================== 2. fetch inventory
# What starts a web request, found by name. `get_text()` with no argument is BeautifulSoup's.
_STRONG = {"get_text", "get_json", "post_json", "fetch_posting", "stream", "request"}
_WEAK = {"get", "post", "put", "patch", "delete", "head"}
_RECEIVERS = {"http", "client", "http_local", "llm_http", "httpx", "_http"}
_CALLED_BY_NAME = {"fetch_posting", "find_company_board", "resolve_short_link"}
_PROCESS_MODULES = {
    "subprocess", "webbrowser", "smtplib", "ftplib", "requests", "aiohttp", "urllib3",
    "websockets", "http.client", "urllib.request",
}


def _receiver(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _net_sinks(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            if f.attr == "get_text" and not node.args:
                continue
            if f.attr in _STRONG or (f.attr in _WEAK and _receiver(f.value) in _RECEIVERS):
                found.append((f.attr, node.lineno))
        elif isinstance(f, ast.Name) and f.id in _CALLED_BY_NAME:
            found.append((f.id, node.lineno))
    return found


def _process_uses(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name, node.lineno) for a in node.names if a.name in _PROCESS_MODULES]
        elif isinstance(node, ast.ImportFrom) and node.module in _PROCESS_MODULES:
            found.append((node.module, node.lineno))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "os" and node.attr in {"system", "startfile", "popen", "execv"}:
                found.append((f"os.{node.attr}", node.lineno))
    return found


def _py_files() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


# Where each address comes from. NONE of these is a model's reply: every file here is checked by
# guard 3 never to read one. Prefix entries (ending "/") cover a folder of one kind.
FETCH_SITES: dict[str, str] = {
    "kestrel/ingest/adapters/": "job-board adapters: the board address is built from the "
    "operator's saved source (a board name, a tenant) and, for a detail page, a link in the "
    "board's own response; no model is in the path",
    "kestrel/ingest/signal_sources/": "signal sources: the operator's configured feed, or the "
    "fixed SEC and SAM hosts; the page text is read, never followed",
    "kestrel/ingest/http.py": "the shared HTTP client itself: it fetches what its caller hands "
    "it and refuses private addresses for an operator-supplied link",
    "kestrel/ingest/board_discovery.py": "one redirect read of a short link found in a posting "
    "from a board (third-party data), with redirects off",
    "kestrel/ingest/employer_links.py": "an employer link found in a posting from a board "
    "(third-party data), public addresses only",
    "kestrel/research/excerpts.py": "company research: the organization's stored domain or url "
    "(from a posting), fixed SEC hosts with the name URL-encoded into the query, and the "
    "operator's own search-service address; the model receives the text, never an address",
    "kestrel/intelligence/liveness.py": "the posting's own stored link, public addresses only",
    "kestrel/services/applied_import.py": "a posting link the person pasted, public addresses "
    "only, each hop checked",
    "kestrel/services/updater.py": "the update feed address from settings (this computer only)",
    "kestrel/services/delivery.py": "the notification webhook the person set (this computer "
    "only); the model's text can be in the body, never in the address",
    "kestrel/services/hosted_balance.py": "the online service's own balance address from the "
    "saved backend",
    "kestrel/services/lan_servers.py": "a model server address the person typed",
    "kestrel/services/model_tasks.py": "Ollama's own pull and create calls at the saved server "
    "address; the model name is the person's pick",
    "kestrel/setup.py": "the local Ollama address from settings",
    "kestrel/web/pages/models_ui.py": "the saved native Ollama address",
    "kestrel/web/pages/sources_ui.py": "a link the person pasted into the Sources page",
    "kestrel/web/pages/sources.py": "a company name the person typed; it becomes lowercase "
    "letters and digits in the path of a fixed job-board host (board_lookup.candidate_slugs)",
    "kestrel/services/source_suggestions.py": "the name of a company the person has a pursuit "
    "at (a posting's or an extracted name); it becomes lowercase letters and digits in the path "
    "of a fixed job-board host (board_lookup.candidate_slugs), never a host or a full address",
    "kestrel/web/routes_control.py": "the saved native Ollama address (/api/ps)",
    "kestrel/web/serve.py": "this computer's own health check",
    "kestrel/cli.py": "this computer's own health check",
    "kestrel/desktop/shell.py": "this computer's own app address",
    "kestrel/llm/client.py": "the model transport: the saved backend address and the fixed "
    "paths of each protocol; the one place a prompt leaves, and it sends no tool (guard 1)",
    "kestrel/llm/providers.py": "an online service's key check and model list at its saved "
    "address",
}


def _covered(rel: str) -> str | None:
    for key, why in FETCH_SITES.items():
        if rel == key or (key.endswith("/") and rel.startswith(key)):
            return why
    return None


def test_every_place_that_makes_a_web_request_is_listed_with_its_address_source():
    unlisted = {}
    for p in _py_files():
        sinks = _net_sinks(ast.parse(p.read_text(encoding="utf-8")))
        if sinks and _covered(_rel(p)) is None:
            unlisted[_rel(p)] = sinks[:3]
    assert not unlisted, (
        "A new place that makes a web request. Show that its address does not come from a "
        "model's reply (the stored organization address, a saved setting, or what the person "
        "typed), then list the file in FETCH_SITES in tests/test_model_output_boundary.py with "
        f"where its address comes from: {unlisted}"
    )


def test_the_fetch_list_has_no_stale_entry():
    with_sinks = {
        _rel(p) for p in _py_files() if _net_sinks(ast.parse(p.read_text(encoding="utf-8")))
    }
    stale = [
        k for k in FETCH_SITES
        if not any(f == k or (k.endswith("/") and f.startswith(k)) for f in with_sinks)
    ]
    assert not stale, f"listed but no longer making a web request: {stale}"
    assert all(len(why) > 20 for why in FETCH_SITES.values())


def test_the_scan_finds_a_fetch_and_a_program_start_and_skips_beautifulsoup():
    sample = ast.parse(
        "async def f(http, soup):\n"
        "    await http.get_text(url)\n"
        "    await client.post(u, json=b)\n"
        "    x = soup.get_text()\n"
        "    d = row.get('a')\n"
        "    await fetch_posting(http, u)\n"
        "import subprocess\n"
        "os.startfile(p)\n"
    )
    assert [a for a, _ in _net_sinks(sample)] == ["get_text", "post", "fetch_posting"]
    assert [a for a, _ in _process_uses(sample)] == ["subprocess", "os.startfile"]


# ===================================================================== 3. no fetch beside a reply
# A file that reads a model's reply: it runs a prompt, validates a reply schema, or calls the
# client. (The helpers that only build a prompt are not readers and are not matched.)
_READS_REPLIES = re.compile(
    r"\b(run_prompt|run_prompt_hosted|run_guarded|run_doc_select|parse_and_validate)\b"
    r"|\bclient\.complete\(|\bChatResult\b|from kestrel\.llm\.schemas import"
    r"|import kestrel\.llm\.schemas"
)

# The transport that sends the prompt and reads the reply bytes is the one allowed overlap: it
# fetches only the saved backend address, and guard 1 pins what it sends.
_TRANSPORT = {"kestrel/llm/client.py", "kestrel/services/model_tasks.py"}


def _reply_readers() -> list[Path]:
    return [p for p in _py_files() if _READS_REPLIES.search(p.read_text(encoding="utf-8"))]


def test_the_scan_sees_the_files_that_read_replies():
    names = {_rel(p) for p in _reply_readers()}
    for must in ("kestrel/research/contacts.py", "kestrel/research/company.py",
                 "kestrel/docs/select.py", "kestrel/pipeline/tier1.py",
                 "kestrel/ingest/triggers.py", "kestrel/interview/mock.py",
                 "kestrel/negotiation/scripts.py"):
        assert must in names, f"{must} no longer looks like a file that reads a reply"
    assert len(names) >= 20


def test_no_file_that_reads_a_model_reply_makes_a_request_or_starts_a_program():
    bad: dict[str, list] = {}
    for p in _reply_readers():
        rel = _rel(p)
        if rel in _TRANSPORT:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        hits = _net_sinks(tree) + _process_uses(tree)
        if hits:
            bad[rel] = hits[:4]
    assert not bad, (
        "A file that reads a model's reply also makes a web request or starts a program. A "
        "reply must never reach one: move the fetch into a file that never sees a reply and "
        f"pass it an address from stored or typed data: {bad}"
    )


# ======================================================================== 4. hostile replies
class _Recorder:
    """A stand-in for the shared HTTP client that records every address it is asked for."""

    user_agent = "Kestrel/1.0 (ops@kestrel-test.org)"

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def _hit(self, url, *a, **k):
        self.urls.append(str(url))
        raise httpx.ConnectError("offline by test")

    get_text = get_json = post_json = request = _hit


@pytest.fixture
def no_network(monkeypatch):
    """Any request through httpx, sync or async, is recorded and refused."""
    urls: list[str] = []

    def record(self, request, *a, **k):
        urls.append(str(request.url))
        raise httpx.ConnectError("offline by test")

    async def arecord(self, request, *a, **k):
        urls.append(str(request.url))
        raise httpx.ConnectError("offline by test")

    monkeypatch.setattr(httpx.Client, "send", record)
    monkeypatch.setattr(httpx.AsyncClient, "send", arecord)
    return urls


@pytest.fixture
def opened(monkeypatch):
    """Every path handed to open(), so a test can prove a hostile path was never opened."""
    paths: list[str] = []
    real = builtins.open

    def spy(file, *a, **k):
        paths.append(str(file))
        return real(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    return paths


class _FakeLLM:
    def __init__(self, payload) -> None:
        self.settings = get_settings()
        self._payload = payload

    async def complete(self, *, tier, messages, schema=None, **_):
        from kestrel.llm.client import ChatResult

        return ChatResult(
            content=json.dumps(self._payload), prompt_tokens=1, completion_tokens=1,
            latency_ms=1, model=self.settings.llm.tiers[tier].model,
        )


async def test_a_contact_reply_with_a_hostile_address_makes_no_request_and_keeps_no_link(
    sm, no_network, opened
):
    from kestrel.db.models import Contact, Org
    from kestrel.research.contacts import discover_contacts
    from kestrel.research.excerpts import Excerpt, ExcerptSet

    async with sm() as s:
        org = Org(name="Northwind", normalized_name="northwind")
        s.add(org)
        await s.commit()
        org_id = org.id
    # The hostile address is even present in the page text, the strongest case for "verbatim".
    excerpts = ExcerptSet(excerpts=[Excerpt(
        index=1, url="https://northwind.example/team", kind="company_page",
        text=f"Pat Lee, CIO. Profile {HOSTILE_URL} and https://www.linkedin.com/in/patlee",
    )])
    payload = {"contacts": [
        {"name": HOSTILE_PATH, "title": "CIO", "contact_type": "exec",
         "linkedin_url": HOSTILE_URL, "email": None, "confidence": 0.9, "source_index": 1,
         "why_this_person": HOSTILE_URL, "draft_message": f"See {HOSTILE_URL} {HOSTILE_PATH}"},
        {"name": "Pat Lee", "title": "CIO", "contact_type": "exec",
         "linkedin_url": "https://www.linkedin.com/in/patlee", "email": None, "confidence": 0.9,
         "source_index": 1, "why_this_person": "named", "draft_message": "Hi Pat."},
    ]}
    async with sm() as s:
        org = await s.get(Org, org_id)
        await discover_contacts(s, _FakeLLM(payload), org, excerpts)
    async with sm() as s:
        rows = (await s.execute(select(Contact))).scalars().all()
    by_name = {c.name: c for c in rows}
    assert by_name[HOSTILE_PATH].linkedin_url is None  # another site's address is not a profile
    assert by_name["Pat Lee"].linkedin_url == "https://www.linkedin.com/in/patlee"
    assert all(c.source_url == "https://northwind.example/team" for c in rows)
    assert no_network == []
    assert not [p for p in opened if "secret" in p]


async def test_a_research_reply_with_hostile_addresses_makes_no_request(sm, no_network, opened):
    from kestrel.db.models import Org, OrgResearch
    from kestrel.research.company import research_org
    from kestrel.research.excerpts import Excerpt, ExcerptSet

    async with sm() as s:
        org = Org(name="Northwind", normalized_name="northwind")
        s.add(org)
        await s.commit()
        org_id = org.id
    excerpts = ExcerptSet(excerpts=[
        Excerpt(index=1, url="https://northwind.example/about", kind="company_page", text="a"),
        Excerpt(index=2, url="https://northwind.example/news", kind="company_page", text="b"),
    ])
    payload = {
        "summary_md": f"Read {HOSTILE_URL} and open {HOSTILE_PATH}",
        "signals": [{"category": "growth", "statement": HOSTILE_URL, "source_index": 1,
                     "implication": HOSTILE_PATH}],
        "unknowns": [HOSTILE_URL], "angle_for_candidate": HOSTILE_URL, "sources_used": [1],
    }
    recorder = _Recorder()
    async with sm() as s:
        org = await s.get(Org, org_id)
        row = await research_org(s, _FakeLLM(payload), org, settings=get_settings(),
                                 http=recorder, excerpts=excerpts)
        await s.commit()
        org = await s.get(Org, org_id)
    assert isinstance(row, OrgResearch)
    assert HOSTILE_URL in (row.summary_md or "")  # kept as text
    assert [x["source_url"] for x in org.signals] == ["https://northwind.example/about"]
    assert recorder.urls == [] and no_network == []
    assert not [p for p in opened if "secret" in p]


async def test_research_fetches_only_stored_addresses_whatever_the_name_says(sm):
    """A model can write the organization's name (a trigger reply names it). The name goes into a
    URL-encoded query on the fixed SEC host and nowhere else: no other host is asked for, and a
    name made of address pieces does not change the host."""
    from kestrel.db.models import Org
    from kestrel.research.excerpts import acquire_excerpts

    hostile_name = f'x" @attacker.invalid/ {HOSTILE_URL} {HOSTILE_PATH}'
    async with sm() as s:
        org = Org(name=hostile_name, normalized_name="x")  # no domain, no url
        s.add(org)
        await s.commit()
        recorder = _Recorder()
        settings = get_settings()
        await acquire_excerpts(s, recorder, org, settings=settings)
    assert recorder.urls, "the SEC lookup should have been tried"
    hosts = {httpx.URL(u).host for u in recorder.urls}
    assert hosts == {"efts.sec.gov"}
    assert all("attacker.invalid" not in httpx.URL(u).host for u in recorder.urls)


async def test_research_pages_come_from_the_stored_domain_only(sm):
    from kestrel.db.models import Org
    from kestrel.research.excerpts import acquire_excerpts

    async with sm() as s:
        org = Org(name=HOSTILE_URL, normalized_name="x", domain="northwind.example")
        s.add(org)
        await s.commit()
        recorder = _Recorder()
        await acquire_excerpts(s, recorder, org, settings=get_settings())
    hosts = {httpx.URL(u).host for u in recorder.urls}
    assert hosts <= {"northwind.example", "efts.sec.gov"}
    assert "northwind.example" in hosts


def test_a_hostile_variant_or_slot_key_is_refused_never_used_as_a_path():
    from kestrel.llm.docselect import build_doc_select_schema, validate_doc_selection

    catalog = {
        "layouts": ["executive"], "summary_variants": [{"key": "plain"}],
        "skill_groups": [{"key": "core"}],
        "slots": [{"key": "r1b1", "optional": False, "variants": [{"key": "cost"}]}],
    }
    schema = build_doc_select_schema(**catalog)
    # Structurally impossible on a constrained backend: the enum holds only the real keys.
    assert HOSTILE_PATH not in json.dumps(schema)
    chosen = {"layout": "executive", "summary": "plain", "slots": {"r1b1": HOSTILE_PATH}}
    args = {k: catalog[k] for k in ("slots", "layouts", "summary_variants")}
    errors = validate_doc_selection(chosen, **args)
    assert errors and HOSTILE_PATH in errors[0], "a key not in the catalog must be refused"
    chosen["slots"] = {HOSTILE_PATH: "cost"}
    assert any("unknown slot key" in e for e in validate_doc_selection(chosen, **args))


def test_a_company_name_becomes_only_letters_and_digits_in_a_board_address():
    from kestrel.ingest.board_lookup import candidate_slugs

    slugs = candidate_slugs(f"Northwind {HOSTILE_URL}/{HOSTILE_PATH} @x.invalid")
    assert slugs
    for options in slugs.values():
        for slug in options:
            assert re.fullmatch(r"[A-Za-z0-9-]+", slug), slug


def test_a_file_name_built_from_model_or_posting_text_has_no_path_in_it():
    from kestrel.docs.render import filename_for

    name = filename_for({"name": f"Pat {HOSTILE_PATH}"}, f"{HOSTILE_URL}/..\\..", HOSTILE_PATH)
    assert re.fullmatch(r"[A-Za-z0-9]+_[A-Za-z0-9]+_[A-Za-z0-9]+\.pdf", name), name


async def test_the_pdf_browser_may_load_only_what_is_inside_the_page(monkeypatch):
    """Last defense for any address in printed markup (an image, a style link, a frame): the page
    is not allowed to fetch it. The browser is a stand-in; the route handler is the real one."""
    from kestrel.docs import render

    asked: dict = {}
    continued: list[str] = []
    aborted: list[str] = []

    class _Route:
        def __init__(self, url):
            self.request = SimpleNamespace(url=url)

        async def continue_(self):
            continued.append(self.request.url)

        async def abort(self):
            aborted.append(self.request.url)

    class _Page:
        async def route(self, pattern, handler):
            asked["pattern"], asked["handler"] = pattern, handler

        async def set_content(self, html, wait_until=None):
            asked["order"] = "route-first" if "handler" in asked else "content-first"
            for url in (HOSTILE_URL, "https://attacker.invalid/a.css", "file:///etc/passwd",
                        "data:image/png;base64,AAAA", "about:blank", "blob:x"):
                await asked["handler"](_Route(url))

        async def pdf(self, **_):
            return b"%PDF-1.4"

    class _Browser:
        async def new_page(self):
            return _Page()

        async def close(self):
            pass

    async def fake_launch(_p):
        return _Browser()

    class _Pw:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    import playwright.async_api as pw

    monkeypatch.setattr(pw, "async_playwright", lambda: _Pw())
    monkeypatch.setattr(render, "launch_browser", fake_launch)
    monkeypatch.setattr(render, "_scrub_and_count", lambda raw: (raw, 1))
    await render.render_pdf(f"<img src='{HOSTILE_URL}'>")
    assert asked["order"] == "route-first"
    assert asked["pattern"] == "**/*"
    assert aborted == [HOSTILE_URL, "https://attacker.invalid/a.css", "file:///etc/passwd"]
    assert continued == ["data:image/png;base64,AAAA", "about:blank", "blob:x"]
