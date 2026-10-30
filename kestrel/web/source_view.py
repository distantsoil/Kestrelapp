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
}
_SIGNAL_TYPE = {
    "sec_edgar": "SEC filings", "sam_gov": "SAM.gov", "pe_portfolio": "PE portfolio page",
    "remoterocketship": "RemoteRocketship", "usajobs": "USAJobs", "adzuna": "Adzuna",
    "himalayas": "Himalayas", "weworkremotely": "We Work Remotely",
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
_RATE = re.compile(r"\b429\b|too many requests|rate.?limit|slow down", re.I)
_MOVED = re.compile(r"\b(404|410)\b|not found|moved", re.I)

REMOVED_KEY = "_removed"  # config marker: the source was removed and Undo can bring it back


def is_removed(src: Source) -> bool:
    return bool((src.config or {}).get(REMOVED_KEY))


BOARD_KINDS = ("jobspy", "remoterocketship", "usajobs", "adzuna", "himalayas", "weworkremotely")
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


def run_char(run: ScrapeRun) -> str:
    """One check as a sparkline bar: g healthy, w slowed or partly done, b failed."""
    if run.status == "ok":
        return "g"
    if run.error and _RATE.search(run.error):
        return "w"
    return "b" if run.status == "failed" else "w"


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
    model_used: str = ""
    unreadable: int = 0

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
    if latest is not None and latest.status != "ok" and _RATE.search(err):
        return "w", SLOWED_REASON, err
    if src.consecutive_failures or (latest is not None and latest.status == "failed"):
        if _MOVED.search(err):
            return "b", ("This company page moved. Kestrel could not find it at the saved "
                         "address."), err
        return "b", "Kestrel could not read this source. It tries again on the next check.", err
    if latest is not None and latest.status == "partial" and err:
        return "w", "Part of the last check did not finish. Kestrel tries again next check.", err
    quiet = [s["label"] for s in (sites or []) if s["count"] == 0]
    if quiet:
        return "w", f"{' and '.join(quiet)} found nothing this check.", ""
    return "g", "", ""


def build_view(
    src: Source, runs: list[ScrapeRun], *, summary: str = "", sites: list[dict] | None = None
) -> SourceView:
    """`runs` are this source's scrape runs, newest first."""
    from kestrel.services import run_model as _run_model

    origin = origin_of(src)
    key, reason, raw = _health(src, runs, sites) if src.enabled else ("m", "", "")
    if not src.enabled and _no_boards(src):
        reason = _NO_BOARDS_REFUSED()  # off, and the row still says why and what to do
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
    return SourceView(
        src=src, id=src.id, name=src.name, group=group_of(src), origin=origin,
        origin_label=ORIGINS[origin][0], type_line=f"{line} · {detail}" if detail else line,
        health=key, health_label=HEALTH[key][0], tone=HEALTH[key][1], reason=reason, raw=raw,
        last=last,
        open_n=counts.get("fetched") if latest is not None else None,
        new_n=int(counts.get("new") or 0), spark=spark, enabled=bool(src.enabled),
        removable=src.origin in ("custom", "suggested"), sites=sites or [],
        model_used=_run_model.label_of(counts) or "",
        unreadable=int(counts.get("unreadable") or 0),
    )


def health_counts(views: list[SourceView]) -> dict[str, int]:
    """Healthy, Slowed down and Needs you count only sources that are on."""
    out = {"g": 0, "w": 0, "b": 0}
    for v in views:
        if v.enabled and v.health in out:
            out[v.health] += 1
    return out


def ring(n: int, total: int) -> dict:
    """The tile ring: circumference and the dash offset for n of total."""
    import math

    c = 2 * math.pi * 21
    frac = (n / total) if total else 0.0
    return {"c": round(c, 2), "offset": round(c * (1 - frac), 2)}
