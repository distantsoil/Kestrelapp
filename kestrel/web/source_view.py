"""How a source reads on the Sources page (D-UI-102): its group, origin tag, health, ten-check
sparkline, counts and the one plain reason line. Pure functions over rows already loaded, so the
page, the drawer and the tests all read one definition.

Health is derived, never stored: the newest finished run and the failure streak decide it.
A rate limit (HTTP 429) is "Slowed down", never "Needs you" and never a failed run (D-SRC-5 note);
the raw status stays behind Show details. A source that is off is Paused whatever its history."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kestrel.db.models import ScrapeRun, Source
from kestrel.ingest import failure_code
from kestrel.web.retry_words import when_words

BOARDS, ATS, FEEDS, GOV, SIG = "boards", "ats", "feeds", "gov", "sig"

# Groups per track, in page order: (key, title, plain description). A row whose kind belongs
# to a group the track does not list still shows, in its own group after these.
GROUPS: dict[str, list[tuple[str, str, str]]] = {
    "employment": [
        (BOARDS, "Job board searches", "Searches Kestrel runs on public job boards."),
        (ATS, "Company career pages",
         "Each company's own job list, read from the hiring system it uses."),
        (FEEDS, "Feeds", "Job feeds you added."),
    ],
    "consulting": [
        (BOARDS, "Job board searches", "Contract and freelance searches on public job boards."),
        (GOV, "Public bids", "Government requests for proposals."),
        (SIG, "Company signals",
         "Public filings and news Kestrel watches for openings to offer help."),
    ],
}
GROUP_TITLES = {k: t for rows in GROUPS.values() for k, t, _d in rows}
GROUP_NOTES = {k: d for rows in GROUPS.values() for k, _t, d in rows}
# Names in the shared icon set (templates/ui/macros.html).
GROUP_ICON = {BOARDS: "compass", ATS: "folder", FEEDS: "sources", GOV: "shield", SIG: "sources"}
ADD_LABEL = {
    BOARDS: "Add a search", ATS: "Add a company", FEEDS: "Add a feed",
    GOV: "Add a bid search", SIG: "Watch a company",
}
PAUSED_NOTE = "Kept with their settings. Turn one on to include it in runs again."

PLATFORMS = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday",
    "smartrecruiters": "SmartRecruiters", "rippling": "Rippling", "adp": "ADP",
    "paylocity": "Paylocity", "workable": "Workable",
    "teamtailor": "Teamtailor",
    "personio": "Personio",
    "breezy": "Breezy HR",
    "recruitee": "Recruitee",
    "jobvite": "Jobvite",
}
_SIGNAL_TYPE = {
    "sec_edgar": "SEC filings", "sam_gov": "SAM.gov", "pe_portfolio": "PE portfolio page",
    "remoterocketship": "RemoteRocketship", "usajobs": "USAJobs", "adzuna": "Adzuna",
    "himalayas": "Himalayas", "weworkremotely": "We Work Remotely",
    "arbeitnow": "Arbeitnow", "jobicy": "Jobicy", "remotive": "Remotive",
    "fourdayweek": "4 Day Week",
    "hn_hiring": "Hacker News",
}

# Where a source came from (Source.origin). `suggested` is a company the operator added from
# the Suggested companies list; `custom` is anything else they added.
ORIGINS: dict[str, tuple[str, str]] = {
    "built": ("Built in", "Comes with Kestrel."),
    "profile": ("From your profile", "Searches built from your titles and places."),
    "found": ("Found in results", "Company job boards linked from search results."),
    "sug": ("Suggested", "Companies you chose to add from suggestions."),
    "you": ("Added by you", "Anything you add, by link or by name."),
}
_ORIGIN_KEY = {"shipped": "built", "generated": "profile", "discovered": "found",
               "suggested": "sug"}

HEALTH: dict[str, tuple[str, str]] = {  # key: (label, pill tone)
    "g": ("Healthy", "good"), "w": ("Slowed down", "warn"), "b": ("Needs you", "bad"),
    "m": ("Paused", ""), "n": ("Not checked yet", ""),
}
SLOWED_REASON = "Site asked us to slow down. Retrying later."
# D-UI-902, owner sign-off pending: the row a search the runner paused shows.
AUTO_PAUSED_REASON = ("Kestrel switched this search off after three failed checks. "
                      "Turn it back on for a clean slate.")
# D-UI-902, owner sign-off pending: the Needs you tile's caption now that it counts the rows
# the runner paused too.
NEEDS_YOU_SUB = "Failed checks or a moved address"
_RATE = re.compile(r"\b429\b|too many requests|rate.?limit|slow down", re.I)
_MOVED = re.compile(r"\b(404|410)\b|not found|moved", re.I)

# One decision: when a check stored a code, the reason on the page comes from that code, so the
# sentence and the code beside it cannot disagree. Rows saved before codes keep the older reading
# of the stored text (`_RATE`, `_MOVED`) because they have no code to read.
REASON_BY_CODE = {
    "SRC-001": "Kestrel could not use how this source is set up. Open it and check the setup.",
    "SRC-002": SLOWED_REASON,
    "SRC-003": "The site had a server problem. Kestrel tries again on the next check.",
    "SRC-004": "The site took too long to answer. Kestrel tries again on the next check.",
    "SRC-005": "The site's rules ask programs not to read this page, so Kestrel did not open it.",
    "SRC-006": "This board needs a key of your own. Add it on the Sources page.",
    "SRC-007": ("This source allows only a few checks a day, and today's are used. Kestrel "
                "checks again tomorrow."),
    "SRC-008": "Kestrel could not find this page or board at its saved address. It may have moved.",
    "SRC-009": "Part of the last check did not finish. Kestrel tries again next check.",
    "SRC-010": "Kestrel could not read this source. It tries again on the next check.",
    "SRC-011": "A posting could not be saved. Kestrel tries again on the next check.",
    "SRC-012": ("Something unexpected stopped the last check. Kestrel tries again on the next "
                "check."),
    "SRC-013": "",  # a saved flag, handled before any run is read
    "SRC-014": ("No posting from the last check could be saved. Kestrel tries again on the next "
                "check."),
}

REMOVED_KEY = "_removed"  # config marker: the source was removed and Undo can bring it back


def is_removed(src: Source) -> bool:
    return bool((src.config or {}).get(REMOVED_KEY))


BOARD_KINDS = (
    "jobspy", "remoterocketship", "usajobs", "adzuna", "himalayas", "weworkremotely",
    "arbeitnow", "jobicy", "remotive", "fourdayweek", "hn_hiring",
)
KEYED_BOARDS = ("usajobs", "adzuna")  # boards that need a key of the user's own


def _key_problem(kind: str, error: str) -> bool:
    """True when a keyed board's run error is its missing-key or rejected-key sentence."""
    if kind == "adzuna":
        from kestrel.ingest.adapters.adzuna import is_key_problem
    else:
        from kestrel.ingest.adapters.usajobs import is_key_problem
    return is_key_problem(error)


def group_of(src: Source) -> str:
    if src.kind in PLATFORMS or src.kind in (
        "page", "careers_page", "import", "manual"
    ):
        return ATS
    if src.kind in BOARD_KINDS:
        return BOARDS
    if src.kind == "sam_gov":
        return GOV
    if src.kind == "rss":
        return SIG if src.track == "consulting" else FEEDS
    if src.kind in ("sec_edgar", "pe_portfolio"):
        return SIG
    return FEEDS


def origin_of(src: Source) -> str:
    return _ORIGIN_KEY.get(src.origin or "", "you")


def type_label(src: Source) -> str:
    """Plain name of the kind: the hiring system for a company page, the boards for a search."""
    from kestrel.analytics.run_summary import _SITE_WORDS
    from kestrel.ingest.adapters.jobspy import _sites

    if src.kind == "jobspy":
        sites = _sites(src.config or {})
        return " + ".join(_SITE_WORDS.get(s, s.replace("_", " ").title()) for s in sites)
    if src.kind == "rss":
        return "Company news" if src.track == "consulting" else "Feed"
    if src.kind in PLATFORMS:
        return PLATFORMS[src.kind]
    own = "Careers page" if src.kind in ("page", "careers_page") else src.kind
    return _SIGNAL_TYPE.get(src.kind, own)


def check_tone(status: str | None) -> str:
    """The color of a check that did not finish well, from its own status only, never from its
    text or from how many checks failed before it: a failed check is red (it counts toward the
    switch-off after three), anything else amber. The Runs page reads a failed search red and a
    partial one amber the same way, so the dot, the bar and Runs agree (D-ERR-24)."""
    return "b" if status == "failed" else "w"


def run_char(run: ScrapeRun) -> str:
    """One check as a sparkline bar: g healthy, w slowed or partly done, b failed."""
    if run.status == "ok":
        return "g"
    return check_tone(run.status)


@dataclass
class SourceView:
    src: Source
    id: int
    name: str
    group: str
    origin: str
    origin_label: str
    type_line: str
    health: str  # g w b m n
    health_label: str
    tone: str
    reason: str  # one plain line when not healthy, else ""
    raw: str  # the unprocessed status, behind Show details
    last: str  # "Checked 9:41 AM" or "Off since ..."
    open_n: int | None
    new_n: int
    spark: str  # ten chars: g w b m
    enabled: bool
    removable: bool
    sites: list[dict] = field(default_factory=list)
    bodies: list[dict] = field(default_factory=list)  # new roles with no description
    model_used: str = ""
    unreadable: int = 0
    code: str = ""  # the SRC code of the last problem, "" when none (#174, D-ERR-20)

    @property
    def search_text(self) -> str:
        return f"{self.name} {self.type_line} {self.origin_label}".lower()


def _no_boards(src: Source) -> bool:
    from kestrel.ingest.adapters.jobspy import NO_BOARDS_KEY

    return src.kind == "jobspy" and bool((src.config or {}).get(NO_BOARDS_KEY))


def _NO_BOARDS_REFUSED() -> str:  # noqa: N802 - a constant read late, to keep the import lazy
    from kestrel.ingest.adapters.jobspy import NO_BOARDS_REFUSED

    return NO_BOARDS_REFUSED


def _health(src: Source, runs: list[ScrapeRun], sites: list[dict] | None) -> tuple[str, str, str]:
    """(key, reason, raw) for an enabled source."""
    from kestrel.ingest.adapters.jobspy import NO_BOARDS_KEY, NO_BOARDS_REFUSED

    if src.kind == "jobspy" and (src.config or {}).get(NO_BOARDS_KEY):
        return "b", NO_BOARDS_REFUSED, NO_BOARDS_KEY
    done = [r for r in runs if r.status != "running"]
    latest = done[0] if done else None
    if latest is None and not src.consecutive_failures:
        return "n", "", ""
    err = (latest.error if latest is not None else "") or ""
    if src.kind in KEYED_BOARDS and latest is not None and latest.status != "ok":
        if _key_problem(src.kind, err):  # no key yet, or the board refused it: say so, plainly
            return "b", err.split(": ", 1)[-1].rstrip(".") + ".", err
    code = failure_code.stored_code(latest.counts) if latest is not None else None
    if latest is not None and code and REASON_BY_CODE.get(code) and latest.status != "ok":
        return check_tone(latest.status), REASON_BY_CODE[code], err
    # A row saved before codes: its text is read only to word a partial check; a failed one is
    # red below whatever its text says, as on Runs.
    if latest is not None and latest.status == "partial" and _RATE.search(err):
        return "w", SLOWED_REASON, err
    partial = latest is not None and latest.status == "partial"
    if (src.consecutive_failures and not partial) or (
            latest is not None and latest.status == "failed"):
        if _MOVED.search(err):
            return "b", ("This company page moved. Kestrel could not find it at the saved "
                         "address."), err
        return "b", "Kestrel could not read this source. It tries again on the next check.", err
    if partial and (err or src.consecutive_failures):
        return "w", "Part of the last check did not finish. Kestrel tries again next check.", err
    quiet = [s["label"] for s in (sites or []) if s["count"] == 0]
    if quiet:
        return "w", f"{' and '.join(quiet)} found nothing this check.", ""
    return "g", "", ""


def body_lines(src: Source, counts: dict | None) -> list[dict]:
    """Per source (per board for a search) the share of the last run's new roles that had no
    description, with its sample size, and the plain warning when it jumped (issue 182). The
    state and the sentence come from one reading (`empty_bodies.Reading`)."""
    from kestrel.analytics.run_summary import _SITE_WORDS
    from kestrel.ingest import empty_bodies as eb

    bodies = counts.get(eb.COUNTS_KEY) if isinstance(counts, dict) else None
    if not isinstance(bodies, dict):
        return []
    out: list[dict] = []
    for key, raw in bodies.items():
        e = eb.clean_entry(raw)  # rebuilt from our own schema, never trusted as stored (D-SEC-20)
        if e is None or not isinstance(key, str) or not 0 < len(key) <= 40:
            continue
        new, empty = e["new"], e["empty"]
        reading = eb.Reading(new, empty, e["state"], None)
        if key == eb.WHOLE_SOURCE:
            label = str(src.name or "This source")
        else:
            label = str(_SITE_WORDS.get(key) or key.replace("_", " ").title())
        note = reading.warning(label)
        out.append({"label": label, "text": reading.share_text, "state": reading.state,
                    "warn": note.sentence if note else None})
    return out


def grade(src: Source, runs: list[ScrapeRun], sites: list[dict] | None) -> tuple[str, str, str]:
    """(key, reason, raw) exactly as the row pill shows them, for a source on or off: the one
    grading the Sources page and the menu's failing count both call (D-UI-904 review), so the
    rail count and the Needs you tile cannot diverge - a runner-paused three-failure row whose
    last check is partial grades Needs you here and is counted there."""
    auto_paused = not src.enabled and src.consecutive_failures >= 3 and src.origin != "retired"
    key, reason, raw = _health(src, runs, sites) if src.enabled else (
        ("b", AUTO_PAUSED_REASON, "") if auto_paused else ("m", "", ""))
    if not src.enabled and _no_boards(src):
        reason = _NO_BOARDS_REFUSED()  # off, and the row still says why and what to do
    return key, reason, raw


def build_view(
    src: Source, runs: list[ScrapeRun], *, summary: str = "", sites: list[dict] | None = None
) -> SourceView:
    """`runs` are this source's scrape runs, newest first."""
    from kestrel.services import run_model as _run_model

    origin = origin_of(src)
    key, reason, raw = grade(src, runs, sites)
    done = [r for r in runs if r.status != "running"]
    latest = done[0] if done else None
    counts = (latest.counts or {}) if latest is not None else {}
    when = when_words(src.last_run_at)
    if src.enabled:
        last = f"Checked {when}" if when else "Not checked yet"
    else:
        last = f"Off since {when}" if when else "Off"
    line = type_label(src)
    detail = summary or ""
    if not detail and src.kind == "jobspy":
        detail = str((src.config or {}).get("search_term") or "").strip('"')
    if not detail and src.kind in ("usajobs", "adzuna", "himalayas"):
        detail = str((src.config or {}).get("keyword") or "").strip('"')
    if src.kind in PLATFORMS and src.name:
        detail = ""
    chars = [run_char(r) for r in reversed(done[:10])]
    spark = "m" * (10 - len(chars)) + "".join(chars)
    code = ""
    if _no_boards(src):  # a saved flag, so the cause is known without a run
        code = failure_code.NO_BOARDS
    elif src.enabled and key in ("w", "b") and latest is not None:
        code = failure_code.stored_code(latest.counts) or ""  # an old run has none
    return SourceView(
        src=src, id=src.id, name=src.name, group=group_of(src), origin=origin,
        origin_label=ORIGINS[origin][0], type_line=f"{line} · {detail}" if detail else line,
        health=key, health_label=HEALTH[key][0], tone=HEALTH[key][1], reason=reason, raw=raw,
        last=last,
        open_n=counts.get("fetched") if latest is not None else None,
        new_n=int(counts.get("new") or 0), spark=spark, enabled=bool(src.enabled),
        removable=src.origin in ("custom", "suggested"), sites=sites or [],
        bodies=body_lines(src, counts),
        model_used=_run_model.label_of(counts) or "",
        unreadable=int(counts.get("unreadable") or 0), code=code,
    )


def health_counts(views: list[SourceView]) -> dict[str, int]:
    """Healthy, Slowed down and Needs you count what the operator must hear about: the on rows,
    and a search the runner switched off after three failed checks (issue 256) - the same rows
    the menu's failing_sources counts."""
    out = {"g": 0, "w": 0, "b": 0}
    for v in views:
        if v.health in out and (v.enabled or v.health == "b"):
            out[v.health] += 1
    return out


def ring(n: int, total: int) -> dict:
    """The tile ring: circumference and the dash offset for n of total."""
    import math

    c = 2 * math.pi * 21
    frac = (n / total) if total else 0.0
    return {"c": round(c, 2), "offset": round(c * (1 - frac), 2)}
