"""A health check row's code and the words beside it come from one decision (#174 task E, D-ERR-24
round 5). These rows build their words in a variable, so a static reading of the source cannot
see them; each branch is driven here with fakes and the code is checked against the words."""

from __future__ import annotations

import re
from types import SimpleNamespace

import httpx
import pytest

from kestrel import doctor
from kestrel.doctor import FAIL, INFO, PASS, WARN, DoctorReport
from kestrel.ingest.base import AdapterError
from kestrel.services import local_ai_egress as egress
from kestrel.services.local_ai_egress import ConnectionSample, EgressResult

# Every function whose rows are proven here; tests/test_doctor_codes.py checks the same set.
RUNTIME_PROVEN = frozenset({
    "_check_python", "_check_uv", "_check_ollama_env", "_check_local_ai_settings",
    "_check_cloud_model_tags", "_check_local_ai_egress", "_check_model_digests",
    "_check_ats_shapes",
})

# What each code's words must say. CHK-027 says the check could not finish and names no cause.
SAYS = {
    "CHK-001": r"^running \d",
    "CHK-002": r"not on PATH",
    "CHK-013": r"^OLLAMA_(NUM_PARALLEL|KEEP_ALIVE)=(unset|\S+)$",
    "CHK-014": r"^https?://(?!127\.)",
    "CHK-015": r"^(?!127\.)",
    "CHK-016": r"-cloud",
    "CHK-017": r"Ollama connected to",
    "CHK-018": r"differ \(tag has moved\)",
    "CHK-020": r"missing field|laid out differently",
    "CHK-027": r"(?i)^(could not|unverified)",
}
CAUSE_WORDS = re.compile(r"missing field|laid out|differ|connected to|-cloud")


def _agrees(row) -> None:
    """A warning or failure has a code whose words match; a pass or a note has none."""
    if row.status in (PASS, INFO):
        assert row.code == "", (row.name, row.code, row.detail)
        return
    assert row.code in SAYS, (row.name, row.code, row.detail)
    assert re.search(SAYS[row.code], row.detail), (row.code, row.detail)
    assert row.code != "CHK-027" or not CAUSE_WORDS.search(row.detail), row.detail


def _only(rep: DoctorReport, prefix: str = ""):
    rows = [r for r in rep.results if r.name.startswith(prefix)]
    assert len(rows) == 1, [(r.name, r.status) for r in rep.results]
    _agrees(rows[0])
    return rows[0]


# ------------------------------------------------------------------------------------- egress
def _sample(kind: str, addr: str = "203.0.113.9") -> ConnectionSample:
    return ConnectionSample(4242, "ollama", addr, 443, kind)


def _egress(monkeypatch, result: EgressResult):
    monkeypatch.setattr(egress, "sample_connections", lambda **_k: result)
    rep = DoctorReport()
    doctor._check_local_ai_egress(rep)
    return _only(rep)


DENIED = ("the operating system did not let Kestrel list the connections of 1 of 2 Ollama "
          "process(es)")


@pytest.mark.parametrize(("result", "status", "code"), [
    # one process could not be listed, the other was seen reaching a public host: what was seen
    # is certain, so the specific code wins
    (EgressResult(samples=[_sample("public")], unverified=DENIED), WARN, "CHK-017"),
    (EgressResult(samples=[_sample("private", "192.168.1.7")], unverified=DENIED), WARN,
     "CHK-017"),
    (EgressResult(samples=[_sample("public")]), WARN, "CHK-017"),
    # nothing could be observed
    (EgressResult(unverified=DENIED), WARN, "CHK-027"),
    (EgressResult(samples=[_sample("ollama_service")], unverified=DENIED), WARN, "CHK-027"),
    (EgressResult(error="the list failed"), WARN, "CHK-027"),
    # seen and fine
    (EgressResult(), PASS, ""),
    (EgressResult(samples=[_sample("ollama_service")]), INFO, ""),
])
def test_the_egress_row_code_follows_what_was_seen(monkeypatch, result, status, code):
    row = _egress(monkeypatch, result)
    assert (row.status, row.code) == (status, code), row.detail


def test_a_failed_connection_listing_is_the_general_code(monkeypatch):
    def boom(**_k):
        raise OSError("denied")

    monkeypatch.setattr(egress, "sample_connections", boom)
    rep = DoctorReport()
    doctor._check_local_ai_egress(rep)
    assert (_only(rep).status, _only(rep).code) == (WARN, "CHK-027")


# ------------------------------------------------------------------------------- ATS shapes
class _Adapter:
    def __init__(self, outcome):
        self.outcome = outcome

    async def fetch(self, _config, _http):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _posting(**over):
    base = dict(title="Analyst", url="https://example.test/j/1", external_id="1",
                description_md="Invented text.", description_html=None)
    return SimpleNamespace(**{**base, **over})


_SETTINGS = SimpleNamespace(ingest=SimpleNamespace(
    user_agent="Kestrel-test", request_timeout_s=1.0, respect_robots=False))


def _ats(monkeypatch, outcome):
    from kestrel.db import migrate
    from kestrel.ingest import adapters

    monkeypatch.setattr(migrate, "current_revision", lambda: "head")
    monkeypatch.setattr(doctor, "_enabled_ats_sources", lambda: [
        SimpleNamespace(name="Example Co", kind="greenhouse", config={})])
    monkeypatch.setattr(adapters, "get_adapter", lambda _kind: _Adapter(outcome))
    rep = DoctorReport()
    doctor._check_ats_shapes(rep, _SETTINGS)
    return _only(rep, "ATS ")


@pytest.mark.parametrize(("outcome", "status", "code"), [
    ([_posting(title=None)], WARN, "CHK-020"),
    ([_posting(url=None, external_id=None)], WARN, "CHK-020"),
    ([_posting(description_md=None)], WARN, "CHK-020"),
    (httpx.ReadTimeout("slow"), WARN, "CHK-027"),
    (AdapterError("The board was not found.", code="SRC-008"), WARN, "CHK-027"),
    (RuntimeError("unexpected"), WARN, "CHK-027"),
    ([], WARN, "CHK-027"),
    ([_posting()], PASS, ""),
])
def test_only_a_real_shape_mismatch_is_chk_020(monkeypatch, outcome, status, code):
    row = _ats(monkeypatch, outcome)
    assert (row.status, row.code) == (status, code), row.detail
    # the "shape may have changed" hint sits only beside the code that says so
    assert ("shape may have changed" in row.remediation) == (code == "CHK-020"), row.remediation


def _stopped(sentence: str, kind: str = "stopped"):
    """A batch with one notice of `kind`. A known kind takes only its shared sentence (the kind
    alone), so `sentence` is used only for the general kind; read the words from the batch."""
    from kestrel.ingest.base import SHARED_KINDS, Notice, NoticeKind, PostingBatch

    batch = PostingBatch()
    k = NoticeKind(kind)
    batch.add(Notice(k) if k in SHARED_KINDS else Notice(k, sentence))
    return batch


@pytest.mark.parametrize(("sentence", "kind"), [
    ("The site is rate limiting requests. Kestrel will try again next run.", "rate_limited"),
    ("ROBOTS", "robots_refused"),  # replaced by the shared robots sentence below
    ("This board has its job feed turned off.", "feeds_off"),
    ("Example board did not return the details of 1 of its roles.", "body_unread"),
    ("Example board stopped for its own reason.", "stopped"),
])
def test_an_empty_batch_that_says_why_it_stopped_is_could_not_read(monkeypatch, sentence, kind):
    """A board that stopped early with a reason was not read: CHK-027 with that sentence, and no
    hint that its layout may have changed."""
    from kestrel.errors import ROBOTS_SENTENCE

    batch = _stopped(ROBOTS_SENTENCE if sentence == "ROBOTS" else sentence, kind)
    sentence = batch.partial_error
    row = _ats(monkeypatch, batch)
    assert (row.status, row.code) == (WARN, "CHK-027"), row.detail
    assert row.detail == f"could not read: {sentence}"
    assert "layout" not in row.remediation and "shape" not in row.remediation


def test_a_real_board_that_rate_limits_is_could_not_read(monkeypatch):
    """The real Breezy adapter answers a 429 with an empty batch and its own sentence."""
    from kestrel.db import migrate
    from kestrel.ingest import http

    class _Limited(_OddReplies):
        async def get_json(self, url, *_a, **_k):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("429", request=request,
                                        response=httpx.Response(429, request=request))

    monkeypatch.setattr(migrate, "current_revision", lambda: "head")
    monkeypatch.setattr(doctor, "_enabled_ats_sources", lambda: [
        SimpleNamespace(name="Example Co", kind="breezy", config={"slug": "example-co"})])
    monkeypatch.setattr(http, "HttpClient", _Limited)
    rep = DoctorReport()
    doctor._check_ats_shapes(rep, _SETTINGS)
    row = _only(rep, "ATS ")
    assert (row.status, row.code) == (WARN, "CHK-027"), row.detail
    assert "rate limiting" in row.detail and "layout" not in row.remediation


def _body_failure(kind: str):
    """An HttpClient stand-in whose list or feed answers with one role, and whose role page fails
    the way `kind` says."""
    feed = ('<?xml version="1.0"?><workzag-jobs><position><id>101</id>'
            '<name>Analyst</name><office>Lisbon</office></position></workzag-jobs>')

    class _Client(_OddReplies):
        async def get_json(self, url, *_a, **_k):
            return [{"friendly_id": "abc-analyst", "name": "Analyst",
                     "location": {"name": "Lisbon"}, "url": "https://example-co.breezy.hr/p/abc"}]

        async def get_text(self, url, *_a, **_k):
            if url.endswith("/xml"):
                return feed
            request = httpx.Request("GET", url)
            if kind == "robots":
                from kestrel.errors import RobotsDisallowed

                raise RobotsDisallowed()
            status = 429 if kind == "429" else 404
            raise httpx.HTTPStatusError(str(status), request=request,
                                        response=httpx.Response(status, request=request))

    return _Client


@pytest.mark.parametrize("adapter", [("breezy", {"slug": "example-co"}),
                                     ("personio", {"slug": "example-co"})])
@pytest.mark.parametrize(("failure", "words"), [
    ("429", "rate limiting"), ("robots", "robots.txt"), ("404", "did not return")])
def test_a_role_page_that_failed_is_could_not_read_not_a_layout_change(
        monkeypatch, adapter, failure, words):
    """A posting with no description because its own page failed is CHK-027 with the adapter's
    sentence, never CHK-020 "missing field(s): description"."""
    from kestrel.db import migrate
    from kestrel.ingest import http

    kind, config = adapter
    monkeypatch.setattr(migrate, "current_revision", lambda: "head")
    monkeypatch.setattr(doctor, "_enabled_ats_sources", lambda: [
        SimpleNamespace(name="Example Co", kind=kind, config=config)])
    monkeypatch.setattr(http, "HttpClient", _body_failure(failure))
    rep = DoctorReport()
    doctor._check_ats_shapes(rep, _SETTINGS)
    row = _only(rep, "ATS ")
    assert (row.status, row.code) == (WARN, "CHK-027"), row.detail
    assert words in row.detail and "shape" not in row.remediation


def test_a_plain_empty_batch_keeps_the_empty_board_advice(monkeypatch):
    from kestrel.ingest.base import PostingBatch

    row = _ats(monkeypatch, PostingBatch())
    assert (row.status, row.code) == (WARN, "CHK-027")
    assert row.detail.startswith("unverified: no postings returned")
    assert "layout may have changed" in row.remediation


def test_an_empty_board_says_the_layout_may_have_changed_if_it_keeps_happening(monkeypatch):
    row = _ats(monkeypatch, [])
    assert (row.status, row.code) == (WARN, "CHK-027")
    assert row.remediation == ("Run the check again when the board lists openings. If it keeps "
                               "finding none, the board's layout may have changed.")


class _OddReplies:
    """An HttpClient stand-in whose every reply is valid JSON in a layout no adapter reads."""

    def __init__(self, **_k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def get_json(self, *_a, **_k):
        return {"unexpected": True}

    async def get_text(self, *_a, **_k):
        return "{}"


@pytest.mark.parametrize(("kind", "config"), [
    ("breezy", {"slug": "example-co"}),
    ("adp", {"cid": "11111111-2222-3333-4444-555555555555"}),
])
def test_an_adapter_that_says_unexpected_shape_is_chk_020(monkeypatch, kind, config):
    """The real adapter raises its own shape error; the row is CHK-020 with the shape hint."""
    from kestrel.db import migrate
    from kestrel.ingest import http

    monkeypatch.setattr(migrate, "current_revision", lambda: "head")
    monkeypatch.setattr(doctor, "_enabled_ats_sources", lambda: [
        SimpleNamespace(name="Example Co", kind=kind, config=config)])
    monkeypatch.setattr(http, "HttpClient", _OddReplies)
    rep = DoctorReport()
    doctor._check_ats_shapes(rep, _SETTINGS)
    row = _only(rep, "ATS ")
    assert (row.status, row.code) == (WARN, "CHK-020"), row.detail
    assert "shape may have changed" in row.remediation


@pytest.mark.parametrize("where", ["sources", "client"])
def test_a_probe_that_could_not_start_is_the_general_code(monkeypatch, where):
    from kestrel.db import migrate
    from kestrel.ingest import http

    def boom(*_a, **_k):
        raise OSError("invented failure")

    monkeypatch.setattr(migrate, "current_revision", lambda: "head")
    if where == "sources":
        monkeypatch.setattr(doctor, "_enabled_ats_sources", boom)
    else:
        monkeypatch.setattr(doctor, "_enabled_ats_sources", lambda: [
            SimpleNamespace(name="Example Co", kind="greenhouse", config={})])
        monkeypatch.setattr(http, "HttpClient", boom)
    rep = DoctorReport()
    doctor._check_ats_shapes(rep, _SETTINGS)
    row = _only(rep, "ATS shapes")
    assert (row.status, row.code) == (WARN, "CHK-027"), row.detail
    assert row.detail.startswith("could not probe")


# ------------------------------------------------------------------------------ model digests
def _catalog() -> dict:
    import json
    from importlib.resources import files

    text = files("kestrel.resources.config").joinpath("model_digests.json").read_text("utf-8")
    return json.loads(text)["models"]


def _digests(local: list[dict]):
    class _Client:
        def list_models_with_digests(self, timeout: float = 10.0):
            return local

    rep = DoctorReport()
    doctor._check_model_digests(rep, _Client())
    return _only(rep, "Local AI: model digests")


def test_a_digest_that_differs_is_chk_018():
    cat = _catalog()
    first = next(iter(cat))
    local = [{"name": n, "digest": m["digest"]} for n, m in cat.items()]
    local[0] = {"name": first, "digest": "sha256:" + "0" * 64}
    row = _digests(local)
    assert (row.status, row.code) == (WARN, "CHK-018")


def test_a_model_not_installed_has_no_differs_code():
    cat = _catalog()
    local = [{"name": n, "digest": m["digest"]} for n, m in list(cat.items())[1:]]
    row = _digests(local)
    assert "not installed" in row.detail
    assert (row.status, row.code) == (INFO, "")


def test_an_unknown_model_has_no_differs_code():
    cat = _catalog()
    local = [{"name": n, "digest": m["digest"]} for n, m in cat.items()]
    local.append({"name": "someone-else:7b", "digest": "sha256:" + "1" * 64})
    row = _digests(local)
    assert "unknown" in row.detail
    assert (row.status, row.code) == (INFO, "")


# --------------------------------------------------------------- the other variable-built rows
def test_python_row(monkeypatch):
    rep = DoctorReport()
    doctor._check_python(rep)
    assert _only(rep).status == PASS  # the suite runs on 3.12
    monkeypatch.setattr(doctor.sys, "version_info", SimpleNamespace(major=3, minor=11, micro=4))
    rep = DoctorReport()
    doctor._check_python(rep)
    assert (_only(rep).status, _only(rep).code) == (FAIL, "CHK-001")


def test_uv_row(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/uv")
    rep = DoctorReport()
    doctor._check_uv(rep)
    assert (_only(rep).status, _only(rep).code) == (PASS, "")
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: None)
    rep = DoctorReport()
    doctor._check_uv(rep)
    assert (_only(rep).status, _only(rep).code) == (WARN, "CHK-002")


@pytest.mark.parametrize(("par", "keep", "codes"), [
    ("8", "2h", ["", ""]),
    (None, None, ["CHK-013", "CHK-013"]),
    ("1", "2h", ["CHK-013", ""]),
    ("8", "5m", ["", "CHK-013"]),
])
def test_ollama_env_rows(monkeypatch, par, keep, codes):
    """The rows read where Ollama reads them (#181): a backend holding these values, never
    Kestrel's own environment, which is set to the opposite to prove it is not read."""
    from kestrel.services import ollama_env

    class _Place(ollama_env.Backend):
        def owned(self):
            return {"OLLAMA_NUM_PARALLEL": par, "OLLAMA_KEEP_ALIVE": keep}

        def commands(self, values):
            return []

    monkeypatch.setattr(ollama_env, "_override", [_Place(mechanism="test", where="here")])
    monkeypatch.setattr("kestrel.llm.profiles.ollama_needed", lambda s: True)
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "1" if par == "8" else "16")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "1m" if keep == "2h" else "24h")
    s = SimpleNamespace(concurrency=SimpleNamespace(tier1=2, tier2=1, embed=1),
                        llm=SimpleNamespace(ollama_native_url="http://127.0.0.1:11434"))
    rep = DoctorReport()
    doctor._check_ollama_env(rep, s)
    for row in rep.results:
        _agrees(row)
    assert [r.code for r in rep.results] == codes


def test_ollama_env_rows_that_cannot_be_read_are_uncoded_notes(monkeypatch):
    from kestrel.services import ollama_env

    def broken(*a, **k):
        raise ollama_env.EnvChangeFailed("x")

    monkeypatch.setattr(ollama_env, "assess", broken)
    s = SimpleNamespace(concurrency=SimpleNamespace(tier1=2, tier2=1, embed=1),
                        llm=SimpleNamespace(ollama_native_url="http://127.0.0.1:11434"))
    rep = DoctorReport()
    doctor._check_ollama_env(rep, s)
    assert [(r.status, r.code) for r in rep.results] == [(INFO, ""), (INFO, "")]
    for row in rep.results:
        _agrees(row)


@pytest.mark.parametrize(("base_url", "host", "codes"), [
    ("http://127.0.0.1:11434", "", ["", ""]),
    ("http://10.0.0.5:11434", "0.0.0.0:11434", ["CHK-014", "CHK-015"]),
    ("http://127.0.0.1:11434", "127.0.0.1:11434", ["", ""]),
])
def test_local_ai_settings_rows(monkeypatch, base_url, host, codes):
    monkeypatch.setenv("OLLAMA_HOST", host)
    s = SimpleNamespace(llm=SimpleNamespace(backends={"local": SimpleNamespace(base_url=base_url)}))
    rep = DoctorReport()
    doctor._check_local_ai_settings(rep, s)
    for row in rep.results:
        _agrees(row)
    assert [r.code for r in rep.results] == codes


@pytest.mark.parametrize(("tags", "status", "code"), [
    (["qwen3:8b", "qwen3:8b-cloud"], WARN, "CHK-016"),
    (["qwen3:8b"], PASS, ""),
])
def test_cloud_model_tag_rows(tags, status, code):
    client = SimpleNamespace(list_models=lambda: tags)
    rep = DoctorReport()
    doctor._check_cloud_model_tags(rep, client)
    assert (_only(rep).status, _only(rep).code) == (status, code)


# ------------------------------------------------- every runtime-proven function is driven here
def _skip_names(tree) -> set[str]:
    """The names a plain call to pytest's skip goes by in this module, read from its
    module-level import table: `<name>.skip` for every `import pytest` (or `import pytest as
    <name>`), and any name bound by `from pytest import skip` (or `skip as <name>`)."""
    import ast

    names: set[str] = set()
    for node in tree.body:  # the module's own import table, not imports inside one function
        if isinstance(node, ast.Import):
            names |= {f"{a.asname or a.name}.skip" for a in node.names if a.name == "pytest"}
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            names |= {a.asname or a.name for a in node.names if a.name == "skip"}
    return names


def _skipped(fn, skip_names=frozenset({"pytest.skip"})) -> bool:
    """A test pytest would not run: marked skip, skipif or xfail, or whose body calls pytest's
    skip (`skip_names`) as a plain statement of its own (not under a condition)."""
    import ast

    if any("skip" in ast.unparse(d) or "xfail" in ast.unparse(d) for d in fn.decorator_list):
        return True
    return any(isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
               and ast.unparse(st.value.func) in skip_names for st in fn.body)


def _module_skipped(tree) -> bool:
    """A module-level `pytestmark` (plain or annotated) that skips every test in the file."""
    import ast

    for n in tree.body:
        if isinstance(n, ast.Assign):
            targets, value = n.targets, n.value
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            targets, value = [n.target], n.value
        else:
            continue
        if any(getattr(t, "id", "") == "pytestmark" for t in targets) and (
                "skip" in ast.unparse(value) or "xfail" in ast.unparse(value)):
            return True
    return False


def _doctor_calls_reachable_from_tests(source: str) -> set[str]:
    """The `doctor.<name>(...)` calls that a collected, non-skipped test reaches: in a `test_*`
    function itself, or in a module-level helper that such a test calls by name, followed through
    helpers that call helpers. A helper no test calls, or a skipped test, counts for nothing."""
    import ast

    tree = ast.parse(source)
    if _module_skipped(tree):
        return set()
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def body_nodes(fn):
        """The nodes of `fn` itself: a nested lambda, def or class may never be called, so its
        body is not counted as reached."""
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            yield node
            stack.extend(child for child in ast.iter_child_nodes(node) if not isinstance(
                child, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))

    def calls(fn) -> tuple[set[str], set[str]]:
        doctor_calls, helper_calls = set(), set()
        for node in body_nodes(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "doctor"):
                doctor_calls.add(f.attr)
            elif isinstance(f, ast.Name) and f.id in funcs and not f.id.startswith("test_"):
                helper_calls.add(f.id)
        return doctor_calls, helper_calls

    skips = _skip_names(tree)
    todo = [n for name, n in funcs.items()
            if name.startswith("test_") and not _skipped(n, skips)]
    seen: set[str] = set()
    reached: set[str] = set()
    while todo:
        fn = todo.pop()
        if fn.name in seen:
            continue
        seen.add(fn.name)
        doctor_calls, helpers = calls(fn)
        reached |= doctor_calls
        todo.extend(funcs[h] for h in helpers)
    return reached


def test_every_runtime_proven_function_is_called_by_a_test_in_this_file():
    """A name in RUNTIME_PROVEN must be reached by a test that runs, read from the source so the
    check does not depend on which tests ran before it."""
    from pathlib import Path

    reached = _doctor_calls_reachable_from_tests(Path(__file__).read_text(encoding="utf-8"))
    assert RUNTIME_PROVEN <= reached, RUNTIME_PROVEN - reached


def test_the_reachability_reading_ignores_dead_helpers_and_skipped_tests():
    src = (
        "import pytest\n"
        "def _dead():\n    doctor._check_uv(rep)\n"
        "def _live():\n    doctor._check_python(rep)\n"
        "def _via():\n    _live()\n"
        "@pytest.mark.skip(reason='x')\ndef test_off():\n    doctor._check_ollama_env(rep, s)\n"
        "def test_on():\n    _via()\n    doctor._check_model_digests(rep, c)\n"
        "    unused = lambda: doctor._check_local_ai_egress(rep)\n"
    )
    assert _doctor_calls_reachable_from_tests(src) == {"_check_python", "_check_model_digests"}


def test_the_reachability_reading_ignores_a_skipped_module_and_a_bare_skip():
    on = "import pytest\ndef test_on():\n    doctor._check_python(rep)\n"
    bare = ("import pytest\ndef test_off():\n    pytest.skip('x')\n"
            "    doctor._check_uv(rep)\n")
    marked = "import pytest\npytestmark = pytest.mark.skipif(True, reason='x')\n" + on
    assert _doctor_calls_reachable_from_tests(on) == {"_check_python"}
    assert _doctor_calls_reachable_from_tests(bare) == set()
    assert _doctor_calls_reachable_from_tests(marked) == set()


def test_the_reachability_reading_ignores_an_annotated_pytestmark_and_an_imported_skip():
    on = "def test_on():\n    doctor._check_python(rep)\n"
    annotated = ("import pytest\npytestmark: pytest.MarkDecorator = pytest.mark.skip("
                 "reason='x')\n" + on)
    imported = ("from pytest import skip\ndef test_off():\n    skip('x')\n"
                "    doctor._check_uv(rep)\n" + on)
    aliased = ("from pytest import skip as stop\ndef test_off():\n    stop('x')\n"
               "    doctor._check_uv(rep)\n" + on)
    assert _doctor_calls_reachable_from_tests(annotated) == set()
    assert _doctor_calls_reachable_from_tests(imported) == {"_check_python"}
    assert _doctor_calls_reachable_from_tests(aliased) == {"_check_python"}


def test_the_reachability_reading_resolves_an_aliased_pytest_import():
    on = "def test_on():\n    doctor._check_python(rep)\n"
    aliased = ("import pytest as pt\ndef test_off():\n    pt.skip('x')\n"
               "    doctor._check_uv(rep)\n" + on)
    unbound = ("import pytest as pt\ndef test_kept():\n    pytest.skip('x')\n"
               "    doctor._check_uv(rep)\n")  # `pytest` is not bound here: not pytest's skip
    assert _doctor_calls_reachable_from_tests(aliased) == {"_check_python"}
    assert _doctor_calls_reachable_from_tests(unbound) == {"_check_uv"}
    local = ("def test_other():\n    import pytest as pt\n    pt.skip('x')\n"
             "def test_kept():\n    pt.skip('x')\n    doctor._check_uv(rep)\n")
    # a local import binds only its own test
    assert _doctor_calls_reachable_from_tests(local) == {"_check_uv"}


@pytest.mark.parametrize("kind", ["breezy", "personio"])
@pytest.mark.parametrize(("failure", "code"), [("robots", "SRC-005"), ("404", "SRC-009"),
                                              ("429", "SRC-002")])
def test_the_adapter_says_why_a_role_page_was_not_read(kind, failure, code):
    """Each adapter adds a notice when a role's own page fails, so Runs sees it too: robots is the
    shared robots sentence (SRC-005), a 429 rate limiting (SRC-002), anything else the general
    partial code. The code is read from the notices, as the runner reads it."""
    import asyncio

    from kestrel.errors import ROBOTS_SENTENCE
    from kestrel.ingest import failure_code
    from kestrel.ingest.adapters import get_adapter

    async def go():
        async with _body_failure(failure)() as client:
            return await get_adapter(kind).fetch({"slug": "example-co"}, client)

    batch = asyncio.run(go())
    assert len(batch) == 1 and not batch[0].description_html
    assert batch.partial_error
    assert (batch.partial_error == ROBOTS_SENTENCE) == (failure == "robots")
    assert failure_code.code_for_notices(batch.notices, fallback="SRC-009") == code


def test_a_cap_notice_beside_a_whole_posting_still_proves_the_layout(monkeypatch):
    """The probe reads one role page (max_results 1), so a board with more roles always says the
    rest were added without a description. The posting that was read is whole: a pass."""
    batch = _stopped("Example board lists 9 roles, and Kestrel reads at most 1 posting pages.",
                     "capped")
    batch.append(_posting())
    row = _ats(monkeypatch, batch)
    assert (row.status, row.code) == (PASS, ""), row.detail
