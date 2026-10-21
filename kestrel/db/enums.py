"""Valid values for the string-typed enum-like columns. Stored as plain TEXT (no DB-level enum):
pursuit.status differs per track, and low churn beats CHECK-constraint migrations. Services
validate against these (decision log)."""

from __future__ import annotations

TRACKS = ("employment", "consulting")

OPP_TYPES = (
    "job_posting",
    "contract_posting",
    "rfp",
    "trigger_event",
    "referral",
    "manual",
)

SOURCE_KINDS = (
    "jobspy",
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "rippling",
    "adp",
    "paylocity",
    "page",
    "rss",
    "sec_edgar",
    "sam_gov",
    "pe_portfolio",
    "manual",
    "import",
)

SCRAPE_STATUS = ("running", "ok", "partial", "failed")

# Why the operator hid an opportunity (opportunity.archive_reason). "Hide" is the operator-facing
# word; the column keeps the archive name. The labels are the picker's copy, in picker order.
ARCHIVE_REASONS = {
    "applied": "Applied elsewhere",
    "not_interested": "Not interested",
    "later": "Later",
    "other": "Other",
}

REMOTE_TYPES = ("remote", "hybrid", "onsite", "unknown")

COMP_SOURCES = ("structured", "parsed", "inferred", "none")

STAGES = (
    "new",
    "t0_pass",
    "t0_reject",
    "t1_pass",
    "t1_reject",
    "t2_done",
    "t3_done",
    "error",
    "quarantined",
)

LIVENESS = ("live", "closed", "unknown", "unchecked")

# Why a posting was marked closed (opportunity.closed_reason). A posting closes only on the second
# consecutive expired read (D-INTEL-1). `ats_missing` (the ATS no longer lists the id) is reserved,
# not implemented.
CLOSED_REASONS = ("http_404", "http_410", "page_text", "ats_missing")

VERDICTS = ("apply", "pursue", "consider", "pass", "keep", "kill")

# No code sets "failed" today (a task goes to "dead" after its last attempt); it stays because
# existing databases and constraints may name it.
TASK_STATUS = ("pending", "leased", "done", "failed", "dead", "cancelled")

RUN_STATUS = ("running", "stopping", "done", "failed", "interrupted")

# pursuit.status is track-specific (SPEC.md 4.5)
PURSUIT_STATUS = {
    "employment": (
        "shortlisted",
        "preparing",
        "applied",
        "screening",
        "interviewing",
        "final",
        "offer",
        "accepted",
        "rejected",
        "withdrawn",
        "ghosted",
    ),
    "consulting": (
        "shortlisted",
        "researching",
        "outreach_sent",
        "conversation",
        "scoping",
        "proposal_sent",
        "negotiating",
        "won",
        "lost",
        "no_response",
    ),
}

# Pursuit statuses that mean "an application (or proposal) has gone out", on either track. A new
# posting that dedups into such a pursuit's group is a re-post (SPEC.md 5.6). Closed statuses are
# included on purpose: a rejection or a loss is still a role the operator already pursued.
APPLIED_OR_LATER = frozenset(
    {
        "applied",
        "screening",
        "interviewing",
        "final",
        "offer",
        "accepted",
        "rejected",
        "withdrawn",
        "ghosted",
        "outreach_sent",
        "conversation",
        "scoping",
        "proposal_sent",
        "negotiating",
        "won",
        "lost",
        "no_response",
    }
)

DOC_KINDS = ("resume", "capability_statement")
SECTION_KINDS = ("role", "service_line", "proof_engagement", "credential")

CONTACT_TYPES = (
    "hiring_manager",
    "recruiter",
    "peer",
    "exec",
    "referral",
    "board",
    "pe_operator",
)
RELATIONSHIP = ("none", "weak", "known", "strong")
OUTREACH_CHANNELS = ("linkedin", "email", "other")
OUTREACH_STATUS = ("draft", "copied", "sent_manually", "discarded")
INTERACTION_KINDS = ("message", "call", "meeting", "email", "event")

MOCK_MODES = ("behavioral", "technical_leadership", "executive_panel", "consulting_discovery")

OFFER_STATUS = ("verbal", "written", "accepted", "declined", "expired")

LABEL_SOURCES = ("active_learning", "manual", "derived_from_outcome")
CALIBRATION_STATUS = ("proposed", "accepted", "rejected")
ML_PROMOTED_BY = ("auto", "operator")

NOTIFY_SEVERITY = ("info", "success", "warning", "error")


def valid_pursuit_status(track: str, status: str) -> bool:
    return status in PURSUIT_STATUS.get(track, ())
