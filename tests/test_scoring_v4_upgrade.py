"""Scoring Version 4 is the default and not a user setting, and there is no re-approve step
(issue #208; owner rulings D-SCORE-32 and D-SCORE-60).

INVARIANT: nothing goes to an online model without the confirmed consent dialog, including
retries. An acceptance made under Version 3 does not cover what Version 4 sends (lines from the
current resume), so after the upgrade every online send is refused until the person confirms the
new text through the online model's own consent form; there is no banner, no Approve and no other
route that can turn online models back on. A person who chose Local, and an older install whose
online models were switched off, stay local. The prompt version is an internal setting: no page
or route writes it, and only this computer may. Invented text only; nothing reaches the network
(the online service is a mocked route).
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
import respx
import structlog
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.config import bind_disclosure
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import DocMaster, DocSection, DocSlot, DocVariant, Opportunity, Setting
from kestrel.llm.client import ExternalBackendError, LLMClient
from kestrel.pipeline.context import RESUME_DIGEST_HEADER
from kestrel.pipeline.profile import load_profile
from kestrel.pipeline.tier2 import handle_t2
from kestrel.web.app import build_app
from tests.pipeline_fakes import _p2

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

KEY = "scoring.evaluate_version"
RESUME = "lines from your current resume"
_OR_CHAT = "https://openrouter.ai/api/v1/chat/completions"
_BULLET = "Rebuilt the harbor crane rota for three berths."
_PAD = "Run the terminal schedule and the crane rota. " * 12


def _client(**kw) -> TestClient:
    migrate.run_migrations()
    forget_engine()
    return TestClient(build_app(worker=False), **kw)


def _db(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                out = await fn(s)
                await s.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _row(key=KEY):
    async def f(s):
        r = await s.get(Setting, key)
        return None if r is None else r.value

    return _db(f)


def _settings():
    from kestrel.services.setting_overrides import load_settings_with_db

    return _db(load_settings_with_db)


def _v3_terms() -> dict:
    """The disclosure as Version 3 rendered it (what a 0.7.6 Hosted user accepted)."""
    s = _settings().model_copy(deep=True)
    s.scoring.evaluate_version = "v3"
    bind_disclosure(s)
    terms = s.llm.hosted_mode.disclosure_terms()
    assert RESUME not in terms["sends"]["tier2"]
    return terms


def _accept(terms: dict, *, enabled: bool = True, hosted: bool = False) -> None:
    """The rows an acceptance writes; `hosted` adds the Online switch and Tier 1 and Tier 2
    assignments, as Settings and `kestrel setup --hosted` write them."""
    rows = [("llm.hosted_mode.enabled", enabled),
            ("llm.hosted_mode.acknowledged_at", "2026-11-01T00:00:00+00:00"),
            ("llm.hosted_mode.acknowledged_terms", terms)]
    if hosted:
        rows += [("llm.mode", "hosted"),
                 ("llm.hosted_tiers.tier1.backend", "frontier"),
                 ("llm.hosted_tiers.tier1.model", "invented/screen-model"),
                 ("llm.hosted_tiers.tier2.backend", "frontier"),
                 ("llm.hosted_tiers.tier2.model", "invented/scorer-model")]

    async def f(s):
        for k, v in rows:
            await s.merge(Setting(key=k, value=v))

    _db(f)


def _chose(choice: str) -> None:
    """The person's run choice as Settings > This computer stores it (with the mode row
    set_run_mode writes for it)."""
    async def f(s):
        await s.merge(Setting(key="setup.run_choice", value=choice))
        if choice != "hosted":
            await s.merge(Setting(key="llm.mode", value="local"))

    _db(f)


def _online_tier2(settings):
    s = settings.model_copy(deep=True)
    s.llm.tiers["tier2"].backend = "frontier"
    s.llm.tiers["tier2"].model = "invented/scorer-model"
    s.confidential_mode = False
    return s


def _seed_role_and_resume() -> int:
    async def f(s):
        m = DocMaster(track="employment", name="Mine", doc_kind="resume", is_active=True,
                      skill_groups=[{"key": "ops", "label": "Operations",
                                     "items": ["Scheduling"], "default_visible": True}])
        s.add(m)
        await s.flush()
        sec = DocSection(doc_master_id=m.id, section_kind="role", title="Lead",
                         org_label="Gullwing Ports", sort_order=0)
        s.add(sec)
        await s.flush()
        slot = DocSlot(doc_section_id=sec.id, key="r0", sort_order=0, default_variant_key="a")
        s.add(slot)
        await s.flush()
        s.add(DocVariant(doc_slot_id=slot.id, key="a", text=_BULLET))
        o = Opportunity(track="employment", opp_type="job_posting", title="Terminal Lead",
                        org_name="Gullwing Ports", stage="t1_pass", stage_reason="invented",
                        description_md=_PAD, seen_count=1, ghost_score=0.0, version=1)
        s.add(o)
        await s.flush()
        return o.id

    return _db(f)


async def _score(settings, oid: int):
    engine = make_async_engine(paths.db_path())
    try:
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with httpx.AsyncClient() as http:
            return await handle_t2(
                sm, LLMClient(settings, http=http), settings, opportunity_id=oid,
                profiles={t: load_profile(t) for t in ("employment", "consulting")})
    finally:
        await engine.dispose()


# ------------------------------------------------------------- the version is not a setting
def test_v4_is_the_default_on_a_clean_install():
    _client().__enter__().__exit__(None, None, None)
    assert _row() is None
    s = _settings()
    assert s.scoring.evaluate_version == "v4"
    assert RESUME in s.llm.hosted_mode.disclosure_terms()["sends"]["tier2"]


def test_no_page_or_route_offers_the_version_choice():
    with _client(raise_server_exceptions=False) as c:
        assert c.post("/settings/scoring/version", data={"version": "v3"}).status_code in (
            404, 405)
        assert c.get("/settings/scoring/version").status_code in (404, 405)
        pane = c.get("/settings/section/scoring")
        assert pane.status_code == 200
        assert 'data-testid="st-version' not in pane.text
        assert "Scoring instructions" not in pane.text
    assert _row() is None


def test_the_internal_setting_stays_this_computers_and_undo_cannot_write_it():
    from kestrel.web.settings_io import needs_local, plausible

    assert needs_local(KEY, "v3") and needs_local(KEY, "v4")
    assert plausible(KEY, "v3") and not plausible(KEY, "v9")
    _client().__enter__().__exit__(None, None, None)

    async def support_sets_v4(s):  # the row holds exactly what the crafted link names as "now"
        await s.merge(Setting(key=KEY, value="v4"))

    _db(support_sets_v4)
    with _client() as c:
        r = c.post("/settings/undo?key=scoring.evaluate_version&prev=%22v3%22&now=%22v4%22")
    assert _row() == "v4", r.text  # no page writes the version, so Undo never may
    assert _settings().scoring.evaluate_version == "v4"


# ------------------------------------------- after the upgrade, only the consent form sends
@respx.mock
def test_upgrade_with_a_v3_acceptance_sends_nothing_until_the_consent_form_is_confirmed(
        monkeypatch):
    """End to end: a v3 acceptance on record; nothing is sent (no banner asks for anything);
    the person confirms the consent form, and the online v4 send carries exactly what it lists."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(_OR_CHAT).mock(return_value=Response(200, json={
        "choices": [{"message": {"content": _p2(None)}}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 200, "cost": 0.0}}))
    _client().__enter__().__exit__(None, None, None)
    _accept(_v3_terms(), hosted=True)
    oid = _seed_role_and_resume()
    stale = _settings()
    assert not stale.llm.hosted_mode.active
    # Nothing is sent: the stored Online choice resolves to this computer, and the client
    # refuses even a tier routed online directly, naming the consent form that exists.
    assert stale.llm.mode == "local"
    out = asyncio.run(_score(_online_tier2(stale), oid))
    assert out.action == "error" and not route.called, out
    with pytest.raises(ExternalBackendError, match="tick I understand what each hosted tier sends"):
        asyncio.run(LLMClient(_online_tier2(stale)).complete(
            tier="tier2", messages=[{"role": "user", "content": "x"}]))
    assert not route.called
    with _client() as c:  # the confirmed consent form
        c.post("/settings/hosted/mode", data={"action": "on", "ack": "on"})
    settings = _settings()
    hm = settings.llm.hosted_mode
    assert hm.active and settings.llm.mode == "hosted"
    approved = hm.acknowledged_terms["sends"]["tier2"]
    assert approved == hm.disclosure_terms()["sends"]["tier2"] and RESUME in approved
    out = asyncio.run(_score(settings, oid))
    assert out.action == "t2_done", out
    assert route.call_count == 1
    blob = "\n".join(m["content"] for m in json.loads(route.calls[0].request.content)["messages"])
    assert RESUME_DIGEST_HEADER in blob and f"- {_BULLET}" in blob
    assert "Operations: Scheduling" in blob  # the skills group the disclosure names


# Every install shape -> (online before the upgrade, the run mode before). After the upgrade every
# shape is offline and on local models until the consent form is confirmed.
SHAPES = {
    "fresh_install": (False, "local"),
    "legacy_no_run_choice_online_off": (False, "local"),
    "legacy_no_run_choice_online_on": (True, "hosted"),
    "chose_local": (True, "local"),  # online models were left on before choosing Local
    "chose_online": (True, "hosted"),
}


def _seed(shape: str) -> None:
    if shape == "fresh_install":
        return
    if shape == "legacy_no_run_choice_online_off":
        # A 0.7.6 user who accepted the v3 disclosure and then turned online models off: the
        # off switch then wrote no run-choice row (round 12 review, B1).
        _accept(_v3_terms(), enabled=False)

        async def mode(s):
            await s.merge(Setting(key="llm.mode", value="local"))

        _db(mode)
        return
    _accept(_v3_terms(), hosted=True)
    if shape == "chose_local":
        _chose("local")
    elif shape == "chose_online":
        _chose("hosted")


def _v3_view() -> tuple[bool, str]:
    """(online, run mode) as 0.7.6 saw this install: the same rows read under Version 3."""
    async def put(s):
        await s.merge(Setting(key=KEY, value="v3"))

    async def drop(s):
        await s.delete(await s.get(Setting, KEY))

    _db(put)
    try:
        s = _settings()
    finally:
        _db(drop)
    assert _row() is None
    return s.llm.hosted_mode.active, s.llm.mode


def _online_now() -> tuple[object, bool, str]:
    s = _settings()
    return _row("llm.hosted_mode.enabled"), s.llm.hosted_mode.active, s.llm.mode


def _old_digest() -> str:
    """The digest the removed Approve panel posted, so a crafted request is a real attempt."""
    terms = _settings().llm.hosted_mode.disclosure_terms()
    blob = json.dumps(terms, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _plain_tracebacks() -> None:
    """Print a logged exception as plain text (D-TEST-841). structlog's default renderer draws
    a rich traceback with every local variable and syntax colors; a route that raises under the
    blocked network spent about 9 s on that, three times a run, and nothing here reads the log.
    The suite's process-globals fixture puts structlog's configuration back after each test."""
    plain = structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)
    processors = [
        plain if isinstance(p, structlog.dev.ConsoleRenderer) else p
        for p in structlog.get_config()["processors"]
    ]
    structlog.configure(processors=processors)


# Sweeps every write route: 37-44 s alone on the owner's Windows PC against the 60 s default,
# and a timeout under the full gate's -n 8 load stopped the worker there. The sweep's size,
# not a wait, is the cost (profile in the PR notes); a smaller sweep is filed for 0.7.8.
@pytest.mark.timeout(240)
@respx.mock  # any unmocked request fails the test: nothing here may reach the network
@pytest.mark.parametrize("shape", list(SHAPES))
def test_no_route_turns_online_on_without_the_confirmed_dialog(monkeypatch, shape):
    """D-SCORE-60. INVARIANT: nothing goes to an online model without the confirmed dialog,
    including retries. For every install shape: the online state before and after the upgrade,
    no banner or Approve on any page, and every route that can write the online switch, posted
    without the confirmed dialog, leaves it exactly as it was. Then the confirmed dialog (and
    only it) turns online models on."""
    _plain_tracebacks()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")  # a key is saved
    _client().__enter__().__exit__(None, None, None)
    _seed(shape)
    assert _v3_view() == SHAPES[shape], "before the upgrade (Version 3)"
    stored, active, mode = _online_now()
    assert (active, mode) == (False, "local"), "after the upgrade (Version 4)"
    with _client(raise_server_exceptions=False) as c:
        today = c.get("/announcements").text
        models = c.get("/models").text
        for text in (today, models):
            assert "hosted-reapprove" not in text and "/settings/hosted/approve" not in text
            assert "hosted-approve-card" not in text
        attempts = [
            ("the removed Approve", "/settings/hosted/approve",
             {"terms": _old_digest(), "ack": "on"}),
            ("Hosted on, unticked", "/settings/hosted/mode", {"action": "on"}),
            ("Settings connect, unticked", "/settings/hosted/connect", {"provider": "openrouter"}),
            ("setup connect, unticked", "/onboarding/models/connect", {"provider": "openrouter"}),
            ("Models connect, unticked", "/models/online/connect", {}),
            ("free models only", "/settings/computer/free",
             {"free_only": "on", "allow_training": "on"}),
            ("choose Online", "/settings/computer/mode", {"mode": "hosted"}),
            ("switch to hosted", "/settings/hosted/switch", {"mode": "hosted"}),
        ]
        for name, url, data in attempts:
            c.post(url, data=data)
            now = _online_now()
            assert now[0] == stored and now[1] is False and now[2] == "local", (name, now)
        assert c.post("/settings/hosted/approve", data={"ack": "on"}).status_code in (404, 405)
        # The confirmed consent dialog is the one path that turns online models on.
        c.post("/settings/hosted/mode", data={"action": "on", "ack": "on"})
    assert _online_now()[:2] == (True, True)


def test_every_refusal_names_the_consent_form_that_exists(monkeypatch):
    """Every message that asks for the confirmation names one place (config.ONLINE_CONSENT_HOW),
    and that place has the control: Help's Replay the walkthrough, then the Models step's
    I understand what gets sent and Connect (D-UI-129)."""
    from kestrel.bench.check import NO_APPROVAL_MESSAGE
    from kestrel.config import ONLINE_CONSENT_HOW, hosted_mode_problem
    from kestrel.web.pages.models_ui import online_problem

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _client().__enter__().__exit__(None, None, None)
    _accept(_v3_terms(), hosted=True)
    _chose("hosted")
    settings = _settings()
    assert online_problem(settings) == NO_APPROVAL_MESSAGE
    assert ONLINE_CONSENT_HOW in NO_APPROVAL_MESSAGE
    assert ONLINE_CONSENT_HOW in hosted_mode_problem(settings)
    with _client() as c:
        help_page = c.get("/help").text
        assert 'data-testid="hp-walkthrough"' in help_page and 'href="/onboarding"' in help_page
        step = c.get("/onboarding").text
        for _ in range(len(SHAPES)):  # Next through the steps before Models, as a person would
            if 'data-testid="ob-ack"' in step:
                break
            c.post("/onboarding/next", data={})
            step = c.get("/onboarding").text
    assert 'data-testid="ob-ack"' in step and "I understand what gets sent" in step
    assert 'hx-post="/onboarding/models/connect"' in step


def test_proof_points_cancel_answers_with_nothing_to_draw():
    """Cancel (htmx, no inline handler; the Chromium test in test_proof_points_browser.py is the
    CSP guard) asks the server only for an empty reply, so its panel is deleted."""
    with _client() as c:
        r = c.get("/proof-points/cancel")
    assert r.status_code == 200 and r.text == ""
