"""Online consent covers the correction lines a score sends (issue 170 review, D-SCORE-3,
D-PRIV-145). Written from the outside: a correction is saved through the role page's route, the
Score again dialog is opened and confirmed as a browser would, the correction is then changed, and
the real worker drains the queue with a recording model.

INVARIANT: nothing is sent to an online model without the confirmed dialog matching both the
model AND the exact correction lines at send time, including retries, give-backs and standing
Hosted mode; anything that changed since the confirmation is held for a new confirmation, never
sent.

Invented text only; the model is a recording fake and the network is closed (tests.test_contracts
recorder)."""

from __future__ import annotations

import asyncio
import json

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel.config import HostedMode
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Opportunity, RequirementCorrection, Task
from kestrel.match_text import requirement_hash
from kestrel.services.online_consent import APPROVAL_HOLD
from kestrel.services.setting_overrides import load_settings_with_db
from tests.pipeline_fakes import FakeLLM
from tests.test_contracts import _world, recorder  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

PROCESS = "Process improvement across distribution centers"  # on the seeded strong role
FIRST = "I ran the night shift rota for two winters."
CHANGED = "Only one winter, and only as a stand-in."


class _Recording(FakeLLM):
    """The pipeline's fake model, keeping every message it was sent."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.sent: list[str] = []

    async def complete(self, **kw):
        self.sent.append("\n".join(m.get("content", "") for m in kw["messages"]))
        return await super().complete(**kw)


def _db(fn):
    async def run():
        async with get_sessionmaker()() as s:
            out = await fn(s)
            await s.commit()
            return out

    return asyncio.run(run())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False), raise_server_exceptions=False)


def _correct(c, opp: int, note: str, reason: str = "not_me") -> None:
    r = c.post(
        f"/opportunities/{opp}/role/correction",
        data={"h": requirement_hash(PROCESS), "reason": reason, "note": note},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200


def _dialog_form(c, opp: int) -> dict:
    """The hidden fields of the Score again dialog plus Same model and the confirm tick: what
    pressing "Send and score" posts."""
    d = BeautifulSoup(c.get("/rescore/choose", params={"opp_ids": str(opp)}).text, "html.parser")
    form = {i["name"]: i.get("value", "") for i in d.select("input[type=hidden][name]")}
    form.update({"model_choice": "same", "model": "same", "confirmed": "1"})
    return form


def _t2_tasks(opp: int) -> list[Task]:
    async def fn(s):
        rows = (await s.execute(select(Task).where(Task.type == "t2"))).scalars().all()
        return [t for t in rows if (t.payload or {}).get("opportunity_id") == opp]

    return _db(fn)


def _drain(client_cls=None) -> list[str]:
    """The real worker drains Tier 2 once with the settings in effect; returns what was sent."""
    from kestrel.pipeline.tier2 import handle_t2
    from kestrel.pipeline.worker import TierStats, Worker

    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            settings = await load_settings_with_db(s)
        client = (client_cls or _Recording)(settings)
        await Worker(sm, client, settings).drain_tier("t2", 1, handle_t2, TierStats())
        return getattr(client, "sent", [])

    return asyncio.run(go())


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    return asyncio.run(_world())


def _confirmed_score_again(c, opp: int) -> None:
    r = c.post("/rescore", data=_dialog_form(c, opp))
    assert "Queued 1 role" in r.text, r.text


def test_a_confirmed_send_with_unchanged_lines_is_sent_with_them(world, recorder):  # noqa: F811
    """The control: confirm, change nothing, the worker sends once and the line is in it."""
    opp = world["strong"]
    with _client() as c:
        _correct(c, opp, FIRST)
        _confirmed_score_again(c, opp)
    sent = _drain()
    assert len(sent) == 1 and FIRST in sent[0]


def test_a_correction_changed_after_send_and_score_is_held_not_sent(world, recorder):  # noqa: F811
    opp = world["strong"]
    with _client() as c:
        _correct(c, opp, FIRST)
        _confirmed_score_again(c, opp)
        _correct(c, opp, CHANGED, reason="overstated")  # after the confirmation
    assert _drain() == []  # nothing sent, not the old lines and not the new ones
    (task,) = _t2_tasks(opp)
    assert task.status == "pending" and task.last_error == APPROVAL_HOLD
    # A release pass does not let it through while the lines still differ.
    from kestrel.queue.queue import release_holds

    async def release():
        async with get_sessionmaker()() as s:
            settings = await load_settings_with_db(s)
        return await release_holds(get_sessionmaker(), settings)

    assert asyncio.run(release()) == 0
    assert _drain() == []
    # A new confirmation, which shows the new lines, replaces the hold and is sent.
    with _client() as c:
        _confirmed_score_again(c, opp)
    assert asyncio.run(release()) == 1  # the worker's release pass, as each cycle runs it
    sent = _drain()
    assert len(sent) == 1 and CHANGED in sent[0] and FIRST not in sent[0]


def test_a_correction_added_after_the_confirmation_is_held(world, recorder):  # noqa: F811
    """The dialog showed no corrections; one added before the worker runs is never sent."""
    opp = world["strong"]
    with _client() as c:
        _confirmed_score_again(c, opp)
        _correct(c, opp, FIRST)
    assert _drain() == []
    (task,) = _t2_tasks(opp)
    assert task.last_error == APPROVAL_HOLD


def test_a_correction_removed_after_the_confirmation_is_held(world, recorder):  # noqa: F811
    opp = world["strong"]
    with _client() as c:
        _correct(c, opp, FIRST)
        _confirmed_score_again(c, opp)

    async def remove(s):
        for row in (await s.execute(select(RequirementCorrection))).scalars():
            await s.delete(row)

    _db(remove)
    assert _drain() == []


def test_a_retry_carries_the_confirmed_lines_and_is_held_when_they_changed(
    world, recorder  # noqa: F811
):
    """Try again re-queues the same payload: its lines are still the confirmed ones."""
    from kestrel.queue.rescore import requeue_rescore_task

    opp = world["strong"]
    with _client() as c:
        _correct(c, opp, FIRST)
        _confirmed_score_again(c, opp)
    (task,) = _t2_tasks(opp)
    payload = dict(task.payload)

    async def retry(s):
        t = await s.get(Task, task.id)
        t.status = "dead"  # the first attempt ended without a score
        return await requeue_rescore_task(s, opp, payload)

    assert _db(retry)
    with _client() as c:
        _correct(c, opp, CHANGED, reason="overstated")
    assert _drain() == []
    held = [t for t in _t2_tasks(opp) if t.status == "pending"]
    assert len(held) == 1 and held[0].last_error == APPROVAL_HOLD


def test_a_score_again_through_tier1_hands_the_confirmed_lines_to_tier2(
    world, recorder  # noqa: F811
):
    """A role the quick screen set aside is scored again from Tier 1; the Tier 2 task it queues
    still carries the confirmed lines, so a change made meanwhile is held, not sent."""
    from kestrel.pipeline.tier1 import handle_t1
    from kestrel.pipeline.worker import TierStats, Worker

    opp = world["strong"]

    async def set_aside(s):
        (await s.get(Opportunity, opp)).stage = "t1_reject"

    _db(set_aside)
    with _client() as c:
        _correct(c, opp, FIRST)
        _confirmed_score_again(c, opp)

    async def tier1():
        sm = get_sessionmaker()
        async with sm() as s:
            settings = await load_settings_with_db(s)
        await Worker(sm, _Recording(settings), settings).drain_tier("t1", 1, handle_t1,
                                                                    TierStats())

    asyncio.run(tier1())
    (task,) = _t2_tasks(opp)
    with _client() as c:
        _correct(c, opp, CHANGED, reason="overstated")
    assert _drain() == []
    (task,) = _t2_tasks(opp)
    assert task.last_error == APPROVAL_HOLD


def test_a_give_back_keeps_the_confirmed_lines(world, recorder):  # noqa: F811
    """A lease handed back unsent (Quit, D-QUEUE-6) keeps its payload, so the bound lines with
    it: a change made before the next lease is held, not sent."""
    from kestrel.queue.queue import TaskQueue

    opp = world["strong"]
    with _client() as c:
        _correct(c, opp, FIRST)
        _confirmed_score_again(c, opp)

    async def lease_and_hand_back():
        q = TaskQueue(get_sessionmaker())
        leased = await q.lease("quitting-worker", types=["t2"])
        assert leased is not None
        return await q.release_all_leased()

    assert asyncio.run(lease_and_hand_back()) == 1
    (task,) = _t2_tasks(opp)
    assert task.status == "pending"
    with _client() as c:
        _correct(c, opp, CHANGED, reason="overstated")
    assert _drain() == []
    (task,) = _t2_tasks(opp)
    assert task.last_error == APPROVAL_HOLD


def test_a_correction_changed_while_the_dialog_is_open_is_refused(world, recorder):  # noqa: F811
    opp = world["strong"]
    with _client() as c:
        _correct(c, opp, FIRST)
        form = _dialog_form(c, opp)
        _correct(c, opp, CHANGED, reason="overstated")  # in another window, before Send
        r = c.post("/rescore", data=form)
    assert "Your corrections changed since this dialog opened" in r.text
    assert _t2_tasks(opp) == []
    # Opening the dialog may look up the key's balance (a bodiless GET); nothing else goes out.
    assert recorder.llm == []
    assert [q for q in recorder.http if q.method != "GET" or q.content] == []


# ------------------------------------------------------------------ standing Hosted mode
OLD_TIER2_SENDS = (
    "the description slice (up to {t2} tokens in hosted mode), the organization context "
    "Kestrel has gathered, your confirmed labels used as examples, and your full profile; "
    "and, only when you click Assess on Profile > Documents or Suggest again on Profile, "
    "your resume text"
)


def test_the_tier2_disclosure_names_corrections_and_their_notes():
    line = HostedMode().disclosure_terms()["sends"]["tier2"]
    assert "corrections" in line and "notes" in line


def test_an_acknowledgement_of_the_old_disclosure_no_longer_covers_tier2_sends():
    """Accepted before corrections were sent: Hosted mode is no longer active, so no standing
    send happens until the person accepts the disclosure that names them."""
    terms = HostedMode().disclosure_terms()
    old = json.loads(json.dumps(terms))
    old["sends"]["tier2"] = OLD_TIER2_SENDS.format(t2=terms["tier2_ceiling_tokens"])
    hm = HostedMode(enabled=True, acknowledged_at="2026-10-15T00:00:00Z", acknowledged_terms=old)
    assert not hm.acknowledgement_current and not hm.active


def test_a_standing_hosted_send_under_the_old_acknowledgement_sends_nothing(world, monkeypatch):
    """An automatic Tier 2 task (no per-role dialog) with the old acknowledgement on file: the
    real client, with every transport recording and closed, opens no connection."""
    import httpx

    from kestrel.db.models import Setting
    from kestrel.llm.client import LLMClient

    opened: list[str] = []

    async def ahandle(self, request):
        opened.append(str(request.url))
        raise httpx.ConnectError("test: the network is closed")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", ahandle)
    opp = world["strong"]
    with _client() as c:
        _correct(c, opp, FIRST)

    async def old_ack(s):
        terms = HostedMode().disclosure_terms()
        terms["sends"]["tier2"] = OLD_TIER2_SENDS.format(t2=terms["tier2_ceiling_tokens"])
        await s.merge(Setting(key="llm.hosted_mode.acknowledged_terms", value=terms))
        o = await s.get(Opportunity, opp)
        o.stage = "t1_pass"
        s.add(Task(type="t2", status="pending", payload={"opportunity_id": opp}))

    _db(old_ack)
    _drain(LLMClient)
    assert opened == []
    (task,) = [t for t in _t2_tasks(opp) if not (t.payload or {}).get("rescore")]
    assert task.status != "done"


# ------------------------------------------------------------------ Tier 3 deeper read dialog
def test_the_deeper_read_dialog_lists_the_correction_lines(world, recorder):  # noqa: F811
    opp = world["strong"]
    with _client() as c:
        _correct(c, opp, FIRST)
        d = BeautifulSoup(c.get(f"/opportunities/{opp}/role/deeper").text, "html.parser")
    sends = d.select_one('[data-testid="deeper-corrections"]')
    assert sends is not None and sends.find_parent(class_="rl-send") is not None
    lines = [li.get_text() for li in d.select('[data-testid="deeper-correction-lines"] li')]
    assert len(lines) == 1 and PROCESS in lines[0] and FIRST in lines[0]
    assert recorder.llm == []


# ------------------------------------------------------------------ which corrections are sent
def test_only_corrections_about_this_posting_are_sent_and_listed(world, recorder):  # noqa: F811
    """24 newer corrections about requirements this posting does not ask for and one, the
    oldest, about one it does (owner, 2026-11-11, D-SCORE-1). Only the matching one is stated,
    the dialog lists exactly that line, and an online send carries it and none of the others."""
    from kestrel.learning.corrections import scoring_corrections
    from kestrel.pipeline.context import operator_correction_lines

    opp = world["strong"]

    async def seed(s):
        rows = [RequirementCorrection(
            track="employment", opportunity_id=None, requirement_text=PROCESS,
            requirement_hash=requirement_hash(PROCESS), kestrel_state="met",
            operator_state="gap", reason="not_me", scope="person", note=FIRST,
            created_at="2026-01-01T00:00:00.000000+00:00",
            updated_at="2026-01-01T00:00:00.000000+00:00")]
        for i in range(24):
            text = f"Invented skill number {i:02d} for some other role"
            stamp = f"2026-02-{i + 1:02d}T00:00:00.000000+00:00"
            rows.append(RequirementCorrection(
                track="employment", opportunity_id=None, requirement_text=text,
                requirement_hash=requirement_hash(text), kestrel_state="gap",
                operator_state="gap", reason="agree", scope="person", note=None,
                created_at=stamp, updated_at=stamp))
        s.add_all(rows)

    _db(seed)

    async def read(s):
        role = await s.get(Opportunity, opp)
        return operator_correction_lines(await scoring_corrections(s, role))

    lines = _db(read)
    assert len(lines) == 1
    assert PROCESS in lines[0] and FIRST in lines[0]
    with _client() as c:
        d = BeautifulSoup(c.get("/rescore/choose", params={"opp_ids": str(opp)}).text,
                          "html.parser")
    listed = [li.get_text() for li in d.select('[data-testid="score-again-correction-lines"] li')]
    assert listed == lines
    assert not d.select('[data-testid="score-again-correction-more"]')


def test_a_multi_role_dialog_lists_every_line_it_sends(world, recorder):  # noqa: F811
    """Three roles, each with eight notes of its own: 24 distinct lines go out across the three
    tasks, so the dialog lists all 24 (never 'and N more'), and its digest covers them."""
    from kestrel.learning.correction_lines import dialog_digest, lines_for

    roles = [world["strong"], world["second"], world["maybe"]]

    async def seed(s):
        for n, oid in enumerate(roles):
            for i in range(8):
                text = f"Invented duty {n}-{i} on this posting"
                s.add(RequirementCorrection(
                    track="employment", opportunity_id=oid, requirement_text=text,
                    requirement_hash=requirement_hash(text), kestrel_state="gap",
                    operator_state="gap", reason="misread", scope="posting",
                    note=f"They mean the invented rota {n}-{i}."))

    _db(seed)
    lines = _db(lambda s: lines_for(s, roles))
    assert len(lines) == 24
    with _client() as c:
        d = BeautifulSoup(c.get("/rescore/choose",
                                params={"opp_ids": ",".join(map(str, roles))}).text,
                          "html.parser")
    listed = [li.get_text() for li in d.select('[data-testid="score-again-correction-lines"] li')]
    assert listed == lines
    assert "more" not in " ".join(listed)
    form = d.select_one('input[name="corrections_digest"]')
    assert form is not None and form["value"] == _db(lambda s: dialog_digest(s, roles))


def test_a_first_score_lists_and_sends_exactly_the_text_matched_lines(world, recorder):  # noqa: F811
    """D-SCORE-7: a role with no read yet states the person corrections whose requirement text
    is in its description. The dialog lists exactly those lines, the confirmation is bound to
    them, and the online send carries them and nothing else."""
    from sqlalchemy import update

    from kestrel.db.models import Evaluation
    from kestrel.learning.corrections import scoring_corrections
    from kestrel.pipeline.context import operator_correction_lines

    opp = world["strong"]
    gauge = "Keeps the tide gauge calibrated"
    absent = "Holds a harbor crane license"

    async def seed(s):
        await s.execute(update(Evaluation).where(Evaluation.opportunity_id == opp)
                        .values(tier=1, blocks={}))
        role = await s.get(Opportunity, opp)
        role.description_md = (f"{role.description_md or ''}\n\nYou {gauge.lower()} every "
                               "morning before the first tide.")
        for i, text in enumerate((gauge, absent)):
            stamp = f"2026-03-0{i + 1}T00:00:00.000000+00:00"
            s.add(RequirementCorrection(
                track="employment", opportunity_id=None, requirement_text=text,
                requirement_hash=requirement_hash(text), kestrel_state="gap",
                operator_state="met", reason="all", scope="person", note=None,
                created_at=stamp, updated_at=stamp))

    _db(seed)

    async def read(s):
        role = await s.get(Opportunity, opp)
        return operator_correction_lines(await scoring_corrections(s, role))

    lines = _db(read)
    assert lines == [f'- "{gauge}": has this']
    with _client() as c:
        d = BeautifulSoup(c.get("/rescore/choose", params={"opp_ids": str(opp)}).text,
                          "html.parser")
        listed = [li.get_text() for li in
                  d.select('[data-testid="score-again-correction-lines"] li')]
        assert listed == lines
        _confirmed_score_again(c, opp)
    sent = _drain()
    assert len(sent) == 1
    assert lines[0] in sent[0] and absent not in sent[0]
