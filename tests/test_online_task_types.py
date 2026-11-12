"""A restored task is held for a fresh click, for every task type that can send online (D-SEC-10).

Invariant: nothing is sent to an online model on the strength of a confirmation that arrived in a
backup. The consent keys are defined once (kestrel.services.online_consent.CONSENT_PAYLOAD_KEYS);
every task writer records a confirmation only under them and the restore removes all of them.

Invented data only. Nothing here reaches the network."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

import kestrel
from kestrel import paths
from kestrel.config import get_settings
from kestrel.services import online_consent as oc
from kestrel.services.backup import _strip_consent, create_backup, restore_backup
from tests.test_backup_restore import _fresh_db

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

# How each online task type refuses to send once its confirmation is gone.
FAMILY = {
    "t1": "approval",
    "t2": "approval",
    "bench": "approval",
    "letter_draft": "model",
    "build_stories": "model",
    "interview_questions": "model",
    "title_suggestions": "model",
    "follow_up": "model",
    "org_summary": "online_flag",
    "org_research": "online_flag",
    "org_contacts": "online_flag",
}
TIER = {"follow_up": "tier3", "t1": "tier1"}


def _online_settings(tier: str):
    s = get_settings()
    s.llm.tiers[tier].backend = "frontier"
    s.llm.tiers[tier].model = "vendor/model-a"
    s.llm.hosted_mode.enabled = True
    return s


def _task_types_in_source() -> set[str]:
    root = Path(kestrel.__file__).parent
    found: set[str] = {"t1", "t2", "embed", "scrape"}
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found |= set(
            re.findall(
                r"""\b(?:TASK_TYPE|PULL_TYPE|CREATE_TYPE|_LETTER_TASK_TYPE)\s*=\s*"([a-z0-9_]+)\"""",
                text,
            )
        )
        found |= set(re.findall(r"""\bTask\(\s*type\s*=\s*"([a-z0-9_]+)\"""", text))
        for m in re.finditer(r"^TYPES\s*=\s*\(([^)]*)\)", text, re.M):
            found |= set(re.findall(r'"([a-z0-9_]+)"', m.group(1)))
    return found


def test_every_task_type_is_sorted_into_online_or_local():
    found = _task_types_in_source()
    known = oc.ONLINE_TASK_TYPES | oc.LOCAL_TASK_TYPES
    assert not found - known, (
        f"a new task type must be sorted in online_consent.py: {found - known}"
    )
    assert not oc.ONLINE_TASK_TYPES & oc.LOCAL_TASK_TYPES
    assert set(FAMILY) == set(oc.ONLINE_TASK_TYPES), "every online type needs a held check here"


def _full_consent_payload() -> dict:
    return {
        "opportunity_id": 7,
        "keep": "me",
        "nested": {oc.APPROVAL_KEY: {"tier2": ["d"]}},
        **{k: "x" for k in oc.CONSENT_PAYLOAD_KEYS},
    }


def _types(family: str) -> list[str]:
    return sorted(t for t, f in FAMILY.items() if f == family)


def _stripped() -> dict:
    stripped, changed = _strip_consent(_full_consent_payload())
    assert changed and stripped == {"opportunity_id": 7, "keep": "me", "nested": {}}
    return stripped


@pytest.mark.parametrize("task_type", _types("model"))
def test_a_restored_task_that_names_a_model_is_held_for_a_fresh_click(task_type):
    tier = TIER.get(task_type, "tier2")
    assert oc.held_for_consent(_online_settings(tier), tier, _stripped())


@pytest.mark.parametrize("task_type", _types("approval"))
def test_a_restored_task_that_carries_an_approval_no_longer_covers_the_send(task_type):
    tier = TIER.get(task_type, "tier2")
    expected = oc.approval_for(_online_settings(tier), (tier,))
    assert expected
    assert not oc.approval_covers(expected, _stripped().get(oc.APPROVAL_KEY))


@pytest.mark.parametrize("task_type", _types("online_flag"))
def test_a_restored_task_that_needs_an_online_flag_has_none(task_type):
    assert _stripped().get(oc.CONFIRMED_ONLINE_KEY) is None


def test_restoring_a_backup_brings_back_no_task_of_any_online_type(tmp_path):
    """D-SEC-20: the queue is not restored, so no online task (stamped or not) can run after a
    restore without a fresh click on this computer."""
    _fresh_db()
    con = sqlite3.connect(str(paths.db_path()))
    for t in sorted(oc.ONLINE_TASK_TYPES):
        con.execute(
            "INSERT INTO task (type, payload, status, priority, attempts, max_attempts, "
            "created_at, updated_at) VALUES (?, ?, 'pending', 0, 0, 3, '2026-01-01', '2026-01-01')",
            (t, json.dumps(_full_consent_payload())),
        )
    con.commit()
    con.close()
    archive = create_backup(tmp_path)
    _fresh_db()
    restore_backup(archive)
    con = sqlite3.connect(str(paths.db_path()))
    rows = con.execute("SELECT type, payload FROM task").fetchall()
    con.close()
    assert rows == []


def test_the_consent_key_list_names_every_key_a_writer_uses():
    """Pinned by hand, not derived: a writer's key missing from the list would survive a restore."""
    assert {
        "consented_model",
        "consented_destination",
        "online_approval",
        "confirmed_online",
        "confirmed_model",
        "confirmed_dest",
    } <= set(oc.CONSENT_PAYLOAD_KEYS)
