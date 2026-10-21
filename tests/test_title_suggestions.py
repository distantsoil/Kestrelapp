"""P15: suggested target titles (D-TTL-3, #31). A model suggests titles from the user's own
document; nothing reaches target_titles without an explicit Accept. Offline: a scripted model."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import httpx
import pytest
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import (
    DocMaster,
    DocSection,
    DocSlot,
    DocVariant,
    Opportunity,
    Pursuit,
    Quarantine,
    Setting,
    Task,
    TitleSuggestion,
)
from kestrel.llm.client import ChatResult
from kestrel.llm.prompts import load_prompt
from kestrel.services import profile_edit as pe
from kestrel.services import title_suggest as ts

BULLET_1 = "Cut weekly reporting time from two days to two hours."
BULLET_2 = "Led a process review that cut order-entry errors by 40 percent."


class _Client:
    def __init__(self, replies: list[str] | None = None, settings=None) -> None:
        self.settings = settings or get_settings()
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **kw):
        self.calls.append({"tier": tier, "messages": messages, "schema": schema})
        content = self.replies.pop(0) if self.replies else json.dumps(_reply())
        return ChatResult(content=content, prompt_tokens=5, completion_tokens=5, latency_ms=1,
                          model=self.settings.llm.tiers[tier].model)


def _reply(*titles: tuple[str, list[str]]) -> dict:
    titles = titles or (("Operations Analyst", ["r1b1"]), ("Process Improvement Lead", ["r1b2"]))
    return {"titles": [{"title": t, "why": f"{t} fits the bullets.", "evidence_slot_keys": k}
                       for t, k in titles]}


async def _seed_master(sm, *, track="employment", kind="resume") -> int:
    async with sm() as s:
        m = DocMaster(track=track, name="Resume", doc_kind=kind, is_active=True)
        s.add(m)
        await s.flush()
        sec = DocSection(doc_master_id=m.id, section_kind="role", org_label="Acme",
                         title="Analyst", sort_order=0)
        s.add(sec)
        await s.flush()
        for n, text in enumerate((BULLET_1, BULLET_2), start=1):
            slot = DocSlot(doc_section_id=sec.id, key=f"r1b{n}", sort_order=n,
                           default_variant_key="v1")
            s.add(slot)
            await s.flush()
            s.add(DocVariant(doc_slot_id=slot.id, key="v1", text=text, tags=[], derived=False))
        await s.commit()
        return m.id


async def _count(sm, model, *where) -> int:
    async with sm() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _run(sm, client=None, reason="manual") -> int:
    async with sm() as s:
        await ts.request_title_suggestions(s, reason)
        await s.commit()
    return await ts.drain_title_suggestions(sm, client or _Client(), get_settings())


async def _rows(s, model=TitleSuggestion) -> list:
    return list((await s.execute(select(model).order_by(model.id))).scalars().all())


async def _the_task(s) -> Task:
    return (await s.execute(select(Task).where(Task.type == ts.TASK_TYPE))).scalar_one()


def _write_profile(titles: list[str]) -> None:
    pe.save_section("employment", "employment", {"employment_terms": {"target_titles": titles}})


def _target_titles() -> list[str]:
    state = pe.load_state()
    return list((state.tracks["employment"].get("employment_terms") or {}).get("target_titles")
                or [])


# ------------------------------------------------------------------------------ the trigger
def _proposal():
    v = NS(key="v1", text=BULLET_1, tags=[], derived=False)
    slot = NS(key="r1b1", optional=False, default_variant_key="v1", variants=[v])
    role = NS(company_label="Acme", title="Analyst", start_date="2020", end_date="2024",
              context_line="ctx", slots=[slot])
    return NS(roles=[role], summary_variants=[], skill_groups=[], proof_points=[])


@pytest.mark.parametrize(("track", "kind", "queued"), [
    ("employment", "resume", 1), ("consulting", "resume", 0), ("employment", "capability", 0),
])
async def test_a_resume_import_queues_one_task(sm, track, kind, queued):
    from kestrel.docs.imports import accept_import

    async with sm() as s:
        await accept_import(s, _proposal(), track=track, doc_kind=kind, name="x",
                            source_markdown=BULLET_1)
    assert await _count(sm, Task, Task.type == ts.TASK_TYPE) == queued


async def test_a_second_request_while_one_is_live_adds_nothing(sm):
    async with sm() as s:
        assert await ts.request_title_suggestions(s, "import")
        assert not await ts.request_title_suggestions(s, "weekly")
        await s.commit()
    assert await _count(sm, Task, Task.type == ts.TASK_TYPE) == 1


# --------------------------------------------------------------------------------- the run
async def test_suggestions_are_stored_as_suggested_and_nothing_is_added_to_the_profile(sm):
    await _seed_master(sm)
    _write_profile(["Business Analyst"])
    assert await _run(sm) == 1
    async with sm() as s:
        rows = await _rows(s)
        task = await _the_task(s)
    assert [r.title for r in rows] == ["Operations Analyst", "Process Improvement Lead"]
    assert {r.status for r in rows} == {"suggested"}
    assert rows[0].prompt_version == "title_suggest/v1" and rows[0].decided_at is None
    # the evidence is the user's own bullet, copied by the app, not written by the model
    assert rows[0].evidence == [{"slot_key": "r1b1", "text": BULLET_1}]
    assert task.status == "done"
    assert _target_titles() == ["Business Analyst"]  # untouched without an Accept


async def test_the_schema_evidence_keys_are_an_enum_of_the_documents_real_slots(sm):
    await _seed_master(sm)
    client = _Client()
    await _run(sm, client)
    schema = client.calls[0]["schema"]
    ref = schema["properties"]["titles"]["items"]["$ref"].rsplit("/", 1)[1]
    props = schema["$defs"][ref]["properties"]
    assert props["evidence_slot_keys"]["items"]["enum"] == ["r1b1", "r1b2"]
    assert schema["properties"]["titles"]["maxItems"] == 10
    sent = "\n".join(m["content"] for m in client.calls[0]["messages"])
    assert "[r1b1]" in sent and BULLET_1 in sent


async def test_an_invented_slot_key_is_quarantined_not_defaulted(sm):
    await _seed_master(sm)
    bad = json.dumps(_reply(("Operations Analyst", ["no_such_slot"])))
    await _run(sm, _Client([bad, bad]))
    assert await _count(sm, TitleSuggestion) == 0
    async with sm() as s:
        q = (await s.execute(select(Quarantine))).scalars().one()
        task = (await s.execute(select(Task).where(Task.type == ts.TASK_TYPE))).scalar_one()
    assert q.tier == ts.QUARANTINE_TIER and "no_such_slot" in (q.raw_response or "")
    assert task.status == "done"


async def test_invalid_json_is_repaired_once_then_quarantined_with_the_raw_reply(sm):
    await _seed_master(sm)
    client = _Client(["not json", "still prose"])
    await _run(sm, client)
    assert len(client.calls) == 2
    async with sm() as s:
        q = (await s.execute(select(Quarantine))).scalars().one()
    assert "still prose" in (q.raw_response or "")
    assert await _count(sm, TitleSuggestion) == 0


async def test_a_repaired_reply_is_stored(sm):
    await _seed_master(sm)
    client = _Client(["not json"])
    await _run(sm, client)
    assert len(client.calls) == 2 and await _count(sm, TitleSuggestion) == 2


async def test_no_resume_means_no_call(sm):
    client = _Client()
    await _run(sm, client)
    assert client.calls == [] and await _count(sm, TitleSuggestion) == 0
    async with sm() as s:
        assert (await _the_task(s)).status == "done"


async def test_a_title_already_targeted_or_repeated_is_not_stored(sm):
    await _seed_master(sm)
    _write_profile(["operations   analyst"])
    reply = _reply(("Operations Analyst", ["r1b1"]), ("Process Lead", ["r1b2"]),
                   ("process lead", ["r1b2"]))
    await _run(sm, _Client([json.dumps(reply)]))
    async with sm() as s:
        titles = [r.title for r in (await s.execute(select(TitleSuggestion))).scalars()]
    assert titles == ["Process Lead"]


async def test_overlong_text_is_truncated_and_the_list_capped(sm):
    await _seed_master(sm)
    many = {"titles": [{"title": f"T{n} " + "x" * 200, "why": "y" * 500,
                        "evidence_slot_keys": ["r1b1"] * 9} for n in range(15)]}
    await _run(sm, _Client([json.dumps(many)]))
    async with sm() as s:
        rows = (await s.execute(select(TitleSuggestion))).scalars().all()
    assert len(rows) == 10 and all(len(r.title) <= 80 and len(r.why) <= 200 for r in rows)
    assert all(len(r.evidence) == 1 for r in rows)  # repeats collapse


# ---------------------------------------------------------------- dismiss, accept, never twice
async def test_a_dismissed_title_is_never_suggested_again(sm):
    await _seed_master(sm)
    await _run(sm)
    async with sm() as s:
        first = (await _rows(s))[0]
        assert await ts.dismiss(s, first.id)
    again = _reply(("OPERATIONS  analyst", ["r1b1"]), ("Brand New Title", ["r1b2"]))
    client = _Client([json.dumps(again)])
    await _run(sm, client)
    sent = "\n".join(m["content"] for m in client.calls[0]["messages"])
    assert "Operations Analyst" in sent  # the model is told not to suggest it
    async with sm() as s:
        rows = await _rows(s)
    assert [r.title for r in rows if r.status == "suggested"] == [
        "Process Improvement Lead", "Brand New Title"]
    assert [r.title for r in rows if r.status == "dismissed"] == ["Operations Analyst"]


async def test_accept_adds_the_title_through_profile_edit_and_marks_it_accepted(sm):
    await _seed_master(sm)
    _write_profile(["Business Analyst"])
    await _run(sm)
    async with sm() as s:
        row = (await _rows(s))[0]
        ok, message = await ts.accept(s, row.id)
    assert ok, message
    assert _target_titles() == ["Business Analyst", "Operations Analyst"]
    async with sm() as s:
        got = await s.get(TitleSuggestion, row.id)
        other = await s.get(TitleSuggestion, row.id + 1)
    assert got.status == "accepted" and got.decided_at
    assert other.status == "suggested"  # the rest still wait for their own Accept
    # accepting again changes nothing
    async with sm() as s:
        await ts.accept(s, row.id)
    assert _target_titles() == ["Business Analyst", "Operations Analyst"]


async def test_accept_keeps_the_other_employment_terms(sm):
    await _seed_master(sm)
    pe.save_section("employment", "employment", {"employment_terms": {
        "target_titles": ["Business Analyst"], "comp_floor_usd": 65000,
        "seniority_floor": "manager", "anti_titles": ["Intern"]}})
    await _run(sm)
    async with sm() as s:
        row = (await s.execute(select(TitleSuggestion))).scalars().first()
        await ts.accept(s, row.id)
    terms = pe.load_state().tracks["employment"]["employment_terms"]
    assert terms["comp_floor_usd"] == 65000 and terms["anti_titles"] == ["Intern"]
    assert terms["seniority_floor"] == "manager"


async def test_accept_with_no_profile_yet_creates_one_with_blank_terms(sm):
    await _seed_master(sm)
    await _run(sm)
    async with sm() as s:
        row = (await s.execute(select(TitleSuggestion))).scalars().first()
        ok, _ = await ts.accept(s, row.id)
    assert ok and _target_titles() == [row.title]
    terms = pe.load_state().tracks["employment"]["employment_terms"]
    assert terms["comp_floor_usd"] is None and terms["seniority_floor"] == ""


# ------------------------------------------------------------------------------ the cadence
def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


async def _set_last(sm, *, days_ago: float, applied: int) -> None:
    async with sm() as s:
        s.add(Setting(key=ts.LAST_RUN_KEY, value={"at": _iso(days_ago), "applied_count": applied}))
        await s.commit()


async def _add_applied(sm, n: int) -> None:
    async with sm() as s:
        for i in range(n):
            opp = Opportunity(track="employment", opp_type="job_posting", title=f"Role {i}",
                              org_name="Z", url=f"https://x.example/{i}", stage="t2_done")
            s.add(opp)
            await s.flush()
            s.add(Pursuit(opportunity_id=opp.id, track="employment", status="applied",
                          applied_at=_iso(1)))
        await s.commit()


async def test_due_needs_a_resume_then_runs_first_weekly_and_after_ten_applied(sm):
    async with sm() as s:
        assert await ts.due(s) is None  # no resume, nothing to suggest from
    await _seed_master(sm)
    async with sm() as s:
        assert await ts.due(s) == "first"
    await _set_last(sm, days_ago=1, applied=0)
    async with sm() as s:
        assert await ts.due(s) is None
    await _add_applied(sm, 9)
    async with sm() as s:
        assert await ts.due(s) is None
    await _add_applied(sm, 1)
    async with sm() as s:
        assert await ts.due(s) == "applied"
    async with sm() as s:
        row = await s.get(Setting, ts.LAST_RUN_KEY)
        row.value = {"at": _iso(8), "applied_count": 10}
        await s.commit()
    async with sm() as s:
        assert await ts.due(s) == "weekly"


async def test_the_worker_check_queues_a_run_only_when_one_is_due(sm):
    assert not await ts.maybe_request(sm, force=True)  # no resume
    await _seed_master(sm)
    assert await ts.maybe_request(sm, force=True)  # never run: first
    assert not await ts.maybe_request(sm, force=True)  # one is already queued
    assert await _count(sm, Task, Task.type == ts.TASK_TYPE) == 1


async def test_a_finished_run_records_when_and_how_many_applied(sm):
    await _seed_master(sm)
    await _add_applied(sm, 3)
    await _run(sm)
    async with sm() as s:
        last = (await s.get(Setting, ts.LAST_RUN_KEY)).value
        assert await ts.due(s) is None
    assert last["applied_count"] == 3 and last["added"] == 2


async def test_a_quarantined_run_still_counts_so_it_is_not_retried_every_cycle(sm):
    await _seed_master(sm)
    await _run(sm, _Client(["x", "y"]))
    async with sm() as s:
        assert await ts.due(s) is None


async def test_the_applied_roles_reach_the_prompt(sm):
    await _seed_master(sm)
    await _add_applied(sm, 2)
    client = _Client()
    await _run(sm, client)
    sent = "\n".join(m["content"] for m in client.calls[0]["messages"])
    assert "Role 0" in sent and "Role 1" in sent


# ---------------------------------------------------------------------------- resumability
async def test_a_killed_worker_resumes_after_the_lease_expires(sm):
    from kestrel.queue.queue import TaskQueue

    await _seed_master(sm)
    async with sm() as s:
        await ts.request_title_suggestions(s, "import")
        await s.commit()
    dead = await TaskQueue(sm).lease("dead", types=[ts.TASK_TYPE], lease_seconds=600)
    assert dead is not None
    assert await ts.drain_title_suggestions(sm, _Client(), get_settings()) == 0
    async with sm() as s:
        t = await s.get(Task, dead.id)
        t.lease_until = "2000-01-01T00:00:00+00:00"
        await s.commit()
    assert await ts.drain_title_suggestions(sm, _Client(), get_settings()) == 1
    assert await _count(sm, TitleSuggestion) == 2


async def test_a_transport_error_goes_back_to_the_queue(sm):
    await _seed_master(sm)

    class _Down(_Client):
        async def complete(self, **kw):
            raise ConnectionError("down")

    assert await _run(sm, _Down()) == 0
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == ts.TASK_TYPE))).scalar_one()
    assert task.status in ("pending", "dead") and task.attempts >= 1


# ------------------------------------------------------------------------- hosted routing
async def test_a_hosted_tier_redacts_the_employer_before_the_send(sm):
    from tests.test_hosted_mode import _EMPLOYER, _PLACEHOLDER, _coded, _hosted_settings

    mid = await _seed_master(sm)
    async with sm() as s:
        sec = (await s.execute(select(DocSection).where(DocSection.doc_master_id == mid))
               ).scalars().one()
        sec.org_label = _EMPLOYER
        sec.org_label_redacted = _PLACEHOLDER
        await s.commit()
    settings = _hosted_settings("tier2", confidential=True)
    client = _Client(settings=settings)
    async with sm() as s:
        await ts.request_title_suggestions(s, "manual")
        await s.commit()
    await ts.drain_title_suggestions(sm, client, settings)
    sent = "\n".join(m["content"] for m in client.calls[0]["messages"])
    assert _EMPLOYER not in sent and _coded(sent)


# -------------------------------------------------------------------------- prompt and pages
def test_the_prompt_loads_and_registers():
    spec = load_prompt("title_suggest/v1")
    assert spec.id == "P15" and spec.tier == "tier2" and spec.model_cls is not None
    for marker in ("{TARGET_TITLES}", "{DISMISSED}", "{PURSUED}", "{document}"):
        assert marker in spec.user
    assert "—" not in spec.system + spec.user


def test_the_migration_head_is_0022():
    from kestrel.db.migrate import head_revision

    assert head_revision() == "0022"


async def test_a_title_cannot_be_stored_twice_in_any_case(sm):
    from sqlalchemy.exc import IntegrityError

    async with sm() as s:
        s.add(TitleSuggestion(title="Ops Lead", why="x", evidence=[], status="suggested",
                              prompt_version="v"))
        s.add(TitleSuggestion(title="OPS LEAD", why="x", evidence=[], status="dismissed",
                              prompt_version="v"))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.fixture
def web(_isolate_data_home):
    from fastapi.testclient import TestClient

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, reset_engine
    from kestrel.web.app import build_app

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    return TestClient(build_app(worker=False))


def _db(fn):
    from kestrel.db.engine import forget_engine, get_sessionmaker

    async def go():
        return await fn(get_sessionmaker())

    out = asyncio.run(go())
    forget_engine()
    return out


def test_the_employment_tab_shows_the_suggestions_and_accept_changes_the_profile(web):
    async def seed(sm):
        await _seed_master(sm)
        await _run(sm)

    _db(seed)
    page = web.get("/profile/title-suggestions")
    assert page.status_code == 200
    assert 'data-testid="title-suggestions"' in page.text
    assert "Operations Analyst" in page.text and "Process Improvement Lead" in page.text
    assert BULLET_1 in page.text  # the evidence is shown in the user's own words
    assert _target_titles() == []
    emp = web.get("/profile?tab=employment&track=employment")
    assert "/profile/title-suggestions" in emp.text
    r = web.post("/title-suggestions/1/accept")
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"
    assert _target_titles() == ["Operations Analyst"]
    after = web.get("/profile/title-suggestions").text
    assert "Process Improvement Lead" in after and ">Operations Analyst<" not in after
    d = web.post("/title-suggestions/2/dismiss")
    assert d.status_code == 200 and "Process Improvement Lead" not in d.text
    assert _target_titles() == ["Operations Analyst"]


def test_unknown_ids_and_refresh_never_error(web):
    assert web.post("/title-suggestions/99/accept").status_code == 200
    assert web.post("/title-suggestions/99/dismiss").status_code == 200
    r = web.post("/title-suggestions/refresh")
    assert r.status_code == 200

    async def n(sm):
        return await _count(sm, Task, Task.type == ts.TASK_TYPE)

    assert _db(n) == 0  # no resume yet: nothing is queued, and it says so
    assert "resume" in r.text.lower()


def test_onboarding_employment_step_carries_the_suggestions_slot():
    from pathlib import Path

    import kestrel.web

    body = Path(kestrel.web.__file__).parent / "templates/partials/onboarding_body.html"
    text = body.read_text(encoding="utf-8")
    start = text.split("{% elif key == 'employment' %}")[1]
    step = start.split("{% elif key == 'consulting' %}")[0]
    assert 'hx-get="/profile/title-suggestions"' in step


async def test_no_automatic_pass_when_tier_2_runs_online(sm, monkeypatch):
    # An automatic pass would send the whole resume online with no user action (review).
    await _seed_master(sm)

    async def online(_s):
        return True

    monkeypatch.setattr(ts, "_runs_online", online)
    assert not await ts.maybe_request(sm, force=True)
    assert await _count(sm, Task, Task.type == ts.TASK_TYPE) == 0
    async with sm() as s:
        assert await ts.due(s) is None  # the skip is recorded, so it stays quiet for a week


async def test_a_failed_pass_is_recorded_so_it_is_not_requeued_every_ten_minutes(sm):
    await _seed_master(sm)

    class _Broken(_Client):
        async def complete(self, **kw):
            raise httpx.ConnectError("no route")

    await _run(sm, _Broken())
    async with sm() as s:
        assert await ts.due(s) is None


async def test_an_out_of_credit_key_defers_the_task_and_records_no_error_run(sm):
    from kestrel.llm.credit import OutOfCredit

    await _seed_master(sm)

    class _Broke(_Client):
        async def complete(self, **kw):
            raise OutOfCredit("openmodel")

    client = _Broke()
    async with sm() as s:
        await ts.request_title_suggestions(s, "manual")
        await s.commit()
    for _ in range(4):  # more cycles than the task has attempts
        assert await ts.drain_title_suggestions(sm, client, get_settings()) == 0
    async with sm() as s:
        task = await _the_task(s)
        last = await s.get(Setting, ts.LAST_RUN_KEY)
    assert (task.status, task.attempts) == ("pending", 0)
    assert "credit has run out" in (task.last_error or "")
    assert last is None  # no "error" run: the weekly cadence must not swallow the retry
