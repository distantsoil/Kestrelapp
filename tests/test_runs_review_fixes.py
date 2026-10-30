"""Runs review fixes (D-UI-135): the "Both" run choice, the online cost and credit, History's
found column and full sentences, a strict window for a run's searches after Stop, and Run now
after Stop. Cloud-safe: TestClient, a throwaway data directory, fictional runs, no network."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel.config import HOSTED_REQUIRED_TIERS, RUN_CHOICES, load_settings
from kestrel.db.models import Evaluation, Opportunity, Run, ScrapeRun, Setting, Source, Task
from kestrel.llm.providers import ModelInfo
from kestrel.services.setting_overrides import PENDING_RUN_MODE_KEY
from kestrel.web.app import build_app, set_paused
from tests import runs_seed
from tests.test_run_mode import _HOSTED_T1, _HOSTED_T2, _nested, _rows
from tests.test_runs_page import _read, _write

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_CSS = Path(__file__).resolve().parents[1] / "kestrel/web/static/css/pages/runs.css"


def _hosted_ready():
    async def go(s):
        for k, v in _rows().items():
            s.add(Setting(key=k, value=v))

    _write(go)


def _prices(monkeypatch, *, listed: bool = True):
    from kestrel.web.pages import runs_page

    m = ModelInfo(
        id=_HOSTED_T2,
        prompt_usd_per_m=3.0 if listed else None,
        completion_usd_per_m=15.0 if listed else None,
    )
    monkeypatch.setattr(runs_page, "_price_map", lambda settings: {m.id: m})


def _online_approval(client: TestClient) -> str:
    page = client.get("/runs/start/choose").text
    return html.unescape(
        re.search(r'name="online_approval" value=\'([^\']*)\'', page).group(1)
    )


@pytest.fixture
def idle():
    seed = runs_seed.seed()
    with TestClient(build_app(worker=False)) as c:
        yield c, seed


@pytest.fixture
def active():
    seed = runs_seed.seed(active=True)
    with TestClient(build_app(worker=False)) as c:
        yield c, seed
    set_paused(False)


# ------------------------------------------------------------------------------- Both
def test_both_overlays_only_the_rating_tier_and_keeps_the_quick_screen_local():
    local = load_settings()
    s = load_settings(db_overrides=_nested(_rows()), mode="both")
    assert s.llm.mode == "both" and "both" in RUN_CHOICES
    assert s.llm.tiers["tier2"].backend == "frontier" and s.llm.tiers["tier2"].model == _HOSTED_T2
    assert s.llm.tiers["tier1"] == local.llm.tiers["tier1"] != s.llm.hosted_tiers["tier1"]
    assert s.llm.tiers["tier1"].model != _HOSTED_T1
    assert set(HOSTED_REQUIRED_TIERS) == {"tier1", "tier2"}


def test_both_is_refused_with_the_reason_when_hosted_cannot_run():
    from kestrel.config import HostedModeError

    with pytest.raises(HostedModeError, match="acknowledged"):
        load_settings(mode="both")


def test_the_sheet_offers_both_and_lists_only_the_rating_send(idle):
    c, _ = idle
    html = c.get("/runs/start/choose").text
    assert 'data-testid="rn-mode-both"' in html
    both = html.split('data-sends="both"')[1].split("</ul>")[0]
    assert 'data-testid="rn-sends-both"' in both and "nothing is sent for it" in both
    assert both.count("<li") == 2  # the rating line and the "quick screen stays here" line


def test_a_both_run_needs_the_confirmation_then_queues_the_mode(idle):
    c, _ = idle
    _hosted_ready()
    r = c.post("/runs/start", data={"mode": "both"})
    assert r.status_code == 400 and "Nothing was sent" in r.text
    assert c.post(
        "/runs/start",
        data={"mode": "both", "confirmed": "1", "online_approval": _online_approval(c)},
    ).status_code == 200

    async def pending(s):
        return (await s.get(Setting, PENDING_RUN_MODE_KEY)).value

    assert _read(pending)["mode"] == "both"


def test_an_online_run_is_refused_server_side_when_confidential_has_no_employer(idle):
    c, _ = idle
    _hosted_ready()
    _write(lambda s: s.merge(Setting(key="confidential_mode", value=True)))
    approval = _online_approval(c)
    for mode in ("both", "hosted"):
        r = c.post(
            "/runs/start",
            data={"mode": mode, "confirmed": "1", "online_approval": approval},
        )
        assert r.status_code == 409 and "Nothing was sent" in r.text, mode

    async def n(s):
        return len((await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all())

    assert _read(n) == 0


# --------------------------------------------------------------------- cost and credit
async def _hosted_eval(s, run: Run, *, tokens_in=1000, tokens_out=500, n=1):
    start = datetime.fromisoformat(run.started_at)
    for i in range(n):
        opp = Opportunity(
            track="employment",
            opp_type="job_posting",
            title=f"Online role {i}",
            url=f"https://example.test/online-{run.id}-{i}",
            stage="t2_done",
            org_name="Acme",
        )
        s.add(opp)
        await s.flush()
        s.add(
            Evaluation(
                opportunity_id=opp.id,
                tier=2,
                track="employment",
                model=_HOSTED_T2,
                prompt_tokens=tokens_in,
                completion_tokens=tokens_out,
                created_at=(start + timedelta(minutes=3)).isoformat(),
            )
        )


def test_history_shows_found_and_the_online_cost_from_recorded_tokens(idle, monkeypatch):
    c, seed = idle
    _prices(monkeypatch)
    online_id = seed["runs"][4]

    async def add(s):
        await _hosted_eval(s, await s.get(Run, online_id), n=10)

    _write(add)
    html = c.get("/runs/history?all=1").text
    assert "found</span>" in html and html.count('data-testid="rn-cost"') == 1
    # 10 ratings x (1000 in x $3/M + 500 out x $15/M) = $0.105
    assert "about $0.10" in html or "about $0.11" in html
    drawer = c.get(f"/runs/{online_id}/drawer").text
    assert 'data-testid="rn-dr-cost"' in drawer and "10 ratings" in drawer
    assert ">found<" in drawer.replace("</b>found", ">found<")


def test_an_online_run_with_no_listed_price_says_not_recorded(idle, monkeypatch):
    c, seed = idle
    _prices(monkeypatch, listed=False)

    async def add(s):
        await _hosted_eval(s, await s.get(Run, seed["runs"][4]))

    _write(add)
    html = c.get("/runs/history?all=1").text
    assert re.search(r'data-testid="rn-cost"><b>Not recorded</b>', html)
    assert "$" not in html.split('data-testid="rn-cost"')[1].split("</span>")[0]


def test_the_live_card_of_an_online_run_shows_spend_estimate_and_credit(active, monkeypatch):
    c, seed = active
    _hosted_ready()
    _prices(monkeypatch)

    async def go(s):
        run = await s.get(Run, seed["active_run"])
        run.mode = "both"
        await _hosted_eval(s, run, n=6)

    _write(go)
    from kestrel.services import hosted_balance

    async def left(session, settings):
        return "$4.20 left on your key's limit"

    monkeypatch.setattr(hosted_balance, "credit_left", left)
    html = c.get("/runs").text
    spend = html.split('data-testid="rn-spend"')[1].split("</div>")[0]
    assert "This run so far: <b>about $" in spend and "To finish:" in spend
    assert "$4.20 left on your key" in spend
    assert "Quick screen on this computer" in html
    tiles = html.split('data-bar="quick"')[1].split('data-bar="rating"')[0]
    assert "rn-cloud" not in tiles  # Both: the quick screen is not an online step
    assert "rn-cloud" in html.split('data-bar="rating"')[1].split("</div>")[0]


def test_a_local_run_shows_no_cost(idle):
    c, _ = idle
    html = c.get("/runs/history?all=1").text
    first = html.split('data-testid="rn-run"')[1]
    assert "rn-cost" not in first.split("</button>")[0]


# --------------------------------------------------------------------------- History rows
def test_history_why_lines_are_whole_sentences_not_cut():
    css = _CSS.read_text()
    why = re.search(r"\.rn-run__why \{[^}]*\}", css).group(0)
    assert "ellipsis" not in why and "nowrap" not in why


def test_every_history_sentence_is_short_enough_to_stand_alone(idle):
    from kestrel.db.models import Run as R
    from kestrel.web.pages.runs_page import status_of

    for status in ("failed", "interrupted", "stopping", "running", "done"):
        assert len(status_of(R(status=status, counts={}))["why"]) <= 70


# ------------------------------------------------------------------------------- Stop
def test_a_search_that_finishes_after_stop_is_not_shown_on_the_stopped_run(active):
    c, seed = active
    assert c.post("/runs/stop").status_code == 204

    async def late(s):
        run = await s.get(Run, seed["active_run"])
        stop = datetime.fromisoformat(run.finished_at)
        src = (await s.execute(select(Source).where(Source.name == "Search 2"))).scalar_one()
        # Began before Stop, was still going, wrote its row after: belongs to no run.
        s.add(
            ScrapeRun(
                source_id=src.id,
                status="ok",
                started_at=(stop - timedelta(seconds=5)).isoformat(),
                finished_at=(stop + timedelta(seconds=30)).isoformat(),
                counts={"new": 9},
            )
        )
        # Began after Stop (the next run's search).
        s.add(
            ScrapeRun(
                source_id=src.id,
                status="ok",
                started_at=(stop + timedelta(seconds=40)).isoformat(),
                finished_at=(stop + timedelta(seconds=60)).isoformat(),
                counts={"new": 7},
            )
        )

    _write(late)
    drawer = c.get(f"/runs/{seed['active_run']}/drawer").text
    assert "9 new" not in drawer and "7 new" not in drawer
    assert "5 new" in drawer  # its own searches are still there


def test_a_run_does_not_claim_the_next_runs_searches(idle):
    c, seed = idle

    async def go(s):
        runs = (await s.execute(select(Run).order_by(Run.started_at.desc()))).scalars().all()
        newest = runs[0]
        src = (await s.execute(select(Source))).scalars().first()
        s.add(
            ScrapeRun(
                source_id=src.id,
                status="ok",
                counts={"new": 99},
                started_at=(
                    datetime.fromisoformat(newest.finished_at) + timedelta(hours=1)
                ).isoformat(),
                finished_at=(
                    datetime.fromisoformat(newest.finished_at) + timedelta(hours=1, minutes=1)
                ).isoformat(),
            )
        )
        return newest.id

    rid = _write(go)
    assert "99 new" not in c.get(f"/runs/{rid}/drawer").text


# -------------------------------------------------------------------- Run now after Stop
def test_run_now_after_stop_resumes_and_keeps_the_queued_work(active):
    from kestrel.web.app import is_paused

    c, _ = active
    c.post("/runs/stop")
    assert is_paused()
    assert c.post("/runs/start").status_code == 200
    assert not is_paused()

    async def look(s):
        t1 = (await s.execute(select(Task).where(Task.type == "t1"))).scalars().all()
        scrape = (
            (await s.execute(select(Task).where(Task.type == "scrape", Task.status == "pending")))
            .scalars()
            .all()
        )
        return [t.status for t in t1], len(scrape)

    t1, scrapes = _read(look)
    assert t1 and all(s == "pending" for s in t1)  # the scoring the Stop left is still queued
    assert scrapes >= 3  # and every enabled search is queued again


# --------------------------------------------------------------------------- idle layout
def test_the_idle_minis_stay_three_across_unless_online_adds_a_fourth():
    css = _CSS.read_text()
    narrow = css.split("@container kwin (max-width: 1300px)")[1].split("}\n@container")[0]
    assert ".rn-minis--4" in narrow and ".rn-minis {" not in narrow
