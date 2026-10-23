"""In-app announcements (WP-22): fixed text shipped with the build, never fetched.

Kestrel never phones home, so an announcement is a code constant keyed by id. Dismissing one
stores its id in a setting row on this install; a future announcement has a new id and shows
again. This is the only registry: add an entry here, do not add a second banner system."""

from __future__ import annotations

ANNOUNCEMENTS: tuple[dict[str, str], ...] = (
    {
        "id": "ui-refresh-2026",
        "text": (
            "A refreshed look is coming. Your data stays as it is."
        ),
    },
)

DISMISSED_KEY = "ui.announcements_dismissed"


def known_ids() -> set[str]:
    return {a["id"] for a in ANNOUNCEMENTS}


def pending(dismissed: list[str]) -> dict[str, str] | None:
    """The first announcement the user has not dismissed, else None."""
    for a in ANNOUNCEMENTS:
        if a["id"] not in dismissed:
            return a
    return None
