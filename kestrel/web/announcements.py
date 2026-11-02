"""In-app announcements (WP-22): fixed text shipped with the build, never fetched.

Kestrel never phones home, so an announcement is a code constant keyed by id. Dismissing one
stores its id in a setting row on this install; a future announcement has a new id and shows
again. This is the only registry: add an entry here, do not add a second banner system."""

from __future__ import annotations

# Empty on purpose: the refreshed-look notice (D-UI-82) was removed once the redesign shipped
# (D-UI-157). Add {"id": ..., "text": ...} here for a future notice; the route and the Today
# slot already handle it.
ANNOUNCEMENTS: tuple[dict[str, str], ...] = ()

DISMISSED_KEY = "ui.announcements_dismissed"


# The one announcement that depends on this install's state (D-LLM-9): an upgraded local install
# is told once that Kestrel now uses a model that answers without thinking out loud.
MODEL_UPGRADE_ID = "model-upgrade"


def model_upgrade_notice(gb: float) -> dict[str, object]:
    return {
        "id": MODEL_UPGRADE_ID,
        "text": ("Kestrel now uses a model that answers without thinking out loud. "
                 f"About {gb:.1f} GB to download. Your current models stay installed."),
        "action": {"label": "Download", "post": "/announcements/model-upgrade/download"},
    }


def known_ids() -> set[str]:
    return {a["id"] for a in ANNOUNCEMENTS} | {MODEL_UPGRADE_ID}


def pending(dismissed: list[str]) -> dict[str, str] | None:
    """The first announcement the user has not dismissed, else None."""
    for a in ANNOUNCEMENTS:
        if a["id"] not in dismissed:
            return a
    return None
