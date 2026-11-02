"""P14: likely interview questions generated when a role is marked Applied (D-INTERVIEW-1).
Offline: the model is a capturing fake, nothing touches the network."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Evaluation,
    InterviewQuestionSet,
    Opportunity,
    PrepPack,
    Pursuit,
    Quarantine,
    Story,
    Task,
)
from kestrel.interview import question_task as qt
from kestrel.interview.prep import assemble_prep_pack
from kestrel.llm.client import ChatResult
from kestrel.llm.prompts import load_prompt
from kestrel.queue.queue import TaskQueue
from kestrel.services import pursuit_board as pb

_QUESTIONS = [
    {"question": "Walk me through a platform you rescued.", "category": "behavioral",
     "why_asked": "Tests ownership under pressure.", "story_id": None, "proof_point_key": None},
    {"question": "How would you size the team in year one?", "category": "role_specific",
     "why_asked": "Tests scope judgment.", "story_id": None, "proof_point_key": None},
]


class _Client:
    """A scripted model: returns each queued reply in turn and records every call."""

    def __init__(self, replies: list[str] | None = None, settings=None) -> None:
        self.settings = settings or get_settings()
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **kw):
        self.calls.append({"tier": tier, "messages": messages, "schema": schema})
        content = self.replies.pop(0) if self.replies else json.dumps({"questions": _QUESTIONS})
        return ChatResult(
            content=content, prompt_tokens=10, completion_tokens=10, latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )


def _item_def(schema: dict) -> dict:
    """The per-question definition behind the schema's $ref."""
    ref = schema["properties"]["questions"]["items"]["$ref"]
    return schema["$defs"][ref.rsplit("/", 1)[1]]


def _item_properties(schema: dict) -> dict:
    return _item_def(schema)["properties"]


async def _seed(sm, *, track="employment", status="shortlisted", stories=0) -> tuple[int, int]:
    async with sm() as s:
        opp = Opportunity(
            track=track, opp_type="job_posting" if track == "employment" else "engagement",
            title="VP of IT", org_name="Acme", url=f"https://x.example/{track}",
            description_md="Own IT strategy and security.", stage="t2_done",
        )
        s.add(opp)
        await s.flush()
        p = Pursuit(opportunity_id=opp.id, track=track, status=status)
        s.add(p)
        for n in range(stories):
            s.add(Story(title=f"Story {n}", result="A result", is_canonical=True))
        await s.commit()
        return opp.id, p.id


async def _count(sm, model, *where) -> int:
    async with sm() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _confirm_online(sm, settings) -> None:
    """Stamp every queued question task as the ask-first panel does for an online Tier 2."""
    from kestrel.services.online_consent import destination_id

    async with sm() as s:
        for task in (await s.execute(select(Task).where(Task.type == qt.TASK_TYPE))).scalars():
            task.payload = {
                **task.payload, "consented_model": settings.llm.tiers["tier2"].model,
                "consented_destination": destination_id(settings, "tier2"),
            }
        await s.commit()


async def _apply(sm, pursuit_id: int, to: str | None = None) -> None:
    async with sm() as s:
        p = await s.get(Pursuit, pursuit_id)
        err = await pb.move_to_status(s, p, to or pb.VERBS[p.track]["applied"].to_status)
        assert err is None
        await s.commit()


# ---------------------------------------------------------------------------- the trigger
@pytest.mark.parametrize("track", ["employment", "consulting"])
async def test_applied_enqueues_exactly_one_task_on_either_track(sm, track):
    _opp, pid = await _seed(sm, track=track)
    await _apply(sm, pid)
    assert await _count(sm, Task, Task.type == qt.TASK_TYPE) == 1
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == qt.TASK_TYPE))).scalar_one()
    assert task.payload["opportunity_id"] == _opp and task.payload["pursuit_id"] == pid


async def test_moving_again_or_reapplying_does_not_duplicate(sm):
    opp, pid = await _seed(sm)
    await _apply(sm, pid)
    await _apply(sm, pid)  # same status: nothing changes
    await _apply(sm, pid, "screening")
    await _apply(sm, pid, "applied")  # back to applied while the task is still queued
    assert await _count(sm, Task, Task.type == qt.TASK_TYPE) == 1


async def test_a_role_with_a_set_is_not_asked_again(sm):
    opp, pid = await _seed(sm)
    await _apply(sm, pid)
    await qt.drain_interview_questions(sm, _Client(), get_settings())
    await _apply(sm, pid, "screening")
    await _apply(sm, pid, "applied")
    assert await _count(sm, Task, Task.type == qt.TASK_TYPE, Task.status == "pending") == 0
    assert await _count(sm, InterviewQuestionSet) == 1


async def test_other_moves_enqueue_nothing(sm):
    _opp, pid = await _seed(sm, status="interested")
    await _apply(sm, pid, "shortlisted")
    assert await _count(sm, Task, Task.type == qt.TASK_TYPE) == 0


async def test_bulk_import_of_applied_history_enqueues_nothing(sm):
    from kestrel.services import applied_import as svc

    text = (
        "| Company | Role | Date | Status |\n| --- | --- | --- | --- |\n"
        "| Acme Corp | VP IT | 2026-09-30 | Applied |\n"
        "| Northwind | Director of IT | 2026-10-02 | Applied |\n"
    )
    async with sm() as s:
        rev = await svc.review(s, text)
        res = await svc.commit(s, rev, {r.index: svc.RowChoice(include=True) for r in rev.rows})
    assert res.imported == 2
    assert await _count(sm, Task, Task.type == qt.TASK_TYPE) == 0


# ---------------------------------------------------------------------------------- drain
async def test_drain_stores_a_set_and_completes_the_task(sm):
    opp, pid = await _seed(sm)
    await _apply(sm, pid)
    client = _Client()
    assert await qt.drain_interview_questions(sm, client, get_settings()) == 1
    async with sm() as s:
        row = (await s.execute(select(InterviewQuestionSet))).scalar_one()
        task = (await s.execute(select(Task).where(Task.type == qt.TASK_TYPE))).scalar_one()
    assert (row.opportunity_id, row.pursuit_id) == (opp, pid)
    assert row.prompt_version == "interview_questions/v2" and row.superseded_at is None
    assert [q["question"] for q in row.questions] == [q["question"] for q in _QUESTIONS]
    assert task.status == "done"
    assert client.calls[0]["tier"] == "tier2"


async def test_invalid_json_is_repaired_once_then_quarantined_with_the_raw_reply(sm):
    opp, pid = await _seed(sm)
    await _apply(sm, pid)
    client = _Client(["this is prose, not json", "still not json at all"])
    await qt.drain_interview_questions(sm, client, get_settings())
    assert len(client.calls) == 2  # the attempt and exactly one repair
    async with sm() as s:
        q = (await s.execute(select(Quarantine))).scalars().one()
        task = (await s.execute(select(Task).where(Task.type == qt.TASK_TYPE))).scalar_one()
    assert q.tier == qt.QUARANTINE_TIER and q.opportunity_id == opp
    assert "still not json at all" in (q.raw_response or "")
    assert await _count(sm, InterviewQuestionSet) == 0
    assert task.status == "done"  # surfaced through the quarantine, not retried in a loop


async def test_a_repair_that_works_is_stored(sm):
    _opp, pid = await _seed(sm)
    await _apply(sm, pid)
    client = _Client(["not json"])
    await qt.drain_interview_questions(sm, client, get_settings())
    assert len(client.calls) == 2
    assert await _count(sm, InterviewQuestionSet) == 1 and await _count(sm, Quarantine) == 0


async def test_the_schema_enums_hold_only_real_ids_and_an_invented_id_is_quarantined(
    sm, example_operator_profile
):
    opp, pid = await _seed(sm, stories=2)
    async with sm() as s:
        ids = [r for (r,) in (await s.execute(select(Story.id).order_by(Story.id))).all()]
    await _apply(sm, pid)
    bad = {"questions": [{**_QUESTIONS[0], "story_id": 9999}]}
    client = _Client([json.dumps(bad), json.dumps(bad)])
    await qt.drain_interview_questions(sm, client, get_settings())
    item = _item_properties(client.calls[0]["schema"])
    assert item["story_id"]["enum"] == ids
    assert item["proof_point_key"]["enum"]  # the example profile's real keys
    assert "null" not in json.dumps(item["story_id"])  # the union is normalized away
    assert "story_id" not in _item_def(client.calls[0]["schema"])["required"]
    assert client.calls[0]["schema"]["properties"]["questions"]["maxItems"] == 12
    # An id that is not a story fails validation: quarantine, never a silent default.
    assert await _count(sm, InterviewQuestionSet) == 0
    assert await _count(sm, Quarantine, Quarantine.tier == qt.QUARANTINE_TIER) == 1


async def test_a_real_story_id_and_null_are_both_accepted(sm):
    _opp, pid = await _seed(sm, stories=1)
    async with sm() as s:
        sid = (await s.execute(select(Story.id))).scalar_one()
    await _apply(sm, pid)
    reply = {"questions": [{**_QUESTIONS[0], "story_id": sid}, {**_QUESTIONS[1]}]}
    await qt.drain_interview_questions(sm, _Client([json.dumps(reply)]), get_settings())
    async with sm() as s:
        row = (await s.execute(select(InterviewQuestionSet))).scalar_one()
    assert [q["story_id"] for q in row.questions] == [sid, None]


async def test_no_stories_leaves_the_story_field_out_of_the_schema(sm):
    _opp, pid = await _seed(sm, stories=0)
    await _apply(sm, pid)
    client = _Client()
    await qt.drain_interview_questions(sm, client, get_settings())
    item = _item_properties(client.calls[0]["schema"])
    assert "story_id" not in item  # an empty enum would be unsatisfiable


async def test_overlong_text_is_truncated_and_the_list_capped(sm):
    _opp, pid = await _seed(sm)
    await _apply(sm, pid)
    many = [{**_QUESTIONS[0], "question": "Q" * 400} for _ in range(20)]
    await qt.drain_interview_questions(
        sm, _Client([json.dumps({"questions": many})]), get_settings()
    )
    async with sm() as s:
        row = (await s.execute(select(InterviewQuestionSet))).scalar_one()
    assert len(row.questions) == 12 and len(row.questions[0]["question"]) == 250


# --------------------------------------------------------------------------- regenerate
async def test_regenerate_supersedes_the_old_set_only_after_the_new_one_validates(sm):
    opp, pid = await _seed(sm)
    await _apply(sm, pid)
    await qt.drain_interview_questions(sm, _Client(), get_settings())
    async with sm() as s:
        assert await qt.request_interview_questions(s, opp, pid, regenerate=True)
        await s.commit()
    # A failed regeneration keeps the old set active.
    await qt.drain_interview_questions(sm, _Client(["x", "y"]), get_settings())
    async with sm() as s:
        active = await qt.active_set(s, opp)
    assert active is not None and active.id == 1
    # A good one supersedes it.
    async with sm() as s:
        assert await qt.request_interview_questions(s, opp, pid, regenerate=True)
        await s.commit()
    fresh = {"questions": [{**_QUESTIONS[1], "question": "A new question?"}]}
    await qt.drain_interview_questions(sm, _Client([json.dumps(fresh)]), get_settings())
    async with sm() as s:
        active = await qt.active_set(s, opp)
        old = await s.get(InterviewQuestionSet, 1)
    assert active.id != 1 and active.questions[0]["question"] == "A new question?"
    assert old.superseded_at is not None
    assert await _count(sm, InterviewQuestionSet, InterviewQuestionSet.superseded_at.is_(None)) == 1


# -------------------------------------------------------------------------- resumability
async def test_a_killed_worker_resumes_after_the_lease_expires(sm):
    opp, pid = await _seed(sm)
    await _apply(sm, pid)
    # A worker leases the task and dies before finishing: no completion, no heartbeat.
    dead = await TaskQueue(sm).lease("dead-worker", types=[qt.TASK_TYPE], lease_seconds=600)
    assert dead is not None
    client = _Client()
    assert await qt.drain_interview_questions(sm, client, get_settings()) == 0  # lease still held
    async with sm() as s:
        task = await s.get(Task, dead.id)
        task.lease_until = "2000-01-01T00:00:00+00:00"
        await s.commit()
    assert await qt.drain_interview_questions(sm, client, get_settings()) == 1
    assert await _count(sm, InterviewQuestionSet) == 1
    async with sm() as s:
        assert (await s.get(Task, dead.id)).status == "done"


async def test_a_re_leased_task_that_already_wrote_its_set_does_not_write_another(sm):
    opp, pid = await _seed(sm)
    await _apply(sm, pid)
    async with sm() as s:
        first = await qt.generate_question_set(
            s, _Client(), get_settings(), opportunity_id=opp, pursuit_id=pid
        )
        again = await qt.generate_question_set(
            s, _Client(), get_settings(), opportunity_id=opp, pursuit_id=pid
        )
    assert first.status == "stored" and again.status == "skipped"
    assert await _count(sm, InterviewQuestionSet) == 1


async def test_a_transport_error_goes_back_to_the_queue(sm):
    _opp, pid = await _seed(sm)
    await _apply(sm, pid)

    class _Down(_Client):
        async def complete(self, **kw):
            raise ConnectionError("model unreachable")

    assert await qt.drain_interview_questions(sm, _Down(), get_settings()) == 0
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == qt.TASK_TYPE))).scalar_one()
    assert task.status in ("pending", "dead") and task.attempts >= 1
    assert await _count(sm, InterviewQuestionSet) == 0


# ------------------------------------------------------------------------- hosted routing
async def test_a_hosted_tier_goes_through_the_hosted_plan_and_redacts_the_employer(sm):
    from tests.test_hosted_mode import _EMPLOYER, _PLACEHOLDER, _coded, _hosted_settings

    opp, pid = await _seed(sm)
    async with sm() as s:
        o = await s.get(Opportunity, opp)
        o.description_md = f"Own IT strategy. Candidates from {_EMPLOYER} welcome."
        dm = DocMaster(track="employment", name="resume", doc_kind="resume")
        s.add(dm)
        await s.flush()
        s.add(DocSection(doc_master_id=dm.id, section_kind="role", org_label=_EMPLOYER,
                         org_label_redacted=_PLACEHOLDER))
        await s.commit()
    await _apply(sm, pid)
    settings = _hosted_settings("tier2", confidential=True)
    await _confirm_online(sm, settings)
    client = _Client(settings=settings)
    await qt.drain_interview_questions(sm, client, settings)
    sent = "\n".join(m["content"] for m in client.calls[0]["messages"])
    assert _EMPLOYER not in sent and _coded(sent)
    assert await _count(sm, InterviewQuestionSet) == 1


async def test_a_hosted_tier_with_nothing_to_redact_is_refused_not_sent(sm):
    from tests.test_hosted_mode import _hosted_settings

    _opp, pid = await _seed(sm)
    await _apply(sm, pid)
    settings = _hosted_settings("tier2", confidential=True)
    await _confirm_online(sm, settings)
    client = _Client(settings=settings)
    await qt.drain_interview_questions(sm, client, settings)
    assert client.calls == []
    async with sm() as s:
        q = (await s.execute(select(Quarantine))).scalars().one()
    assert q.attempts == 0 and "hosted send refused" in (q.error or "")


# --------------------------------------------------------------------------- consumers
async def _seed_block_f(sm, opp_id: int) -> None:
    async with sm() as s:
        s.add(Evaluation(
            opportunity_id=opp_id, tier=2, track="employment", verdict="apply",
            verdict_model="apply", score=4.0, weighted_score=4.0, needs_review=False,
            dimensions={}, blocks={
                "a_role_summary": {"lead_with": "Own IT."},
                "f_interview": [{"likely_question": "Block F question one?",
                                 "why_asked": "x", "proof_point_key": None}],
            },
        ))
        await s.commit()


async def test_prep_pack_prefers_the_p14_set_and_falls_back_to_block_f(sm):
    opp, pid = await _seed(sm)
    await _seed_block_f(sm, opp)
    async with sm() as s:
        before = await assemble_prep_pack(s, opp)
    assert "Block F question one?" in before.content_md
    await _apply(sm, pid)
    await qt.drain_interview_questions(sm, _Client(), get_settings())
    async with sm() as s:
        after = await assemble_prep_pack(s, opp)
    assert "Walk me through a platform you rescued." in after.content_md
    assert "Block F question one?" not in after.content_md


async def test_prep_pack_maps_a_p14_question_to_its_story(sm):
    opp, pid = await _seed(sm, stories=1)
    async with sm() as s:
        sid = (await s.execute(select(Story.id))).scalar_one()
    await _apply(sm, pid)
    reply = {"questions": [{**_QUESTIONS[0], "story_id": sid}]}
    await qt.drain_interview_questions(sm, _Client([json.dumps(reply)]), get_settings())
    async with sm() as s:
        pack = await assemble_prep_pack(s, opp)
    assert pack.stories == {"Walk me through a platform you rescued.": sid}
    assert "Story 0" in pack.content_md


async def test_mock_session_opens_with_a_p14_question(sm):
    from kestrel.interview.mock import start_session

    opp, pid = await _seed(sm)
    async with sm() as s:
        stock = await start_session(s, opp, "behavioral")
    assert stock.transcript[0]["question"].startswith("Tell me about a time you led")
    await _apply(sm, pid)
    await qt.drain_interview_questions(sm, _Client(), get_settings())
    async with sm() as s:
        seeded = await start_session(s, opp, "behavioral")
        role = await start_session(s, opp, "technical_leadership")
    assert seeded.transcript[0]["question"] == "Walk me through a platform you rescued."
    assert role.transcript[0]["question"] == "How would you size the team in year one?"


async def test_debrief_scores_against_the_p14_set_else_block_f(sm):
    from kestrel.interview.debrief import capture_debrief

    opp, pid = await _seed(sm)
    await _seed_block_f(sm, opp)
    async with sm() as s:
        r = await capture_debrief(s, opp, questions_asked=["Block F question one?"])
    assert r.predicted == 1 and r.matched == ["Block F question one?"]
    await _apply(sm, pid)
    await qt.drain_interview_questions(sm, _Client(), get_settings())
    async with sm() as s:
        r = await capture_debrief(
            s, opp, questions_asked=["Walk me through a platform you rescued"]
        )
    assert r.predicted == 2 and r.matched == ["Walk me through a platform you rescued."]


# ------------------------------------------------------------------------ prompt and page
def test_the_p14_prompt_loads_and_registers():
    spec = load_prompt("interview_questions/v2")
    assert spec.id == "P14" and spec.tier == "tier2" and spec.temperature == 0.3
    assert spec.model_cls is not None and "{GLOBAL_RULES}" not in spec.system
    for marker in ("{PROFILE_FULL}", "{description}", "{STORIES}", "{PROOF_KEYS}", "{GAPS}"):
        assert marker in spec.user
    assert "—" not in spec.system + spec.user


def test_the_migration_chains_onto_0020():
    import importlib

    mod = importlib.import_module("kestrel.db.migrations.versions.0021_interview_question_set")
    assert (mod.revision, mod.down_revision) == ("0021", "0020")


async def test_one_active_set_per_pursuit_is_enforced_by_the_database(sm):
    from sqlalchemy.exc import IntegrityError

    opp, pid = await _seed(sm)
    async with sm() as s:
        for _ in range(2):
            s.add(InterviewQuestionSet(
                opportunity_id=opp, pursuit_id=pid, prompt_version="v", questions=[],
                created_at=utcnow_iso(),
            ))
        with pytest.raises(IntegrityError):
            await s.commit()


# ------------------------------------------------------------------------------- the page
def _client():
    from fastapi.testclient import TestClient

    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


@pytest.fixture
def web(_isolate_data_home):
    """A TestClient over a freshly migrated app database."""
    import asyncio

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, reset_engine

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    return _client()


def _seed_web(status: str = "shortlisted") -> tuple[int, int]:
    import asyncio

    from kestrel.db.engine import get_sessionmaker

    async def go():
        sm = get_sessionmaker()
        return await _seed(sm, status=status)

    return asyncio.run(go())


def test_the_opportunity_page_shows_the_card_and_regenerate_queues_a_task(web):
    import asyncio

    from kestrel.db.engine import forget_engine, get_sessionmaker

    # Only on Applied (#13 review): a shortlisted role shows no button, and the route refuses.
    short, _ = _seed_web()
    page = web.get(f"/opportunities/{short}")
    assert page.status_code == 200
    assert 'data-testid="questions-card"' in page.text
    assert "Marking this role Applied writes the list" in page.text
    assert 'data-testid="questions-regenerate"' not in page.text
    refused = web.post(f"/opportunities/{short}/questions/regenerate")
    assert "when you mark this role Applied" in refused.text

    opp, pid = _seed_web("applied")
    applied_page = web.get(f"/opportunities/{opp}").text
    assert 'data-testid="questions-none"' in applied_page  # applied, no set yet: says so
    assert "Marking this role Applied" not in applied_page
    r = web.post(f"/opportunities/{opp}/questions/regenerate")
    assert r.status_code == 200 and "Queued." in r.text
    again = web.post(f"/opportunities/{opp}/questions/regenerate")
    assert "already being written" in again.text and "disabled" in again.text

    async def n_tasks():
        return await _count(get_sessionmaker(), Task, Task.type == qt.TASK_TYPE)

    assert asyncio.run(n_tasks()) == 1
    forget_engine()


def test_the_card_lists_the_set_and_a_role_off_the_list_has_no_card(web):
    import asyncio

    from kestrel.db.engine import forget_engine, get_sessionmaker

    opp, pid = _seed_web("applied")

    async def store():
        sm = get_sessionmaker()
        async with sm() as s:
            s.add(InterviewQuestionSet(
                opportunity_id=opp, pursuit_id=pid, prompt_version="interview_questions/v2",
                questions=_QUESTIONS, created_at=utcnow_iso(),
            ))
            lone = Opportunity(track="employment", opp_type="job_posting", title="Other",
                               org_name="Z", url="https://x.example/other", stage="t2_done")
            s.add(lone)
            await s.commit()
            return lone.id

    lone = asyncio.run(store())
    html = web.get(f"/opportunities/{opp}").text
    assert 'data-testid="question-list"' in html
    assert "Walk me through a platform you rescued." in html
    assert "Write a new list" in html
    assert "—" not in html.split('id="questions-card"')[1].split("</div>\n")[0]
    assert 'data-testid="questions-card"' not in web.get(f"/opportunities/{lone}").text
    assert web.post(f"/opportunities/{lone}/questions/regenerate").text.count("Add this role") == 1
    forget_engine()


def test_the_prep_page_leads_with_the_likely_questions_and_the_same_control(web):
    import asyncio

    from kestrel.db.engine import forget_engine, get_sessionmaker

    short, _ = _seed_web()
    page = web.get(f"/prep/{short}").text
    assert 'data-testid="questions-card"' in page
    assert "Likely interview questions" in page
    assert "Marking this role Applied writes the list" in page  # not applied yet
    assert 'data-testid="questions-regenerate"' not in page

    opp, pid = _seed_web("applied")
    none_yet = web.get(f"/prep/{opp}").text
    assert 'data-testid="questions-none"' in none_yet
    assert "Write the list now" in none_yet
    assert "Queued." in web.post(f"/opportunities/{opp}/questions/regenerate").text
    pending = web.get(f"/prep/{opp}").text
    assert 'data-testid="questions-pending"' in pending

    async def store():
        async with get_sessionmaker()() as s:
            s.add(InterviewQuestionSet(
                opportunity_id=opp, pursuit_id=pid, prompt_version="interview_questions/v2",
                questions=_QUESTIONS, created_at=utcnow_iso(),
            ))
            await s.commit()

    asyncio.run(store())
    html = web.get(f"/prep/{opp}").text
    assert 'data-testid="question-list"' in html
    assert "Walk me through a platform you rescued." in html
    assert "Write a new list" in html
    # with no pack built yet the list shows in the strip; once built it is the likely-ask card,
    # and that card sits above the pack sections (gaps, role, questions to ask)
    async def add_pack():
        async with get_sessionmaker()() as s:
            s.add(PrepPack(opportunity_id=opp, content_md="# Prep\n\nBe ready.", stories={},
                           questions_to_ask=[], panel=[]))
            await s.commit()

    asyncio.run(add_pack())
    built = web.get(f"/prep/{opp}").text
    assert built.index('data-testid="pp-qcard"') < built.index('data-testid="pp-gaps"')
    # the mock interview page does not list the set; it still draws from it internally
    assert 'data-testid="question-list"' not in web.get(f"/mock/{opp}").text
    forget_engine()


async def test_a_card_moved_straight_past_applied_still_gets_its_list(sm):
    _opp, pid = await _seed(sm)
    await _apply(sm, pid, "interviewing")
    assert await _count(sm, Task, Task.type == qt.TASK_TYPE) == 1


def test_remove_from_list_deletes_the_question_set_too(web):
    # The set references the pursuit; leaving it made Remove fail on the foreign key (review).
    import asyncio

    from kestrel.db.engine import forget_engine, get_sessionmaker

    opp, pid = _seed_web("applied")

    async def store():
        async with get_sessionmaker()() as s:
            s.add(InterviewQuestionSet(
                opportunity_id=opp, pursuit_id=pid, prompt_version="interview_questions/v2",
                questions=_QUESTIONS, created_at=utcnow_iso(),
            ))
            await s.commit()

    asyncio.run(store())
    r = web.post(f"/opportunities/{opp}/remove")
    assert r.status_code == 200

    async def left():
        sm = get_sessionmaker()
        return (await _count(sm, Pursuit, Pursuit.id == pid),
                await _count(sm, InterviewQuestionSet, InterviewQuestionSet.pursuit_id == pid))

    assert asyncio.run(left()) == (0, 0)
    forget_engine()


async def test_an_out_of_credit_key_defers_the_task_unstarted_instead_of_failing_it(sm):
    from kestrel.llm.credit import OutOfCredit

    _opp, pid = await _seed(sm)
    await _apply(sm, pid)

    class _Broke(_Client):
        async def complete(self, **kw):
            self.calls.append(kw)
            raise OutOfCredit("openmodel")

    client = _Broke()
    for _ in range(4):  # more cycles than the task has attempts
        assert await qt.drain_interview_questions(sm, client, get_settings()) == 0
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == qt.TASK_TYPE))).scalar_one()
    assert (task.status, task.attempts) == ("pending", 0)
    assert "credit has run out" in (task.last_error or "")
    assert await _count(sm, InterviewQuestionSet) == 0
