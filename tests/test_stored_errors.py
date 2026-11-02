"""D-SEC-7: stored task errors keep the diagnosis, pages show only the plain sentence, and the
robots refusal is one authored sentence with no host or URL."""

from __future__ import annotations

import pathlib
import re

import httpx

from kestrel.db.models import Task
from kestrel.errors import ROBOTS_SENTENCE, RobotsDisallowed, failure_record, plain_os_reason
from kestrel.web.errors import fixed_reason, plain_error

LEAK = "http://127.0.0.1:11434/x"


def _httpx_error() -> httpx.ConnectError:
    return httpx.ConnectError(f"connection refused at {LEAK}")


async def _dead(sm, task_type: str, payload: dict | None = None) -> None:
    async with sm() as s:
        s.add(Task(type=task_type, status="dead", payload=payload or {}, attempts=3,
                   max_attempts=3, last_error=failure_record(_httpx_error())))
        await s.commit()


def _clean(text: object) -> None:
    low = str(text).lower()
    assert "127.0.0.1" not in low and "http" not in low and "11434" not in low, text
    assert "connecterror" not in low and "connection refused" not in low, text


async def test_the_stored_error_keeps_the_detail(sm):
    await _dead(sm, "bench")
    async with sm() as s:
        from sqlalchemy import select

        stored = (await s.execute(select(Task.last_error))).scalar_one()
    assert LEAK in stored and "ConnectError" in stored


async def test_the_bench_panel_shows_no_exception_text(sm):
    from kestrel.bench import check

    await _dead(sm, check.TASK_TYPE)
    async with sm() as s:
        view = await check.status(s)
    assert view["error"]
    _clean(view["error"])


async def test_the_story_build_state_shows_no_exception_text(sm):
    from kestrel.interview import story_task

    await _dead(sm, story_task.TASK_TYPE)
    async with sm() as s:
        state = await story_task.build_state(s)
    assert state["state"] == "failed"
    _clean(state["reason"])


async def test_the_calibrate_training_state_shows_no_exception_text(sm):
    from kestrel.learning import training

    await _dead(sm, training.TASK_TYPE, {"track": "employment"})
    async with sm() as s:
        state = await training.state(s, "employment")
    assert state["message"]
    _clean(state["detail"])


def test_every_task_failure_writer_stores_a_failure_record():
    root = pathlib.Path(__file__).resolve().parent.parent / "kestrel"
    bad = re.compile(
        r"\.(fail|defer)\(\s*task\.id,\s*"
        r"(str\(exc\)|repr\(exc\)|f\"\{type\(exc\)\.__name__\}: \{exc\}\")"
    )
    hits = [str(p) for p in root.rglob("*.py") if bad.search(p.read_text(encoding="utf-8"))]
    assert hits == []


async def test_the_question_writer_stores_a_record_and_the_page_is_plain(sm, monkeypatch):
    from kestrel.config import get_settings
    from kestrel.interview import question_task
    from kestrel.services import online_consent

    async def boom(*a, **k):
        raise _httpx_error()

    async def not_held(*a, **k):
        return False

    monkeypatch.setattr(question_task, "generate_question_set", boom)
    monkeypatch.setattr(online_consent, "held_at_run_time", not_held)
    async with sm() as s:
        s.add(Task(type=question_task.TASK_TYPE, max_attempts=1,
                   payload={"opportunity_id": 1, "pursuit_id": 1}))
        await s.commit()
    await question_task.drain_interview_questions(sm, object(), get_settings())
    _kept_and_plain(await _stored(sm, question_task.TASK_TYPE))


def test_the_robots_refusal_is_one_authored_sentence_everywhere():
    exc = RobotsDisallowed()
    assert plain_error(exc) == fixed_reason(exc) == plain_os_reason(exc) == ROBOTS_SENTENCE
    assert "robots.txt" in ROBOTS_SENTENCE and "http" not in ROBOTS_SENTENCE
    adapters = pathlib.Path(__file__).resolve().parent.parent / "kestrel" / "ingest" / "adapters"
    for path in adapters.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "robots.txt does not allow" not in text, path
        assert "asks not to be read automatically" not in text, path


# ----------------------------------------------- the real writers, then the pages that read them
async def _stored(sm, task_type: str) -> str:
    from sqlalchemy import select

    async with sm() as s:
        return (await s.execute(
            select(Task.last_error).where(Task.type == task_type).order_by(Task.id.desc())
        )).scalars().first() or ""


def _kept_and_plain(stored: str) -> None:
    from kestrel.errors import shown_failure

    assert "ConnectError" in stored and LEAK in stored  # the diagnosis is kept
    assert shown_failure(stored)
    _clean(shown_failure(stored))  # and none of it reaches a page


async def test_the_training_writer_stores_a_record_and_the_page_is_plain(sm, monkeypatch):
    from kestrel.learning import reranker, training

    async def boom(*a, **k):
        raise _httpx_error()

    monkeypatch.setattr(reranker, "train_reranker", boom)
    async with sm() as s:
        s.add(Task(type=training.TASK_TYPE, payload={"track": "employment"}, max_attempts=2))
        await s.commit()

    async def no_examples(*a, **k):
        return []

    monkeypatch.setattr(training, "labeled_examples", no_examples)
    for _ in range(3):
        await training.run_cycle(sm)
    stored = await _stored(sm, training.TASK_TYPE)
    _kept_and_plain(stored)
    async with sm() as s:
        task = (await training.state(s, "employment"))
    assert task.get("detail"), task  # the failed state shows a plain sentence
    _clean(task["detail"])


async def test_the_story_build_writer_stores_a_record_and_the_page_is_plain(sm, monkeypatch):
    from kestrel.config import get_settings
    from kestrel.interview import stories, story_task

    async def boom(*a, **k):
        raise _httpx_error()

    monkeypatch.setattr(stories, "consolidate_stories", boom)
    async with sm() as s:
        await story_task.request_build(s)
    for _ in range(4):
        await story_task.drain_story_builds(sm, object(), get_settings())
    stored = await _stored(sm, story_task.TASK_TYPE)
    _kept_and_plain(stored)
    async with sm() as s:
        state = await story_task.build_state(s)
        last = await story_task.last_build(s)
    _clean(state.get("reason", "x"))
    _clean((last or {}).get("error", "x"))


async def test_the_bench_writer_stores_a_record_and_the_panel_is_plain(sm):
    from kestrel.bench import check
    from kestrel.config import get_settings

    async with sm() as s:
        s.add(Task(type=check.TASK_TYPE, payload={}, max_attempts=1))
        await s.commit()

    def installed(settings):
        raise _httpx_error()

    await check.drain_checks(sm, get_settings(), make_client=lambda *a, **k: None,
                             installed=installed)
    _kept_and_plain(await _stored(sm, check.TASK_TYPE))
    async with sm() as s:
        _clean((await check.status(s))["error"])
