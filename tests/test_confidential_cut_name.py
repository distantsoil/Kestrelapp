"""Issue #218, review finding B1 (KF-076, D-PRIV-810): the repair message never quotes a cut
piece of the employer name.

The guard's repair names the field that failed. A reply that copies a stand-in code into an
object key has the key restored to the name before validation; the field path was then cut to
60 characters before the repair was coded, so a cut name no longer read as a listed name and
left as written. Invariant: with Confidential mode on, no repair or retry message carries any
part of the employer name; nothing is sent that the guard has not coded in full.

Invented text only; no network. Every send is captured at LLMClient.complete."""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError, field_validator

from kestrel import privacy
from kestrel.llm.client import ChatResult, LLMClient, ProviderError
from kestrel.llm.guard import validation_reason
from kestrel.privacy import Employer, EmployerMap
from tests.test_confidential_contract import Select, _mix
from tests.test_confidential_sender import _db
from tests.test_contracts import EMPLOYER, MODEL, _confidential, _lines_world

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

ONE_WORD = "Zentrix"
# The prompts hold the plain word "invented", so a whole-name check starts past the first word.
WHOLE_START = EMPLOYER[:10]  # "Invented E"


def _capture(monkeypatch, reply, fail_first: Exception | None = None) -> list[str]:
    """Every message text handed to the client, in order; `fail_first` fails the first call."""
    sent: list[str] = []

    async def complete(self, *, tier, messages, schema=None, **kw):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        sent.append(text)
        if fail_first is not None and len(sent) == 1:
            raise fail_first
        return ChatResult(content=reply(text), model=MODEL)

    monkeypatch.setattr(LLMClient, "complete", complete)
    return sent


def _key_reply(make_key):
    """A doc-select reply whose `skill_groups` key is built from the send's own stand-in code,
    with a value that fails validation, so the repair names the field by that key."""
    def reply(text: str) -> str:
        codes = [c for c in privacy._LIVE if c in text]
        assert codes, "the send must carry a stand-in code for the reply to copy"
        return json.dumps({"layout": "a", "summary": "b", "slots": {}, "rationale": {},
                           "coherence_check": "c", "skill_groups": {make_key(codes[0]): "maybe"}})
    return reply


class _SelectFor(Select):
    """The doc-select site, asked about a role at `name`."""

    name = EMPLOYER

    def drive(self, client, held):
        from kestrel.docs import select as select_mod
        from kestrel.docs.catalog import load_catalog

        self.monkeypatch.setattr(
            select_mod, "_render_user",
            lambda spec, **kw: f"Pick the bullets for a role at {self.name}.",
        )

        async def go(s):
            catalog = await load_catalog(s, self.ids["master"])
            return await select_mod.select_config(
                s, client, catalog, opportunity_id=self.ids["strong"])

        return _db(go)


def _employer(monkeypatch, name: str) -> None:
    """Confidential mode on, with `name` as the one current employer."""
    _confidential(monkeypatch)
    mapping = EmployerMap([Employer(name, "a company")])

    async def mapped(_session, **_kwargs):
        return mapping

    monkeypatch.setattr("kestrel.docs.current_employer.load_employer_map", mapped)


def _site(monkeypatch, tmp_path, name: str = EMPLOYER):
    ids = _lines_world(monkeypatch)
    _employer(monkeypatch, name)
    site = _SelectFor(ids, monkeypatch, tmp_path)
    site.name = name
    client, held = _mix("held_local_client_online", site)
    return site, client, held


def _no_piece(sent: list[str], piece: str) -> None:
    hit = [p for p in sent if piece.lower() in p.lower()]
    assert not hit, f"A SEND NAMES PART OF THE EMPLOYER ({piece!r}):\n" + hit[0][-330:]


def _repair(sent: list[str]) -> str:
    repairs = [p for p in sent if "Your previous response was not valid" in p]
    assert repairs, "the invalid reply must be followed by a repair"
    return repairs[-1]


# ------------------------------------------------------------------ case 1: the reviewer's probe
def test_a_repair_never_quotes_a_cut_name_from_a_reply_key(monkeypatch, tmp_path):
    """The reviewer's `test_A`: the key is 44 letters, a space and the code, so the old cut at 60
    fell inside the restored name and "Invented Employ" left as written."""
    sent = _capture(monkeypatch, _key_reply(lambda code: "x" * 44 + " " + code))
    site, client, held = _site(monkeypatch, tmp_path)
    site.drive(client, held)
    assert len(sent) >= 2, len(sent)
    _repair(sent)
    _no_piece(sent, EMPLOYER[:15])  # "Invented Employ"
    _no_piece(sent, WHOLE_START)


# ------------------------------------------------------------------ case 2: one word, cut mid-word
def test_a_one_word_name_is_never_cut_mid_word(monkeypatch, tmp_path):
    """A one-word name loses only its last letters to the cut: 56 characters, then "Zent"."""
    sent = _capture(monkeypatch, _key_reply(lambda code: "x" * 55 + " " + code))
    site, client, held = _site(monkeypatch, tmp_path, ONE_WORD)
    site.drive(client, held)
    assert len(sent) >= 2, len(sent)
    _repair(sent)
    _no_piece(sent, ONE_WORD[:4])


# ------------------------------------------------------------------ case 3: twice in a long path
def test_a_long_path_holding_the_name_twice_sends_no_piece_of_it(monkeypatch, tmp_path):
    """The first copy sits whole inside the first 60 characters and the second straddles them:
    coding the whole one must not let the cut one through."""
    sent = _capture(
        monkeypatch, _key_reply(lambda code: code + " " + "x" * 24 + " " + code))
    site, client, held = _site(monkeypatch, tmp_path)
    site.drive(client, held)
    assert len(sent) >= 2, len(sent)
    _repair(sent)
    _no_piece(sent, EMPLOYER[:11])  # "Invented Em", what the cut left of the second copy
    _no_piece(sent, WHOLE_START)


# ------------------------------------------------------------------ case 4: through a 429 retry
def test_the_retry_after_a_429_never_quotes_a_cut_name(monkeypatch, tmp_path):
    """The first send is refused with a 429; the work is put back and sent again, and that
    send's reply draws the repair. Neither the retry nor its repair names any part."""
    sent = _capture(monkeypatch, _key_reply(lambda code: "x" * 44 + " " + code),
                    fail_first=ProviderError(429, "rate limited", "https://x.invalid"))
    site, client, held = _site(monkeypatch, tmp_path)
    try:
        site.drive(client, held)
    except ProviderError:
        pass  # the 429 surfaces to the caller at this site; the retry below is the point
    site.rearm()
    site.drive(client, held)
    assert len(sent) >= 3, len(sent)  # the refused send, the retry, the retry's repair
    _repair(sent)
    _no_piece(sent, EMPLOYER[:15])
    _no_piece(sent, WHOLE_START)


# ------------------------------------------------------------------ the owning function
def test_a_short_key_is_still_named_and_goes_coded(monkeypatch, tmp_path):
    """The fix hides nothing it need not: a key that fits is quoted whole, so the guard codes
    the name in it like the rest of the repair and the model still learns which field failed."""
    sent = _capture(monkeypatch, _key_reply(lambda code: "at " + code))
    site, client, held = _site(monkeypatch, tmp_path)
    site.drive(client, held)
    repair = _repair(sent)
    _no_piece(sent, WHOLE_START)
    error_line = next(ln for ln in repair.splitlines() if ln.startswith("Error:"))
    assert "`skill_groups.at " in error_line, error_line
    assert privacy.contains_live_code(error_line), error_line


class _Keys(BaseModel):
    groups: dict[str, bool]


class _Ruled(BaseModel):
    note: str

    @field_validator("note")
    @classmethod
    def _short(cls, value: str) -> str:
        if len(value) > 5:
            raise ValueError(f"the note {value!r} is too long")
        return value


def _reason(model: type[BaseModel], data: dict) -> str:
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data)
    return validation_reason(caught.value)


@pytest.mark.parametrize("length", [61, 75, 400])
def test_a_field_path_is_quoted_whole_or_not_at_all(length):
    """No cut of a reply's own key reaches the message at any length: over the limit the key
    is left out and the field is named by its place."""
    key = ("x" * 55 + " " + ONE_WORD + "y" * 400)[:length]
    reason = _reason(_Keys, {"groups": {key: "maybe"}})
    assert "xxxxx" not in reason, reason
    assert ONE_WORD[:3] not in reason, reason
    assert "`groups.(a long key)`: must be true or false" in reason, reason


def test_a_field_path_at_the_limit_is_quoted_whole():
    key = "x" * (60 - len(ONE_WORD)) + ONE_WORD
    assert len(key) == 60
    assert f"`groups.{key}`" in _reason(_Keys, {"groups": {key: "maybe"}})


def test_our_own_long_sentence_is_never_cut_either():
    """A validator's own sentence can repeat the reply. Over its limit it is replaced by the
    plain problem, never cut, for the same reason as the path."""
    value = "x" * 286 + " " + ONE_WORD  # the old cut at 300 left "Zen"
    reason = _reason(_Ruled, {"note": value})
    assert ONE_WORD[:3] not in reason, reason
    assert "`note`: not valid" in reason, reason
    short = _reason(_Ruled, {"note": "toolong"})
    assert "`note`: the note 'toolong' is too long" in short, short
