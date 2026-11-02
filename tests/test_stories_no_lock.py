"""D-DB-1: a story build holds no write lock while a model call runs, and stays atomic.

The probe opens a second raw SQLite connection inside every model call and takes `BEGIN IMMEDIATE`
with no wait. If the build held a write transaction (a flushed Story, an autoflushed usage row),
that fails with "database is locked". Offline: the model is a fake."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Quarantine, Story, StoryUsage
from kestrel.interview.stories import consolidate_stories
from kestrel.llm.client import ChatResult
from tests.test_interview_stories import _add_eval, _FakeClient, _seed_opp, _sketch


def _db_file(sm) -> str:
    return sm.kw["bind"].url.database


class _Killed(BaseException):
    """What a process kill looks like to the code: nothing in the build catches it."""


class _ProbingClient(_FakeClient):
    """Fails the test run if the database is write-locked at the moment a model call is made."""

    def __init__(self, db_file: str, *, fail_on: str | None = None, explode_on: str | None = None):
        super().__init__()
        self.db_file = db_file
        self.fail_on = fail_on
        self.explode_on = explode_on
        self.probes = 0
        self.locked: list[str] = []

    def _probe(self) -> None:
        conn = sqlite3.connect(self.db_file, timeout=0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")  # fails if another writer holds the lock
            conn.execute("ROLLBACK")
            self.probes += 1
        except sqlite3.OperationalError as exc:
            self.locked.append(str(exc))  # recorded, not raised: the client would swallow it
        finally:
            conn.close()

    async def embed(self, texts, *, tier: str = "embed", timeout: float = 60.0):
        self._probe()
        return await super().embed(texts, tier=tier, timeout=timeout)

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **kw):
        self._probe()
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        if self.explode_on and self.explode_on in user.lower():
            raise _Killed()
        if self.fail_on and self.fail_on in user.lower():
            return ChatResult(
                content="not json {",
                prompt_tokens=1,
                completion_tokens=1,
                latency_ms=1,
                model=self.settings.llm.tiers[tier].model,
            )
        return await super().complete(
            tier=tier, messages=messages, schema=schema, schema_name=schema_name
        )


async def _three_events(sm, order=("delta", "omega", "sigma")) -> None:
    opp = await _seed_opp(sm)
    for word in order:
        await _add_eval(sm, opp, [_sketch(f"A question about {word}?", word)])


async def test_no_write_lock_is_held_across_any_model_call(sm):
    await _three_events(sm)
    client = _ProbingClient(_db_file(sm))
    async with sm() as s:
        result = await consolidate_stories(s, client, get_settings())
    assert len(result.created_story_ids) == 3
    assert client.locked == []
    assert client.probes >= 4  # embed + three merges, each one probed


async def test_no_write_lock_with_a_failed_merge_and_an_attach(sm, monkeypatch):
    # A hosted tier reads settings (a SELECT) before every call; that is what autoflushes any
    # pending attach or quarantine row into a write transaction on the old code.
    monkeypatch.setattr("kestrel.pipeline._shared.tier_is_hosted", lambda *_a, **_k: True)
    await _three_events(sm)  # attach delta, quarantine omega, THEN the sigma call is probed
    async with sm() as s:  # a canonical story that the "delta" cluster will attach to
        s.add(
            Story(
                title="Delta",
                situation="delta",
                task="t",
                action="a",
                result="r",
                reflection="x",
                themes=[],
                is_canonical=True,
                operator_edited=True,
                use_count=1,
            )
        )
        await s.commit()
    client = _ProbingClient(_db_file(sm), fail_on="omega")
    async with sm() as s:
        result = await consolidate_stories(s, client, get_settings())
    assert client.locked == []
    assert len(result.attached) == 1  # delta attached to the operator's story
    assert len(result.created_story_ids) == 1  # sigma; omega quarantined
    async with sm() as s:
        count = select(func.count()).select_from(Quarantine)
        quarantined = (await s.execute(count)).scalar_one()
        edited = (
            await s.execute(select(Story).where(Story.operator_edited.is_(True)))
        ).scalar_one()
    assert quarantined >= 1
    assert edited.use_count == 2 and edited.title == "Delta"


async def test_kill_mid_build_leaves_nothing_and_a_rerun_completes(sm):
    await _three_events(sm)
    killer = _ProbingClient(_db_file(sm), explode_on="sigma")
    async with sm() as s:
        with pytest.raises(_Killed):
            await consolidate_stories(s, killer, get_settings())
    assert killer.locked == []
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Story))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(StoryUsage))).scalar_one() == 0
    async with sm() as s:
        again = _ProbingClient(_db_file(sm))
        result = await consolidate_stories(s, again, get_settings())
    assert again.locked == []
    assert len(result.created_story_ids) == 3
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Story))).scalar_one() == 3
        assert (await s.execute(select(func.count()).select_from(StoryUsage))).scalar_one() == 3


class _DeletingClient(_ProbingClient):
    """Deletes the story the first cluster attached to, from a second session, while the later
    (sigma) model call is in flight: the operator removing a draft mid-build."""

    def __init__(self, sm, victim_id: int):
        super().__init__(_db_file(sm))
        self.sm, self.victim_id, self.deleted = sm, victim_id, False

    async def complete(self, **kw):
        if not self.deleted:
            self.deleted = True
            async with self.sm() as other:
                victim = await other.get(Story, self.victim_id)
                await other.delete(victim)
                await other.commit()
        return await super().complete(**kw)


async def test_an_attach_target_deleted_during_the_build_is_skipped_not_fatal(sm):
    await _three_events(sm, order=("delta", "sigma"))
    async with sm() as s:
        victim = Story(
            title="Delta",
            situation="delta",
            task="t",
            action="a",
            result="r",
            reflection="x",
            themes=[],
            is_canonical=True,
            operator_edited=False,
            use_count=1,
        )
        s.add(victim)
        await s.commit()
        victim_id = victim.id
        # A later row keeps SQLite from handing the victim's id to the next new story.
        s.add(
            Story(
                title="Filler",
                situation="omega",
                task="t",
                action="a",
                result="r",
                reflection="x",
                themes=[],
                is_canonical=True,
                operator_edited=True,
                use_count=1,
            )
        )
        await s.commit()
    client = _DeletingClient(sm, victim_id)
    async with sm() as s:
        first = await consolidate_stories(s, client, get_settings())  # must not raise
    assert client.deleted and client.locked == []
    assert first.attached == [] and len(first.created_story_ids) == 1  # sigma still committed
    async with sm() as s:
        assert await s.get(Story, victim_id) is None
        stories = (await s.execute(select(Story).order_by(Story.id))).scalars().all()
        assert [st.title for st in stories][0] == "Filler"
        assert [st.id for st in stories][1:] == first.created_story_ids
        usages = (await s.execute(select(StoryUsage))).scalars().all()
        assert [u.story_id for u in usages] == first.created_story_ids  # only sigma's usage
    # The orphaned delta sketch was NOT consumed: the next build picks it up again.
    async with sm() as s:
        second = await consolidate_stories(s, _ProbingClient(_db_file(sm)), get_settings())
    assert second.sketches == 1 and len(second.created_story_ids) == 1
