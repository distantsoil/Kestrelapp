"""Issue #218 round 2 (D-PRIV-803 to D-PRIV-806): the four leaks the adversarial review found on
28ddad7e, one group of tests per leak.

Invariant: with Confidential mode on, the current employer's name never leaves the machine in
any send, including repairs and retries, and never appears in MCP tool output.

1. The repair: the guard quoted pydantic's validation error, which carries the reply's own
   value after the stand-in codes were restored, so the second send named the employer (KF-071).
2. The matcher: a send hid a name only exactly as listed.
3. `llm.hosted_mode.redact_confidential: false` switched the hiding off with Confidential mode
   on.
4. Two MCP tools returned free text as stored.

Threat model: the product code is attacked as hostile; these tests catch a later accident and
are not a security boundary (D-PROC-12). All text is invented. Nothing reaches the network.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import unicodedata
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from kestrel import privacy
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting
from kestrel.llm.client import ChatResult, LLMClient
from kestrel.privacy import Employer, EmployerMap
from tests.test_confidential_contract import (
    ALL_TIERS,
    Assess,
    Bench,
    Import,
    Select,
    Tier1,
    Tier2,
    Tier3,
    _mix,
    _settings,
    _wrapper_send,
)
from tests.test_confidential_sender import _db, _hold_local
from tests.test_contracts import EMPLOYER, MODEL, TIERS, _confidential, _lines_world

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

ROOT = Path(__file__).resolve().parents[1]


class Sent:
    last: Sent

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.schemas: list[str] = []

    def named(self, name: str = EMPLOYER) -> list[str]:
        return [p for p in (*self.messages, *self.schemas) if name in p]


def _capture(monkeypatch, reply) -> Sent:
    rec = Sent()
    Sent.last = rec

    async def complete(self, *, tier, messages, schema=None, **kw):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        rec.messages.append(text)
        rec.schemas.append(json.dumps(schema, ensure_ascii=False) if schema else "")
        return ChatResult(content=reply(text), model=MODEL)

    monkeypatch.setattr(LLMClient, "complete", complete)
    return rec


# ----------------------------------------------------------------------------- 1. the repair
def _echo_reply(text: str) -> str:
    """A model that copies the stand-in code it was sent into fields of the wrong type (prose
    where a list or an enum belongs), so the guard's one repair runs."""
    codes = [c for c in privacy._LIVE if c in text]
    bad = f"keep, the team at {codes[0]}" if codes else "keep"
    return json.dumps({"verdict": bad, "variants": bad, "confidence": "high", "reason": "r",
                       "signals": []})


@pytest.mark.parametrize("tier", TIERS)
def test_the_wrapper_repair_never_names_the_employer(tier, monkeypatch):
    rec = _capture(monkeypatch, _echo_reply)
    _lines_world(monkeypatch)
    _confidential(monkeypatch)
    client = LLMClient(_settings())
    _hold_local(ALL_TIERS)
    _wrapper_send(client, tier)
    assert len(rec.messages) == 2, "the repair did not run, so this proves nothing"
    assert not rec.named(), "the repair named the employer:\n" + rec.named()[0][-400:]
    assert "input_value" not in rec.messages[1], "the repair quotes the validation error raw"
    assert "must be a list" in rec.messages[1], "the repair no longer says what was wrong"


@pytest.mark.parametrize("site_cls", [Tier1, Tier2, Tier3, Bench, Assess, Select, Import])
def test_no_direct_site_repair_names_the_employer(site_cls, monkeypatch, tmp_path):
    rec = _capture(monkeypatch, _echo_reply)
    ids = _lines_world(monkeypatch)
    _confidential(monkeypatch)
    site = site_cls(ids, monkeypatch, tmp_path)
    client, held = _mix("held_local_client_online", site)
    site.drive(client, held)
    assert len(rec.messages) >= 2, "the repair did not run, so this proves nothing"
    assert not rec.named(), "the repair named the employer:\n" + rec.named()[0][-400:]


class _Shape(BaseModel):
    verdict: int
    note: str


def test_a_validation_reason_never_quotes_the_reply():
    """The reason names the field and the kind of problem, from a fixed vocabulary."""
    from kestrel.llm.guard import parse_and_validate, reason_for_model, validation_reason

    reply = json.dumps({"verdict": f"the team at {EMPLOYER}"})
    value, reason = parse_and_validate(reply, _Shape)
    assert value is None
    assert EMPLOYER in reason, "the record kept on this computer names the value that failed"
    assert reason_for_model(reason) == (
        "schema validation failed: `verdict`: must be a whole number; "
        "`note`: required field missing")
    # A schema failure that reaches the repair as a plain string is not quoted either.
    assert reason_for_model(str(reason)) == "schema validation failed"
    with pytest.raises(ValidationError) as caught:
        _Shape.model_validate({"verdict": EMPLOYER, "note": 3})
    assert EMPLOYER in str(caught.value), "pydantic's own text quotes the value"
    assert EMPLOYER not in validation_reason(caught.value)


def _guarded(monkeypatch, reply, *, schema=None, mapping=None):
    """One guarded call on an online client with the employer coded, as a send site makes it."""
    from kestrel.llm.guard import run_guarded

    rec = _capture(monkeypatch, reply)
    mapping = mapping or EmployerMap([Employer(EMPLOYER, "a company")])
    messages = [{"role": "user", "content": f"Ran the night shift at {EMPLOYER}."}]
    book = mapping.codebook(m["content"] for m in messages)
    _lines_world(monkeypatch)
    client = LLMClient(_settings())
    result = asyncio.run(run_guarded(
        client, tier="tier2", messages=book.encode_messages(messages), model_cls=_Shape,
        schema=schema, reply_filter=book))
    return rec, result


def test_whatever_the_guard_adds_to_a_repair_is_coded(monkeypatch):
    """The guard's own text (here a reply that spells the name out itself, quoted back in the
    repair) goes through the same coding as the first send: redaction runs on every outgoing
    message, not only on the caller's."""
    rec, result = _guarded(
        monkeypatch, lambda _t: json.dumps({"verdict": f"ask {EMPLOYER}", "note": EMPLOYER}))
    assert len(rec.messages) == 2 and not result.ok
    assert not rec.named(), rec.named()[0][-300:]


def test_the_schema_of_a_send_is_coded_and_the_reply_restored(monkeypatch):
    """A per-call schema carries enum values and keys of the person's own (a title, a slot key
    built from the employer's name): they go coded, and the reply's codes come back as the
    values."""
    key = "r1_invented_employer_group_rollout"
    schema = {"type": "object",
              "properties": {"verdict": {"type": "integer"},
                             "note": {"type": "string", "enum": [key, f"Lead at {EMPLOYER}"]}},
              "required": ["verdict", "note"]}

    def reply(text: str) -> str:
        sent_schema = json.loads(Sent.last.schemas[-1])  # the model picks a value it was sent
        return json.dumps({"verdict": 1, "note": sent_schema["properties"]["note"]["enum"][0]})

    rec, result = _guarded(monkeypatch, reply, schema=schema)
    assert not rec.named(), rec.named()[0][-300:]
    assert "invented_employer_group" not in rec.schemas[0]
    assert result.ok and result.value.note == key


# ---------------------------------------------------------------------------- 2. the matcher
FORMS = {
    "exact": EMPLOYER,
    "possessive": EMPLOYER + "'s",
    "newline_split": "Invented Employer\nGroup",
    "double_space": "Invented  Employer   Group",
    "lowercase": EMPLOYER.lower(),
    "title_lower_mix": "Invented employer group",
    "camel": "InventedEmployerGroup",
    "underscore_slug": "invented_employer_group",
    "underscore_exact": "Invented_Employer_Group",
    "hyphen_slug": "invented-employer-group",
    "domain": "inventedemployer.com",
    "email": "pat@inventedemployergroup.com",
    "url": "https://www.example.invalid/company/invented-employer-group",
    "initials": "IEG",
    "hyphen_linebreak": "Invented Em-\nployer Group",
    "cyrillic_lookalike": "Invented Employer Grоup",
    "fullwidth": "Ｉnvented Employer Group",
    "zero_width": "Invented Emp​loyer Group",
    "nbsp": "Invented Employer Group",
    "plural_suffix": "Invented Employer Groups",
    "digit_adjacent": "Invented Employer Group2",
    "html_entity": "Invented&nbsp;Employer&nbsp;Group",
    "markdown_bold": "**Invented** Employer Group",
    "split_fields": "Invented | Employer | Group",
}


@pytest.mark.parametrize("form", sorted(FORMS))
def test_a_send_hides_the_name_however_it_is_written(form, monkeypatch):
    """Through a real send, and back: the reply's code is restored to exactly what was written."""
    rec = _capture(monkeypatch, lambda _t: "{}")
    _lines_world(monkeypatch)
    _confidential(monkeypatch)
    text = f"Shift lead at {FORMS[form]} since the invented winter."
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline._shared import run_prompt_hosted

    spec = dataclasses.replace(load_prompt("variant_suggest/v1"), tier="tier3")
    client = LLMClient(_settings())

    async def go(s):
        return await run_prompt_hosted(
            s, client, spec, [{"role": "user", "content": text}], quarantine_tier=4)

    _db(go)
    assert rec.messages, "nothing was sent"
    assert text not in rec.messages[0], f"sent as written: {FORMS[form]!r}"
    book = EmployerMap([Employer(EMPLOYER, "a company")]).codebook([text])
    assert book.decode(book.encode(text)) == text


def test_a_name_with_accents_is_hidden_composed_or_decomposed():
    name = "Société Inventée"
    mapping = EmployerMap([Employer(name, "a company")])
    apart = unicodedata.normalize("NFD", name)
    assert apart != name
    coded = [mapping.codebook([]).encode(f"Shift lead at {n}.") for n in (name, apart)]
    assert [n in c for n, c in zip((name, apart), coded, strict=True)] == [False, False], coded


def test_a_listed_alias_is_hidden_beside_any_word_edge():
    mapping = EmployerMap([Employer(EMPLOYER, "x", names=(EMPLOYER, "IEG", "ieg"))])
    texts = ("IEG", "IEG's", "at IEG.", "IEG_ops", "IEG2026", "team-IEG", "ieg_platform_lead",
             "r1_ieg_rollout", "https://ieg.example.invalid/careers", "IEGers", "I.E.G.")
    kept = [t for t in texts if mapping.codebook([t]).encode(t) == t]
    assert not kept, f"sent as written: {kept}"
    schema = mapping.codebook([]).encode_json(
        {"properties": {"r1_ieg_rollout": {"enum": ["ieg_a"]}}}, keys=True)
    assert "ieg" not in json.dumps(schema).lower(), schema


def test_the_wide_match_stops_at_word_edges_and_keeps_protected_labels():
    """What must NOT be hidden: a name inside a longer word, another employer's label, and an
    everyday-word name in a capitalization the person did not list."""
    alias = EmployerMap([Employer(EMPLOYER, "x", names=(EMPLOYER, "IEG"))])
    for text in ("the siege of the piegan trail", "an invented thing", "employer groupings"):
        assert alias.codebook([text]).encode(text) == text, text
    current = EmployerMap([Employer("Acme", "x")], protected=["Acme Labs"])
    coded = current.codebook([]).encode("Joined Acme Labs after Acme.")
    assert coded.startswith("Joined Acme Labs after ") and not coded.endswith("Acme.")
    everyday = EmployerMap([Employer("Apple", "x")])
    coded = everyday.codebook([]).encode("An apple a day at Apple and APPLE; a pineapple.")
    assert coded.startswith("An apple a day at ") and coded.endswith("; a pineapple.")
    assert "Apple" not in coded and "APPLE" not in coded


def test_what_a_person_reads_on_screen_is_still_matched_exactly():
    """Outputs keep D-PRIV-1's exact match: only a send and MCP output match widely."""
    mapping = EmployerMap([Employer(EMPLOYER, "a company")])
    text = "invented employer group and Invented Employer Group"
    assert mapping.redact(text) == "invented employer group and a company"
    assert mapping.redact_sent(text) == "a company and a company"


# --------------------------------------------------------------------------- 3. the setting
def test_the_redact_setting_cannot_switch_the_hiding_off(monkeypatch):
    rec = _capture(monkeypatch, lambda _t: "{}")
    _lines_world(monkeypatch)
    _confidential(monkeypatch)

    async def off(s):
        await s.merge(Setting(key="llm.hosted_mode.redact_confidential", value=False))

    _db(off)
    client = LLMClient(_settings())
    assert client.settings.llm.hosted_mode.redact_confidential is False
    for tier in TIERS:
        _wrapper_send(client, tier)
    assert len(rec.messages) == 2 * len(TIERS)
    assert not rec.named(), f"sent by name with the setting off: {len(rec.named())}"


def test_the_panels_say_hidden_when_the_send_hides(monkeypatch):
    """The three places that tell the person what happens to the employer read the same one
    condition as the send (KF-038)."""
    from kestrel.services.online_consent import employer_state
    from kestrel.web.pages.needs_try import _employer_state
    from kestrel.web.pages.renders import _employer_for

    _lines_world(monkeypatch)
    _confidential(monkeypatch)
    settings = _settings().model_copy(deep=True)
    settings.confidential_mode = True
    settings.llm.hosted_mode.redact_confidential = False

    async def go(s):
        return [await employer_state(s, settings), await _employer_state(s, settings),
                await _employer_for(s, settings, None)]

    assert _db(go) == ["hidden"] * 3


# --------------------------------------------------------------------------------- 4. MCP
def _mcp_world():
    from kestrel.db.models import Pursuit, Story
    from tests import test_confidential_api_mcp as m

    asyncio.run(m._seed(confidential=True))

    async def add():
        async with get_sessionmaker()() as s:
            s.add(Story(title=f"Cut overtime at {m._EMPLOYER}", is_canonical=True, themes=[]))
            await s.execute(Pursuit.__table__.update().values(
                next_action=f"Ask my manager at {m._EMPLOYER.lower()} for a reference"))
            await s.commit()

    asyncio.run(add())
    return m


@pytest.mark.parametrize("tool", ["get_story_bank", "get_pursuits"])
def test_an_mcp_tool_never_returns_the_employer(tool):
    m = _mcp_world()
    out = str(m._mcp_call(tool, {}))
    assert "reference" in out or "overtime" in out, "the seeded text is not in the output"
    assert m._EMPLOYER.lower() not in out.lower(), out[:300]


def test_every_mcp_tool_is_registered_through_the_one_exit():
    """`@tool` wraps a result in `_leaving`; `mcp.tool` appears once, inside `tool` itself."""
    source = (ROOT / "kestrel" / "mcp" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    build = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "build_mcp")
    decorated = [(fn.name, [ast.unparse(d) for d in fn.decorator_list])
                 for fn in ast.walk(build)
                 if isinstance(fn, ast.AsyncFunctionDef) and fn.decorator_list]
    tools = [name for name, decs in decorated if decs == ["tool"]]
    other = [(name, decs) for name, decs in decorated
             if decs != ["tool"] and name != "leaving"]
    assert len(tools) >= 15 and not other, (tools, other)
    assert source.count("mcp.tool") == 2, "one call inside `tool`, one mention in its docstring"


def test_a_code_never_reads_as_a_listed_name(monkeypatch):
    """Codes pass through the matcher again when the guard codes a repair. One that began with a
    short listed alias would be coded a second time and could not be restored."""
    mapping = EmployerMap([Employer(EMPLOYER, "x", names=(EMPLOYER, "IEG"))])
    draws = iter(["IEG2X9QK", "ABIEG234", "K7M2P9QR"])
    monkeypatch.setattr(privacy, "_new_code", lambda taken, blob: next(draws))
    book = mapping.codebook([])
    coded = book.encode(f"Lead at {EMPLOYER}.")
    assert coded == "Lead at ABIEG234.", "IEG2X9QK reads as the alias then a number; refused"
    assert book.encode(coded) == coded and book.decode(coded) == f"Lead at {EMPLOYER}."


ACCENTED = "Société Inventée"
ESCAPED = [
    (EMPLOYER, "https://example.invalid/?q=Invented%20Employer%20Group&z=1"),
    (EMPLOYER, json.dumps({"employer": "Invented\nEmployer Group"})),
    (EMPLOYER, json.dumps("Invented\tEmployer Group")),
    (ACCENTED, json.dumps(f"Lead at {ACCENTED}")),
    (ACCENTED, "Soci&eacute;t&eacute; Invent&eacute;e"),
    (ACCENTED, "Soci%C3%A9t%C3%A9%20Invent%C3%A9e"),
]


@pytest.mark.parametrize(("name", "text"), ESCAPED, ids=[t[:28] for _n, t in ESCAPED])
def test_a_name_written_with_escapes_is_hidden(name, text):
    """Text built with json.dumps, a web address and HTML all spell a name with escapes
    (D-PRIV-807, the lesson of KF-066 and KF-073 applied to names): each is read as what it
    stands for, and the code is restored to exactly what was written."""
    book = EmployerMap([Employer(name, "a company")]).codebook([text])
    coded = book.encode(text)
    assert coded != text and book.decode(coded) == text
    assert "invent" not in coded.lower(), coded
