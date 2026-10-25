"""Model check (D-UI-105, D-UI-147): the page and every action that swaps a card in place. The
queued task itself is covered by tests/test_model_check.py; this file is the screen: the states
(too few ratings, ready, running with live progress and Cancel, recommends a change, no changes,
online best), the apply and undo flow, and the refusals in plain words. Cloud-safe: TestClient on
the real app, a throwaway data directory, no model and no network."""

from __future__ import annotations

import asyncio
import html
import json
import re

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from kestrel.bench import check
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting, Task
from kestrel.llm.profiles import Hardware
from kestrel.services import hosted_models as hm
from kestrel.services import setup_state as ss
from tests.model_check_seed import seed_check, seed_labels
from tests.test_hosted_mode import _fresh_app
from tests.test_hosted_overrides import _connect, _mock


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(ss, "detected_hardware",
                        lambda: Hardware(os="linux", gpu_vendor="nvidia", vram_gb=12.0,
                                         ram_gb=32.0))
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: ("0.5.0", []))
    from kestrel.web.pages import bench

    monkeypatch.setattr(bench, "_available_models", lambda settings: ([], None))
    hm.clear_cache()
    yield
    hm.clear_cache()


def _row(key: str):
    async def go():
        async with get_sessionmaker()() as s:
            row = await s.get(Setting, key)
            return None if row is None else row.value

    return asyncio.run(go())


def _tasks() -> list[Task]:
    async def go():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(Task).where(Task.type == check.TASK_TYPE))
                    ).scalars().all()

    return asyncio.run(go())


# ------------------------------------------------------------------------------ the page
def test_model_check_is_a_section_of_models_and_the_route_keeps_its_name():
    client = _fresh_app()
    page = client.get("/bench").text
    assert "<h1>Model check</h1>" in page
    assert 'href="/models">Models</a> ›' in page  # the breadcrumb
    assert "Is your AI still the right fit for you?" in page
    assert "kestrel bench" not in page  # never points at the command line
    assert "—" not in page.split('class="mo-page"')[1]
    assert '<link rel="stylesheet" href="/static/css/kestrel.css">' not in page  # legacy sheet


def test_too_few_ratings_disables_the_check_and_says_how_many():
    client = _fresh_app()
    seed_labels(3)
    page = client.get("/bench").text
    assert 'data-testid="ck-too-few"' in page and "3 of 5 needed" in page
    assert 'disabled data-testid="ck-run"' in page
    assert "Rate a few in Calibrate" in page
    assert 'data-testid="ck-none"' in page


def test_ready_state_shows_the_rated_roles_and_what_the_check_costs(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    seed_labels(9)
    page = client.get("/bench").text
    counter = page.split('data-testid="ck-counter"')[1].split("</div>")[0]
    assert "<b>9</b>" in counter and "roles you rated" in counter and "6 kept" in counter
    assert "3 passed on" in counter
    assert "Also check Deeper read" in page
    assert "uses more credit here than in other steps" in page
    assert "Runs in the background" in page and "About" in page
    assert 'disabled data-testid="ck-run"' not in page
    assert 'data-testid="ck-online"' not in page  # no provider connected: nothing to include


def test_the_online_option_appears_with_a_provider_and_states_what_is_sent():
    client = _fresh_app()
    seed_labels(9)
    with respx.mock as router:
        _connect(client, router)
    page = client.get("/bench").text
    assert 'data-testid="ck-online"' in page
    import html

    from kestrel.config import load_settings
    from kestrel.setup import tier_sends

    sends = tier_sends(load_settings())
    text = html.unescape(page)
    assert "Sends to OpenRouter" in text and "uses your credit" in text
    assert sends["tier1"] in text and sends["tier2"] in text  # the accepted disclosure's words


# ------------------------------------------------------------- queued, live, and cancel
def test_start_queues_one_background_task_and_shows_progress_with_cancel():
    client = _fresh_app()
    seed_labels(6)
    r = client.post("/bench/start", data={})
    assert r.status_code == 200
    assert 'data-testid="ck-live"' in r.text and 'data-testid="ck-cancel"' in r.text
    assert "Waiting to start" in r.text and "picks up after a restart" in r.text
    assert 'hx-trigger="every 2s"' in r.text  # polls in place
    tasks = _tasks()
    assert len(tasks) == 1 and tasks[0].payload["steps"] == ["quick_screen", "rating"]
    again = client.post("/bench/start", data={})
    assert "already running" in again.text and len(_tasks()) == 1


def test_deeper_read_is_added_only_when_ticked():
    client = _fresh_app()
    seed_labels(6)
    client.post("/bench/start", data={"deeper": "on"})
    assert _tasks()[0].payload["steps"] == ["quick_screen", "rating", "deeper_read"]


def test_live_progress_names_the_step_and_a_friendly_model():
    client = _fresh_app()
    seed_labels(6)
    client.post("/bench/start", data={})

    async def progress():
        async with get_sessionmaker()() as s:
            t = (await s.execute(select(Task).where(Task.type == check.TASK_TYPE))).scalar_one()
            t.status = "leased"
            t.payload = {**t.payload, "progress": {"done": 2, "total": 8, "step": "Rating",
                                                  "model": "openai/gpt-4o-mini"}}
            await s.commit()

    asyncio.run(progress())
    panel = client.get("/bench/panel").text
    assert "Checking Rating with gpt-4o-mini" in panel and "2 of 8 done" in panel
    assert 'aria-valuenow="25"' in panel
    assert "openai/gpt-4o-mini" not in panel  # never the raw id


def test_cancel_stops_the_check_in_place_and_the_button_comes_back():
    client = _fresh_app()
    seed_labels(6)
    client.post("/bench/start", data={})
    r = client.post("/bench/cancel")
    assert "The check was cancelled." in r.text
    assert 'data-testid="ck-live"' not in r.text and 'data-testid="ck-run"' in r.text
    assert 'hx-trigger="every 2s"' not in r.text
    assert client.get("/bench/check/status").json()["state"] == "cancelled"
    assert "No check was running." in client.post("/bench/cancel").text


def test_a_failed_check_says_why_and_offers_another_try():
    client = _fresh_app()
    seed_labels(6)
    client.post("/bench/start", data={})

    async def fail():
        async with get_sessionmaker()() as s:
            t = (await s.execute(select(Task).where(Task.type == check.TASK_TYPE))).scalar_one()
            t.status, t.last_error = "dead", "The AI engine is not running."
            await s.commit()

    asyncio.run(fail())
    panel = client.get("/bench/panel").text
    assert 'data-testid="ck-failed"' in panel and "The AI engine is not running." in panel


def test_the_old_run_button_queues_the_task_and_an_online_pick_needs_a_provider(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    seed_labels(6)
    r = client.post("/bench/run", data={"tier": "tier2", "model": "hosted:vendor/x"})
    assert "Connect an online provider first" in r.text and _tasks() == []
    ok = client.post("/bench/run", data={"tier": "tier2"})
    assert 'data-testid="ck-live"' in ok.text and len(_tasks()) == 1


def test_including_online_models_with_no_provider_is_refused_and_queues_nothing(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    seed_labels(6)
    r = client.post("/bench/start", data={"online": "on"})
    assert "Connect an online provider first" in r.text and _tasks() == []


def test_including_online_models_names_them_in_the_task_with_the_ticked_consent():
    client = _fresh_app()
    seed_labels(6)
    with respx.mock as router:
        _connect(client, router)
    with respx.mock as router:
        _mock(router)
        # Ticking without the destinations the card discloses (D-UI-178) is refused and queues
        # nothing; the card that comes back names them, and posting those goes through.
        bare = client.post("/bench/start", data={"online": "on"})
        assert "Confirm the model and online service destinations" in bare.text
        assert 'data-testid="ck-live"' not in bare.text and _tasks() == []
        approval = html.unescape(
            re.search(r"name=\"online_approval\" value='([^']*)'", bare.text).group(1))
        assert "acme/plain-chat-70b" in approval
        r = client.post("/bench/start", data={"online": "on", "online_approval": approval})
    assert 'data-testid="ck-live"' in r.text
    online = _tasks()[0].payload["online"]
    assert online["quick_screen"] and online["rating"]
    assert all(m["backend"] == "frontier" for m in online["rating"])


# -------------------------------------------------------------------------------- results
def test_a_recommended_change_leads_with_the_decision_and_names_both_models():
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=1)
    page = client.get("/bench").text
    assert "Kestrel recommends one change." in page and "Everything else stays as it is." in page
    assert "Rating: switch from" in page and "gemma3:12b" in page
    assert ("Matches the answer key on 6 more roles in every 100, "
            "from 38 roles in the answer key") in page
    assert "A little slower: about 4 minutes more per 100 roles" in page
    assert "Free, runs on your computer" in page
    assert "From your check on Oct 12 · 38 roles in the answer key" in page
    assert "NO CHANGE NEEDED".lower() in page.lower()
    assert "Quick screen: keep" in page and "Nothing tested matched you better." in page
    assert 'data-testid="ck-apply"' in page and "Switch Rating to gemma3:12b" in page
    assert "You can switch back any time on Models." in page


def test_no_changes_is_a_quiet_summary_with_no_buttons():
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=0)
    page = client.get("/bench").text
    assert "No changes recommended." in page
    assert 'data-testid="ck-apply"' not in page
    assert page.count('data-testid="ck-quiet-') == 2


def test_under_five_rated_roles_in_a_check_says_too_few_never_a_figure():
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=0, n=3)
    page = client.get("/bench").text
    assert "Too few rated roles to compare Rating (3 of 5 needed)." in page
    assert "Too few rated roles to check your models yet" in page


def test_see_the_numbers_has_one_table_and_caption_per_step_never_side_by_side():
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=1)
    page = client.get("/bench").text
    numbers = page.split('data-testid="ck-numbers"')[1].split("</details>")[0]
    assert "How often each model agrees with you on keep or skip" in numbers
    assert "How often each model reaches the same verdict you did" in numbers
    assert numbers.count("<table") == 2 and "Not comparable with the other steps." in numbers
    assert "43% of 38" in numbers and "37% of 38" in numbers and "80% of 38" in numbers


def test_earlier_checks_are_listed_with_their_date_in_words():
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=0, at="2026-09-02T10:00:00+00:00")
    seed_check(changes=1, at="2026-10-12T10:00:00+00:00")
    page = client.get("/bench").text
    history = page.split('data-testid="ck-history"')[1]
    assert "<b>Sep 2</b>" in history and "No changes recommended." in history
    assert "Earlier checks" in page and "Oct 12" not in history  # the newest is the result


# -------------------------------------------------------------------------- apply and undo
TWICE = "step=rating&step=rating"  # the same step ticked twice
FORM = {"content-type": "application/x-www-form-urlencoded"}


def _tags(monkeypatch, *extra: str) -> None:
    from kestrel.llm import profiles

    hw = Hardware(os="linux", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0)
    base = profiles.load_catalog().profile(profiles.recommend_profile(hw)[0]).models.base_tags()
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: (
        "0.5.0", [*base, *extra]))


def _create(router, status: int = 200):
    return router.post("http://127.0.0.1:11434/api/create").mock(
        return_value=Response(status, json={"status": "success"}))


def test_a_local_switch_tunes_the_model_first_and_undo_puts_it_back(monkeypatch):
    _tags(monkeypatch, "gemma3:12b")
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=1)
    with respx.mock as router:
        route = _create(router)
        r = client.post("/bench/apply", data={"step": "rating"})
    assert "Switched Rating." in r.text and "switch back here or on Models" in r.text
    assert _row("llm.tiers.tier2.model") == "kestrel-tier2-gemma3-12b"  # never a raw tag
    assert json.loads(route.calls.last.request.content)["from"] == "gemma3:12b"
    page = client.get("/bench").text
    assert "In use now." in page and 'data-testid="ck-undo"' in page
    assert 'data-testid="ck-apply"' not in page
    assert "kestrel-tier2-gemma3" not in page.split('data-testid="ck-result"')[1]
    u = client.post(
        "/bench/undo", data={"step": "rating", "token_rating": _row("models.undo.tier2")["token"]}
    )
    assert "Put Rating back as it was." in u.text
    assert _row("llm.tiers.tier2.model") is None
    assert (
        client.post("/bench/undo", data={"step": "rating"}).text.count(
            "This can no longer be undone."
        )
        == 1
    )


def test_the_same_step_twice_is_applied_once_and_one_undo_restores_it(monkeypatch):
    _tags(monkeypatch, "gemma3:12b")
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=1)
    with respx.mock as router:
        _create(router)
        client.post("/bench/apply", content=TWICE, headers=FORM)
    assert _row("llm.tiers.tier2.model") == "kestrel-tier2-gemma3-12b"
    client.post(
        "/bench/undo",
        content=TWICE + "&token_rating=" + _row("models.undo.tier2")["token"],
        headers=FORM,
    )
    assert _row("llm.tiers.tier2.model") is None


def test_a_model_not_downloaded_is_not_switched_to_and_the_download_starts(monkeypatch):
    _tags(monkeypatch)  # qwen3:14b is a model Kestrel knows, and is not on this computer
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=1, best_model="qwen3:14b")
    r = client.post("/bench/apply", data={"step": "rating"})
    assert "Not switched" in r.text and "qwen3:14b is not downloaded yet" in r.text
    assert _row("llm.tiers.tier2.model") is None


def test_applying_a_step_with_no_recommendation_is_refused():
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=1)
    r = client.post("/bench/apply", data={"step": "quick_screen"})
    assert "the check recommends no change" in r.text
    assert _row("llm.tiers.tier1.model") is None


def test_an_online_switch_states_what_is_sent_and_asks_first():
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=1, online_best=True)
    with respx.mock as router:
        _connect(client, router)
    page = client.get("/bench").text
    assert "Runs online with your key and uses your credit" in page
    import html

    from kestrel.config import load_settings
    from kestrel.setup import tier_sends

    text = html.unescape(page)
    assert f"Switching sends {tier_sends(load_settings())['tier2']} to OpenRouter" in text
    assert 'data-testid="ck-confirm-online"' in page
    assert "The best option on this computer" not in page  # none was tested locally
    with respx.mock as router:
        _mock(router)
        no_tick = client.post("/bench/apply", data={"step": "rating"})
    assert "tick the box to confirm what will be sent first" in no_tick.text


def test_an_online_switch_with_no_connected_provider_is_refused_in_plain_words(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=1, online_best=True)
    r = client.post("/bench/apply", data={"step": "rating", "confirm": "on"})
    assert "no online provider is connected. Set one up under Models first" in r.text


def test_the_best_local_option_is_named_when_an_online_one_wins():
    from tests.model_check_seed import result

    client = _fresh_app()
    seed_labels(9)
    stored = seed_check(changes=1, online_best=True)
    # add a tested local alternative to the stored check
    async def add_local():
        async with get_sessionmaker()() as s:
            from kestrel.bench.check import StepResult, recommend_step

            rows = [StepResult(**{k: v for k, v in r.items() if k != "points"})
                    for r in stored["results"]]
            rows.append(result("rating", "gemma3:12b", 0.45, ms=3000.0))
            verdict = check.asdict(recommend_step("rating", rows, "cur-rate", "local"))
            row = await s.get(Setting, check.HISTORY_KEY)
            fresh = dict(row.value[0])
            fresh["steps"] = [verdict if v["step"] == "rating" else v for v in fresh["steps"]]
            row.value = [fresh]
            await s.commit()

    asyncio.run(add_local())
    page = client.get("/bench").text
    assert "The best option on this computer is gemma3:12b" in page
    assert "You can stay private." in page


# ------------------------------------------------------------------------------ advanced
def test_advanced_is_collapsed_and_keeps_the_pickers_and_the_comparison():
    client = _fresh_app()
    page = client.get("/bench").text
    advanced = page.split('data-testid="ck-advanced"')[1]
    assert "<details" in page.split('data-testid="ck-advanced"')[0][-80:]
    assert 'data-testid="bench-run-model"' in advanced
    assert 'data-testid="bench-promote-model"' in advanced
    assert "Kestrel's instructions to the AI" in advanced and "Current" in advanced
    assert "came with the latest update" in advanced
    assert "v1" not in advanced.split("instructions to the AI")[1].split("</p>")[0]
    assert "Nothing to compare yet" in advanced


def test_promoting_a_local_model_tunes_it_and_confirms_in_plain_words(monkeypatch):
    _tags(monkeypatch, "gemma3:12b")
    client = _fresh_app()
    with respx.mock as router:
        _create(router)
        r = client.post("/bench/promote", data={"tier": "tier2", "model": "gemma3:12b"})
    assert 'data-testid="bench-promoted"' in r.text and "Rating now uses gemma3:12b" in r.text
    assert _row("llm.tiers.tier2.model") == "kestrel-tier2-gemma3-12b"  # never a raw tag
    bad = client.post("/bench/promote", data={"tier": "tier9", "model": "x"})
    assert "Pick a step first." in bad.text
    unknown = client.post("/bench/promote", data={"tier": "tier2", "model": "made-up:1b"})
    assert "does not know that model" in unknown.text


def test_promoting_an_online_model_needs_a_provider_and_the_approval(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    r = client.post("/bench/promote", data={"tier": "tier2", "model": "hosted:vendor/x"})
    assert "Connect an online provider first" in r.text
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    r = client.post("/bench/promote", data={"tier": "tier2", "model": "hosted:vendor/x"})
    assert "Online models need your approval first" in r.text


def test_the_json_routes_still_work_for_the_cli_and_tests():
    client = _fresh_app()
    seed_labels(5)
    assert client.post("/bench/check", json={"steps": ["rating"]}).json()["queued"] is True
    assert client.get("/bench/check/status").json()["live"] is True
    assert client.post("/bench/check/cancel").json() == {"cancelled": 1}


def test_including_online_models_needs_the_operators_approval_first():
    client = _fresh_app()
    seed_labels(6)  # a key is in the environment, but the disclosure was never accepted
    r = client.post("/bench/start", data={"online": "on"})
    assert "Online models need your approval first" in r.text and _tasks() == []


def test_an_online_switch_needs_the_approval_too(monkeypatch):
    client = _fresh_app()
    seed_labels(9)
    seed_check(changes=1, online_best=True)
    r = client.post("/bench/apply", data={"step": "rating", "confirm": "on"})
    assert "Online models need your approval first" in r.text
    assert _row("models.undo.tier2") is None


def test_a_rate_from_under_five_roles_is_never_shown_as_a_rate_in_the_numbers():
    from kestrel.web import bench_view as bv

    stored = {"steps": [{"step": "rating", "n": 38}], "results": [
        {"step": "rating", "model": "a", "online": False, "n": 38, "agreement": 0.5,
         "mean_latency_ms": 1000.0, "first_attempt_valid": 1.0},
        {"step": "rating", "model": "b", "online": False, "n": 3, "agreement": 1.0,
         "mean_latency_ms": 1000.0, "first_attempt_valid": 1.0}]}
    rows = bv.numbers_view(stored, {})[0]["rows"]
    assert [r["few"] for r in rows] == [False, True]


def test_a_tuned_model_reads_as_the_model_it_was_built_from():
    from kestrel.web import bench_view as bv

    verdict = {"step": "rating", "label": "Rating", "n": 38, "enough": True, "change": True,
               "points": 6, "best_local": None,
               "current": {"model": "kestrel-tier2-old-model", "online": False, "points": 40.0,
                           "mean_latency_ms": 1000.0},
               "best": {"model": "gemma3:12b", "online": False, "points": 46.0,
                        "mean_latency_ms": 1000.0, "backend": "local"}}
    bases = {"kestrel-tier2-old-model": "old-model:7b", "tier2": "qwen3:14b"}
    card = bv.step_view(verdict, bases, current_model="kestrel-tier2-old-model")
    assert card["cur_name"] == "old-model:7b" and not card["applied"]
    done = bv.step_view(verdict, {**bases, "kestrel-tier2-gemma3-12b": "gemma3:12b"},
                        current_model="kestrel-tier2-gemma3-12b")
    assert done["applied"] is True
