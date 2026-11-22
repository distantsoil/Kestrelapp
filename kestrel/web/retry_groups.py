"""Needs another try: one banner when several roles failed the same way (D-UI-550 to D-UI-556).

A group is the open, actionable roles that share a step, a stored typed failure kind and a model
(kestrel.llm.failure_kind), newest failure within a day. It forms only from that stored kind, never
from the wording of an error (section 8a.2), and a failure with no kind is never in one. A group
needs `THRESHOLD` roles; below it there is no banner. Grouping only adds a banner: every role stays
in the list, the filter `?group=` shows exactly a group's roles, and the count is `len(members)`.
Pure functions plus the dismiss marker, so tests can call them without a page."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting
from kestrel.llm import failure_kind as fk
from kestrel.llm.failure_kind import split_stored
from kestrel.queue import row_retry
from kestrel.web.retry_words import (
    _parse,
    is_held,
    not_sent,
    place_for,
    plain_reason,
    refused_key,
    step_for,
    tries_words,
    when_words,
)

ROW_PREFIX = "rt-row-"
# A view joins a group only when its own row can be run again (kestrel.queue.row_retry.plan,
# D-UI-790): the banner, its dialog and its send all read that one decision, and the send queues
# exactly the task the plan names. Today that is Fit score alone.
THRESHOLD = 3  # hard rule 6: below it there is no banner
WINDOW = timedelta(days=1)  # "within the last day": the newest failure of each member
DISMISS_PREFIX = "retry_group_dismissed."

# One fix per kind, always a choice the person makes, never applied by itself (section 8a.3). The
# cause is said in plain words; the label is the one button the banner offers. Every kind here is
# a different-model try, so the group action reuses the per-row model menu and its consent rules.
# `quality` is the one word that differs: a more careful model, or one with more room.
CAUSES: dict[str, tuple[str, str]] = {
    fk.UNREADABLE: ("the AI’s answer couldn’t be read", "Try these with a more careful model"),
    fk.OUT_OF_ROOM: ("the AI ran out of room", "Try these with a model that has more room"),
    fk.REPEATED: ("the AI repeated itself", "Try these with a different model"),
}


def _now() -> datetime:
    """This module's own clock, so a test can move it without touching the global one."""
    return datetime.now(UTC)


def shown_ids(members: list[dict]) -> str:
    """The role ids a group holds, as the ask-first dialog carries them (a comma list). The send
    acts on these roles only, never on a role that joined the group after the dialog (D-UI-562)."""
    return ",".join(str(i) for i in sorted(m["opp"].id for m in members))


def parse_shown_ids(raw: object) -> set[int]:
    """The ids a dialog carried, read back. Anything that is not a plain integer is dropped."""
    return {int(p) for p in str(raw or "").split(",") if p.strip().isdigit()}


def group_key(step_key: str, kind: str, model: str | None) -> str:
    """A key safe for a URL and a setting name: step, kind and a short hash of the model."""
    digest = hashlib.sha256((model or "").encode("utf-8")).hexdigest()[:10]
    return f"{step_key}.{kind}.{digest}"


def build_groups(views: list[dict], *, now: datetime | None = None) -> list[dict]:
    """The groups among the page's role views, largest first. A view joins when it has a role, is
    not already waiting or hidden, has a kind on the allowlist and failed within the window.
    Each group carries its member views untouched (`members`) and its exact `count`."""
    cutoff = (now or _now()) - WINDOW
    buckets: dict[str, dict] = {}
    for v in views:
        kind = v.get("kind")
        if v.get("rescore"):
            continue  # a failed score-again keeps its earlier score and is counted apart (D-UI-70)
        if kind not in CAUSES or v.get("opp") is None or v.get("queued") or v.get("hidden"):
            continue
        if row_retry.plan(v["q"], v["opp"]).task is None:
            continue  # its own row's step cannot be run again here, so no fix can claim it
        if not v.get("needs_you"):
            continue  # the rows Today and the sidebar count: not hidden, not already waiting
        when = _parse(v["q"].created_at)
        if when is None or when < cutoff:
            continue
        model = v["q"].model or ""
        key = group_key(v["step_key"], kind, model)
        g = buckets.setdefault(key, {
            "key": key, "step_key": v["step_key"], "step_label": v["step_label"], "kind": kind,
            "model": model, "members": [],
        })
        g["members"].append(v)
    out = []
    for g in buckets.values():
        g["count"] = len(g["members"])
        if g["count"] < THRESHOLD:
            continue
        g["cause"], g["fix"] = CAUSES[g["kind"]]
        out.append(g)
    return sorted(out, key=lambda g: (-g["count"], g["key"]))


def find(groups: list[dict], key: str) -> dict | None:
    return next((g for g in groups if g["key"] == key), None)


# ------------------------------------------------------------------------------------ dismiss
def dismiss_key(key: str) -> str:
    return f"{DISMISS_PREFIX}{key}"


async def dismissed_counts(session: AsyncSession) -> dict[str, int]:
    """group key -> the count when the person dismissed its banner."""
    rows = (
        await session.execute(select(Setting).where(Setting.key.like(f"{DISMISS_PREFIX}%")))
    ).scalars().all()
    out: dict[str, int] = {}
    for r in rows:
        count = r.value.get("count") if isinstance(r.value, dict) else None
        if isinstance(count, int) and not isinstance(count, bool):
            out[r.key[len(DISMISS_PREFIX):]] = count
    return out


def visible(groups: list[dict], dismissed: dict[str, int]) -> list[dict]:
    """Groups whose banner shows: not dismissed, or grown since (a dismissal holds only while the
    group is no bigger than it was)."""
    return [g for g in groups if g["count"] > dismissed.get(g["key"], 0)]


async def dismiss(session: AsyncSession, key: str, count: int) -> str:
    """Hide a group's banner until it grows past `count`; returns the marker's stamp (for Undo).
    The caller commits."""
    value = {"count": int(count), "at": utcnow_iso()}
    row = await session.get(Setting, dismiss_key(key))
    if row is None:
        session.add(Setting(key=dismiss_key(key), value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()
    return value["at"]


async def prune_dismissed(session: AsyncSession, live_keys: set[str]) -> int:
    """Delete the hide markers of groups that no longer exist, so they do not pile up. Returns how
    many were removed; the caller commits. A marker for a group that is still there is kept."""
    rows = (
        await session.execute(select(Setting).where(Setting.key.like(f"{DISMISS_PREFIX}%")))
    ).scalars().all()
    stale = [r for r in rows if r.key[len(DISMISS_PREFIX):] not in live_keys]
    for r in stale:
        await session.delete(r)
    return len(stale)


async def undo_dismiss(session: AsyncSession, key: str, *, at: str) -> bool:
    """Bring the banner back, only if the marker is the one the toast made."""
    row = await session.get(Setting, dismiss_key(key))
    if row is None or not isinstance(row.value, dict) or row.value.get("at") != at:
        return False
    await session.delete(row)
    return True


# ------------------------------------------------------------------------------------ views
def view(r: dict) -> dict:
    """One quarantine row as the page shows it: the step, the plain reason, when and the tries."""
    q = r["q"]
    key, label = step_for(q.tier)
    kind = r.get("kind")
    error = r["error"] or split_stored(q.error)[1]
    return {
        **r,
        "id": q.id,
        "dom_id": f"{ROW_PREFIX}{q.id}",
        "step_key": key,
        "step_label": label,
        "kind": kind,
        "error": error,  # without the kind line: that is stored typing, not a message
        "reason": plain_reason(error),
        "refused_key": refused_key(error),
        "on_hold": is_held(error),
        "not_sent": not_sent(error),
        "when": when_words(q.created_at),
        "attempts": max(int(q.attempts or 0), 1),
        "tries": tries_words(q.attempts),
        "can_act": r["opp"] is not None,
        # A failure with no role (D-UI-224) runs again on its own page, never from here.
        "place": place_for(q.tier) if r["opp"] is None else None,
    }


def role_views(open_rows: list[dict]) -> list[dict]:
    """One row per role, newest failure first. An older failure of the same role is not a second
    row; its tries add to the first, so a role that failed again reads "5 tries", not twice."""
    seen: dict[int, dict] = {}
    out: list[dict] = []
    for r in open_rows:
        v = view(r)
        oid = v["opp"].id if v["opp"] is not None else None
        if oid is not None and oid in seen:
            seen[oid]["attempts"] += v["attempts"]
            continue
        out.append(v)
        if oid is not None:
            seen[oid] = v
    for v in out:
        v["tries"] = tries_words(v["attempts"])
    return out


async def groups_now(s: AsyncSession) -> list[dict]:
    """The groups as the rows are now: the one lookup the page, its actions and Today share."""
    from kestrel.web.pages.runs_detail import _quarantine_rows

    rows = await _quarantine_rows(s, resolved_cap=0)
    return build_groups(role_views([r for r in rows if not r["resolved"]]))
