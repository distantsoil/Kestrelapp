"""What the person is told when they change a setting while a run is working (#103).

A run keeps the settings it was confirmed with (the worker resolves them once per run, so the
whole run scores on one tier set, and a run the person confirmed for an online model finishes as
confirmed). The defect was only that nothing said so. This module holds the run's settings while
it is in flight, compares them with what is stored now, and words the notice that the Settings
and Models saves carry back with a Stop button.

Invariant: nothing here sends, starts or changes a run. A change toward online never takes effect
without the confirmed dialog, because the run is never re-pointed; Stop reuses `/runs/stop`."""

from __future__ import annotations

import json
from typing import Any

from kestrel.config import Settings

STOP_URL = "/runs/stop"
STOP_LABEL = "Stop the run"
EVENT = "k-run-notice"
_SHOWN_MS = 20000


def _tier(settings: Settings, tier: str) -> tuple[str, bool]:
    """The model a tier uses and whether it is an online one."""
    cfg = settings.llm.tiers.get(tier)
    if cfg is None or not cfg.model:
        return "", False
    backend = settings.llm.backends.get(cfg.backend)
    return str(cfg.model), bool(backend and backend.external)


def fingerprint(settings: Settings) -> dict[str, Any]:
    """The settings a run is confirmed on, as plain values to compare: where the models run, which
    models, the reasoning effort and the parallel requests."""
    return {
        "mode": settings.llm.mode,
        "tier1": _tier(settings, "tier1"),
        "tier2": _tier(settings, "tier2"),
        "reasoning": {name: b.hosted.reasoning_effort
                      for name, b in settings.llm.backends.items() if b.external},
        "concurrency": (settings.concurrency.tier1, settings.concurrency.tier2),
    }


def changed(run: Settings, stored: Settings) -> list[str]:
    """Plain names for what differs between the run's settings and the stored ones."""
    a, b = fingerprint(run), fingerprint(stored)
    names = (
        ("mode", "where the models run"), ("tier1", "the quick-screen model"),
        ("tier2", "the full-review model"), ("reasoning", "reasoning"),
        ("concurrency", "parallel requests"),
    )
    return [label for key, label in names if a[key] != b[key]]


def describe_run(run: Settings) -> str:
    """"qwen3:14b on this computer" or "some/model online": the run's full-review model."""
    model, online = _tier(run, "tier2")
    where = "online" if online else "on this computer"
    return f"{model} {where}" if model else f"the model you confirmed {where}"


def message(run: Settings) -> str:
    return (
        f"This run is still using {describe_run(run)}, as confirmed. Your change applies to the "
        "next run. Stop the run if you do not want it to continue."
    )


def toast_detail(run: Settings) -> dict[str, Any]:
    """The toast the page shows: the notice with its Stop button (the toast's action button,
    which posts to /runs/stop)."""
    return {"message": message(run), "tone": "warn", "undoUrl": STOP_URL,
            "undoLabel": STOP_LABEL, "timeout": _SHOWN_MS}


def merge_trigger(existing: str | None, detail: dict[str, Any]) -> str:
    """The response's HX-Trigger header with the notice event added, keeping what a route already
    set (a JSON object of events, or htmx's plain comma-separated event names)."""
    events: dict[str, Any] = {}
    raw = (existing or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            events = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            events = {}
    elif raw:
        events = {name.strip(): True for name in raw.split(",") if name.strip()}
    events[EVENT] = detail
    return json.dumps(events, ensure_ascii=True)
