"""Work mode (Employment and Consulting) and the Location tab answer one question, remote or
hybrid (D-UI-214). `work_modes` is the source of truth; the Location boxes follow it, and a save
on either side leaves the two agreeing. Offline, on the per-test data home from conftest."""

from __future__ import annotations

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from kestrel.services import profile_edit as pe


@pytest.fixture
def client():
    from kestrel.web.app import build_app

    pe.profile_path().unlink(missing_ok=True)
    yield TestClient(build_app(worker=False))
    pe.profile_path().unlink(missing_ok=True)


def _data() -> dict:
    return yaml.safe_load(pe.profile_path().read_text("utf-8"))


def _checked(html: str, scope: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    boxes = soup.select(f"{scope} input[type=checkbox]")
    return {i["name"] for i in boxes if i.has_attr("checked")}


def _location(client, track="employment") -> set[str]:
    return _checked(client.get(f"/profile?tab=location&track={track}").text,
                    "[data-testid=profile-location]")


def _modes(client, track="employment") -> set[str]:
    return _checked(client.get(f"/profile?tab={track}&track={track}").text,
                    "[data-testid=work-mode]")


def test_ticking_hybrid_in_work_mode_ticks_the_metro_rule_and_remote_follows(client):
    client.post("/profile/employment/employment", data={
        "seniority_floor": "", "mode_remote": "on", "mode_hybrid": "on"})
    assert "geo_rules" not in _data()["employment"]  # Location stays "not saved yet"
    assert _location(client) >= {"remote_ok", "hybrid_requires_metro"}
    assert _modes(client) == {"mode_remote", "mode_hybrid"}


def test_unticking_modes_clears_the_location_boxes(client):
    client.post("/profile/employment/location", data={"remote_ok": "on"})
    client.post("/profile/employment/employment", data={
        "seniority_floor": "", "mode_remote": "on", "mode_hybrid": "on"})
    client.post("/profile/employment/employment", data={"seniority_floor": "", "mode_onsite": "on"})
    geo = _data()["employment"]["geo_rules"]
    assert geo["remote_ok"] is False and geo["hybrid_requires_metro"] is False
    assert not _location(client) & {"remote_ok", "hybrid_requires_metro"}
    assert _modes(client) == {"mode_onsite"}


def test_a_location_save_updates_work_mode(client):
    client.post("/profile/employment/employment", data={
        "seniority_floor": "", "mode_hybrid": "on", "mode_onsite": "on"})
    # Location: remote on, the metro rule left unticked. Hybrid stays (it can be anywhere).
    client.post("/profile/employment/location", data={"remote_ok": "on"})
    assert _data()["employment"]["employment_terms"]["work_modes"] == [
        "remote", "hybrid", "onsite"]
    assert _modes(client) == {"mode_remote", "mode_hybrid", "mode_onsite"}
    # Remote unticked: Remote leaves Work mode.
    client.post("/profile/employment/location", data={"hybrid_requires_metro": "on"})
    assert _modes(client) == {"mode_hybrid", "mode_onsite"}
    assert _location(client) == {"hybrid_requires_metro"}


def test_the_metro_rule_ticked_in_location_adds_hybrid(client):
    client.post("/profile/employment/employment", data={"seniority_floor": "", "mode_onsite": "on"})
    client.post("/profile/employment/location", data={"hybrid_requires_metro": "on"})
    assert "hybrid" in _data()["employment"]["employment_terms"]["work_modes"]
    assert "mode_hybrid" in _modes(client)


def test_the_metro_rule_never_stays_ticked_without_hybrid(client):
    # An older file: modes saved without Hybrid, the rule still true in geo_rules.
    pe.save_section("employment", "employment", {"employment_terms": {"work_modes": ["remote"]}})
    pe.save_section("employment", "location", {"geo_rules": {"hybrid_requires_metro": True}})
    # Location save added Hybrid, so they agree; now remove it through Work mode.
    client.post("/profile/employment/employment", data={"seniority_floor": "", "mode_remote": "on"})
    assert _data()["employment"]["geo_rules"]["hybrid_requires_metro"] is False
    assert "hybrid_requires_metro" not in _location(client)


def test_keeping_hybrid_keeps_a_deliberately_unticked_metro_rule(client):
    client.post("/profile/employment/employment", data={"seniority_floor": "", "mode_hybrid": "on"})
    client.post("/profile/employment/location", data={})
    client.post("/profile/employment/employment", data={
        "seniority_floor": "", "mode_hybrid": "on", "mode_onsite": "on"})
    assert _data()["employment"]["geo_rules"]["hybrid_requires_metro"] is False


def test_an_older_file_without_work_modes_reads_its_location_flags(client):
    pe.save_section("employment", "location", {"geo_rules": {
        "remote_ok": True, "hybrid_requires_metro": True}})
    block = _data()["employment"]
    # The Location save wrote the modes, but a file written before D-UI-214 has none:
    block["employment_terms"].pop("work_modes")
    pe.profile_path().write_text(yaml.safe_dump({"employment": block}), "utf-8")
    assert pe.effective_modes(block, "employment") == ["remote", "hybrid"]
    assert _modes(client) == {"mode_remote", "mode_hybrid"}
    assert _location(client) >= {"remote_ok", "hybrid_requires_metro"}


def test_a_stale_location_flag_is_shown_as_the_modes_say(client):
    pe.save_section("employment", "employment", {"employment_terms": {"work_modes": ["onsite"]}})
    block = _data()["employment"]
    block["geo_rules"] = {"remote_ok": True, "hybrid_requires_metro": True}
    pe.profile_path().write_text(yaml.safe_dump({"employment": block}), "utf-8")
    assert not _location(client) & {"remote_ok", "hybrid_requires_metro"}


def test_consulting_track_stays_in_step_and_separate(client):
    client.post("/profile/employment/location", data={})
    client.post("/profile/consulting/location", data={})
    client.post("/profile/employment/employment", data={"seniority_floor": "", "mode_remote": "on"})
    client.post("/profile/consulting/consulting", data={"mode_hybrid": "on"})
    data = _data()
    geo = data["consulting"]["geo_rules"]
    assert geo["remote_ok"] is False and geo["hybrid_requires_metro"] is True
    assert data["employment"]["geo_rules"]["remote_ok"] is True
    assert _location(client, "consulting") >= {"hybrid_requires_metro"}
    assert "remote_ok" not in _location(client, "consulting")
    assert _modes(client, "consulting") == {"mode_hybrid"}
    assert _modes(client) == {"mode_remote"}
