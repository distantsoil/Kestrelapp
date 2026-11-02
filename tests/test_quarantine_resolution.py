"""D-UI-70 and D-UI-71: a failed score is resolved once a later score of the same role at the same
or a higher tier lands, and every surface agrees on what is still open; Retry on Failed scoring
opens the Score again dialog on the model that failed instead of silently re-running it.

The operator's report (Windows, Hosted mode): a role failed Tier 2 on
nvidia/nemotron-3.5-super-vl:free, was scored again on ~deepseek/deepseek-pro-latest and scored
fine (t2_done, a new Tier 2 evaluation), yet Today's Fix now still listed it as failed. Nothing
here reaches a model or the network."""

from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from kestrel import paths
from kestrel.analytics.progress import scoring_progress
from kestrel.analytics.today import happening_now, overnight, system_asks
from kestrel.config import get_settings
from kestrel.db.base import iso
from kestrel.db.models import Evaluation, Opportunity, Quarantine, Task
from kestrel.queue.rescore import enqueue_rescore
from kestrel.web.pages.runs_detail import _quarantine_rows
from kestrel.web.queries import (
    failure_summary,
    live_activity,
    open_quarantine_count,
    scoring_failed_count,
)
from tests import clock_seed

FAILED_MODEL = "nvidia/nemotron-3.5-super-vl:free"
GOOD_MODEL = "~deepseek/deepseek-pro-latest"


def _ago(**kw) -> str:
    return iso(clock_seed.ago(**kw))


async def _role(sm, *, stage: str = "quarantined", title: str = "Director of Operations") -> int:
    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title=title, stage=stage,
                        org_name="Fictional Co", url=f"https://example.test/{title}")
        s.add(o)
        await s.commit()
        return o.id


async def _fail(sm, oid: int, *, tier: int = 2, model: str = FAILED_MODEL, at: str,
                error: str = "generation loop: the reply repeated itself") -> int:
    async with sm() as s:
        q = Quarantine(opportunity_id=oid, tier=tier, model=model, prompt="p",
                       raw_response="{{{{", error=error, attempts=2, created_at=at)
        s.add(q)
        await s.commit()
        return q.id


async def _score(sm, oid: int, *, tier: int = 2, at: str, forced_by: str | None = None) -> None:
    async with sm() as s:
        s.add(Evaluation(opportunity_id=oid, tier=tier, track="employment", model=GOOD_MODEL,
                         verdict="apply" if tier >= 2 else "keep", score=4.2,
                         weighted_score=4.2, forced_by=forced_by, created_at=at))
        await s.commit()


async def _surfaces(sm, monkeypatch) -> dict:
    """Every count that can call a role failed, read the way each surface reads it."""
    async def no_models(_settings):
        return []

    monkeypatch.setattr("kestrel.analytics.today._derived_models_missing", no_models)
    settings = get_settings()
    async with sm() as s:
        asks = await system_asks(s, settings, None)
        ask = next((a for a in asks if a["key"] == "quarantine"), None)
        now = await happening_now(s, mode="hosted")
        return {
            "pipeline_group": await scoring_failed_count(s),
            "rail": (await scoring_progress(s, settings))["open_failed"],
            "open_quarantine": await open_quarantine_count(s),
            "fix_now": sum(int(n) for n in re.findall(r"\d+", ask["title"])) if ask else 0,
            "now_line": now["quarantined"],
            "runs_page": (await live_activity(s))["quarantined"],
            "overnight": (await overnight(s, None, ghosting_days=21))["quarantine_open"],
            "rows": await _quarantine_rows(s),
        }


def _counts(view: dict) -> set[int]:
    return {v for k, v in view.items() if k != "rows"}


# ------------------------------------------------------------------- the reported scenario
async def test_a_role_scored_after_it_failed_is_no_longer_failed_anywhere(sm, monkeypatch):
    oid = await _role(sm)
    qid = await _fail(sm, oid, at=_ago(minutes=30))

    before = await _surfaces(sm, monkeypatch)
    assert _counts(before) == {1}, before
    assert [r["q"].id for r in before["rows"] if not r["resolved"]] == [qid]

    # Score again on another model: queued, so Being scored, not failed (D-UI-56).
    async with sm() as s:
        override = {"backend": "openrouter", "model": GOOD_MODEL, "json_mode": "schema"}
        res = await enqueue_rescore(s, [oid], model_override=override)
        await s.commit()
    assert res.queued == [oid]
    queued = await _surfaces(sm, monkeypatch)
    assert _counts(queued) == {0}, queued
    (row,) = queued["rows"]
    assert row["queued"] and not row["resolved"]

    # It scores fine: a new Tier 2 evaluation, stage t2_done, the task done.
    await _score(sm, oid, at=_ago(minutes=1))
    async with sm() as s:
        (await s.get(Opportunity, oid)).stage = "t2_done"
        for t in (await s.execute(select(Task))).scalars():
            t.status = "done"
        await s.commit()
    after = await _surfaces(sm, monkeypatch)
    assert _counts(after) == {0}, after
    (row,) = after["rows"]
    assert row["resolved"] and not row["queued"]
    async with sm() as s:  # kept as history with its raw reply (hard rule 3)
        kept = await s.get(Quarantine, qid)
    assert kept is not None and kept.raw_response == "{{{{"


async def test_only_a_later_score_at_the_same_or_higher_tier_resolves(sm, monkeypatch):
    # An older score never resolves a newer failure (a failed score-again of a scored role).
    a = await _role(sm, stage="t2_done", title="A")
    await _score(sm, a, at=_ago(days=2))
    await _fail(sm, a, at=_ago(hours=1), error="[score again failed #7] generation loop")
    # A Tier 1 score never resolves a Tier 2 failure.
    b = await _role(sm, title="B")
    await _fail(sm, b, at=_ago(hours=2))
    await _score(sm, b, tier=1, at=_ago(hours=1))
    # Tier 1's own promotion row records the failure; it is not a score. Tier 2 resolves it.
    c = await _role(sm, stage="t1_pass", title="C")
    await _fail(sm, c, tier=1, at=_ago(hours=3))
    await _score(sm, c, tier=1, at=_ago(hours=3), forced_by="t1_quarantine_promoted")
    d = await _role(sm, stage="t2_done", title="D")
    await _fail(sm, d, tier=1, at=_ago(hours=3))
    await _score(sm, d, tier=1, at=_ago(hours=3), forced_by="t1_quarantine_promoted")
    await _score(sm, d, at=_ago(hours=2))
    # A research failure is never resolved by a score.
    e = await _role(sm, stage="t2_done", title="E")
    await _score(sm, e, at=_ago(days=1))
    await _fail(sm, e, tier=5, at=_ago(days=2), error="research failed")

    async with sm() as s:
        rows = {r["opp"].title: r["resolved"] for r in await _quarantine_rows(s)}
        n = await open_quarantine_count(s)
    assert rows == {"A": False, "B": False, "C": False, "D": True, "E": False}
    assert n == 4


async def test_every_surface_agrees_across_kinds_of_failure(sm, monkeypatch):
    """Fix now, the rail, the Now line, the Runs pages, the tray and overnight read one number
    (failure_summary): parked roles, failed screens, failed score-agains, other failures and
    orphan rows, once per role. The Pipeline group counts only the parked roles among them."""
    parked = await _role(sm, title="Parked")
    await _fail(sm, parked, at=_ago(hours=3))
    errored = await _role(sm, stage="error", title="Errored")  # parked with no quarantine row
    again = await _role(sm, stage="t2_done", title="Again")
    await _score(sm, again, at=_ago(days=1))
    await _fail(sm, again, at=_ago(hours=1), error="[score again failed #3] generation loop")
    research = await _role(sm, stage="t2_done", title="Research")
    await _score(sm, research, at=_ago(days=1))
    await _fail(sm, research, tier=5, at=_ago(hours=1), error="research failed")
    async with sm() as s:
        s.add(Quarantine(opportunity_id=None, tier=12, model="m", error="summary failed",
                         attempts=2, created_at=_ago(hours=1)))
        await s.commit()
    view = await _surfaces(sm, monkeypatch)
    assert view["pipeline_group"] == 2  # Parked and Errored: stage-based, a subset
    others = {k: v for k, v in view.items() if k not in ("rows", "pipeline_group")}
    assert set(others.values()) == {5}, others
    async with sm() as s:
        summary = await failure_summary(s)
        asks = await system_asks(s, get_settings(), None)
    assert (summary.scoring, summary.rescore, summary.other) == (2, 1, 2)
    ask = next(a for a in asks if a["key"] == "quarantine")
    assert ask["title"] == "4 postings need another try; score again failed for 1"
    assert "Its earlier score stands." in ask["line"] and "—" not in ask["line"]
    del errored


async def test_a_failed_score_again_alone_says_so_in_fix_now(sm, monkeypatch):
    again = await _role(sm, stage="t2_done", title="Again")
    await _score(sm, again, at=_ago(days=1))
    await _fail(sm, again, at=_ago(hours=1), error="[score again failed #3] generation loop")
    view = await _surfaces(sm, monkeypatch)
    assert view["pipeline_group"] == 0  # its earlier score stands: not parked
    assert {v for k, v in view.items() if k not in ("rows", "pipeline_group")} == {1}
    async with sm() as s:
        asks = await system_asks(s, get_settings(), None)
    ask = next(a for a in asks if a["key"] == "quarantine")
    assert ask["title"] == "Score again failed for 1 posting"


async def test_a_task_held_by_the_track_switch_never_hides_a_failure(sm, monkeypatch):
    """Only an in-flight task (queue.in_flight) makes a failure "queued"; a background task the
    track switch holds keeps the row open, counted, and offering Retry."""
    from kestrel.services import tracks

    async with sm() as s:
        o = Opportunity(track="consulting", opp_type="contract", title="Held",
                        stage="quarantined", url="https://example.test/held")
        s.add(o)
        await s.commit()
        oid = o.id
    await _fail(sm, oid, at=_ago(hours=1))
    async with sm() as s:
        s.add(Task(type="t2", payload={"opportunity_id": oid}))  # background, not requested
        await s.commit()
    monkeypatch.setitem(tracks._cache, "value", ("employment",))  # consulting switched off
    view = await _surfaces(sm, monkeypatch)
    assert _counts(view) == {1}, view
    (row,) = view["rows"]
    assert not row["queued"] and row["needs_you"]
    monkeypatch.setitem(tracks._cache, "value", ("employment", "consulting"))
    view = await _surfaces(sm, monkeypatch)
    assert _counts(view) == {0}, view
    assert view["rows"][0]["queued"]


async def test_only_a_resolving_task_marks_a_scoring_row_queued(sm, monkeypatch):
    oid = await _role(sm, stage="t2_done", title="Scored")
    await _score(sm, oid, at=_ago(days=1))
    await _fail(sm, oid, tier=3, model="frontier", at=_ago(hours=2))
    await _fail(sm, oid, tier=5, at=_ago(hours=2), error="research failed")
    t2 = await _role(sm, title="Parked")
    await _fail(sm, t2, at=_ago(hours=2))
    async with sm() as s:
        s.add(Task(type="t2", payload={"opportunity_id": oid, "requested": True}))
        s.add(Task(type="t1", payload={"opportunity_id": t2, "requested": True}))  # cannot fix T2
        await s.commit()
        rows = await _quarantine_rows(s)
    assert [r["queued"] for r in rows] == [False, False, False]
    assert all(r["needs_you"] for r in rows)


async def test_an_old_open_failure_is_listed_behind_many_newer_resolved_ones(sm):
    """Hard rule 4: an open failure Today counts is never pushed off Failed scoring."""
    old = await _role(sm, title="Old failure")
    old_q = await _fail(sm, old, at=_ago(days=30))
    async with sm() as s:
        for i in range(230):
            o = Opportunity(track="employment", opp_type="job_posting", title=f"R{i}",
                            stage="t2_done", url=f"https://example.test/r{i}")
            s.add(o)
            await s.flush()
            s.add(Quarantine(opportunity_id=o.id, tier=2, model="m", error="loop", attempts=2,
                             created_at=_ago(days=2)))
            s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment", model="m",
                             verdict="pass", score=2.0, created_at=_ago(days=1)))
        await s.commit()
        rows = await _quarantine_rows(s)
    open_rows = [r for r in rows if not r["resolved"]]
    assert [r["q"].id for r in open_rows] == [old_q]
    assert len([r for r in rows if r["resolved"]]) == 200  # history keeps its own cap


async def test_a_hidden_role_and_a_repeat_failure_count_once_or_not_at_all(sm, monkeypatch):
    oid = await _role(sm)
    await _fail(sm, oid, at=_ago(hours=2))
    await _fail(sm, oid, at=_ago(hours=1))  # the same role failing twice is one posting
    hidden = await _role(sm, title="Hidden")
    await _fail(sm, hidden, at=_ago(hours=1))
    async with sm() as s:
        (await s.get(Opportunity, hidden)).archived_at = _ago(minutes=5)
        await s.commit()
    view = await _surfaces(sm, monkeypatch)
    assert _counts(view) == {1}, view


# ------------------------------------------------------------------------- Retry (D-UI-71)
def _client(monkeypatch, *, hosted_ids: tuple[str, ...] = ()):
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.services.hosted_models import PickerModel
    from kestrel.web.app import build_app

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    forget_engine()
    monkeypatch.setattr("kestrel.web.pages.rescore._local_models",
                        lambda settings: (["kestrel-other:latest"], None))

    async def hosted(_settings):
        picker = [PickerModel(id=i, name=i, free=i.endswith(":free"), context_length=131072,
                              price="free" if i.endswith(":free") else "$1 / M")
                  for i in hosted_ids]
        return ("OpenRouter", "openrouter", [], picker, {}, None)

    monkeypatch.setattr("kestrel.web.pages.rescore._hosted_models", hosted)

    async def seed() -> dict:
        sm = get_sessionmaker()
        t2 = await _role(sm, title="Parked")
        t1 = await _role(sm, stage="t1_pass", title="Promoted")
        scored = await _role(sm, stage="t2_done", title="Scored")
        await _score(sm, scored, at=_ago(days=1))
        return {
            "t2": t2, "t1": t1, "scored": scored,
            "q2": await _fail(sm, t2, at=_ago(hours=1)),
            "q1": await _fail(sm, t1, tier=1, model="kestrel-tier1", at=_ago(hours=1)),
            "q_rescore": await _fail(sm, scored, model="kestrel-other", at=_ago(minutes=5),
                                     error="[score again failed #1] generation loop"),
            "q3": await _fail(sm, scored, tier=3, model="frontier", at=_ago(minutes=4)),
        }

    ids = asyncio.run(seed())
    forget_engine()
    return TestClient(build_app(worker=False)), ids


def _picked(body: str, value: str) -> bool:
    """The shared picker (partials/model_picker.html) opened on `value`: its selected <option>,
    or the value its script-driven field starts with. The macro is used as-is."""
    v = re.escape(value)
    return bool(re.search(rf'<option value="{v}"[^>]*\sselected', body)
                or re.search(rf'name="model" value="{v}"', body))


def _row_html(page: str, qid: int) -> str:
    """One row of Needs another try: from its element to the next row (or the end of the list)."""
    start = page.index(f'id="rt-row-{qid}"')
    nxt = page.find('id="rt-row-', start + 10)
    return page[start:nxt if nxt != -1 else len(page)]


def test_the_page_lists_each_role_once_and_the_score_again_dialog_still_opens_on_the_failed_model(
    monkeypatch,
):
    client, ids = _client(monkeypatch)
    page = client.get("/quarantine").text
    # One row per role (the scored role's two failures are one row), the pill counts roles.
    assert "<h1>Needs another try</h1>" in page and "3 roles waiting" in page
    for key in ("q2", "q1", "q3"):
        assert f'id="rt-row-{ids[key]}"' in page, key
    assert f'id="rt-row-{ids["q_rescore"]}"' not in page  # folded into the newer failure's row
    # Try again re-queues in place (D-UI-104); the model menu loads on demand; the dialog opens
    # on the model that failed only from the Score again dialog now (D-UI-71 stays for it).
    row = _row_html(page, ids["q2"])
    assert f'hx-post="/quarantine/{ids["q2"]}/retry"' in row and "Try again" in row
    assert f'hx-get="/quarantine/{ids["q2"]}/models"' in row
    assert "—" not in page

    hx = {"HX-Request": "true"}
    body = client.get("/rescore/choose", params={"opp_ids": ids["scored"],
                                                 "model": "kestrel-other",
                                                 "quarantine_id": ids["q_rescore"]},
                      headers=hx).text
    assert 'value="other" data-testid="score-again-other" checked' in body
    assert 'value="same" data-testid="score-again-same"' in body
    assert 'value="same" checked' not in body
    assert _picked(body, "kestrel-other:latest")
    assert f'name="quarantine_id" value="{ids["q_rescore"]}"' in body
    assert "showModal" in body and 'data-testid="score-again-dialog" open' not in body

    # The model that failed is not offered now: Same model, and a note that says so.
    body = client.get("/rescore/choose", params={"opp_ids": ids["t2"], "model": FAILED_MODEL},
                      headers=hx).text
    assert 'value="same" checked' in body
    assert f"The model that failed ({FAILED_MODEL}) is not offered right now" in body


def test_retry_preselects_a_hosted_model_and_keeps_it_or_another(monkeypatch):
    client, ids = _client(monkeypatch, hosted_ids=(FAILED_MODEL, GOOD_MODEL))
    body = client.get("/rescore/choose", params={"opp_ids": ids["t2"], "model": FAILED_MODEL},
                      headers={"HX-Request": "true"}).text
    assert 'value="other" data-testid="score-again-other" checked' in body
    assert _picked(body, f"hosted:{FAILED_MODEL}")
    assert not _picked(body, f"hosted:{GOOD_MODEL}")
    assert f'value="hosted:{GOOD_MODEL}"' in body


def test_submitting_queues_an_operator_request_and_counts_the_attempt(monkeypatch):
    client, ids = _client(monkeypatch)
    r = client.post("/rescore", data={"opp_ids": str(ids["t2"]), "model_choice": "other",
                                      "model": "kestrel-other:latest",
                                      "quarantine_id": str(ids["q2"])})
    assert "Queued 1 role for scoring." in r.text

    async def read():
        from kestrel.db.engine import forget_engine, get_sessionmaker

        sm = get_sessionmaker()
        async with sm() as s:
            tasks = list((await s.execute(select(Task))).scalars())
            q = await s.get(Quarantine, ids["q2"])
            stage = (await s.get(Opportunity, ids["t2"])).stage
        forget_engine()
        return tasks, q, stage

    tasks, q, stage = asyncio.run(read())
    (task,) = tasks
    assert task.type == "t2" and task.payload["requested"] is True  # never held by a track
    assert task.payload["rescore"] is True
    assert task.payload["model_override"]["model"] == "kestrel-other:latest"
    assert q.attempts == 3 and stage == "quarantined"  # the stage moves when the score lands
    # Now queued: the row says so instead of offering Try again.
    row = _row_html(client.get("/quarantine").text, ids["q2"])
    assert 'data-testid="retry-queued"' in row and "Trying again" in row


def test_retry_works_without_script(monkeypatch):
    client, ids = _client(monkeypatch)
    page = client.get("/rescore/choose", params={"opp_ids": ids["t2"], "model": FAILED_MODEL,
                                                 "quarantine_id": ids["q2"],
                                                 "next": "/quarantine", "page": "1"}).text
    assert "Score this role again?" in page and "Score again · Kestrel" in page
    assert ('<dialog class="score-again-dialog sf-dialog" data-testid="score-again-dialog" open'
            in page)
    assert 'method="post" action="/rescore"' in page and 'hx-post="/rescore"' not in page
    # The dialog's own script stays out of the no-script page. (Match its own code, not
    # "showModal": the shell's quit dialog uses that too when the desktop shell is on.)
    assert "syncPick" not in page and 'name="next" value="/quarantine"' in page
    assert 'href="/quarantine" data-testid="score-again-cancel"' in page

    # "A different model" with nothing picked is refused, never quietly the same model.
    r = client.post("/rescore", data={"opp_ids": str(ids["t2"]), "model_choice": "other",
                                      "model": "", "next": "/quarantine"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/quarantine?outcome=no_pick&n=0"
    refused = client.get(r.headers["location"]).text
    assert 'class="rt-banner is-warn" role="status" data-testid="retry-note"' in refused
    assert "Not queued: pick a model, or choose Same model." in refused
    r = client.post("/rescore", data={"opp_ids": str(ids["t2"]), "model_choice": "same",
                                      "quarantine_id": str(ids["q2"]),
                                      "next": "https://elsewhere.test/"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/quarantine?outcome=queued&n=1"
    landed = client.get(r.headers["location"]).text
    assert 'class="rt-banner is-good" role="status" data-testid="retry-note"' in landed
    assert "Queued 1 role for scoring." in landed
    # Free text in the URL is never shown; an unknown code shows nothing.
    page = client.get("/quarantine", params={"note": "Call this number", "outcome": "x"}).text
    assert "Call this number" not in page and 'data-testid="retry-note"' not in page
