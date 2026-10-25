"""P16: the sourced company summary (D-LLM-9). Offline: a scripted model replays recorded replies
from tests/fixtures/org_summary/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Org, Quarantine, Task
from kestrel.llm.client import ChatResult
from kestrel.llm.prompts import load_prompt
from kestrel.research import org_summary as osum

FIX = Path(__file__).parent / "fixtures" / "org_summary"


def _fixture(name: str) -> str:
    return (FIX / f"{name}.json").read_text(encoding="utf-8")


class _Client:
    def __init__(self, *replies: str) -> None:
        self.settings = get_settings()
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **kw):
        self.calls.append({"tier": tier, "messages": messages, "schema": schema, **kw})
        return ChatResult(
            content=self.replies.pop(0),
            prompt_tokens=5,
            completion_tokens=5,
            latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )


def _excerpts(n: int = 3) -> list[osum.SummaryExcerpt]:
    texts = [
        "Harbor Ledger builds bookkeeping software for small restaurants.",
        "Our customers are independent restaurants with fewer than 50 staff. We are hiring.",
        "Careers: 12 open roles in support and engineering.",
    ]
    kinds = ["website", "website", "careers"]
    return [
        osum.SummaryExcerpt(
            index=i + 1,
            kind=kinds[i],
            url=f"https://harborledger.example/{i}",
            title=f"page {i}",
            fetched_at="2026-10-01T00:00:00+00:00",
            text=texts[i],
        )
        for i in range(n)
    ]


async def _org(sm) -> int:
    async with sm() as s:
        org = Org(name="Harbor Ledger", normalized_name="harbor ledger")
        s.add(org)
        await s.commit()
        return org.id


async def _run(sm, client, excerpts, org_id) -> osum.Outcome:
    async with sm() as s:
        org = await s.get(Org, org_id)
        return await osum.summarize(s, client, org, excerpts)


async def _quarantine(sm) -> list[Quarantine]:
    async with sm() as s:
        return list((await s.execute(select(Quarantine))).scalars().all())


# ------------------------------------------------------------------------------ the prompt
def test_prompt_is_registered_with_the_specified_settings():
    spec = load_prompt("org_summary/v1")
    assert (spec.tier, spec.temperature, spec.max_tokens, spec.reasoning) == (
        "tier2",
        0.2,
        900,
        "none",
    )
    assert spec.model_cls is not None
    assert "Never use knowledge that is not in the excerpts" in spec.system
    assert "{org_name}" in spec.user and "{EXCERPTS}" in spec.user


def test_schema_source_enum_is_built_per_call():
    cls = osum.summary_model_for(_excerpts(2))
    schema = osum.summary_schema(cls)
    text = json.dumps(schema)
    assert '"enum": [1, 2]' in text and '"enum": [3' not in text
    assert schema["required"] == ["sentences", "tags", "insufficient"]


# ------------------------------------------------------------------------------ the figures
@pytest.mark.parametrize(
    ("sentence", "source", "ok"),
    [
        ("It has 1,200 staff.", "About 1200 people work here.", True),
        ("It has 500+ staff.", "More than 500 staff.", True),
        ("It has 40 staff.", "It has 400 staff.", False),
        ("Founded in 2015.", "Founded in 2015.", True),
        ("Growth was 2.5 times.", "Up 3.25 times.", False),
    ],
)
def test_figure_check(sentence, source, ok):
    assert (osum.unsupported_figures(sentence, [source]) == []) is ok


# ------------------------------------------------------------------------------ the run
async def test_valid_reply_is_stored_with_sources_model_and_date(sm):
    org_id = await _org(sm)
    client = _Client(_fixture("valid"))
    out = await _run(sm, client, _excerpts(), org_id)
    assert out.status == "ok" and len(client.calls) == 1
    async with sm() as s:
        rec = await osum.load_record(s, org_id)
    assert rec["status"] == "ok" and rec["insufficient_sources"] is False
    assert rec["source_count"] == 3 and rec["prompt_version"] == "org_summary/v1"
    assert rec["model"] == get_settings().llm.tiers["tier2"].model and rec["generated_at"]
    assert [s["source_indexes"] for s in rec["sentences"]] == [[1], [1, 2], [2, 3]]
    assert {t["key"] for t in rec["tags"]} == {"does", "hiring"}
    assert [s["index"] for s in rec["sources"]] == [1, 2, 3]
    assert {"url", "title", "kind", "fetched_at"} <= set(rec["sources"][0])
    assert await _quarantine(sm) == []
    call = client.calls[0]
    assert call["tier"] == "tier2" and call["temperature"] == 0.2
    user = call["messages"][1]["content"]
    assert "Company: Harbor Ledger" in user and "[3] (careers)" in user


async def test_invalid_then_repaired(sm):
    org_id = await _org(sm)
    client = _Client(_fixture("invented_figure"), _fixture("valid"))
    out = await _run(sm, client, _excerpts(), org_id)
    assert out.status == "ok" and len(client.calls) == 2
    repair = client.calls[1]
    assert repair["temperature"] == 0.0
    # the repair quotes the failing check
    assert "3,000" in repair["messages"][-1]["content"]
    assert await _quarantine(sm) == []


async def test_unrepairable_is_quarantined_with_the_raw_reply_and_never_crashes(sm):
    org_id = await _org(sm)
    client = _Client(_fixture("invented_figure"), _fixture("invented_figure"))
    out = await _run(sm, client, _excerpts(), org_id)
    assert out.status == "quarantined"
    rows = await _quarantine(sm)
    assert len(rows) == 1 and rows[0].tier == 16 and rows[0].opportunity_id is None
    assert "3,000" in rows[0].raw_response and "Harbor Ledger" in rows[0].prompt
    async with sm() as s:
        rec = await osum.load_record(s, org_id)
    assert rec["status"] == "quarantined" and rec["sentences"] == []  # never an unsourced line


async def test_quarantine_keeps_the_previous_good_summary(sm):
    org_id = await _org(sm)
    assert (await _run(sm, _Client(_fixture("valid")), _excerpts(), org_id)).status == "ok"
    bad = _fixture("invented_figure")
    assert (await _run(sm, _Client(bad, bad), _excerpts(), org_id)).status == "quarantined"
    async with sm() as s:
        rec = await osum.load_record(s, org_id)
    assert rec["status"] == "ok" and len(rec["sentences"]) == 3
    assert len(await _quarantine(sm)) == 1


async def test_hallucinated_source_index_is_rejected_not_dropped(sm):
    org_id = await _org(sm)
    client = _Client(_fixture("hallucinated_index"), _fixture("hallucinated_index"))
    out = await _run(sm, client, _excerpts(), org_id)
    assert out.status == "quarantined" and len(client.calls) == 2
    assert "9" in (await _quarantine(sm))[0].error  # the unresolved index is named
    async with sm() as s:
        assert (await osum.load_record(s, org_id))["sentences"] == []


async def test_a_sentence_without_an_index_is_a_failure(sm):
    org_id = await _org(sm)
    raw = json.dumps(
        {
            "sentences": [
                {"text": "It makes software.", "source_indexes": []},
                {"text": "It serves restaurants.", "source_indexes": [1]},
            ],
            "tags": [],
            "insufficient": False,
        }
    )
    out = await _run(sm, _Client(raw, raw), _excerpts(), org_id)
    assert out.status == "quarantined"


async def test_not_enough_sources_makes_no_model_call(sm):
    org_id = await _org(sm)
    client = _Client()
    out = await _run(sm, client, _excerpts(1), org_id)
    assert out.status == "insufficient" and not out.model_called and client.calls == []
    async with sm() as s:
        rec = await osum.load_record(s, org_id)
    assert rec["status"] == "insufficient" and rec["insufficient_sources"] is True
    assert rec["source_count"] == 1 and rec["sentences"] == [] and rec["model"] is None


async def test_blank_excerpts_do_not_count_as_sources(sm):
    org_id = await _org(sm)
    ex = _excerpts(2)
    ex[1].text = "   "
    client = _Client()
    assert (await _run(sm, client, ex, org_id)).status == "insufficient"
    assert client.calls == []


async def test_model_says_insufficient_keeps_no_text(sm):
    org_id = await _org(sm)
    out = await _run(sm, _Client(_fixture("insufficient")), _excerpts(), org_id)
    assert out.status == "insufficient" and out.model_called
    async with sm() as s:
        rec = await osum.load_record(s, org_id)
    assert rec["status"] == "insufficient" and rec["sentences"] == [] and rec["tags"] == []


async def test_em_dash_becomes_a_comma_for_display(sm):
    org_id = await _org(sm)
    await _run(sm, _Client(_fixture("em_dash")), _excerpts(), org_id)
    async with sm() as s:
        rec = await osum.load_record(s, org_id)
    assert "—" not in rec["sentences"][0]["text"]
    assert "software, for small" in rec["sentences"][0]["text"]


# ------------------------------------------------------------------------------ the task
async def _count(sm) -> int:
    async with sm() as s:
        return (
            await s.execute(
                select(func.count()).select_from(Task).where(Task.type == osum.TASK_TYPE)
            )
        ).scalar_one()


async def test_refresh_queues_one_task_and_the_worker_drains_it(sm):
    org_id = await _org(sm)
    async with sm() as s:
        assert await osum.refresh_summary(s, org_id)
        assert not await osum.refresh_summary(s, org_id)  # one live task per company
        await s.commit()
    assert await _count(sm) == 1

    async def acquire(session, org, settings):
        return _excerpts()

    client = _Client(_fixture("valid"))
    assert await osum.drain_org_summaries(sm, client, get_settings(), acquire=acquire) == 1
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == osum.TASK_TYPE))).scalar_one()
        assert task.status == "done"
        assert (await osum.load_record(s, org_id))["status"] == "ok"
        assert not await osum.needs_summary(s, org_id)


async def test_a_failed_acquire_is_retried_then_dead_lettered_with_the_reason(sm):
    org_id = await _org(sm)
    async with sm() as s:
        await osum.refresh_summary(s, org_id)
        await s.commit()

    async def acquire(session, org, settings):
        raise OSError("network down")

    assert await osum.drain_org_summaries(sm, _Client(), get_settings(), acquire=acquire) == 0
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == osum.TASK_TYPE))).scalar_one()
    assert task.attempts == osum.MAX_ATTEMPTS and task.status == "dead"
    assert "network down" in task.last_error


async def test_a_quarantined_run_completes_the_task(sm):
    org_id = await _org(sm)
    async with sm() as s:
        await osum.refresh_summary(s, org_id)
        await s.commit()

    async def acquire(session, org, settings):
        return _excerpts()

    bad = _fixture("hallucinated_index")
    assert (
        await osum.drain_org_summaries(sm, _Client(bad, bad), get_settings(), acquire=acquire) == 1
    )
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == osum.TASK_TYPE))).scalar_one()
    assert task.status == "done"


async def test_automatic_request_is_skipped_when_tier2_is_online(sm, monkeypatch):
    org_id = await _org(sm)

    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    async with sm() as s:
        assert not await osum.request_if_needed(s, org_id, "opened")
        assert await osum.refresh_summary(s, org_id)  # the click is the consent
        await s.commit()


async def test_request_if_needed_only_when_missing_or_stale(sm):
    org_id = await _org(sm)
    async with sm() as s:
        assert await osum.request_if_needed(s, org_id, "opened")
        await s.commit()
        assert not await osum.request_if_needed(s, org_id, "opened")  # already queued
    assert await _count(sm) == 1


def test_from_excerpt_set_maps_kinds():
    from types import SimpleNamespace as NS

    found = NS(
        excerpts=[
            NS(index=1, url="https://x.example/careers", kind="company_page", text="a"),
            NS(index=2, url=None, kind="ats", text="b"),
            NS(index=3, url="https://sec.example/f", kind="edgar", text="c"),
            NS(index=4, url=None, kind="operator_paste", text="d"),
        ]
    )
    out = osum.from_excerpt_set(found)
    assert [e.kind for e in out] == ["careers", "ats_board", "filing", "pasted"]
    assert all(e.kind in osum.KINDS for e in out)


# ------------------------------------------------------------------------------ review fixes
def _app_client():
    from tests.test_hosted_mode import _fresh_app

    return _fresh_app()


def _seed_org_sync() -> int:
    import asyncio

    from kestrel.db.engine import get_sessionmaker

    async def go() -> int:
        async with get_sessionmaker()() as s:
            org = Org(name="Harbor Ledger", normalized_name="harbor ledger")
            s.add(org)
            await s.commit()
            return org.id

    return asyncio.run(go())


def _live_tasks_sync() -> list[Task]:
    import asyncio

    from kestrel.db.engine import get_sessionmaker

    async def go():
        async with get_sessionmaker()() as s:
            return list(
                (await s.execute(select(Task).where(Task.type == osum.TASK_TYPE))).scalars().all()
            )

    return asyncio.run(go())


def test_opening_the_company_page_queues_the_summary_once():
    client = _app_client()
    org_id = _seed_org_sync()
    assert client.get(f"/orgs/{org_id}").status_code == 200
    client.get(f"/orgs/{org_id}")
    tasks = _live_tasks_sync()
    assert len(tasks) == 1 and tasks[0].payload["reason"] == "opened"
    assert tasks[0].payload["automatic"] is True


def test_refresh_endpoint_queues_the_task_and_404s_for_a_missing_company():
    client = _app_client()
    org_id = _seed_org_sync()
    r = client.post(f"/orgs/{org_id}/summary/refresh")
    assert r.status_code == 200 and "Refreshing the summary" in r.text
    assert "already waiting" in client.post(f"/orgs/{org_id}/summary/refresh").text
    assert client.post("/orgs/9999/summary/refresh").status_code == 404
    assert [t.payload["reason"] for t in _live_tasks_sync()] == ["refresh"]


async def test_weekly_pass_requeues_only_stale_summaries(sm):
    from datetime import UTC, datetime, timedelta

    fresh = await _org(sm)
    async with sm() as s:
        other = Org(name="Old Co", normalized_name="old co")
        s.add(other)
        await s.commit()
        stale = other.id
        now = datetime.now(UTC)
        await osum.save_record(s, fresh, {"status": "ok", "generated_at": now.isoformat()})
        await osum.save_record(
            s, stale, {"status": "ok", "generated_at": (now - timedelta(days=9)).isoformat()}
        )
        await s.commit()
    assert await osum.request_weekly(sm, force=True) == 1
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == osum.TASK_TYPE))).scalar_one()
    assert task.payload["org_id"] == stale and task.payload["reason"] == "weekly"
    assert await osum.request_weekly(sm) == 0  # throttled between checks


async def test_a_quarantine_starts_the_cooldown_but_refresh_still_runs(sm):
    org_id = await _org(sm)
    assert (await _run(sm, _Client(_fixture("valid")), _excerpts(), org_id)).status == "ok"
    # age the good summary and its attempt past the window, then fail a retry
    async with sm() as s:
        rec = await osum.load_record(s, org_id)
        rec["generated_at"] = "2020-01-01T00:00:00+00:00"
        await osum.save_record(s, org_id, rec)
        await osum.note_attempt(s, org_id, "2020-01-01T00:00:00+00:00")
        await s.commit()
        assert await osum.needs_summary(s, org_id)
    bad = _fixture("invented_figure")
    assert (await _run(sm, _Client(bad, bad), _excerpts(), org_id)).status == "quarantined"
    async with sm() as s:
        assert not await osum.needs_summary(s, org_id)  # automatic triggers wait a week
        assert not await osum.request_if_needed(s, org_id, "opened")
        assert await osum.refresh_summary(s, org_id)  # the operator's click always runs
        await s.commit()


async def test_a_dead_lettered_task_also_starts_the_cooldown(sm):
    org_id = await _org(sm)
    async with sm() as s:
        await osum.refresh_summary(s, org_id)
        await s.commit()

    async def acquire(session, org, settings):
        raise OSError("down")

    await osum.drain_org_summaries(sm, _Client(), get_settings(), acquire=acquire)
    async with sm() as s:
        assert not await osum.needs_summary(s, org_id)


async def test_an_automatic_task_is_dropped_if_tier2_went_online_meanwhile(sm, monkeypatch):
    org_id = await _org(sm)
    async with sm() as s:
        assert await osum.request_if_needed(s, org_id, "opened")
        await s.commit()

    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    client = _Client()

    async def acquire(session, org, settings):
        raise AssertionError("must not fetch or call a model")

    await osum.drain_org_summaries(sm, client, get_settings(), acquire=acquire)
    assert client.calls == []
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == osum.TASK_TYPE))).scalar_one()
    assert task.status == "done"


async def test_a_manual_refresh_queued_locally_is_held_if_tier2_went_online(sm, monkeypatch):
    org_id = await _org(sm)
    async with sm() as s:
        assert await osum.refresh_summary(s, org_id)  # queued while Tier 2 was local
        await s.commit()

    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    client = _Client(_fixture("valid"))

    async def acquire(session, org, settings):
        raise AssertionError("must not fetch or call a model")

    await osum.drain_org_summaries(sm, client, get_settings(), acquire=acquire)
    assert client.calls == []
    async with sm() as s:
        assert await osum.is_held(s, org_id)
        assert not await osum.is_queued(s, org_id)
    # Pressing Refresh again, now online, is the consent: it runs and clears the hold.
    async with sm() as s:
        assert await osum.refresh_summary(s, org_id)
        await s.commit()
        assert not await osum.is_held(s, org_id)

    async def acquire_ok(session, org, settings):
        return _excerpts()

    assert await osum.drain_org_summaries(sm, client, get_settings(), acquire=acquire_ok) == 1


async def test_a_manual_refresh_still_runs_when_tier2_is_online(sm, monkeypatch):
    org_id = await _org(sm)

    async def online(session):
        return True

    monkeypatch.setattr(osum, "_runs_online", online)
    async with sm() as s:
        await osum.refresh_summary(s, org_id)
        await s.commit()

    async def acquire(session, org, settings):
        return _excerpts()

    assert (
        await osum.drain_org_summaries(
            sm, _Client(_fixture("valid")), get_settings(), acquire=acquire
        )
        == 1
    )


async def test_long_sentences_are_cut_to_30_words(sm):
    org_id = await _org(sm)
    long = "Harbor Ledger makes " + " ".join(["bookkeeping"] * 40) + " software."
    raw = json.dumps(
        {
            "sentences": [
                {"text": long, "source_indexes": [1]},
                {"text": "It serves restaurants.", "source_indexes": [1]},
            ],
            "tags": [],
            "insufficient": False,
        }
    )
    assert (await _run(sm, _Client(raw), _excerpts(), org_id)).status == "ok"
    async with sm() as s:
        rec = await osum.load_record(s, org_id)
    cut = rec["sentences"][0]["text"]
    assert len(cut.split()) <= 30 and cut.endswith("\u2026") and len(rec["sentences"]) == 2


async def test_live_check_is_per_company(sm):
    a = await _org(sm)
    async with sm() as s:
        b = Org(name="Other", normalized_name="other")
        s.add(b)
        await s.commit()
        assert await osum.request_org_summary(s, a, "x")
        assert await osum.request_org_summary(s, b.id, "x")  # a's live task does not block b
        assert not await osum.request_org_summary(s, a, "x")
