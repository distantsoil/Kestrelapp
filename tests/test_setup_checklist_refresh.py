"""The setup checklist follows a change to how Kestrel runs without leaving Settings (D-UI-241).
Switching Models to local (or online, or both) and saving or removing a key answer with the
`kestrel-setup-changed` event next to the toast, and the checklist listens for it and fetches
itself again. Cloud-safe: TestClient on the real app, a throwaway data directory."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from kestrel.db import migrate
from kestrel.db.engine import forget_engine
from kestrel.web.app import build_app

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

EVENT = "kestrel-setup-changed"


def _client() -> TestClient:
    migrate.run_migrations()
    forget_engine()
    return TestClient(build_app(worker=False))


def _events(r) -> dict:
    return json.loads(r.headers.get("hx-trigger") or "{}")


def test_switching_to_local_fires_the_event_and_keeps_the_toast():
    with _client() as c:
        c.post("/settings/computer/mode", data={"mode": "both"})
        r = c.post("/settings/computer/mode", data={"mode": "local"})
    ev = _events(r)
    assert ev.get(EVENT) is True
    assert ev["k-toast"]["message"] == "Kestrel will run on this computer."


def test_choosing_what_is_already_chosen_changes_nothing_so_fires_nothing():
    with _client() as c:
        c.post("/settings/computer/mode", data={"mode": "local"})
        r = c.post("/settings/computer/mode", data={"mode": "local"})
    assert EVENT not in _events(r)


def test_the_card_fetched_again_after_undo_also_fires_it():
    with _client() as c:
        r = c.get("/settings/computer/mode")
    assert _events(r).get(EVENT) is True


def test_the_checklist_listens_for_the_event_whether_or_not_work_is_running():
    with _client() as c:
        html = c.get("/settings/computer/checklist").text
    assert 'hx-trigger="kestrel-setup-changed from:body' in html
    assert 'hx-get="/settings/computer/checklist"' in html


def test_the_checklist_reads_the_new_choice_on_the_next_fetch():
    with _client() as c:
        first = c.get("/settings/computer/checklist").text
        c.post("/settings/computer/mode", data={"mode": "hosted"})
        after = c.get("/settings/computer/checklist").text
        c.post("/settings/computer/mode", data={"mode": "local"})
        back = c.get("/settings/computer/checklist").text
    assert "Not needed while you use only an online model" not in first
    assert "Not needed while you use only an online model" in after
    assert "Not needed while you use only an online model" not in back
