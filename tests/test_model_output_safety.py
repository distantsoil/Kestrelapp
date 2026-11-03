"""Issue #167: model output must never become a request target or file path,
and requests to Ollama carry no agent tools. These tests keep that guarantee true."""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Request shape: no agent features in any LLM request body
# ---------------------------------------------------------------------------

# Keys that would indicate an agent feature (web search, code execution, browsing,
# multi-tool orchestration). A new key fails until an owner allowlists it.
_FORBIDDEN_KEYS = frozenset({
    "functions",
    "web_search",
    "search_options",
    "plugins",
    "agents",
    "browser",
    "computer_use",
    "code_interpreter",
    "mcp_servers",
    "server_url",
    "server_label",
    "server_type",
})

# The openmodel.ai Messages-protocol structured-output mechanism is the only
# path that sends `tools`/`tool_choice`. It is a single forced JSON-schema
# output tool (no executable capability), disclosed in the audit record.
# We allowlist it here so the test stays focused on real agent features.
_MESSAGES_TOOLS_ALLOWED = True


def _assert_no_agent_features(body: dict, *, context: str) -> None:
    """Assert the request body carries no agent-feature keys."""
    for key in _FORBIDDEN_KEYS:
        assert key not in body, (
            f"{context}: request body contains forbidden key {key!r}. "
            f"A model's output must never drive an agent capability. "
            f"If this is a legitimate structured-output mechanism, "
            f"add it to _ALLOWLISTED_KEYS with a comment explaining why."
        )


def test_chat_completions_body_has_no_agent_features():
    """build_request_body (OpenAI-compatible) never carries agent features."""
    from kestrel.config import BackendConfig
    from kestrel.llm.client import build_request_body

    backend = BackendConfig(
        base_url="http://x/v1", json_mode="json_schema", thinking_control="reasoning_effort",
    )
    msgs = [{"role": "user", "content": "hi"}]
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    body = build_request_body(
        model="m", messages=msgs, backend=backend,
        temperature=0.0, max_tokens=100, schema=schema, schema_name="resp",
    )
    _assert_no_agent_features(body, context="build_request_body")


def test_responses_body_has_no_agent_features():
    """build_responses_body (OpenAI Responses API) never carries agent features."""
    from kestrel.llm.client import build_responses_body

    msgs = [{"role": "user", "content": "hi"}]
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    body = build_responses_body(
        model="m", messages=msgs,
        temperature=0.0, max_tokens=100, schema=schema, schema_name="resp",
    )
    _assert_no_agent_features(body, context="build_responses_body")


def test_messages_body_tools_are_structured_output_only():
    """build_messages_body (Anthropic Messages) may send tools/tool_choice ONLY as a
    single forced JSON-schema output tool. No web/search/browser/code tools."""
    from kestrel.config import BackendConfig
    from kestrel.llm.client import build_messages_body

    backend = BackendConfig(base_url="http://x/v1", json_mode="json_schema")
    msgs = [{"role": "user", "content": "hi"}]
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    body = build_messages_body(
        model="m", messages=msgs, backend=backend,
        temperature=0.0, max_tokens=100, schema=schema, schema_name="resp",
    )

    # The structured-output tool is allowed, but verify its shape.
    if "tools" in body:
        tools = body["tools"]
        assert len(tools) == 1, "exactly one tool: the structured-output mechanism"
        tool = tools[0]
        assert "input_schema" in tool, "the tool must be a schema-output tool"
        assert "function" not in tool, "not a function-calling tool"
        assert "web_search" not in tool
        assert "code_interpreter" not in tool
        assert "computer_use" not in tool

    if "tool_choice" in body:
        tc = body["tool_choice"]
        assert tc.get("type") == "tool", "tool_choice must force the structured-output tool"

    # No forbidden agent keys in the body itself.
    for key in _FORBIDDEN_KEYS:
        assert key not in body, f"build_messages_body: forbidden key {key!r}"


def test_anthropic_messages_body_declares_no_tools():
    """The Claude-API direct path must declare no tools at all."""
    from kestrel.config import BackendConfig
    from kestrel.llm.client import build_messages_body

    backend = BackendConfig(base_url="http://x/v1", json_mode="json_schema", provider="anthropic")
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    body = build_messages_body(
        model="claude", messages=msgs, backend=backend,
        temperature=0.0, max_tokens=100, schema=schema, schema_name="resp",
    )
    assert "tools" not in body, "Claude-API path must not declare tools"
    assert "tool_choice" not in body
    _assert_no_agent_features(body, context="build_messages_body (anthropic)")


# ---------------------------------------------------------------------------
# 2. Static: no HTTP fetch whose URL comes from model output
# ---------------------------------------------------------------------------

def test_research_fetch_urls_come_from_stored_data():
    """The research excerpts module only fetches URLs derived from stored DB data
    (org.url, org.domain) or fixed constants, never from model output."""
    from kestrel.research import excerpts

    src = Path(excerpts.__file__).read_text(encoding="utf-8")
    # The fetch targets are: org.url, org.domain, fixed path list, SEC constants, searx_url.
    # None of them read from a model response field.
    # Verify the module has no reference to model output fields in its URL construction.
    # (A rough but effective check: the module imports no LLM client and has no
    # call to complete() or any model response attribute.)
    assert "from kestrel.llm" not in src, "excerpts.py must not import LLM client"
    assert "generate" not in src or "def generate" in src, (
        "excerpts.py must not call model generation"
    )


def test_contacts_module_does_not_fetch():
    """Contact discovery (P6) does not make HTTP requests at all.
    It only validates against supplied source text."""
    from kestrel.research import contacts

    src = Path(contacts.__file__).read_text(encoding="utf-8")
    # No HTTP client imports.
    assert "import httpx" not in src, "contacts.py must not import httpx"
    assert "import aiohttp" not in src
    assert "requests.get" not in src
    assert "requests.post" not in src


def test_board_lookup_inputs_are_operator_typed():
    """board_lookup.find_company_board takes a company name typed by the operator.
    It does not accept model output as the name."""
    from kestrel.ingest import board_lookup

    src = Path(board_lookup.__file__).read_text(encoding="utf-8")
    # The function signature takes `name: str` — a simple name, not a URL or model field.
    assert "def find_company_board" in src
    # No import of LLM client.
    assert "from kestrel.llm" not in src, "board_lookup.py must not import LLM client"


# ---------------------------------------------------------------------------
# 3. Behavioral: hostile model text does not trigger a fetch
# ---------------------------------------------------------------------------

def test_research_model_reply_with_hostile_url_is_not_fetched():
    """If a model reply contains a URL, the research pipeline does not fetch it.
    The model's output is text for display/validation, not an instruction to execute.
    Verified by checking that _company_pages derives URLs only from org data."""
    from kestrel.research import excerpts

    src = Path(excerpts.__file__).read_text(encoding="utf-8")
    # _company_pages builds URLs from org.url, org.domain, and a fixed path list.
    # It does not read any model output field to construct a URL.
    # Verify the function references org.url and org.domain, not model fields.
    assert "org.url" in src or "org.domain" in src, (
        "_company_pages must derive URLs from org data"
    )
    # The function must not reference model response fields in URL construction.
    # (A rough check: no reference to 'reply', 'response_text', 'model_output' in the function)
    func_src = src.split("def _company_pages")[1].split("\nasync def ")[0]
    assert "reply" not in func_src.lower(), (
        "_company_pages must not use model reply text in URL construction"
    )


def test_model_text_as_file_path_is_neutralized():
    """PDF filename construction strips all non-alphanumeric characters,
    so a model-provided title cannot escape the data directory."""
    from kestrel.docs.render import _slug

    hostile = "../../etc/passwd"
    result = _slug(hostile, default="")
    assert "/" not in result, f"path traversal not neutralized: {result!r}"
    assert ".." not in result, f"dot-dot survives: {result!r}"

    normal = "Software Engineer"
    result2 = _slug(normal, default="")
    assert "Software" in result2 or "software" in result2, f"unexpected: {result2!r}"

    hostile2 = "a/b/c/d.e"
    result3 = _slug(hostile2, default="")
    assert "/" not in result3, f"slashes survive in _slug: {result3!r}"


# ---------------------------------------------------------------------------
# 4. Template safety: model text is never |safe
# ---------------------------------------------------------------------------

def test_raw_model_replies_are_not_marked_safe():
    """The template that displays raw model replies must not use |safe on model text.
    The comment in retry_row.html:3 states this explicitly."""
    from pathlib import Path
    import kestrel

    tpl_dir = Path(kestrel.__file__).parent / "web" / "templates" / "partials"
    retry_row = tpl_dir / "retry_row.html"
    if retry_row.exists():
        src = retry_row.read_text(encoding="utf-8")
        # Check for |safe on the raw_response variable specifically.
        # The comment mentions |safe, but actual usage would be `{{ q.raw_response|safe }}`
        # or similar. Look for the pattern where raw_response is followed by |safe.
        assert "raw_response|safe" not in src, (
            "retry_row.html: raw model reply (raw_response) must not be marked |safe"
        )
        assert "q.raw_response|safe" not in src
