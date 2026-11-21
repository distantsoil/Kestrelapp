"""A model timeout and a refused key never become a grouped quarantine row (D-UI-553, D-UI-566).

That is why Needs another try has no group and no fix for them: after a try the worker keeps the
task and retries it with the plain sentence the Runs page shows, and the role keeps its stage. When
the tries run out, a later change may park the dead task as a quarantine row; any such row carries
no typed kind, so it is never grouped and gets no fix button. Driven through the real worker and
the real tier 2 handler with a faked model that fails the way the transport fails. Invented text
only; nothing reaches a network."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Quarantine, Task
from kestrel.errors import MODEL_KEY_REJECTED, MODEL_TIMEOUT
from kestrel.llm.client import ProviderError
from kestrel.llm.failure_kind import split_stored
from kestrel.pipeline.worker import Worker
from tests.pipeline_fakes import FakeLLM
from tests.test_score_again import _drain, _opp


class _Failing(FakeLLM):
    def __init__(self, settings, exc: BaseException) -> None:
        super().__init__(settings)
        self.exc = exc

    async def complete(self, **kw):
        raise self.exc


async def _queue_t2(sm, oid: int) -> None:
    async with sm() as s:
        s.add(Task(type="t2", payload={"opportunity_id": oid}, priority=30))
        await s.commit()


@pytest.mark.parametrize(("exc", "sentence"), [
    (httpx.ReadTimeout("slow"), MODEL_TIMEOUT),
    (ProviderError(401, "invalid api key", "https://api.example.test/v1"), MODEL_KEY_REJECTED),
])
async def test_a_request_failure_keeps_the_task_and_writes_no_quarantine_row(sm, exc, sentence):
    oid = await _opp(sm, stage="t1_pass")
    await _queue_t2(sm, oid)
    llm = _Failing(get_settings(), exc)

    stats = await _drain(Worker(sm, llm, get_settings()), "t2")

    async with sm() as s:
        rows = (await s.execute(select(Quarantine))).scalars().all()
        task = (await s.execute(select(Task))).scalars().one()
        opp = await s.get(Opportunity, oid)
    assert rows == []  # no row, so no kind, so nothing for Needs another try to group
    assert stats.errors == 1 and stats.completed == 0
    assert task.status != "done" and task.attempts == 1  # kept, one try counted
    assert task.last_error and sentence in task.last_error  # the plain sentence, not the raw text
    assert opp.stage == "t1_pass"  # the role is untouched


@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("slow"),
    ProviderError(401, "invalid api key", "https://api.example.test/v1"),
])
async def test_when_the_tries_run_out_any_row_has_no_kind_and_is_never_grouped(sm, exc):
    """Drain until the task is out of tries (clearing its backoff between drains). Whatever the
    task ends as, no quarantine row it leaves behind carries a kind, so no group can form."""
    oid = await _opp(sm, stage="t1_pass")
    await _queue_t2(sm, oid)
    llm = _Failing(get_settings(), exc)
    worker = Worker(sm, llm, get_settings())

    for _ in range(8):
        async with sm() as s:
            task = (await s.execute(select(Task))).scalars().one()
            if task.status != "pending":
                break
            task.retry_at = None
            await s.commit()
        await _drain(worker, "t2")

    async with sm() as s:
        task = (await s.execute(select(Task))).scalars().one()
        rows = (await s.execute(select(Quarantine))).scalars().all()
    assert task.status != "pending" and task.attempts >= 2  # it really ran out of tries
    assert all(split_stored(q.error)[0] is None for q in rows)
