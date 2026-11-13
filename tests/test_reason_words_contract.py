"""D-TEST-18: every reason the app stores for a role that did not finish reaches the person in
plain words, never the generic fallback (issue 188 item 5).

Invariant: every reason or error a quarantine row can hold renders as its own plain-words
sentence on Needs another try (`retry_words.plain_reason`) and on the Pipeline row
(`fit.failure_words`), never as the "did not finish" fallback. A new reason added without words
fails here.

The reasons are found from the source, not from a list in this file:
1. Static: an AST scan of `kestrel/` for every string handed to a place that stores a quarantine
   reason (`record_refusal(reason=...)`, `HostedPlan(refuse=...)`, and the error of a
   `HandlerOutcome("quarantined", ...)`), a literal or a module constant resolved by import.
2. Produced: the functions that write the model-side reasons are called on protocol inputs
   (a provider refusal, a capped reply, a loop, bad JSON, a failed schema) and their output is
   checked. A new branch in those functions needs a new input here.

Invented text only. Nothing touches the network.
"""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from kestrel.llm import guard
from kestrel.llm.client import messages_stop_problem
from kestrel.pipeline._shared import HOSTED_REFUSAL
from kestrel.pipeline.tier2 import PROMPT_OVER_BUDGET
from kestrel.web import fit, retry_words

ROOT = Path(__file__).resolve().parents[1] / "kestrel"
_STORES = {"record_refusal": "reason", "HostedPlan": "refuse", "Quarantine": "error"}
# Reason arguments that pass on a reason made elsewhere (a plan's refusal, a call's error), so
# the reason itself is checked where it is written (D-TEST-18). Any other argument the scan
# cannot read as a literal or a module constant fails: a new reason must be readable here.
FORWARDED = {
    ("kestrel/docs/assess.py", "plan.refuse"),
    ("kestrel/pipeline/_shared.py", "plan.refuse"),
    ("kestrel/pipeline/tier1.py", "plan.refuse"),
    ("kestrel/pipeline/tier2.py", "plan.refuse"),
    ("kestrel/pipeline/tier2.py", "result.error"),
    ("kestrel/pipeline/_shared.py", "(error_prefix + reason)[:4000]"),
    ("kestrel/pipeline/_shared.py", "(error_prefix + (error or ''))[:4000]"),
    # Model-call failures stored by each module's quarantine writer: the reasons come from the
    # client and guard producers checked in _produced() below.
    ("kestrel/analytics/summary.py", "(error or '')[:4000]"),
    ("kestrel/ingest/triggers.py", "(error or '')[:4000]"),
    ("kestrel/interview/mock.py", "(error or '')[:4000]"),
    ("kestrel/interview/stories.py", "(error or '')[:4000]"),
    ("kestrel/negotiation/scripts.py", "(error or '')[:4000]"),
    ("kestrel/research/company.py", "(error or '')[:4000]"),
    ("kestrel/research/contacts.py", "(error or '')[:4000]"),
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _resolve(node: ast.expr, module: str) -> str | None:
    """The string a reason argument holds when it is a literal or a module constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = getattr(importlib.import_module(module), node.id, None)
        return value if isinstance(value, str) else None
    return None


def _stored_reason_args(call: ast.Call) -> list[ast.expr]:
    name = getattr(call.func, "id", getattr(call.func, "attr", ""))
    if name not in _STORES and name != "HandlerOutcome":
        return []
    out = [k.value for k in call.keywords if k.arg is not None and k.arg == _STORES.get(name)]
    if name == "HandlerOutcome" and call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and first.value == "quarantined":
            out += [k.value for k in call.keywords if k.arg == "error"]
    return out


def _scan() -> tuple[set[str], set[tuple[str, str]]]:
    """(reasons read from the source, (file, expression) of arguments it could not read)."""
    found: set[str] = set()
    unread: set[tuple[str, str]] = set()
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_name(path)
        rel = path.relative_to(ROOT.parent).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for arg in _stored_reason_args(node):
                    text = _resolve(arg, module)
                    if text:
                        found.add(text)
                    elif not (isinstance(arg, ast.Constant) and arg.value is None):
                        unread.add((rel, ast.unparse(arg)))
    return found, unread


def _discover_static() -> set[str]:
    return _scan()[0]


def test_every_reason_the_scan_cannot_read_is_a_listed_forward():
    unread = _scan()[1]
    assert unread - FORWARDED == set(), "a stored reason the contract cannot read; use a module " \
        "constant or a literal, or list it in FORWARDED if it passes on a checked reason"
    assert FORWARDED - unread == set(), "stale FORWARDED entries"


class _Reply(BaseModel):
    verdict: str


def _produced() -> dict[str, str]:
    """Reasons written by the model-side producers, from real calls on protocol inputs."""
    out: dict[str, str] = {}
    out["provider refusal"] = messages_stop_problem({"stop_reason": "refusal"}) or ""
    out["provider refusal with category"] = messages_stop_problem(
        {"stop_reason": "refusal", "stop_details": {"category": "invented"}}) or ""
    out["capped reply"] = messages_stop_problem({"stop_reason": "max_tokens"}) or ""
    looped = json.dumps({"tags": ["same"] * 40})
    out["generation loop"] = guard.degenerate_repeat(looped) or ""
    out["not json"] = guard.parse_and_validate("no object here", _Reply)[1] or ""
    out["wrong shape"] = guard.parse_and_validate('{"verdict": 3}', _Reply)[1] or ""
    return out


def test_discovery_finds_the_known_reasons():
    found = _discover_static()
    assert PROMPT_OVER_BUDGET in found
    assert HOSTED_REFUSAL in found
    assert len(found) >= 4, found  # a scan that finds nothing must not pass


def test_the_produced_reasons_are_real_text():
    for label, text in _produced().items():
        assert text, f"producer for {label!r} returned nothing; update the input in this test"


@pytest.mark.parametrize("source", ["static", "produced"])
def test_every_reason_has_its_own_plain_words(source):
    reasons = (
        {text: text for text in _discover_static()}
        if source == "static"
        else {text: label for label, text in _produced().items()}
    )
    assert reasons
    problems: list[str] = []
    for text, label in reasons.items():
        needs_try = retry_words.plain_reason(text)
        row = fit.failure_words(text)
        if needs_try == retry_words.REASON_FALLBACK:
            problems.append(f"Needs another try shows the fallback for: {label[:90]}")
        if row == fit.FAILURE_FALLBACK:
            problems.append(f"the Pipeline row shows the fallback for: {label[:90]}")
        if "—" in needs_try or "—" in row:
            problems.append(f"words use an em dash for: {label[:90]}")
    assert not problems, "\n".join(problems)


def test_static_reasons_do_not_share_one_sentence():
    by_words: dict[str, list[str]] = {}
    for text in _discover_static():
        by_words.setdefault(retry_words.plain_reason(text), []).append(text[:70])
    shared = {words: texts for words, texts in by_words.items() if len(texts) > 1}
    assert not shared, f"different reasons read the same on Needs another try: {shared}"


def _hosted_refusals() -> list[str]:
    found = sorted(t for t in _discover_static() if t.startswith("hosted send refused"))
    assert HOSTED_REFUSAL in found and len(found) >= 3, found  # the scan must see all three
    return found


def test_only_the_missing_employer_refusal_tells_the_person_to_choose_an_employer():
    from kestrel.docs.assess import refusal_error
    from kestrel.web.pages.documents import refusal_words

    for text in _hosted_refusals():
        employer = text == HOSTED_REFUSAL
        for words in (refusal_words(text), refusal_error(text)):
            assert ("current employer" in words) == employer, (text[:70], words)
            assert "sent" in words.lower(), (text[:70], words)  # each says nothing went out
            assert "—" not in words and "hosted send refused" not in words


def test_other_document_errors_pass_through_unchanged():
    from kestrel.web.pages.documents import refusal_words

    assert refusal_words(None) is None
    assert refusal_words("Could not reach the model: invented") == (
        "Could not reach the model: invented")
