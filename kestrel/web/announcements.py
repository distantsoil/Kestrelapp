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


# The privacy notice of 0.7.7 (D-REL-41, D-PRIV-795): installs that had online models switched on
# with a saved key are told once what the provider reads sent before this version. While their
# acceptance predates the current disclosure (online paused), it links to Settings > Hosted, where
# the updated text is ticked again; the Models page carries the same link (REVIEW_HREF).
PROVIDER_READS_ID = "provider-reads-0.7.7"
REVIEW_HREF = "/settings/hosted/review"
PROVIDER_READS_TEXT = (
    "About online models: before 0.7.7, your saved key went to the provider that issued it, for "
    "balance and model-list checks, on page load and about every 5 minutes while the app was "
    "open. No job, resume or profile data was ever sent. As of 0.7.7 these checks run only when "
    "online models are in use, and the disclosure now names them."
)
PROVIDER_READS_PAUSED = (
    " Online scoring is paused until you review and accept the updated disclosure."
)


def provider_reads_notice(settings) -> dict[str, object] | None:
    """The notice for this install, or None when it does not apply: online models switched on
    (the stale tick counts, since every upgrader has one) and a key saved for an online service.
    The pause sentence and the button are chosen in the same statement, from the stored rows."""
    from kestrel.config import disclosure_reaccept_needed
    from kestrel.llm import providers as prov

    if not settings.llm.hosted_mode.enabled:
        return None
    if not any(bool(b.api_key) or prov.secret_present(b.api_key_env)
               for b in prov.external_backends(settings).values()):
        return None
    if disclosure_reaccept_needed(settings):
        return {
            "id": PROVIDER_READS_ID,
            "text": PROVIDER_READS_TEXT + PROVIDER_READS_PAUSED,
            "link": {"label": "Review and re-accept", "href": REVIEW_HREF},
        }
    return {"id": PROVIDER_READS_ID, "text": PROVIDER_READS_TEXT}


def known_ids() -> set[str]:
    return {a["id"] for a in ANNOUNCEMENTS} | {MODEL_UPGRADE_ID, PROVIDER_READS_ID}


def pending(dismissed: list[str]) -> dict[str, str] | None:
    """The first announcement the user has not dismissed, else None."""
    for a in ANNOUNCEMENTS:
        if a["id"] not in dismissed:
            return a
    return None
