"""Settings routes (D-UI-149): every save validates, writes only what it names, and its Undo
puts back exactly that change and refuses once something else changed it. Cloud-safe: TestClient
on the real app, a throwaway data directory, fictional data, no model and no network."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine
from kestrel.db.models import (
    Calibration,
    Contact,
    Label,
    Opportunity,
    Org,
    Pursuit,
    Setting,
)
from kestrel.web.app import build_app

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

HX = {"HX-Request": "true"}


def _client() -> TestClient:
    migrate.run_migrations()
    forget_engine()
    return TestClient(build_app(worker=False))


def _db(fn, commit=False):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                out = await fn(s)
                if commit:
                    await s.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _get(model, pk):
    async def f(s):
        return await s.get(model, pk)

    return _db(f)


def _all(stmt):
    async def f(s):
        return list((await s.execute(stmt)).all())

    return _db(f)


def _row(key):
    async def f(s):
        row = await s.get(Setting, key)
        return None if row is None else row.value

    return _db(f)


def _toast(resp) -> dict:
    return json.loads(resp.headers["HX-Trigger"])["k-toast"]


def _undo(c, url):
    return c.post(url).json()


# ----------------------------------------------------------------------------- ui keys
def test_a_ui_key_saves_validates_and_undoes():
    with _client() as c:
        r = c.post("/settings/ui", json={"key": "ui.text_size", "value": "large"}).json()
        assert r["ok"] and r["message"] == "Text size set to large." and "undoUrl" in r
        assert _row("ui.text_size") == "large"
        bad = c.post("/settings/ui", json={"key": "ui.text_size", "value": "huge"}).json()
        assert not bad["ok"] and "must be one of" in bad["message"]
        assert _row("ui.text_size") == "large"  # a refusal writes nothing
        unknown = c.post("/settings/ui", json={"key": "confidential_mode", "value": True}).json()
        assert not unknown["ok"]
        assert _undo(c, r["undoUrl"])["ok"] and _row("ui.text_size") is None


def test_undo_refuses_after_a_later_change():
    with _client() as c:
        first = c.post("/settings/ui", json={"key": "ui.text_size", "value": "large"}).json()
        c.post("/settings/ui", json={"key": "ui.text_size", "value": "small"})
        out = _undo(c, first["undoUrl"])
        assert not out["ok"] and out["message"] == "This can no longer be undone."
        assert _row("ui.text_size") == "small"  # the later change is never overwritten


def test_region_and_scene_keys_and_the_preview():
    with _client() as c:
        r = c.post("/settings/ui", json={"key": "region.date_format", "value": "YYYY-MM-DD"}).json()
        assert "2026-10-23" in r["preview"]
        assert not c.post("/settings/ui", json={"key": "ui.scene", "value": "moon"}).json()["ok"]
        assert not c.post("/settings/ui", json={"key": "ui.scene_rotation",
                                                "value": []}).json()["ok"]  # one stays checked
        ok = c.post("/settings/ui", json={"key": "ui.touches_off", "value": ["spring"]}).json()
        assert ok["ok"] and _row("ui.touches_off") == ["spring"]
        q = c.post("/settings/ui", json={"key": "notify.quiet_hours", "value": {
            "enabled": True, "start": "21:00", "end": "07:00"}}).json()
        assert q["message"] == "Quiet hours are on."
        assert not c.post("/settings/ui", json={"key": "notify.quiet_hours", "value": {
            "enabled": True, "start": "9pm", "end": "07:00"}}).json()["ok"]


def test_scheme_refuses_unknown_schemes_and_tracks_keep_one():
    with _client() as c:
        assert c.post("/settings/prefs/scheme", json={"scheme": "teal"}).json()["ok"]
        assert _row("ui.scheme") == "teal"
        assert not c.post("/settings/prefs/scheme", json={"scheme": "mauve"}).json()["ok"]
        none = c.post("/settings/prefs/tracks", json={"employment": False, "consulting": False})
        assert none.json()["message"] == "Keep at least one on."
        both = c.post("/settings/prefs/tracks", json={"employment": True, "consulting": True})
        assert both.json()["ok"] and both.json()["reload"] is True
        back = _undo(c, both.json()["undoUrl"])
        assert back["ok"] and _row("tracks.enabled") == ["employment"]


def test_notification_groups_write_only_their_kinds_and_undo():
    with _client() as c:
        r = c.post("/settings/prefs/notify", json={"group": "followups", "on": False}).json()
        assert r["message"] == "No more alerts for follow-ups."
        assert _row("notify.events.next_action_due") is False
        assert _row("notify.events.high_score") is None
        assert _undo(c, r["undoUrl"])["ok"] and _row("notify.events.next_action_due") is None
        bogus = c.post("/settings/prefs/notify", json={"group": "bogus", "on": True}).json()
        assert not bogus["ok"]


# --------------------------------------------------------------------------------- roles
def test_switching_a_rule_off_is_stored_skipped_by_tier0_and_queues_a_recheck():
    with _client() as c:
        r = c.post("/settings/roles/rule", data={"track": "employment", "key": "visa"})
        assert r.status_code == 200
        assert _row("tier0.rules_off.employment") == ["sponsorship"]
        t = _toast(r)
        assert t["message"] == "Needs visa sponsorship is off. Those roles will show again."
        assert "undoUrl" in t and t["refresh"].startswith("/settings/roles/rules")
        assert c.get("/settings/roles/rules").text.count('checked') >= 6
        on = c.post("/settings/roles/rule", data={"track": "employment", "key": "visa", "on": "1"})
        assert _row("tier0.rules_off.employment") is None
        assert "is on." in _toast(on)["message"]
        assert c.post("/settings/roles/rule", data={"track": "x", "key": "visa"}).status_code == 404


def test_tier0_skips_a_switched_off_rule(monkeypatch):
    from types import SimpleNamespace

    from kestrel.config import get_settings
    from kestrel.pipeline.tier0 import evaluate_tier0

    base = get_settings()
    filters = {"employment": {"hard_blockers": [
        {"id": "demo", "when": "True", "reason": "demo"}], "soft_signals": []}}
    opp = SimpleNamespace(title="x", location_raw=None)
    args = dict(opp=opp, org=None, profile=None, source=None, filters=filters, track="employment")
    assert evaluate_tier0(settings=base, **args).passed is False
    off = base.model_copy(update={"tier0": base.tier0.model_copy(
        update={"rules_off": {"employment": ["demo"]}})})
    assert evaluate_tier0(settings=off, **args).passed is True


def test_age_and_weeks_numbers_are_validated_and_undo():
    with _client() as c:
        bad = c.post("/settings/roles/number", data={"track": "employment", "key": "age",
                                                     "value": "400"})
        assert "Use a whole number from 1 to 365. Kestrel is still using 45." in bad.text
        assert _row("max_posting_age_days") is None
        ok = c.post("/settings/roles/number", data={"track": "employment", "key": "age",
                                                    "value": "30"})
        assert _row("max_posting_age_days") == 30
        assert _toast(ok)["message"] == "Roles older than 30 days are hidden."
        assert _undo(c, _toast(ok)["undoUrl"])["ok"] and _row("max_posting_age_days") is None
        con = c.post("/settings/roles/number", data={"track": "consulting", "key": "age",
                                                     "value": "90"})
        assert _row("consulting.max_trigger_age_days") == 90 and con.status_code == 200
        wk = c.post("/settings/roles/number", data={"track": "consulting", "key": "short",
                                                    "value": "53"})
        assert "from 1 to 52" in wk.text


def test_cutoffs_validate_restore_and_undo():
    with _client() as c:
        r = c.post("/settings/roles/cutoffs", data={"which": "strong", "value": "4.5"})
        assert _row("thresholds.apply") == 4.5 and "Strong match now starts at 4.5." in \
            _toast(r)["message"]
        for value, text in (("3.04", "steps of 0.1"), ("6", "0.0 to 5.0"), ("abc", "number")):
            bad = c.post("/settings/roles/cutoffs", data={"which": "look", "value": value})
            assert text in bad.text and _row("thresholds.consider") is None
        low = c.post("/settings/roles/cutoffs", data={"which": "strong", "value": "2.0"})
        assert "Worth a look must be lower than Strong match." in low.text
        assert _row("thresholds.apply") == 4.5
        stepped = c.post("/settings/roles/cutoffs", data={"which": "look", "delta": "1"})
        assert _row("thresholds.consider") == 3.1 and stepped.status_code == 200
        undo = _undo(c, _toast(stepped)["undoUrl"])
        assert undo["ok"] and _row("thresholds.consider") is None
        rest = c.post("/settings/roles/cutoffs", data={"restore": "1"})
        assert _row("thresholds.apply") is None and "4.0 and 3.0" in _toast(rest)["message"]
        assert _undo(c, _toast(rest)["undoUrl"])["ok"] and _row("thresholds.apply") == 4.5


def test_hidden_companies_list_unhide_and_undo():
    async def seed(s):
        org = Org(name="Alpha Works", normalized_name="alpha works")
        s.add(org)
        await s.flush()
        s.add(Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                          title="A one", url="https://x/1", stage="t2_done"))
        await s.flush()
        from kestrel.services import hidden_orgs

        await hidden_orgs.hide_org(s, org.id)
        return org.id

    with _client() as c:
        oid = _db(seed, commit=True)
        assert "Alpha Works" in c.get("/settings/roles/hidden").text
        r = c.post(f"/settings/roles/unhide/{oid}")
        assert "Alpha Works" not in r.text and "You have not hidden any companies" in r.text
        undo = _toast(r)["undoUrl"]
        assert undo.startswith(f"/settings/roles/unhide/{oid}/undo?token=")
        u = c.post(undo)  # JSON, as settings.js reads it; the company is hidden again
        assert u.json()["ok"] is True
        assert "Alpha Works" in c.get("/settings/roles/hidden").text
        again = c.post(undo)  # no longer undoable: it is already hidden
        assert again.json()["ok"] is False
        assert again.json()["message"] == "This can no longer be undone."
        assert c.post("/settings/roles/unhide/9999").status_code == 404


def test_show_it_anyway_and_its_undo_touch_only_that_role():
    async def seed(s):
        o = Opportunity(track="employment", opp_type="job_posting", title="Planner",
                        url="https://x/9", stage="t0_reject", stage_reason="Excluded title")
        s.add(o)
        await s.flush()
        return o.id

    with _client() as c:
        oid = _db(seed, commit=True)
        r = c.post(f"/settings/roles/show/{oid}")
        assert "Shown" in r.text
        t = _toast(r)
        assert t["message"].endswith("will show in your Pipeline.")
        o = _get(Opportunity, oid)
        assert o.stage == "new" and _row(f"tier0_override.{oid}")["reason"] == "Excluded title"
        assert _undo(c, t["undoUrl"])["ok"]
        o = _get(Opportunity, oid)
        assert o.stage == "t0_reject" and o.stage_reason == "Excluded title"
        assert _row(f"tier0_override.{oid}") is None
        assert not _undo(c, t["undoUrl"])["ok"]  # used up: the role is no longer as it left it


# ------------------------------------------------------------------------------- scoring
def test_weight_suggestion_accept_undo_and_restore():
    async def seed(s):
        s.add(Calibration(track="employment", n_samples=42,
                          previous_weights={"role_fit": 0.30, "domain_match": 0.20,
                                            "company_context": 0.20, "comp_logistics": 0.15,
                                            "trajectory": 0.15},
                          proposed_weights={"role_fit": 0.35, "domain_match": 0.20,
                                            "company_context": 0.15, "comp_logistics": 0.15,
                                            "trajectory": 0.15},
                          cv_mae_previous=0.82, cv_mae_proposed=0.61, status="proposed"))
        await s.flush()
        return 1

    with _client() as c:
        _db(seed, commit=True)
        weights = c.get("/settings/scoring/weights").text
        assert "Based on 42 of your ratings" in weights and 'data-testid="st-sug-accept"' in weights
        assert "<input" not in weights.split('id="st-w"')[1].split("</section>")[0]  # no sliders
        r = c.post("/settings/scoring/suggestion/1/accept")
        assert _toast(r)["message"] == "Weights updated. Scores use them from your next run."
        assert _row("scoring.weights.employment.role_fit") == 0.35
        assert "35%" in r.text and "Kestrel suggests a change" not in r.text
        undo = _undo(c, _toast(r)["undoUrl"])
        assert undo["ok"] and _row("scoring.weights.employment.role_fit") is None
        again = c.post("/settings/scoring/suggestion/1/accept")
        rest = c.post("/settings/scoring/weights/restore", data={"track": "employment"})
        assert _toast(rest)["message"] == "Original weights restored."
        assert _row("scoring.weights.employment.role_fit") is None
        assert _undo(c, _toast(rest)["undoUrl"])["ok"]
        assert _row("scoring.weights.employment.role_fit") == 0.35 and again.status_code == 200
        nothing = c.post("/settings/scoring/weights/restore", data={"track": "consulting"})
        assert "already using the original weights" in _toast(nothing)["message"]


def test_a_suggestion_that_does_not_beat_current_scoring_is_never_offered():
    async def seed(s):
        s.add(Calibration(track="employment", n_samples=42, previous_weights={"role_fit": 0.3},
                          proposed_weights={"role_fit": 0.4}, cv_mae_previous=0.5,
                          cv_mae_proposed=0.7, status="proposed"))

    with _client() as c:
        _db(seed, commit=True)
        assert "Kestrel suggests a change" not in c.get("/settings/scoring/weights").text


def test_learning_switch_agreement_and_small_numbers():
    with _client() as c:
        r = c.post("/settings/scoring/learn", data={"key": "use_examples", "track": "employment"})
        assert _row("learning.use_examples") is False
        assert _toast(r)["message"] == "Kestrel will stop using your ratings as examples."
        assert "Too few to tell (0)" in r.text  # hard rule 6: no rate from fewer than five
        assert c.post("/settings/scoring/learn", data={"key": "nope"}).status_code == 404


def test_forget_needs_reset_and_keeps_roles(monkeypatch):
    async def seed(s):
        o = Opportunity(track="employment", opp_type="job_posting", title="Keep me",
                        url="https://x/5", stage="t2_done")
        s.add(o)
        await s.flush()
        s.add(Label(opportunity_id=o.id, track="employment", verdict="apply", score=4.0))
        return o.id

    with _client() as c:
        oid = _db(seed, commit=True)
        no = c.post("/settings/scoring/forget", data={"ratings": "on", "confirm": "reset"})
        assert "Type RESET" in no.text
        assert _all(select(Label.id))
        none = c.post("/settings/scoring/forget", data={"confirm": "RESET"})
        assert "Tick at least one" in none.text
        done = c.post("/settings/scoring/forget", data={"ratings": "on", "confirm": "RESET"})
        assert "Forgot your ratings. Roles, scores, applications and documents are kept." in \
            done.text
        assert not _all(select(Label.id))
        assert _get(Opportunity, oid) is not None


def test_export_is_redacted_under_confidential_mode():
    async def seed(s):
        org = Org(name="Northwind Freight", normalized_name="northwind freight")
        s.add(org)
        await s.flush()
        o = Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                        org_name="Northwind Freight", title="Lead", url="https://x/6",
                        stage="t2_done")
        s.add(o)
        await s.flush()
        s.add(Label(opportunity_id=o.id, track="employment", verdict="apply", score=4.0,
                    rationale="Northwind Freight is a fit"))

    with _client() as c:
        _db(seed, commit=True)
        body = c.get("/settings/scoring/export")
        assert body.headers["content-disposition"].startswith(
            'attachment; filename="kestrel-ratings-')
        data = body.json()
        assert data["n"] == 1 and data["redacted"] is False
        assert "Northwind Freight" in body.text


# -------------------------------------------------------------------------------- privacy
def test_confidential_card_saves_labels_and_the_mcp_actions_are_all_listed():
    from kestrel.mcp.server import TOOL_NAMES
    from kestrel.web.settings_view import MCP_ACTIONS

    assert {a.name for a in MCP_ACTIONS} == set(TOOL_NAMES)
    assert all(a.does and "—" not in a.does for a in MCP_ACTIONS)
    with _client() as c:
        r = c.post("/settings/privacy/confidential", data={"conf_mode": "on", "changed": "mode"})
        assert _row("confidential_mode") is True
        assert _toast(r)["message"] == "Confidential mode is on."
        c.post("/settings/mcp", data={"enabled": "1"})
        card = c.get("/settings/privacy/mcp").text
        for name in TOOL_NAMES:
            assert name in card
        assert "Apply for you" in card and "Send email" in card and "Message anyone" in card


def test_mcp_port_is_validated():
    with _client() as c:
        bad = c.post("/settings/privacy/mcp/port", data={"port": "80"})
        assert "Use a number from 1024 to 65535." in bad.text and _row("mcp.port") is None
        ok = c.post("/settings/privacy/mcp/port", data={"port": "9200"})
        assert _row("mcp.port") == 9200 and "Port changed to 9200." in ok.text


def test_access_needs_the_confirm_and_only_this_computer(monkeypatch):
    from kestrel import paths as p
    from kestrel.web import auth

    monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    p.ensure_dirs()
    (p.config_dir() / "settings.yml").write_text(
        (p.packaged_config() / "settings.yml").read_text(encoding="utf-8"), encoding="utf-8")
    with _client() as c:
        assert "Confirm that you understand first" in c.post(
            "/settings/privacy/access", data={"on": "1"}).text
        on = c.post("/settings/privacy/access", data={"on": "1", "ack": "on"})
        assert "Takes effect after Kestrel restarts." in on.text and "lan-qr" in on.text
        assert "The code includes a private key" in on.text
        off = c.post("/settings/privacy/access", data={"on": "0"})
        assert "Off. Only this computer" in off.text and "lan-qr" not in off.text
        c.post("/settings/privacy/access", data={"on": "1", "ack": "on"})
        rotated = c.post("/settings/privacy/access/rotate")
        assert "New key made" in rotated.text


# ------------------------------------------------------------------------------ computer
def test_run_mode_choice_undo_and_the_key_is_never_echoed(monkeypatch):
    from kestrel.llm import providers as prov

    saved = {}
    monkeypatch.setattr(prov, "write_secret", lambda env, value: saved.update({env: value}))
    with _client() as c:
        r = c.post("/settings/computer/mode", data={"mode": "both"})
        assert _row("setup.run_choice") == "both"
        assert _toast(r)["message"] == "Kestrel will use both and ask before going online."
        assert _undo(c, _toast(r)["undoUrl"])["ok"] and _row("setup.run_choice") is None
        assert c.post("/settings/computer/mode", data={"mode": "x"}).status_code == 400
        k = c.post("/settings/computer/key", data={"provider": "openrouter", "op": "save",
                                                   "api_key": "sk-or-secret-1234"})
        assert "sk-or-secret-1234" not in k.text and saved["OPENROUTER_API_KEY"]
        assert "key saved" in k.text
        miss = c.post("/settings/computer/key", data={"provider": "nope", "op": "test"})
        assert "Choose a service first." in miss.text


def test_checklist_shows_four_steps_and_plain_words():
    with _client() as c:
        card = c.get("/settings/computer/checklist").text
        for step in ("engine", "download", "tune", "check"):
            assert f'data-testid="st-step-{step}"' in card
        assert "cpu_8gb" not in card.split("Show details")[0]  # raw names stay behind details
        assert c.post("/settings/computer/action/nope").status_code == 404


def test_everyday_options_save_and_undo():
    with _client() as c:
        r = c.post("/settings/computer/option", json={"key": "tray", "on": False}).json()
        assert r["message"] == "Kestrel will quit when you close the window."
        assert _row("ui.close_to_tray") is False and _undo(c, r["undoUrl"])["ok"]
        u = c.post("/settings/computer/option", json={"key": "updates", "on": False}).json()
        assert _row("updates.enabled") is False and u["ok"]
        assert not c.post("/settings/computer/option", json={"key": "bogus"}).json()["ok"]


def test_autostart_writes_and_removes_the_entry_for_this_platform(tmp_path):
    from kestrel.services import autostart

    assert autostart.set_enabled(True, home=tmp_path, parts=["/opt/k k/Kestrel", "--tray"]) is None
    if autostart.platform_name() != "windows":
        assert autostart.is_enabled(home=tmp_path)
        text = next(p for p in tmp_path.rglob("*") if p.is_file()).read_text(encoding="utf-8")
        assert "--tray" in text and "Kestrel" in text
        assert autostart.set_enabled(False, home=tmp_path) is None
        assert not autostart.is_enabled(home=tmp_path)


def test_tray_flag_is_a_launch_option_not_a_command():
    from kestrel.desktop.__main__ import cli_args

    assert cli_args(["Kestrel", "--tray"]) == [] and cli_args(["Kestrel", "doctor"]) == ["doctor"]


# --------------------------------------------------------------------------------- data
def _csv(text: str, name="apps.csv"):
    return {"file": (name, text.encode(), "text/csv")}


def test_import_wizard_maps_reviews_adds_and_undo_removes_only_those_rows():
    sample = ("Company,Job title,Applied on,Status\n"
              "Birchwood Freight,Operations Analyst,2026-09-04,No response\n"
              "Northgate Supply,Planner,2026-08-29,Interview\n")
    with _client() as c:
        step1 = c.post("/settings/data/applied/read", files=_csv(sample)).text
        assert 'data-step="1"' in step1 and "2 rows found" in step1
        assert 'data-testid="st-map-0"' in step1
        import re

        text = re.search(r'name="text" value="([^"]*)"', step1).group(1)
        import html

        text = html.unescape(text)
        maps = {f"map_{i}": v for i, v in enumerate(("company", "title", "date", "status"))}
        step2 = c.post("/settings/data/applied/review",
                       data={"text": text, "file_name": "apps.csv", **maps}).text
        assert 'data-step="2"' in step2 and "Nothing is added until you press Add." in step2
        assert not _all(select(Pursuit.id))  # nothing stored yet
        text2 = html.unescape(re.search(r'name="text" value="([^"]*)"', step2).group(1))
        add = c.post("/settings/data/applied/add", data={
            "text": text2, "file_name": "apps.csv", "include_1": "on", "include_2": "on"})
        assert 'data-step="3"' in add.text and "2 applications added" in add.text
        t = _toast(add)
        assert len(_all(select(Pursuit.id))) == 2
        out = _undo(c, t["undoUrl"])
        assert out["ok"] and "Removed the 2 applications" in out["message"]
        assert not _all(select(Pursuit.id))
        assert not _undo(c, t["undoUrl"])["ok"]  # used up


def test_import_wizard_refuses_missing_required_columns_and_reads_a_spreadsheet():
    from kestrel.services import xlsx_text

    with _client() as c:
        bad = c.post("/settings/data/applied/read", files=_csv("Name,Thing\nA,B\n")).text
        assert 'data-step="1"' in bad
        text = __import__("html").unescape(
            __import__("re").search(r'name="text" value="([^"]*)"', bad).group(1))
        res = c.post("/settings/data/applied/review", data={
            "text": text, "map_0": "company", "map_1": ""}).text
        assert "Match at least Company, Role, Date applied and Stage" in res
        sample = c.get("/settings/data/applied/sample")
        assert sample.status_code == 200 and "Company,Job title" in sample.text
        assert not xlsx_text.looks_like_xlsx(b"a,b\n1,2")
        with pytest.raises(xlsx_text.SpreadsheetError):
            xlsx_text.to_csv(b"PK\x03\x04 not really a zip")


def _xlsx(rows: list[list[str]]) -> bytes:
    shared = [c for r in rows for c in r]
    sst = "".join(f"<si><t>{s}</t></si>" for s in shared)
    sheet_rows = []
    i = 0
    for ri, r in enumerate(rows, 1):
        cells = ""
        for ci, _ in enumerate(r):
            cells += f'<c r="{chr(65 + ci)}{ri}" t="s"><v>{i}</v></c>'
            i += 1
        sheet_rows.append(f'<row r="{ri}">{cells}</row>')
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/sharedStrings.xml",
                   f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{sst}</sst>')
    return buf.getvalue()


def test_xlsx_first_sheet_becomes_csv():
    from kestrel.services import xlsx_text

    out = xlsx_text.to_csv(_xlsx([["Company", "Role"], ["Acme", "Planner"]]))
    assert out.splitlines() == ["Company,Role", "Acme,Planner"]


CONNECTIONS = ("Notes:\n\"x\"\n\nFirst Name,Last Name,URL,Email Address,Company,Position,"
               "Connected On\nSam,Ortiz,u,sam@example.com,Larkspur Logistics,Ops,12 Aug 2024\n"
               "Priya,Nair,u,,Northgate Supply,Planner,03 Mar 2023\n"
               "Tom,Alvarez,u,,,Consultant,14 Feb 2020\n")


def test_contacts_read_apply_remove_and_the_exact_line_and_no_email():
    async def seed(s):
        s.add(Org(name="Northgate Supply", normalized_name="northgate supply"))

    with _client() as c:
        _db(seed, commit=True)
        review = c.post("/settings/data/contacts/read", files=_csv(CONNECTIONS, "c.csv")).text
        assert "Email addresses in the file are not kept." in review
        assert "3 people" in review and "First import, so there is nothing to compare" in review
        assert "sam@example.com" not in review
        assert "Nothing is added until you press Apply." in review
        assert not _all(select(Contact.id))  # nothing stored yet
        import html
        import re

        payload = html.unescape(re.search(r'name="payload" value="([^"]*)"', review).group(1))
        assert "example.com" not in payload
        done = c.post("/settings/data/contacts/apply", data={"payload": payload,
                                                             "file_name": "c.csv"})
        assert 'data-testid="st-ct-done"' in done.text
        assert ("Because you imported these connections from LinkedIn, their details may not be "
                "public. Kestrel keeps them on this computer only and never shares them.") in \
            " ".join(done.text.split())
        assert "3 contacts, imported" in done.text
        stored = _all(select(Contact.name, Contact.email))
        assert len(stored) == 3 and all(e is None for _n, e in stored)
        # A wrote-notes contact is kept by Remove all, and the card says how many.
        gone = c.post("/settings/data/contacts/remove")
        assert "Removed 3 contacts." in _toast(gone)["message"]
        assert not _all(select(Contact.id))
        bad = c.post("/settings/data/contacts/read", files=_csv("nothing useful", "x.csv"))
        assert "does not look like a LinkedIn connections export" in bad.text
        sample = c.post("/settings/data/contacts/read", data={"sample": "1"})
        assert "Email addresses in the file are not kept." in sample.text


def test_backup_download_records_last_backup_and_restore_message(monkeypatch):
    with _client() as c:
        r = c.get("/settings/data/backup")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert "kestrel-backup-" in r.headers["content-disposition"]
        assert zipfile.is_zipfile(io.BytesIO(r.content))
        assert _row("backup.last_at")
        assert "Last backup: just now" in c.get("/settings/section/data").text
        bad = c.post("/settings/restore", data={"from_settings": "1"},
                     files={"backup_file": ("x.zip", b"not a zip", "application/zip")})
        assert "warnbox" in bad.text  # a refused file says so; nothing was replaced


def test_data_section_carries_diagnostics_danger_zone_and_the_anchor():
    with _client() as c:
        page = c.get("/settings").text
        assert 'id="your-data"' in page
        assert 'data-testid="st-diag"' in page and "Download a diagnostics file" in page
        assert 'data-testid="st-restart"' in page and 'data-testid="st-clear-reset"' in page
        assert 'action="/onboarding/restart"' in page
        about = c.get("/about").text
        assert "Diagnostics live in Settings &gt; Your data." in about
        assert "from About" not in about


def test_legacy_hashes_are_mapped_for_old_links():
    with _client() as c:
        js = c.get("/static/js/settings.js").text
    for old in ("system", "hosted", "applied", "appearance", "learning", "lan", "your-data"):
        assert old in js


def test_page_uses_no_legacy_stylesheet_and_no_em_dash():
    with _client() as c:
        page = c.get("/settings").text
        parts = [page] + [c.get(u).text for u in (
            "/settings/section/roles", "/settings/section/scoring", "/settings/section/data",
            "/settings/section/preferences", "/settings/section/privacy",
            "/settings/section/computer")]
    head = page.split("</head>")[0]
    assert "/static/css/shell.css" in head and "/static/css/kestrel.css" not in head
    assert 'href="/static/css/pages/settings.css"' in page
    assert all("—" not in x for x in parts)


def test_rule_counts_state_how_few_roles_they_come_from():
    with _client() as c:
        html = c.get("/settings/roles/rules").text
        assert "Counts come from only 0 roles so far, too few to tell much." in html


def test_ratings_export_refuses_when_confidential_mode_would_hide_nothing():
    with _client() as c:
        c.post('/settings/privacy/confidential', data={'conf_mode': 'on', 'changed': 'mode'})
        for url in ("/settings/scoring/export", "/settings/learning/export"):
            r = c.get(url)
            assert r.status_code == 409 and "no employer is set" in r.text, url


def test_autostart_test_never_needs_the_registry(tmp_path, monkeypatch):
    from kestrel.services import autostart

    monkeypatch.setattr(autostart, "platform_name", lambda: "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert autostart.set_enabled(True, home=tmp_path, parts=["/opt/k k/Kestrel", "--tray"]) is None
    assert autostart.is_enabled(home=tmp_path)
    assert autostart.set_enabled(False, home=tmp_path) is None
    assert not autostart.is_enabled(home=tmp_path)


def test_a_quiet_tray_start_does_not_raise_the_running_window(monkeypatch):
    from kestrel.desktop import shell

    calls = []
    monkeypatch.setattr(shell, "_acquire_windows_mutex", lambda: False)
    monkeypatch.setattr(shell, "_acquire_lock_file", lambda: False)
    monkeypatch.setattr(shell, "focus_running_instance", lambda: calls.append(1))
    monkeypatch.setattr(shell.sys, "argv", ["Kestrel", "--tray"])
    assert shell.acquire_single_instance() is False and calls == []
    monkeypatch.setattr(shell.sys, "argv", ["Kestrel"])
    assert shell.acquire_single_instance() is False and calls == [1]
