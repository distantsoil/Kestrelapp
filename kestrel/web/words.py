"""The words a page uses for the operator's track (D-UI-54).

Employment and consulting share the pipeline but not the vocabulary: a job seeker applies to roles
from a shortlist with a resume; a consultant contacts leads and sends proposals with a capability
statement. Every template that names one of these things takes it from `words(track)` (a Jinja
global), never from a literal, so a consulting user never reads job-seeker copy.

Fit words (strong fit / good fit / maybe / not a fit / your call) are shared and live in
kestrel/web/fit.py. Technical terms the UI shows only in Advanced views or tooltips live here too
(PLAIN), so the jargon sweep has one place to look up the plain label for each.
"""

from __future__ import annotations

from typing import Any

EMPLOYMENT: dict[str, str] = {
    "track_name": "Employment",
    "role": "role",
    "roles": "roles",
    "list": "shortlist",
    "add_to_list": "Add to my shortlist",
    "on_list": "on my shortlist",
    "board": "Applications",
    "first_column": "My shortlist",
    "applied_q": "Applied?",
    "applied_done": "Applied",
    "doc": "resume",
    "doc_title": "Resume",
    "tailor": "Tailor resume",
    "not_for_me": "Not for me",
    "follow_up": "Follow up",
    "interview_prep": "Interview prep",
}

CONSULTING: dict[str, str] = {
    "track_name": "Consulting",
    "role": "lead",
    "roles": "leads",
    "list": "leads",
    "add_to_list": "Add to my leads",
    "on_list": "in my leads",
    "board": "Leads",
    "first_column": "To contact",
    "applied_q": "Reached out?",
    "applied_done": "Reached out",
    "doc": "capability statement",
    "doc_title": "Capability statement",
    "tailor": "Tailor capability statement",
    "not_for_me": "Not for me",
    "follow_up": "Follow up",
    "interview_prep": "Meeting prep",
}

# Plain labels for technical terms (release concern 7). The technical term may still appear in a
# tooltip or an Advanced view, never as the default label.
PLAIN: dict[str, str] = {
    "tier0": "Rules check",
    "tier1": "Quick screen",
    "tier2": "Full review",
    "tier3": "Deeper read",
    "quarantine": "Failed scoring",
    "worker_idle": "Not scoring anything right now",
    "ollama_unreachable": "AI engine not running",
    "derived_models": "Kestrel's models",
    "context_window": "how much text the model can read at once",
    "hardware_profile": "your computer's size class",
}


def words(track: str | None = None) -> dict[str, Any]:
    """The word set for `track`; anything but "consulting" reads as employment."""
    return CONSULTING if track == "consulting" else EMPLOYMENT
