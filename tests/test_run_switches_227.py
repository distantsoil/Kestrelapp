"""The two online switches agree (#227).

Two controls decide whether Kestrel runs online: the run choice card on Settings > This computer
(POST /settings/computer/mode) and the switch behind Settings > Online models
(POST /settings/hosted/switch, with Turn on, Turn off and Connect beside it). They used to write
different rows, so one page could say online while the other said this computer.

The invariant: the online state the two pages show and the state stored never disagree, and no
online send may go without recorded consent. The owned fields are `setup.run_choice`, `llm.mode`
and `llm.hosted_mode.enabled`. Named cases:

1. the two switches agree after a change in either;
2. turning one on while the other is off;
3. turning one off while a run is going (the rows the per-task send gate reads);
4. a reload after a change (a reload writes nothing);
5. a refused choice still refuses and still stores nothing;
6. two requests together.

Cloud-safe: TestClient on the real app, a throwaway data directory, invented values, every
provider call mocked, no model and no network."""

from __future__ import annotations

import ast
import asyncio
import html
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib import resources

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import Setting

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

CHOICE, MODE, ENABLED = "setup.run_choice", "llm.mode", "llm.hosted_mode.enabled"
ACKED_AT = "llm.hosted_mode.acknowledged_at"
ACKED_TERMS = "llm.hosted_mode.acknowledged_terms"


def _client() -> TestClient:
    migrate.run_migrations()
    forget_engine()
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _db(fn, commit=False):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                out = await fn(s)
                if commit:
                    await s.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _put(rows: dict) -> None:
    async def f(s):
        for key, value in rows.items():
            row = await s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value))
            else:
                row.value = value

    _db(f, commit=True)


def _online_rows(*, enabled: bool, choice: str | None = None, mode: str | None = None,
                 assigned: bool = True) -> None:
    """Online models set up once (the disclosure accepted as it reads now and, unless
    `assigned` is False, both scoring steps given an online model), switched on or off, with the
    run card rows as given."""
    from kestrel.config import HostedMode

    rows = {ENABLED: enabled, ACKED_AT: "2026-10-01T00:00:00+00:00",
            ACKED_TERMS: HostedMode().disclosure_terms()}
    if assigned:
        rows.update(_assigned_rows())
    if choice is not None:
        rows[CHOICE] = choice
    if mode is not None:
        rows[MODE] = mode
    _put(rows)


def _assigned_rows() -> dict:
    rows = {}
    for tier in ("tier1", "tier2"):
        rows[f"llm.hosted_tiers.{tier}.backend"] = "frontier"
        rows[f"llm.hosted_tiers.{tier}.model"] = "invented/model-for-tests"
    return rows


def _stored() -> dict:
    """The three owned fields as the setting table holds them (an absent row is None)."""
    async def f(s):
        found = await s.execute(
            select(Setting.key, Setting.value).where(Setting.key.in_((CHOICE, MODE, ENABLED))))
        return {k: v for k, v in found.all()}

    rows = _db(f)
    return {key: rows.get(key) for key in (CHOICE, MODE, ENABLED)}


def _settings():
    from kestrel.services.setting_overrides import load_settings_with_db

    return _db(load_settings_with_db)


def _gate() -> str | None:
    """What the one send gate says now, from the stored rows (None: a send may go). The client
    asks exactly this before every request to an online provider, and the worker asks the same
    rule of the same rows for each task of a run that is going."""
    from kestrel.services import setup_state as ss
    from kestrel.services.setting_overrides import load_settings_with_db

    async def f(s):
        return await ss.online_send_held_now(await load_settings_with_db(s))

    return _db(f)


def _card(html_text: str) -> str:
    """The run card's selected choice, as stored values (local, hosted, both)."""
    on = re.findall(r'aria-checked="true" data-testid="st-mode-(\w+)"', html_text)
    assert len(on) == 1, on
    return {"online": "hosted"}.get(on[0], on[0])


def _panel_says_online(html_text: str) -> bool:
    """The Online models panel's own statement of where Kestrel runs."""
    said = re.findall(r'data-testid="hosted-run-mode">.*?Kestrel runs on (online|local) models',
                      html_text, flags=re.S)
    assert len(said) == 1, said
    return said[0] == "online"


def _consent_recorded() -> bool:
    from kestrel.config import HostedMode

    async def f(s):
        found = await s.execute(select(Setting.key, Setting.value).where(
            Setting.key.in_((ENABLED, ACKED_AT, ACKED_TERMS))))
        return {k: v for k, v in found.all()}

    rows = _db(f)
    return (rows.get(ENABLED) is True and bool(rows.get(ACKED_AT))
            and rows.get(ACKED_TERMS) == HostedMode().disclosure_terms())


def _pair_problem(stored: dict) -> str | None:
    """Why the stored run choice and the stored mode disagree, or None. On this computer and
    Both run the everyday steps here, so the mode is local; Online model runs them online, so
    the mode is hosted once online models can run at all."""
    from kestrel.config import hosted_mode_problem

    choice, mode = stored[CHOICE], stored[MODE]
    if choice in ("local", "both") and mode not in (None, "local"):
        return f"the card holds {choice} and the mode row holds {mode}"
    if choice == "hosted" and mode != "hosted" and hosted_mode_problem(_settings()) is None:
        return f"the card holds hosted, online models can run, and the mode row holds {mode}"
    return None


def _agree(c, label: str) -> str:
    """THE invariant, checked on the rows and on both pages fetched again as a reload would.
    Returns the choice the card shows."""
    before = _stored()
    assert _pair_problem(before) is None, (label, _pair_problem(before), before)
    card = _card(c.get("/settings/computer/mode").text)
    panel = c.get("/settings/hosted").text
    # Case 4: a reload shows what is stored; it never changes it.
    assert _stored() == before, (label, "a reload changed the stored rows", before, _stored())
    consent = _consent_recorded()
    # Set up once and switched off: the card shows On this computer whatever is stored.
    switched_off = _acked() and before[ENABLED] is not True
    stored_shows_as = "local" if switched_off else before[CHOICE]
    assert before[CHOICE] is None or card == stored_shows_as, (label, card, before)
    gate = _gate()
    # A send may go only on recorded consent and an online choice.
    assert gate is not None or (consent and card != "local"), (label, card, before)
    # The two pages say the same thing: the panel says online exactly when the card shows
    # Online model and nothing holds the send.
    assert _panel_says_online(panel) == (card == "hosted" and gate is None), (label, card, gate)
    assert ("Online models on" in panel) == consent, label
    return card


def _acked() -> bool:
    async def f(s):
        row = await s.get(Setting, ACKED_AT)
        return row is not None and bool(row.value)

    return _db(f)


def _press(c, mode: str):
    return c.post("/settings/computer/mode", data={"mode": mode})


def _switch(c, mode: str):
    return c.post("/settings/hosted/switch", data={"mode": mode})


def _toggle(c, **form):
    return c.post("/settings/hosted/mode", data=form)


# ------------------------------------------------------------------ case 1: either switch
@pytest.mark.parametrize("start, to, want", [
    ("local", "hosted", "hosted"), ("both", "hosted", "hosted"), ("hosted", "hosted", "hosted"),
    ("local", "local", "local"), ("hosted", "local", "local"),
    # Both already runs the everyday steps on this computer: it stays Both.
    ("both", "local", "both"),
])
def test_the_online_models_switch_moves_the_run_card_with_it(start, to, want):
    """Case 1, from the Online models side. The switch used to write the mode row alone: the
    panel said Kestrel runs on online models while the card still showed On this computer and
    the gate held every send; or it said local while the card kept Online model and the next
    visit to the card wrote the online mode back."""
    from kestrel.services import setup_state as ss

    with _client() as c:
        _online_rows(enabled=True, choice=start, mode="hosted" if start == "hosted" else "local")
        assert _agree(c, "start") == start
        r = _switch(c, to)
        assert r.status_code == 200
        want_mode = "hosted" if want == "hosted" else "local"
        assert _stored() == {CHOICE: want, MODE: want_mode, ENABLED: True}
        assert _agree(c, f"{start} switched to {to}") == want
        assert _gate() == (ss.LOCAL_CHOSEN if want == "local" else None)


@pytest.mark.parametrize("start", ["local", "both", "hosted"])
@pytest.mark.parametrize("to", ["local", "online", "both"])
def test_the_run_card_moves_the_online_models_panel_with_it(start, to):
    """Case 1, from the This computer side: after a press on the card, the panel says the same."""
    with _client() as c:
        _online_rows(enabled=True, choice=start, mode="hosted" if start == "hosted" else "local")
        assert _press(c, to).status_code == 200
        want = {"online": "hosted"}.get(to, to)
        assert _stored() == {CHOICE: want, MODE: "hosted" if want == "hosted" else "local",
                             ENABLED: True}
        assert _agree(c, f"{start} to {to}") == want


def test_rows_left_by_the_old_switch_read_the_same_on_both_pages():
    """An install that used the old switch holds On this computer on the card and hosted in the
    mode row. Both pages now say this computer, as the send gate does."""
    from kestrel.services import setup_state as ss

    with _client() as c:
        _online_rows(enabled=True, choice="local", mode="hosted")
        card = _card(c.get("/settings/computer/mode").text)
        panel = c.get("/settings/hosted").text
        assert card == "local" and _gate() == ss.LOCAL_CHOSEN
        assert not _panel_says_online(panel)
        # Pressing the legacy fix or the card settles the rows.
        _switch(c, "local")
        assert _stored() == {CHOICE: "local", MODE: "local", ENABLED: True}
        assert _agree(c, "settled") == "local"


# Every place the Online models panel says that something is being sent or that Kestrel runs
# online, as it appears in the rendered page. Each is drawn from the send gate's one answer
# (`sending`, or `runs_online` = sending and the hosted mode), chosen in the same statement as
# its words (KF-038). The deeper-reads chip is NOT one of them: a deeper read is sent on its own
# per-role confirmation whatever the gate says, so its chip has its own tests below.
SAYS_SENDING = {
    "the run pill": "Kestrel runs on online models",
    "the per-step chips": "online, sending",
    "the picture's label": "Sending now.",
    "the picture, lit": 'class="ho-flow is-on"',
    "the picture's live line": "ho-f-line--on",
}
# The same places, as the template's own source line and the gate variable it must read.
_SOURCE_READS = {
    "Kestrel runs on online models": "runs_online",
    "online, sending": "steps_sending",
    "Sending now.": "sending",
    "ho-flow{{ ' is-on'": "runs_online",
}


def _said(panel: str) -> set[str]:
    return {where for where, words in SAYS_SENDING.items() if words in panel}


def test_rows_left_by_the_old_switch_say_sending_nowhere_on_the_panel():
    """The held state: On this computer on the card, hosted in the mode row, online models on.
    The gate holds every send, so no part of the panel may say one is under way. The per-step
    chips used to read "online, sending" under a status line that said nothing is sent."""
    from kestrel.services import setup_state as ss

    with _client() as c:
        _online_rows(enabled=True, choice="local", mode="hosted")
        assert _gate() == ss.LOCAL_CHOSEN
        panel = c.get("/settings/hosted").text
        assert _said(panel) == set()
        # The two scoring steps keep their assignment, said in the words for a step that is
        # not in use now. Deeper reads is not one of them: it can still be sent, asking first.
        assert panel.count("assigned, used on an online run") == 2
        assert panel.count(DEEPER_ASKS) == 1
        assert HELD_STATUS in html.unescape(panel)
        assert "nothing is sent" not in _status_line(panel)
        assert _picture_label(panel) == PICTURE_LEAD + "Held: " + HELD_LEAD + " " + LOCAL_SAYS


def test_online_chosen_and_running_says_sending_in_every_place():
    """The control for the test above: with Online model chosen and nothing holding the send,
    every one of those places says so."""
    with _client() as c:
        _online_rows(enabled=True, choice="hosted", mode="hosted")
        assert _gate() is None
        panel = c.get("/settings/hosted").text
        assert _said(panel) == set(SAYS_SENDING)
        assert panel.count("online, sending") == 2  # Quick screen and Full review
        assert panel.count(DEEPER_ASKS) == 1  # Deeper reads
        assert "assigned, used on an online run" not in panel


# What the panel says about the scoring steps for every stored combination of the three owned
# fields (online models on?, the card choice, the mode row), with the disclosure accepted, both
# scoring steps assigned and no key saved. Written out, not computed. Each row holds the two
# facts it rests on, read from the settings (may a send go by the one gate; the mode the
# settings resolve to), then what the page shows: the run pill says online, the scoring steps'
# chips say "online, sending", the picture's label says "Sending now.", the picture is lit.
#   - Online models off: nothing is sent, and a hosted mode row resolves to local.
#   - On this computer chosen: the gate holds every scoring send, whatever the mode row says.
#   - Otherwise a send may go. The pill, the chips, "Sending now." and the lit picture say
#     online only when the everyday steps run online (the mode resolves to hosted); when they run
#     on this computer the picture is unlit with no note on it, and its label says so in the
#     owner's words (LOCAL_SAYS, items 61 and 63).
#     With no choice stored the card's default is not a decision, so the gate does not hold.
_OPEN, _HELD = True, False
SCORING_STEPS_SAY = {
    # (on, choice, mode row):       (gate, resolved, pill, chips, sending now, lit)
    (True, None, None):             (_OPEN, "local", False, False, False, False),
    (True, None, "local"):          (_OPEN, "local", False, False, False, False),
    (True, None, "hosted"):         (_OPEN, "hosted", True, True, True, True),
    (True, "local", None):          (_HELD, "local", False, False, False, False),
    (True, "local", "local"):       (_HELD, "local", False, False, False, False),
    (True, "local", "hosted"):      (_HELD, "hosted", False, False, False, False),
    (True, "both", None):           (_OPEN, "local", False, False, False, False),
    (True, "both", "local"):        (_OPEN, "local", False, False, False, False),
    (True, "both", "hosted"):       (_OPEN, "hosted", True, True, True, True),
    (True, "hosted", None):         (_OPEN, "local", False, False, False, False),
    (True, "hosted", "local"):      (_OPEN, "local", False, False, False, False),
    (True, "hosted", "hosted"):     (_OPEN, "hosted", True, True, True, True),
    (False, None, None):            (_HELD, "local", False, False, False, False),
    (False, None, "local"):         (_HELD, "local", False, False, False, False),
    (False, None, "hosted"):        (_HELD, "local", False, False, False, False),
    (False, "local", None):         (_HELD, "local", False, False, False, False),
    (False, "local", "local"):      (_HELD, "local", False, False, False, False),
    (False, "local", "hosted"):     (_HELD, "local", False, False, False, False),
    (False, "both", None):          (_HELD, "local", False, False, False, False),
    (False, "both", "local"):       (_HELD, "local", False, False, False, False),
    (False, "both", "hosted"):      (_HELD, "local", False, False, False, False),
    (False, "hosted", None):        (_HELD, "local", False, False, False, False),
    (False, "hosted", "local"):     (_HELD, "local", False, False, False, False),
    (False, "hosted", "hosted"):    (_HELD, "local", False, False, False, False),
}


@pytest.mark.parametrize("combination", sorted(SCORING_STEPS_SAY, key=repr), ids=repr)
def test_the_panel_says_sending_only_when_the_gate_lets_a_send_go(combination, monkeypatch):
    """The sweep, over every stored combination of the three owned fields (consistent or left
    over from before): what the panel says of the scoring steps is the written table's row,
    and the row's own facts are first checked against the settings."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    enabled, choice, mode = combination
    gate_open, resolved, pill, chips, sending_now, lit = SCORING_STEPS_SAY[combination]
    with _client() as c:
        _online_rows(enabled=enabled, choice=choice, mode=mode)
        # The facts the row is written from.
        assert (_gate() is None) == gate_open, combination
        assert _settings().llm.mode == resolved, combination
        panel = c.get("/settings/hosted").text
        said = _said(panel)
        assert ("the run pill" in said) == pill, combination
        assert ("the per-step chips" in said) == chips, combination
        assert panel.count("online, sending") == (2 if chips else 0), combination
        assert ("the picture's label" in said) == sending_now, combination
        assert ("the picture, lit" in said) == lit, combination
        assert ("the picture's live line" in said) == lit, combination
        # Lit while the everyday steps run on this computer: the label says so, in the owner's
        # words, and never "Sending now.".
        state = (enabled, gate_open, sending_now)
        assert _picture_label(panel) == PICTURE_LEAD + PICTURE_ENDS[state], combination
        assert _picture_notes(panel) == PICTURE_NOTE[state], combination
        assert _status_line(panel).endswith(STATUS_ENDS[state]), combination


def test_no_row_of_the_table_says_sending_while_the_gate_holds():
    """The table itself keeps the invariant: nothing says sending on a held row, and the pill
    and the chips say online only on a row that is open and resolves to hosted."""
    assert len(SCORING_STEPS_SAY) == 24
    for combination, (gate_open, resolved, pill, chips, sending_now, lit) in (
            SCORING_STEPS_SAY.items()):
        assert gate_open or not (pill or chips or sending_now or lit), combination
        assert pill == chips == sending_now == lit == (
            gate_open and resolved == "hosted"), combination


# The owner's words for what the panel says where scoring does not go online (owner, item 61,
# 2026-11-25). Written out here; the template holds the shared sentences once.
DEEPER_CONFIRM = "A deeper read is sent only if you confirm it for that role."
LOCAL_SAYS = "Scoring stays on this computer. " + DEEPER_CONFIRM
HELD_LEAD = "On this computer is chosen under Settings > This computer."
HELD_STATUS = (HELD_LEAD + " " + LOCAL_SAYS
               + " Choose Online model or Both there to use online models for scoring.")
OFF_STATUS = "Every step runs on this machine. " + DEEPER_CONFIRM
FACT_BULLET = "Scoring stays on this computer until you turn online models on."
PICTURE_LEAD = ("Your computer sends text over HTTPS, under your own key, to the provider you "
                "choose. ")
# How the picture's label ends, by (online models on, a send may go, scoring runs online).
PICTURE_ENDS = {
    (True, True, True): "Sending now.",
    (True, True, False): LOCAL_SAYS,
    (True, False, False): "Held: " + HELD_LEAD + " " + LOCAL_SAYS,
    (False, False, False): "Off until you turn it on.",
}
# The short note drawn on the unlit picture (once on each of its two drawings), same key. With
# online models on, a send allowed and scoring on this computer there is none (owner, item 63:
# "Unlit, no new words").
PICTURE_NOTE = {
    (True, True, True): [],
    (True, True, False): [],
    (True, False, False): ["Held", "Held"],
    (False, False, False): ["Off until you turn it on", "Off until you turn it on"],
}
ONLINE_STATUS = ("Online sends happen only on an online run: the choice on Settings > This "
                 "computer, a Run now choice, or the CLI flag. " + DEEPER_CONFIRM)
# How the status line ends, same key.
STATUS_ENDS = {
    (True, True, True): ONLINE_STATUS,
    (True, True, False): ONLINE_STATUS,
    (True, False, False): HELD_STATUS,
    (False, False, False): OFF_STATUS,
}
# What these replaced. None of them is true of a deeper read the person confirms.
OLD_CLAIMS = (
    "so nothing is sent",
    "nothing is sent until you choose Online model or Both there",
    "Nothing is sent until you turn online models on.",
    "every step runs on this machine and nothing leaves it",
)


def _picture_label(panel: str) -> str:
    """The picture's label (both drawings of it carry the same one)."""
    labels = set(re.findall(r'<svg class="ho-flow__(?:wide|narrow)"[^>]*aria-label="([^"]*)"',
                            panel))
    assert len(labels) == 1, labels
    return html.unescape(labels.pop())


def _picture_notes(panel: str) -> list[str]:
    return [html.unescape(note) for note in re.findall(
        r'<text class="ho-f-note"[^>]*>([^<]*)</text>', panel)]


def _status_line(panel: str) -> str:
    found = re.findall(r'<p class="ho-status" data-testid="hosted-ack-status">(.*?)</p>', panel,
                       flags=re.S)
    assert len(found) == 1, len(found)
    return html.unescape(re.sub(r"<[^>]+>", "", found[0]))


@pytest.mark.parametrize("combination", sorted(SCORING_STEPS_SAY, key=repr), ids=repr)
def test_the_panel_never_says_nothing_is_sent_and_always_carries_the_fact_bullet(
        combination, monkeypatch):
    """In every stored combination: none of the replaced claims is on the panel, and the first
    fact bullet reads the owner's sentence."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    enabled, choice, mode = combination
    with _client() as c:
        _online_rows(enabled=enabled, choice=choice, mode=mode)
        panel = html.unescape(c.get("/settings/hosted").text)
        assert [claim for claim in OLD_CLAIMS if claim in panel] == [], combination
        assert panel.count(FACT_BULLET) == 1, combination
        assert "Deeper reads ask you first, every time." in panel, combination


@pytest.mark.parametrize("choice, mode", [(None, None), ("local", "local"), ("hosted", "hosted")])
def test_with_online_models_off_the_status_says_every_step_runs_here_and_a_deeper_read_asks(
        choice, mode):
    with _client() as c:
        _online_rows(enabled=False, choice=choice, mode=mode)
        status = _status_line(c.get("/settings/hosted").text)
        assert status.endswith(OFF_STATUS), status
        assert "nothing leaves it" not in status


def test_a_deeper_read_is_sent_on_its_confirmation_with_online_models_off(monkeypatch):
    """The fact the off and held sentences rest on, at the last step before the request: the
    send gate holds (online models are off), a scoring call to the online service is refused,
    and the confirmed deeper-read call (allow_external, as pipeline/tier3.py makes it) goes."""
    import httpx

    from kestrel.llm.client import ExternalBackendError, LLMClient
    from kestrel.services import setup_state as ss

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    ask = [{"role": "user", "content": "invented text"}]

    async def call(settings, **kw) -> int:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(url__regex=r"https://openrouter\.ai/.*").mock(
                return_value=Response(200, json={
                    "choices": [{"message": {"content": '{"ok": true}'},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
            async with httpx.AsyncClient() as http:
                try:
                    await LLMClient(settings, http=http).complete(
                        tier="frontier", messages=ask, schema=schema, schema_name="probe", **kw)
                except ExternalBackendError:
                    pass
            return route.call_count

    with _client():
        _online_rows(enabled=False, choice="local", mode="local")
        settings = _settings()
        assert ss.online_send_held(settings) == ss.ONLINE_OFF
        assert asyncio.run(call(settings)) == 0
        assert asyncio.run(call(settings, allow_external=True)) == 1

# The Deeper reads chip. A deeper read never goes by itself: it is sent only after the person
# confirms it for that one role, and that confirmation is its whole gate (the run choice and the
# online models switch do not hold it; pipeline/tier3.py passes allow_external=True). So the
# chip says what the step is, in every state where the step names an online service (owner,
# 2026-11-25: "chip says 'online, asks first for each one' always").
DEEPER_ASKS = "online, asks first for each one"
DEEPER_LOCAL = "no online assignment; stays local"
# The chip per stored combination (online models on?, the card choice, the mode row), written
# out and not computed: the packaged settings name an online service for deeper reads, and none
# of the three fields changes that.
DEEPER_CHIP = {
    (True, None, None): DEEPER_ASKS, (True, None, "local"): DEEPER_ASKS,
    (True, None, "hosted"): DEEPER_ASKS, (True, "local", None): DEEPER_ASKS,
    (True, "local", "local"): DEEPER_ASKS, (True, "local", "hosted"): DEEPER_ASKS,
    (True, "both", None): DEEPER_ASKS, (True, "both", "local"): DEEPER_ASKS,
    (True, "both", "hosted"): DEEPER_ASKS, (True, "hosted", None): DEEPER_ASKS,
    (True, "hosted", "local"): DEEPER_ASKS, (True, "hosted", "hosted"): DEEPER_ASKS,
    (False, None, None): DEEPER_ASKS, (False, None, "local"): DEEPER_ASKS,
    (False, None, "hosted"): DEEPER_ASKS, (False, "local", None): DEEPER_ASKS,
    (False, "local", "local"): DEEPER_ASKS, (False, "local", "hosted"): DEEPER_ASKS,
    (False, "both", None): DEEPER_ASKS, (False, "both", "local"): DEEPER_ASKS,
    (False, "both", "hosted"): DEEPER_ASKS, (False, "hosted", None): DEEPER_ASKS,
    (False, "hosted", "local"): DEEPER_ASKS, (False, "hosted", "hosted"): DEEPER_ASKS,
}


def _deeper_row(panel: str) -> str:
    """The Deeper reads row of the per-step table, from its name to the next step's form field."""
    rows = re.findall(r'<div class="ho-tier__name"><b>Deeper reads</b>.*?</div>', panel, flags=re.S)
    assert len(rows) == 1, len(rows)
    return rows[0]


def _deeper_step_is_online() -> tuple[bool, bool]:
    """(the deeper-reads step names an online service, a key for it is saved), read from the
    settings and never from the page: the facts the chip is checked against."""
    from kestrel.llm import providers as prov

    settings = _settings()
    step = settings.llm.tiers[prov.FRONTIER_TIER]
    backend = settings.llm.backends[step.backend]
    return bool(backend.external), bool(backend.api_key) or prov.secret_present(
        backend.api_key_env)


@pytest.mark.parametrize("combination", sorted(DEEPER_CHIP, key=repr), ids=repr)
def test_the_deeper_reads_chip_says_what_the_step_is_in_every_stored_combination(
        combination, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    enabled, choice, mode = combination
    with _client() as c:
        _online_rows(enabled=enabled, choice=choice, mode=mode)
        assert _deeper_step_is_online() == (True, False)  # the fact the table is written from
        row = _deeper_row(c.get("/settings/hosted").text)
        said = [text for text in (DEEPER_ASKS, DEEPER_LOCAL, "online, sending",
                                  "assigned, used on an online run") if text in row]
        assert said == [DEEPER_CHIP[combination]], (combination, said)
        # No key is saved here, and the row says so beside the chip.
        assert "No key saved" in row


def test_the_deeper_reads_chip_is_the_same_with_a_key_saved_and_drops_the_key_note(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    with _client() as c:
        _online_rows(enabled=True, choice="local", mode="hosted")  # held
        assert _deeper_step_is_online() == (True, True)
        row = _deeper_row(c.get("/settings/hosted").text)
        assert DEEPER_ASKS in row and "assigned, used on an online run" not in row
        assert "No key saved" not in row


def test_a_deeper_reads_step_that_names_no_online_service_says_it_stays_local():
    """The step pointed at the local backend (a row a person can store): the chip says so, in
    the words every other unassigned step uses, and never says online."""
    with _client() as c:
        _online_rows(enabled=True, choice="hosted", mode="hosted")
        _put({"llm.tiers.frontier.backend": "local"})
        assert _deeper_step_is_online()[0] is False
        row = _deeper_row(c.get("/settings/hosted").text)
        assert DEEPER_LOCAL in row and DEEPER_ASKS not in row
        assert "No key saved" not in row


def test_every_place_the_panel_says_sending_reads_the_gate_in_its_own_line():
    """The template itself: each of those words sits on a line that reads the gate's variable,
    and those variables are defined from the gate's answer (`send_held`) and nothing else."""
    source = resources.files("kestrel").joinpath(
        "web", "templates", "partials", "hosted_status.html").read_text(encoding="utf-8")
    lines = source.splitlines()
    for words, variable in _SOURCE_READS.items():
        holding = [line for line in lines if words in line]
        assert len(holding) == 1, (words, len(holding))
        assert re.search(rf"\b{variable}\b", holding[0]), (words, variable)
    assert "{% set sending = online_on and not send_held %}" in source
    assert "{% set runs_online = steps_sending|length > 0 %}" in source
    # The live line of the picture is drawn only inside a block that asks the gate.
    for at, line in enumerate(lines):
        assert "ho-f-line--on" not in line or lines[at - 1].strip() == "{% if runs_online %}", at


# A row from before the Local and Online choice: the LOCAL tier set's own Full review step names
# an online service (`llm.tiers.tier2.backend`). That step sends whenever the gate lets a send
# go, whatever the mode row says, because the client asks the step's backend and the gate and
# never the mode (llm/client.py). Owner, 2026-11-25: "Keep sending, but make the pill, picture
# and chip say it is sent". So with that row stored the page says online wherever a step really
# sends, and the row's own warning says "sends" only while it does.
LEGACY_ROW = {"llm.tiers.tier2.backend": "frontier", "llm.tiers.tier2.model": "invented/legacy"}
LEGACY_SENDS = "so it sends even on a local run"
# With LEGACY_ROW stored, for every stored combination of the three owned fields, written out:
# the facts (may a send go; the mode the settings resolve to; Quick screen's and Full review's
# calls go online, by the client's own rule), then the page: the pill and the picture say
# online, each step's chip says "online, sending", the warning says the step sends.
#   - A send may go: Full review sends (its own row, or its online assignment under the hosted
#     mode), so the pill and the picture say online. Quick screen sends only under hosted.
#   - Held or off: nothing is sent, nothing says so, and the warning does not say it sends.
#   - The status line's sentence that online sends happen only on an online run is not shown
#     while such a row sends, under any mode row: with the hosted mode stored the row would
#     still send on a local run (owner, 2026-11-25: "Hide that line while an old row sends").
#     It was never part of the held and off lines, so the column is False in all 24 rows; the
#     sentence itself is checked where it is true, in SCORING_STEPS_SAY.
LEGACY_ROW_SAYS = {
    # (on, choice, mode row):       (gate, resolved, quick sends, full sends, pill and picture,
    #                                the status line says sends happen only on an online run)
    (True, None, None):             (_OPEN, "local", False, True, True, False),
    (True, None, "local"):          (_OPEN, "local", False, True, True, False),
    (True, None, "hosted"):         (_OPEN, "hosted", True, True, True, False),
    (True, "local", None):          (_HELD, "local", False, False, False, False),
    (True, "local", "local"):       (_HELD, "local", False, False, False, False),
    (True, "local", "hosted"):      (_HELD, "hosted", False, False, False, False),
    (True, "both", None):           (_OPEN, "local", False, True, True, False),
    (True, "both", "local"):        (_OPEN, "local", False, True, True, False),
    (True, "both", "hosted"):       (_OPEN, "hosted", True, True, True, False),
    (True, "hosted", None):         (_OPEN, "local", False, True, True, False),
    (True, "hosted", "local"):      (_OPEN, "local", False, True, True, False),
    (True, "hosted", "hosted"):     (_OPEN, "hosted", True, True, True, False),
    (False, None, None):            (_HELD, "local", False, False, False, False),
    (False, None, "local"):         (_HELD, "local", False, False, False, False),
    (False, None, "hosted"):        (_HELD, "local", False, False, False, False),
    (False, "local", None):         (_HELD, "local", False, False, False, False),
    (False, "local", "local"):      (_HELD, "local", False, False, False, False),
    (False, "local", "hosted"):     (_HELD, "local", False, False, False, False),
    (False, "both", None):          (_HELD, "local", False, False, False, False),
    (False, "both", "local"):       (_HELD, "local", False, False, False, False),
    (False, "both", "hosted"):      (_HELD, "local", False, False, False, False),
    (False, "hosted", None):        (_HELD, "local", False, False, False, False),
    (False, "hosted", "local"):     (_HELD, "local", False, False, False, False),
    (False, "hosted", "hosted"):    (_HELD, "local", False, False, False, False),
}


ONLINE_RUN_ONLY = ("Online sends happen only on an online run: the choice on Settings > This "
                   "computer, a Run now choice, or the CLI flag.")
# How the status line ends with such a row stored, by (online models on, a send may go, the
# status line says sends happen only on an online run).
LEGACY_STATUS_ENDS = {
    (True, True, True): ONLINE_RUN_ONLY + " " + DEEPER_CONFIRM,
    (True, True, False): ". " + DEEPER_CONFIRM,  # straight after "Status: acknowledged <date>."
    (True, False, False): HELD_STATUS,
    (False, False, False): OFF_STATUS,
}


def _step_chip(panel: str, step: str) -> list[str]:
    """Which of the chip texts the named step's row of the per-step table shows."""
    rows = re.findall(rf'<div class="ho-tier__name"><b>{step}</b>.*?</div>', panel, flags=re.S)
    assert len(rows) == 1, (step, len(rows))
    return [text for text in ("online, sending", "assigned, used on an online run",
                              DEEPER_LOCAL, DEEPER_ASKS) if text in rows[0]]


def _legacy_warning(panel: str) -> list[str]:
    return [html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text))).strip()
            for text in re.findall(r'<p class="ho-legacy__text">(.*?)</p>', panel, flags=re.S)]


@pytest.mark.parametrize("combination", sorted(LEGACY_ROW_SAYS, key=repr), ids=repr)
def test_with_a_row_from_before_the_choice_the_panel_says_what_each_step_does(
        combination, monkeypatch):
    from kestrel.pipeline._shared import tier_is_hosted

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    enabled, choice, mode = combination
    gate_open, resolved, quick, full, online, only_online_run = LEGACY_ROW_SAYS[combination]
    with _client() as c:
        _online_rows(enabled=enabled, choice=choice, mode=mode)
        _put(LEGACY_ROW)
        # The facts the row is written from, by the rule the client itself applies.
        settings = _settings()
        assert (_gate() is None) == gate_open, combination
        assert settings.llm.mode == resolved, combination
        assert tier_is_hosted(settings, "tier1") == quick, combination
        assert tier_is_hosted(settings, "tier2") == full, combination
        panel = c.get("/settings/hosted").text
        assert _panel_says_online(panel) == online, combination
        assert ('class="ho-flow is-on"' in panel) == online, combination
        assert ("ho-f-line--on" in panel) == online, combination
        assert _picture_label(panel).endswith("Sending now.") == online, combination
        sending, assigned = ["online, sending"], ["assigned, used on an online run"]
        assert _step_chip(panel, "Quick screen") == (sending if quick else assigned), combination
        assert _step_chip(panel, "Full review") == (sending if full else assigned), combination
        assert _step_chip(panel, "Deeper reads") == [DEEPER_ASKS], combination
        # The warning names the step in every state, and says it sends only while it does.
        warning = _legacy_warning(panel)
        assert len(warning) == 1 and warning[0].startswith("Full review (tier2) is set to "), (
            combination, warning)
        assert (LEGACY_SENDS in warning[0]) == gate_open, (combination, warning)
        assert "A setting row from before the Local and Online choice does this." in warning[0]
        # The status line: the rest of it stays, the one sentence is there only where it is so.
        status = re.sub(r"\s+", " ", _status_line(panel)).strip()
        assert (ONLINE_RUN_ONLY in status) == only_online_run, (combination, status)
        state = (enabled, gate_open, only_online_run)
        assert status.endswith(LEGACY_STATUS_ENDS[state]), (combination, status)
        assert ("Status: acknowledged" in status) == enabled, (combination, status)


def test_the_table_for_a_row_from_before_the_choice_keeps_the_invariant():
    assert len(LEGACY_ROW_SAYS) == 24
    for combination, (gate_open, resolved, quick, full, online, only_online_run) in (
            LEGACY_ROW_SAYS.items()):
        assert gate_open or not (quick or full or online or only_online_run), combination
        assert not only_online_run, combination
        assert full == online == gate_open, combination
        assert quick == (gate_open and resolved == "hosted"), combination


# The same kind of row on a step that has NO online assignment (Document selection): its own row
# is the only thing that names an online service. For every stored combination, written out: the
# facts (may a send go; the step's call goes online, by the client's own rule), then the page:
# the step's chip, the lane its node is drawn in, and whether the pill says online.
#   - A send may go: the step sends, on any mode row (an unassigned step is never overlaid).
#   - Held or off: the call is REFUSED by the client, it does not run on this computer, so the
#     chip reads as an assigned step that is not in use, never "stays local", and the node
#     stays in the Online lane: the step is set to an online service in every state.
_SENDING, _ASSIGNED = "online, sending", "assigned, used on an online run"
UNASSIGNED_STEP_ROW = {"llm.tiers.docsel.backend": "frontier",
                       "llm.tiers.docsel.model": "invented/legacy"}
UNASSIGNED_STEP_SAYS = {
    # (on, choice, mode row):       (gate, step sends, chip, lane, pill)
    (True, None, None):             (_OPEN, True, _SENDING, "Online", True),
    (True, None, "local"):          (_OPEN, True, _SENDING, "Online", True),
    (True, None, "hosted"):         (_OPEN, True, _SENDING, "Online", True),
    (True, "local", None):          (_HELD, False, _ASSIGNED, "Online", False),
    (True, "local", "local"):       (_HELD, False, _ASSIGNED, "Online", False),
    (True, "local", "hosted"):      (_HELD, False, _ASSIGNED, "Online", False),
    (True, "both", None):           (_OPEN, True, _SENDING, "Online", True),
    (True, "both", "local"):        (_OPEN, True, _SENDING, "Online", True),
    (True, "both", "hosted"):       (_OPEN, True, _SENDING, "Online", True),
    (True, "hosted", None):         (_OPEN, True, _SENDING, "Online", True),
    (True, "hosted", "local"):      (_OPEN, True, _SENDING, "Online", True),
    (True, "hosted", "hosted"):     (_OPEN, True, _SENDING, "Online", True),
    (False, None, None):            (_HELD, False, _ASSIGNED, "Online", False),
    (False, None, "local"):         (_HELD, False, _ASSIGNED, "Online", False),
    (False, None, "hosted"):        (_HELD, False, _ASSIGNED, "Online", False),
    (False, "local", None):         (_HELD, False, _ASSIGNED, "Online", False),
    (False, "local", "local"):      (_HELD, False, _ASSIGNED, "Online", False),
    (False, "local", "hosted"):     (_HELD, False, _ASSIGNED, "Online", False),
    (False, "both", None):          (_HELD, False, _ASSIGNED, "Online", False),
    (False, "both", "local"):       (_HELD, False, _ASSIGNED, "Online", False),
    (False, "both", "hosted"):      (_HELD, False, _ASSIGNED, "Online", False),
    (False, "hosted", None):        (_HELD, False, _ASSIGNED, "Online", False),
    (False, "hosted", "local"):     (_HELD, False, _ASSIGNED, "Online", False),
    (False, "hosted", "hosted"):    (_HELD, False, _ASSIGNED, "Online", False),
}


def _lane_of(panel: str, short: str) -> list[str]:
    """The lane (Online, Your network, This computer) the step's node is listed under in the
    compact map, and the row class of its node in the wide one; both must agree."""
    compact = re.findall(r'data-testid="hosted-lanes-compact">(.*?)</ul>', panel, flags=re.S)
    assert len(compact) == 1
    lanes = [label for label, tags in re.findall(
        r'<li><b>([^<]+)</b><span class="ho-tags">(.*?)</span></li>', compact[0], flags=re.S)
        if f'<span class="ho-tag">{short}</span>' in tags]
    row = re.findall(rf'<span class="ho-node \S+ (ho-r\d)"><span class="ho-nd"></span>'
                     rf'<span class="ho-nl">{short}</span>', panel)
    wide = [{"ho-r1": "Online", "ho-r2": "Your network", "ho-r3": "This computer"}[r] for r in row]
    assert lanes == wide, (short, lanes, wide)
    return lanes


@pytest.mark.parametrize("combination", sorted(UNASSIGNED_STEP_SAYS, key=repr), ids=repr)
def test_a_step_with_no_online_assignment_and_a_row_from_before_the_choice(
        combination, monkeypatch):
    """Its chip used to read "no online assignment; stays local" while the step sent. After the
    first fix it still read that when the gate closed, and this test pinned it: wrong, because
    a held call to an online backend is refused by the client, it is not run on this computer.
    And its node was drawn in the This computer lane beside a chip that said sending."""
    from kestrel.pipeline._shared import tier_is_hosted

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    enabled, choice, mode = combination
    gate_open, sends, chip, lane, pill = UNASSIGNED_STEP_SAYS[combination]
    with _client() as c:
        _online_rows(enabled=enabled, choice=choice, mode=mode)
        _put(UNASSIGNED_STEP_ROW)
        settings = _settings()
        assert (_gate() is None) == gate_open, combination
        assert tier_is_hosted(settings, "docsel") == sends, combination
        assert "docsel" not in settings.llm.hosted_tiers  # no online assignment of its own
        panel = c.get("/settings/hosted").text
        assert _step_chip(panel, "Document selection") == [chip], combination
        assert _lane_of(panel, "Documents") == [lane], combination
        assert _panel_says_online(panel) == pill, combination
        warning = _legacy_warning(panel)
        assert len(warning) == 1 and warning[0].startswith(
            "Document selection (docsel) is set to "), (combination, warning)
        assert (LEGACY_SENDS in warning[0]) == gate_open, (combination, warning)
        # Under every mode row, the hosted one included: the old row sends on a local run, so
        # the status line never says online sends happen only on an online run.
        assert ONLINE_RUN_ONLY not in re.sub(r"\s+", " ", _status_line(panel)), combination


def test_a_step_with_no_row_of_its_own_and_no_assignment_still_says_it_stays_local():
    """The control: without such a row an unassigned step stays in the This computer lane and
    its chip says so, whatever is chosen."""
    with _client() as c:
        _online_rows(enabled=True, choice="hosted", mode="hosted")
        panel = c.get("/settings/hosted").text
        assert _step_chip(panel, "Document selection") == [DEEPER_LOCAL]
        assert _lane_of(panel, "Documents") == ["This computer"]
        assert _lane_of(panel, "Review") == ["Online"]


# The same rule for the two steps the panel did not read an own row for: Research and drafts
# (tier3) and Signals (trigger). Each case: the step, the stored state, then the fact (its call
# goes online, by the client's own rule), its chip and its lane. Written out.
OTHER_STEPS_SAY = [
    # (tier, name, map label, on, choice, mode row, sends, chip, lane)
    ("tier3", "Research and drafts", "Research", True, "both", "local", True, _SENDING, "Online"),
    ("tier3", "Research and drafts", "Research", True, None, "local", True, _SENDING, "Online"),
    ("tier3", "Research and drafts", "Research", True, "local", "local", False, _ASSIGNED,
     "Online"),
    ("tier3", "Research and drafts", "Research", False, "local", "local", False, _ASSIGNED,
     "Online"),
    ("trigger", "Signals", "Signals", True, "both", "local", True, _SENDING, "Online"),
    ("trigger", "Signals", "Signals", True, None, "local", True, _SENDING, "Online"),
    ("trigger", "Signals", "Signals", True, "local", "local", False, _ASSIGNED, "Online"),
    ("trigger", "Signals", "Signals", False, "local", "local", False, _ASSIGNED, "Online"),
]


@pytest.mark.parametrize("case", OTHER_STEPS_SAY, ids=lambda c: f"{c[0]}-{c[3]}-{c[4]}-{c[5]}")
def test_an_old_row_on_research_or_signals_reads_like_any_other_step(case, monkeypatch):
    """Sending and held: the chip and the lane follow the rule every other step follows. The
    panel used to read an own row only for Quick screen, Full review and Document selection, so
    when held these two read "no online assignment; stays local" in the This computer lane."""
    from kestrel.pipeline._shared import tier_is_hosted

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    tier, name, short, enabled, choice, mode, sends, chip, lane = case
    with _client() as c:
        _online_rows(enabled=enabled, choice=choice, mode=mode)
        _put({f"llm.tiers.{tier}.backend": "frontier", f"llm.tiers.{tier}.model": "invented/old"})
        settings = _settings()
        assert tier not in settings.llm.hosted_tiers  # no online assignment of its own
        assert tier_is_hosted(settings, tier) == sends, case
        panel = c.get("/settings/hosted").text
        assert _step_chip(panel, name) == [chip], case
        assert _lane_of(panel, short) == [lane], case
        assert _panel_says_online(panel) == sends, case
        # The other steps are untouched by that row.
        assert _step_chip(panel, "Document selection") == [DEEPER_LOCAL], case
        assert _lane_of(panel, "Documents") == ["This computer"], case
        # The row has its warning, in the same sentence as any other step's, with its one fix
        # button; it says the step sends only while a send may go.
        warning = _legacy_warning(panel)
        assert len(warning) == 1, (case, warning)
        assert warning[0].startswith(
            f"{name} ({tier}) is set to frontier/invented/old, an online backend"), warning
        assert (LEGACY_SENDS in warning[0]) == (_gate() is None) == sends, (case, warning)
        assert "A setting row from before the Local and Online choice does this." in warning[0]
        assert panel.count('data-testid="hosted-legacy-fix"') == 1, case
        # The button takes its row away, and with it the warning; the owned fields stay.
        owned = _stored()
        form = _fix_forms(panel)[tier]
        posted = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form))
        assert posted == {"mode": "local", "tier": tier}, case
        assert c.post("/settings/hosted/switch", data=posted).status_code == 200
        assert _stored() == owned, case
        assert _db(lambda s: s.get(Setting, f"llm.tiers.{tier}.backend")) is None, case
        after = c.get("/settings/hosted").text
        assert _legacy_warning(after) == [] and 'data-testid="hosted-legacy-fix"' not in after
        assert _step_chip(after, name) == [DEEPER_LOCAL], case
        assert _lane_of(after, short) == ["This computer"], case


@pytest.mark.parametrize("name, short", [("Research and drafts", "Research"),
                                         ("Signals", "Signals")])
def test_research_and_signals_without_such_a_row_stay_local(name, short):
    with _client() as c:
        _online_rows(enabled=True, choice="hosted", mode="hosted")
        panel = c.get("/settings/hosted").text
        assert _step_chip(panel, name) == [DEEPER_LOCAL]
        assert _lane_of(panel, short) == ["This computer"]
        assert _legacy_warning(panel) == []


# A provider that answered "out of credit" is held by the client for a while: the call is
# refused before any request (llm/client.py, OutOfCredit with sent=False). The panel says a
# step sends only when no refusal of the client's applies, that one included. Two different
# claims are on the page, and each has its own fact:
#   - the chip, the pill and the picture say what the step's call does NOW (the route the
#     settings resolve to under the stored mode);
#   - the old row's warning ("sends even on a local run") and the status line's sentence
#     ("online sends happen only on an online run") are claims about a LOCAL run: would the
#     old row's own provider take the call on a local run.
# They part when Full review's online assignment and its old row name two providers.
# Each case: the stored state; which rows are stored for Full review ("none", "old row": its
# own row names OpenRouter, "two providers": that, and its online assignment names Anthropic);
# which provider is on hold; then the facts and the page.
CREDIT_HOLD_SAYS = [
    # (choice, mode row, rows, on hold, Full review's call is online now, requests a LOCAL
    #  run's Full review call makes to OpenRouter (None: it runs on this computer),
    #  quick chip, full chip, pill and picture say online, the warning says it sends)
    ("hosted", "hosted", "none", None, True, None, _SENDING, _SENDING, True, None),
    ("hosted", "hosted", "none", "frontier", True, None, _ASSIGNED, _ASSIGNED, False, None),
    ("both", "local", "none", None, False, None, _ASSIGNED, _ASSIGNED, False, None),
    ("both", "local", "none", "frontier", False, None, _ASSIGNED, _ASSIGNED, False, None),
    # The only sending step is the one with the old row: on hold, nothing on the page says
    # sending, the warning included.
    ("both", "local", "old row", None, True, 1, _ASSIGNED, _SENDING, True, True),
    ("both", "local", "old row", "frontier", True, 0, _ASSIGNED, _ASSIGNED, False, False),
    ("hosted", "hosted", "old row", None, True, 1, _SENDING, _SENDING, True, True),
    ("hosted", "hosted", "old row", "frontier", True, 0, _ASSIGNED, _ASSIGNED, False, False),
    # Two providers. OpenRouter on hold: Full review still sends now, through Anthropic, and
    # Quick screen (assigned to OpenRouter) does not; a local run would send nothing, so the
    # warning does not say it sends.
    ("hosted", "hosted", "two providers", None, True, 1, _SENDING, _SENDING, True, True),
    ("hosted", "hosted", "two providers", "frontier", True, 0, _ASSIGNED, _SENDING, True, False),
    # Anthropic on hold: Full review does not send now, Quick screen does; a local run would
    # send through OpenRouter, so the warning says so and the online-run sentence is hidden.
    ("hosted", "hosted", "two providers", "anthropic", True, 1, _SENDING, _ASSIGNED, True, True),
]


@pytest.mark.parametrize(
    "case", CREDIT_HOLD_SAYS, ids=lambda c: f"{c[0]}-{c[1]}-{c[2]}-hold={c[3]}")
def test_a_provider_held_as_out_of_credit_is_not_shown_as_sending(case, monkeypatch):
    import httpx

    from kestrel.config import load_settings
    from kestrel.llm import credit
    from kestrel.llm import providers as prov
    from kestrel.llm.client import LLMClient, tier_credit_hold
    from kestrel.pipeline._shared import tier_is_hosted
    from kestrel.services.setting_overrides import load_settings_with_db

    keys = {"frontier": "sk-or-invented", "anthropic": "sk-ant-invented"}
    for name, key in keys.items():
        monkeypatch.setenv(prov.external_backends(load_settings())[name].api_key_env, key)
    (choice, mode, rows, hold, full_online, local_requests, quick_chip, full_chip, online,
     warns) = case
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}

    async def openrouter_requests(settings) -> int:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(url__regex=r"https://openrouter\.ai/.*").mock(
                return_value=Response(200, json={
                    "choices": [{"message": {"content": '{"ok": true}'},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
            async with httpx.AsyncClient() as http:
                try:
                    await LLMClient(settings, http=http).complete(
                        tier="tier2", messages=[{"role": "user", "content": "invented text"}],
                        schema=schema, schema_name="probe")
                except credit.OutOfCredit as exc:
                    assert exc.sent is False
            return route.call_count

    stored = {
        "none": {},
        "old row": LEGACY_ROW,
        "two providers": {**LEGACY_ROW, "llm.hosted_tiers.tier2.backend": "anthropic",
                          "llm.hosted_tiers.tier2.model": "invented/other-provider"},
    }[rows]
    with _client() as c:
        _online_rows(enabled=True, choice=choice, mode=mode)
        _put(stored)
        credit.clear()
        held = [hold] if hold else []
        for name in held:
            credit.mark_out_of_credit(name, name, keys[name])
        settings = _settings()
        local = _db(lambda s: load_settings_with_db(s, mode="local"))
        # The facts. The gate is open in every case.
        assert _gate() is None, case
        # NOW: Full review's call is online, on the backend the stored mode resolves it to,
        # and the client's own hold check says whether that backend is held.
        now_backend = "anthropic" if rows == "two providers" else "frontier"
        assert tier_is_hosted(settings, "tier2") == full_online, case
        assert (settings.llm.tiers["tier2"].backend == now_backend) == full_online, case
        assert (tier_credit_hold(settings, "tier2") is not None) == (
            full_online and hold == now_backend), case
        # A LOCAL run: the call itself, on the settings a local run resolves, when the old
        # row makes it an online one (None: it runs on this computer and is never called).
        on_local_run = tier_is_hosted(local, "tier2")
        assert on_local_run == (local_requests is not None), case
        sent = asyncio.run(openrouter_requests(local)) if on_local_run else None
        assert sent == local_requests, case
        panel = c.get("/settings/hosted").text
        assert _step_chip(panel, "Quick screen") == [quick_chip], case
        assert _step_chip(panel, "Full review") == [full_chip], case
        assert _panel_says_online(panel) == online, case
        assert ('class="ho-flow is-on"' in panel) == online, case
        assert ("ho-f-line--on" in panel) == online, case
        assert _picture_label(panel).endswith("Sending now.") == online, case
        # The old row's warning says "sends" exactly when a local run's call would go.
        warning = _legacy_warning(panel)
        assert len(warning) == (0 if rows == "none" else 1), (case, warning)
        said = [LEGACY_SENDS in text for text in warning]
        assert said == ([] if warns is None else [warns]), (case, warning)
        assert (warns is None) == (rows == "none"), case
        assert warns is None or warns == (local_requests == 1), case
        # And the status line says online sends happen only on an online run exactly when no
        # old row would send on a local run.
        status = re.sub(r"\s+", " ", _status_line(panel))
        assert (ONLINE_RUN_ONLY in status) == (warns is not True), (case, status)

# The fix button of a warning: it names one row, and it takes away that row and nothing else.
# With two such rows stored the one button used to remove both, and since the switch became
# the run card's writer it also changed the run choice. Each case: the stored state. In every
# case the press removes the named step's row, keeps the other step's row, and leaves the three
# owned fields exactly as they were.
TWO_OLD_ROWS = {
    "llm.tiers.tier2.backend": "frontier", "llm.tiers.tier2.model": "invented/old-review",
    "llm.tiers.docsel.backend": "frontier", "llm.tiers.docsel.model": "invented/old-docs",
}
FIX_BUTTON_CASES = [
    # (on, choice, mode row)
    (True, None, None), (True, None, "local"), (True, "local", "local"),
    (True, "both", "local"), (True, "hosted", "hosted"), (True, "hosted", "local"),
    (True, "local", "hosted"), (False, "local", "local"), (False, "hosted", "hosted"),
]


def _old_rows() -> dict:
    async def f(s):
        found = await s.execute(select(Setting.key, Setting.value).where(
            Setting.key.in_(tuple(TWO_OLD_ROWS))))
        return {k: v for k, v in found.all()}

    return _db(f)


def _fix_forms(panel: str) -> dict[str, str]:
    """Each warning's own fix form, by the step its warning names: {tier: form html}."""
    out = {}
    for block in panel.split('data-testid="hosted-legacy-row"')[1:]:
        block = block.split("</div>")[0]
        named = re.findall(r'<p class="ho-legacy__text">.*?\(<span class="mono">(\w+)</span>\)',
                           block, flags=re.S)
        forms = re.findall(r"<form\b[^>]*>.*?</form>", block, flags=re.S)
        assert len(named) == 1 and len(forms) == 1, (named, len(forms))
        out[named[0]] = forms[0]
    return out


@pytest.mark.parametrize("pressed, kept", [("docsel", "tier2"), ("tier2", "docsel")])
@pytest.mark.parametrize("state", FIX_BUTTON_CASES, ids=repr)
def test_a_fix_button_takes_away_the_row_it_names_and_nothing_else(state, pressed, kept,
                                                                    monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    enabled, choice, mode = state
    with _client() as c:
        _online_rows(enabled=enabled, choice=choice, mode=mode)
        _put(TWO_OLD_ROWS)
        owned, rows = _stored(), _old_rows()
        forms = _fix_forms(c.get("/settings/hosted").text)
        assert sorted(forms) == ["docsel", "tier2"], state
        # The form posts what it names: the route, the mode word it always posted, its step.
        form = forms[pressed]
        assert 'hx-post="/settings/hosted/switch"' in form
        posted = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form))
        assert posted == {"mode": "local", "tier": pressed}, (state, posted)
        assert "Move it back to the local model" in form
        r = c.post("/settings/hosted/switch", data=posted)
        assert r.status_code == 200
        assert _stored() == owned, (state, owned, _stored())
        left = _old_rows()
        assert left == {k: v for k, v in rows.items() if f".{kept}." in k}, (state, left)
        after = _fix_forms(c.get("/settings/hosted").text)
        assert sorted(after) == [kept], state


@pytest.mark.parametrize("tier", ["frontier", "tier9", "tier1"])
def test_a_fix_press_for_a_step_with_no_such_row_changes_nothing(tier, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with _client() as c:
        _online_rows(enabled=True, choice="hosted", mode="hosted")
        _put(TWO_OLD_ROWS)
        owned, rows = _stored(), _old_rows()
        deeper = _db(lambda s: s.get(Setting, "llm.tiers.frontier.backend"))
        r = c.post("/settings/hosted/switch", data={"mode": "local", "tier": tier})
        assert r.status_code == 200
        assert _stored() == owned and _old_rows() == rows
        after = _db(lambda s: s.get(Setting, "llm.tiers.frontier.backend"))
        assert (deeper is None) == (after is None)

@pytest.mark.parametrize("choice, sends", [("both", True), ("local", False)])
def test_cannot_switch_yet_is_said_whenever_switching_is_impossible(choice, sends, monkeypatch):
    """Online models on, Full review assigned and Quick screen not: the hosted mode cannot be
    chosen, and the page says "Cannot switch yet" with the reason. A row from before the choice
    that makes Full review send does not hide that note."""
    from kestrel.config import hosted_mode_problem
    from kestrel.pipeline._shared import tier_is_hosted

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with _client() as c:
        _online_rows(enabled=True, choice=choice, mode="local", assigned=False)
        _put({"llm.hosted_tiers.tier2.backend": "frontier",
              "llm.hosted_tiers.tier2.model": "invented/model-for-tests", **LEGACY_ROW})
        settings = _settings()
        problem = hosted_mode_problem(settings)
        assert problem is not None and "tier1" in problem
        assert tier_is_hosted(settings, "tier2") == sends
        panel = html.unescape(c.get("/settings/hosted").text)
        assert _panel_says_online(panel) == sends
        assert f"Cannot switch yet: {problem}." in panel

@pytest.mark.parametrize("choice, sent", [("both", 1), ("local", 0)])
def test_the_client_sends_a_step_whose_own_row_is_online_exactly_when_the_gate_is_open(
        choice, sent, monkeypatch):
    """The fact the tables above rest on, at the last step before the request: the mode row is
    local, the step's own row names the online service, and the call goes exactly when the gate
    lets a send go."""
    import httpx

    from kestrel.llm.client import ExternalBackendError, LLMClient

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}

    async def call(settings) -> int:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(url__regex=r"https://openrouter\.ai/.*").mock(
                return_value=Response(200, json={
                    "choices": [{"message": {"content": '{"ok": true}'},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
            async with httpx.AsyncClient() as http:
                try:
                    await LLMClient(settings, http=http).complete(
                        tier="tier2", messages=[{"role": "user", "content": "invented text"}],
                        schema=schema, schema_name="probe")
                except ExternalBackendError:
                    pass
            return route.call_count

    with _client():
        _online_rows(enabled=True, choice=choice, mode="local")
        _put(LEGACY_ROW)
        settings = _settings()
        assert settings.llm.mode == "local"
        assert asyncio.run(call(settings)) == sent


# ---------------------------------------------------- case 2: one on while the other is off
def test_choosing_online_then_turning_online_models_on_needs_no_second_page_view():
    """Case 2. Online model chosen on the card before online models were ever turned on is the
    pending setup. Turning them on with the tick used to leave the mode row on local until some
    page happened to be opened; a page view then wrote it."""
    from kestrel.services import setup_state as ss

    with _client() as c:
        _put(_assigned_rows())
        assert "online model" in _press(c, "online").headers["HX-Trigger"]
        assert _stored() == {CHOICE: "hosted", MODE: None, ENABLED: None}
        assert _agree(c, "pending") == "hosted" and _gate() == ss.ONLINE_OFF
        # Without the tick nothing is recorded and nothing moves.
        _toggle(c, action="on")
        assert _stored() == {CHOICE: "hosted", MODE: None, ENABLED: None}
        assert not _consent_recorded()
        _toggle(c, action="on", ack="on")
        assert _consent_recorded()
        assert _stored() == {CHOICE: "hosted", MODE: "hosted", ENABLED: True}
        assert _agree(c, "turned on") == "hosted" and _gate() is None


def test_turning_online_models_on_never_chooses_online_for_the_person():
    """Case 2, the other way round: online models turned on while the card is on this computer
    changes neither card row, and the gate still holds every send."""
    from kestrel.services import setup_state as ss

    with _client() as c:
        _online_rows(enabled=False, choice="local", mode="local")
        _toggle(c, action="on", ack="on")
        assert _stored() == {CHOICE: "local", MODE: "local", ENABLED: True}
        assert _agree(c, "on, card local") == "local" and _gate() == ss.LOCAL_CHOSEN
        # The switch to online is refused while online models are off, and stores nothing.
        _toggle(c, action="off")
        before = _stored()
        r = _switch(c, "hosted")
        assert "Still on local models" in r.text
        assert _stored() == before == {CHOICE: "local", MODE: "local", ENABLED: False}
        assert _agree(c, "refused while off") == "local" and _gate() == ss.ONLINE_OFF


def test_neither_switch_writes_a_consent_row():
    """Nothing here turns online models on by itself: only the ticked form records consent."""
    with _client() as c:
        _put(_assigned_rows())
        for step in (lambda: _press(c, "online"), lambda: _press(c, "both"),
                     lambda: _switch(c, "hosted"), lambda: _switch(c, "local"),
                     lambda: _press(c, "local"), lambda: _toggle(c, action="on")):
            assert step().status_code == 200
            assert not _consent_recorded() and not _acked()
            assert _stored()[ENABLED] is None
            assert _gate() is not None


# -------------------------------------------- case 3: turning one off while a run is going
@pytest.mark.parametrize("off", ["the card", "the switch", "turn off"])
def test_turning_either_one_off_holds_the_next_send_of_a_run_that_is_going(off):
    """Case 3. A run that began online holds its settings for the whole cycle and asks the send
    gate, on the stored rows, before each role. Whichever control says this computer, the gate
    holds the next send. The Online models switch used to leave the card on Online model, so
    the gate let the rest of the run go online after the page said local."""
    from kestrel.services import setup_state as ss

    with _client() as c:
        _online_rows(enabled=True, choice="hosted", mode="hosted")
        began = _settings()  # what the run holds: loaded while online
        assert began.llm.mode == "hosted" and ss.online_send_held(began) is None

        def held_for_the_run() -> str | None:
            async def f(_s):
                return await ss.online_send_held_now(began)

            return _db(f)

        assert held_for_the_run() is None
        if off == "the card":
            _press(c, "local")
        elif off == "the switch":
            _switch(c, "local")
        else:
            _toggle(c, action="off")
        want = ss.ONLINE_OFF if off == "turn off" else ss.LOCAL_CHOSEN
        assert held_for_the_run() == want
        # The worker's own per-task read is the same rule on the same rows.
        rows = _db(lambda s: _online_state_rows(s))
        live = ss.hosted_mode_from_rows(began.llm.hosted_mode, rows)
        now = began.model_copy(update={"llm": began.llm.model_copy(update={"hosted_mode": live})})
        assert ss.online_send_held(now) == want
        assert _agree(c, f"off by {off}") == "local"


async def _online_state_rows(s) -> dict:
    """The rows the worker reads for each task (worker._hosted_now)."""
    from sqlalchemy import or_

    from kestrel.config import RUN_CARD_KEYS

    found = await s.execute(select(Setting.key, Setting.value).where(
        or_(Setting.key.like("llm.hosted_mode.%"), Setting.key.in_(RUN_CARD_KEYS))))
    return {k: v for k, v in found.all()}


def test_turning_online_models_off_and_on_again_stays_on_this_computer():
    """Turn off writes the card choice through the same owner as the card does."""
    from kestrel.services import setup_state as ss

    with _client() as c:
        _online_rows(enabled=True, choice="both", mode="local")
        _toggle(c, action="off")
        assert _stored() == {CHOICE: "local", MODE: "local", ENABLED: False}
        assert _agree(c, "off") == "local" and _gate() == ss.ONLINE_OFF
        _toggle(c, action="on", ack="on")
        assert _stored() == {CHOICE: "local", MODE: "local", ENABLED: True}
        assert _agree(c, "on again") == "local" and _gate() == ss.LOCAL_CHOSEN


# ------------------------------------------------------------------- Connect and the CLI
_OR = "https://openrouter.ai/api/v1"
_MODELS = {"data": [
    {"id": "invented/cheap", "context_length": 32000,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "invented/strong", "context_length": 128000,
     "pricing": {"prompt": "0.000003", "completion": "0.000015"},
     "supported_parameters": ["structured_outputs"]},
]}


def _connect(c, **form):
    with respx.mock as router:
        router.get(f"{_OR}/key").mock(
            return_value=Response(200, json={"data": {"label": "k", "is_free_tier": False}}))
        router.get(f"{_OR}/models").mock(return_value=Response(200, json=_MODELS))
        return c.post("/settings/hosted/connect", data={"provider": "openrouter", **form})


@pytest.mark.parametrize("start", [None, "local", "both", "hosted"])
def test_connect_with_switch_now_moves_the_card_and_the_mode_together(start, monkeypatch):
    """Connect's "Switch Kestrel to online models now" wrote the mode row alone, so the card
    kept On this computer (and the gate held every send) or kept Both."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    with _client() as c:
        if start is not None:
            _press(c, {"hosted": "online"}.get(start, start))
        assert _stored() == {CHOICE: start, MODE: None if start in (None, "hosted") else "local",
                             ENABLED: None}
        r = _connect(c, ack="on", switch="on")
        assert "Connected" in r.text, r.text[:400]
        assert _stored() == {CHOICE: "hosted", MODE: "hosted", ENABLED: True}
        assert _agree(c, "connected and switched") == "hosted" and _gate() is None


@pytest.mark.parametrize("start", ["local", "both"])
def test_connect_without_the_switch_leaves_the_card_and_the_mode_alone(start, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    with _client() as c:
        assert _press(c, start).status_code == 200
        before = _stored()
        r = _connect(c, ack="on")
        assert "Connected" in r.text, r.text[:400]
        assert _stored() == {**before, ENABLED: True}
        assert _agree(c, "connected, not switched") == start


def test_a_press_on_the_choice_shown_by_default_is_stored_and_survives_connect(monkeypatch):
    """A fresh install shows On this computer with nothing stored. Pressing it used to store
    nothing, so connecting a provider later turned the card to Both with no press on the card.
    The press is the person's choice: it is stored once, and a second press says nothing."""
    from kestrel.services import setup_state as ss

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    with _client() as c:
        assert _stored() == {CHOICE: None, MODE: None, ENABLED: None}
        assert _card(c.get("/settings/computer/mode").text) == "local"
        assert "k-toast" in _press(c, "local").headers.get("HX-Trigger", "")
        assert _stored() == {CHOICE: "local", MODE: "local", ENABLED: None}
        assert "HX-Trigger" not in _press(c, "local").headers
        assert "Connected" in _connect(c, ack="on").text
        assert _stored() == {CHOICE: "local", MODE: "local", ENABLED: True}
        assert _agree(c, "connected after choosing this computer") == "local"
        assert _gate() == ss.LOCAL_CHOSEN


def test_connect_without_the_tick_changes_no_owned_field(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    with _client() as c:
        _press(c, "local")
        before = _stored()
        c.post("/settings/hosted/connect", data={"provider": "openrouter", "switch": "on"})
        assert _stored() == before and not _consent_recorded()


def test_the_setup_rows_carry_the_card_choice_with_the_mode():
    """`kestrel setup --hosted` and Connect write the same rows (setup.hosted_rows): the run
    choice travels with the mode, from the owner's one mapping."""
    from kestrel import setup as setup_mod
    from kestrel.config import load_settings
    from kestrel.llm import providers as prov
    from kestrel.services import setup_state as ss

    backend = prov.external_backends(load_settings())["frontier"]
    pick = prov.ModelInfo(id="invented/model-for-tests")
    rows = setup_mod.hosted_rows(
        "frontier", backend, {t: pick for t in setup_mod.PICK_TIERS}, "2026-10-01T00:00:00+00:00")
    assert {k: rows.get(k) for k in (CHOICE, MODE)} == {CHOICE: "hosted", MODE: "hosted"}
    assert {k: rows[k] for k in (CHOICE, MODE)} == ss.run_card_rows("hosted", hosted_ready=True)


def test_the_owner_mapping_of_a_choice_to_its_rows():
    from kestrel.services import setup_state as ss

    assert ss.run_card_rows("local", hosted_ready=True) == {CHOICE: "local", MODE: "local"}
    assert ss.run_card_rows("both", hosted_ready=True) == {CHOICE: "both", MODE: "local"}
    assert ss.run_card_rows("hosted", hosted_ready=True) == {CHOICE: "hosted", MODE: "hosted"}
    # Online model chosen before online models can run: the mode row is left as it is.
    assert ss.run_card_rows("hosted", hosted_ready=False) == {CHOICE: "hosted"}
    with pytest.raises(ValueError):
        ss.run_card_rows("online", hosted_ready=True)


# --------------------------------------------------- case 5: the refused choice (regression)
def test_a_choice_the_card_cannot_show_is_still_refused_and_stores_nothing():
    """Case 5. Online models set up, then switched off: Online model and Both are refused on
    the card with the reason, and neither the card nor the Online models switch stores them."""
    import html

    from kestrel.services import setup_state as ss

    with _client() as c:
        _online_rows(enabled=False, choice="local", mode="local")
        before = _stored()
        for asked in ("online", "both"):
            r = _press(c, asked)
            assert r.status_code == 200
            assert "k-toast" not in r.headers.get("HX-Trigger", "")
            said = re.findall(r'data-testid="st-mode-message"[^>]*>([^<]+)<', r.text)
            assert [html.unescape(t) for t in said] == [ss.ONLINE_OFF]
            assert _stored() == before
        assert "Still on local models" in _switch(c, "hosted").text
        assert _stored() == before
        assert _agree(c, "refused") == "local" and _gate() == ss.ONLINE_OFF


def test_online_switched_off_between_the_switch_check_and_its_write_stores_no_online_choice(
        monkeypatch):
    """The same moment as the card's own race: online models are switched off after the Online
    models switch decided it may go online and before its rows are committed."""
    from kestrel.services import setup_state as ss

    real = ss.set_run_mode
    fired: list[str] = []

    async def off_first(session, choice, settings):
        if not fired:
            fired.append(choice)
            # Another request's committed write, on its own connection.
            await asyncio.to_thread(_put, {ENABLED: False, CHOICE: "local", MODE: "local"})
        return await real(session, choice, settings)

    with _client() as c:
        _online_rows(enabled=True, choice="local", mode="local")
        monkeypatch.setattr(ss, "set_run_mode", off_first)
        r = _switch(c, "hosted")
        assert r.status_code == 200 and fired == ["hosted"]
        assert _stored() == {CHOICE: "local", MODE: "local", ENABLED: False}
        assert "Kestrel now runs on hosted models" not in r.text
        assert _agree(c, "off in between") == "local" and _gate() == ss.ONLINE_OFF


# --------------------------------------------------------------- case 6: two requests together
_PAIRS = {
    "card online, switch local": (
        lambda c: _press(c, "online"), lambda c: _switch(c, "local"),
        {CHOICE: "hosted", MODE: "hosted", ENABLED: True},
        {CHOICE: "local", MODE: "local", ENABLED: True}),
    "card both, switch online": (
        lambda c: _press(c, "both"), lambda c: _switch(c, "hosted"),
        {CHOICE: "both", MODE: "local", ENABLED: True},
        {CHOICE: "hosted", MODE: "hosted", ENABLED: True}),
    "card online, turn off": (
        lambda c: _press(c, "online"), lambda c: _toggle(c, action="off"),
        # Turn off always ends on this computer; the card's press is refused or overwritten.
        {CHOICE: "local", MODE: "local", ENABLED: False},
        {CHOICE: "local", MODE: "local", ENABLED: False}),
}


@pytest.mark.parametrize("pair", sorted(_PAIRS))
@pytest.mark.parametrize("first", [0, 1], ids=["card writes first", "other writes first"])
def test_two_requests_together_leave_one_owner_state(pair, first, monkeypatch):
    """Case 6. One request from each page arrives together: each has read the stored state
    before the other writes (the race is the precondition, not a timing accident), then they
    write in the order under test. Both answer 200, the rows are exactly what one of the two
    asked for, and after a reload both pages show those rows."""
    from kestrel.web.pages import settings as hosted_page
    from kestrel.web.pages import settings_computer as card_page

    card_request, other_request, card_state, other_state = _PAIRS[pair]
    arrived: set[object] = set()
    both_arrived = asyncio.Event()
    first_done = asyncio.Event()
    loop_box: list[asyncio.AbstractEventLoop] = []

    def gated(index: int, real):
        """`real`, the page's settings read. Each request's first read (its view of the stored
        state) waits until the other request has read too; the request that writes second then
        waits until the first has answered."""
        async def load(session, *args, **kwargs):
            out = await real(session, *args, **kwargs)
            task = asyncio.current_task()
            if task not in arrived and len(arrived) < 2:
                arrived.add(task)
                loop_box[:] = [asyncio.get_running_loop()]
                if len(arrived) == 2:
                    both_arrived.set()
                await asyncio.wait_for(both_arrived.wait(), timeout=20)
                if index != first:
                    await asyncio.wait_for(first_done.wait(), timeout=20)
            return out

        return load

    with _client() as c:
        _online_rows(enabled=True, choice="local", mode="local")
        monkeypatch.setattr(card_page, "load_settings_with_db",
                            gated(0, card_page.load_settings_with_db))
        monkeypatch.setattr(hosted_page, "load_settings_with_db",
                            gated(1, hosted_page.load_settings_with_db))
        start = threading.Barrier(2, timeout=20)

        def send(index: int):
            start.wait()
            answer = (card_request, other_request)[index](c)
            if index == first:
                loop_box[0].call_soon_threadsafe(first_done.set)
            return answer

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(send, i) for i in (0, 1)]
            answers = [f.result(timeout=60) for f in futures]
        monkeypatch.undo()

        assert [r.status_code for r in answers] == [200, 200]
        assert _stored() in (card_state, other_state), _stored()
        _agree(c, pair)


# ------------------------------------------------------------- the write-site contract
# Every place in the product that writes `setup.run_choice` or `llm.mode`, found from the code
# itself. A new one outside the owner fails here: the writer goes through
# setup_state.set_run_mode (or takes its rows from setup_state.run_card_rows) instead.
_KEY_NAMES = {"CHOICE_KEY": CHOICE, "MODE_KEY": MODE}
_WRITERS = {"_set", "_unset", "write", "Setting", "values"}
OWNER_SITES = {
    # THE mapping of a choice to its rows; set_run_mode writes what it returns.
    ("services/setup_state.py", "run_card_rows"),
    # The deferred half of an Online model choice made before online models could run.
    ("services/setup_state.py", "apply_pending_hosted_mode"),
}
# A dict that names a key without storing it: the rows as they WOULD be stored, built to ask
# the card's check before anything is written.
NOT_A_WRITE = {("web/pages/settings_computer.py", "mode_save")}


def _key_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and node.value in (CHOICE, MODE):
        return node.value
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
    return _KEY_NAMES.get(name)


def _write_sites(source: str) -> list[tuple[str, int, str]]:
    """(enclosing function, line, key) for each write of a run card row in `source`: a call to
    a setting writer that names the key, a `rows[key] = ...` assignment, or a dict that maps the
    key to a value."""
    tree = ast.parse(source)
    owner: dict[ast.AST, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(fn):
                owner[child] = fn.name  # the innermost function wins: it is walked last
    found = []
    for node in ast.walk(tree):
        keys: list[str | None] = []
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _WRITERS:
                keys = [_key_of(a) for a in [*node.args, *(k.value for k in node.keywords)]]
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            keys = [_key_of(node.slice)]
        elif isinstance(node, ast.Dict):
            keys = [_key_of(k) for k in node.keys if k is not None]
        for key in keys:
            if key is not None:
                found.append((owner.get(node, "<module>"), node.lineno, key))
    return found


def _product_write_sites() -> set[tuple[str, str]]:
    sites = set()
    root = resources.files("kestrel")

    def walk(folder, prefix: str) -> None:
        for entry in folder.iterdir():
            if entry.is_dir():
                if entry.name not in ("__pycache__", "static", "templates"):
                    walk(entry, f"{prefix}{entry.name}/")
            elif entry.name.endswith(".py"):
                for fn, _line, _key in _write_sites(entry.read_text(encoding="utf-8")):
                    sites.add((f"{prefix}{entry.name}", fn))

    walk(root, "")
    return sites


def test_only_the_owner_writes_the_run_card_rows():
    extra = _product_write_sites() - OWNER_SITES - NOT_A_WRITE
    assert not extra, (
        "these functions write setup.run_choice or llm.mode themselves; go through "
        f"setup_state.set_run_mode or take the rows from setup_state.run_card_rows: {sorted(extra)}"
    )
    # Every listed owner site still exists, so the list cannot go stale unnoticed.
    assert OWNER_SITES <= _product_write_sites()


@pytest.mark.parametrize("source, key", [
    ('async def f(s):\n    await _set(s, "llm.mode", "hosted")\n', MODE),
    ("async def f(s):\n    await io.write(s, ss.CHOICE_KEY, 'local')\n", CHOICE),
    ("def f(rows):\n    rows[MODE_KEY] = 'hosted'\n", MODE),
    ('def f():\n    return {"setup.run_choice": "both"}\n', CHOICE),
    ("def f(s):\n    s.add(Setting(key=CHOICE_KEY, value='local'))\n", CHOICE),
])
def test_the_sweep_sees_each_way_a_row_is_written(source, key):
    assert _write_sites(source) == [("f", 2, key)]


@pytest.mark.parametrize("source", [
    "async def f(s):\n    return await s.get(Setting, MODE_KEY)\n",
    'def f(rows):\n    return rows.get("llm.mode"), rows.pop("setup.run_choice", None)\n',
    'KEYS = ("setup.run_choice", "llm.mode")\n',
    "async def f(s):\n    return await io.read(s, ss.CHOICE_KEY)\n",
])
def test_the_sweep_leaves_reads_alone(source):
    assert _write_sites(source) == []
