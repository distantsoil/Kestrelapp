"""The source catalog: one operator-facing description of every source KIND Kestrel can scan.

The LIST of kinds comes from the live registries (`kestrel.ingest.adapters` for the employment
adapters, `kestrel.ingest.signal_sources` for the consulting signal sources) plus the `page` and
`manual` kinds the `source` table accepts without an automated fetch. It is never a hand-copied
list: a kind that is registered but undescribed renders as a visibly undocumented row (and
tests/test_about_sources.py fails), and a description for a kind no registry knows is dropped, so
the Help page, docs/SOURCES.md and the Sources form cannot drift apart silently.

`how_to_add` for each kind matches what the Sources page (kestrel/web/pages/sources.py) actually
accepts today: the plain-words route first where there is one (the company board lookup, the
SAM.gov key field, the feed and portfolio forms, the profile-generated searches; D-SRC-3), then
the manual form under Advanced: a name, a track, the kind, a cadence in hours, and a JSON config
object that is checked with the same `validate_config` the scan runner uses. The example config
per kind is the smallest JSON the validator accepts."""

from __future__ import annotations

from dataclasses import dataclass, field

from kestrel.db.enums import SOURCE_KINDS
from kestrel.ingest.adapters import known_kinds
from kestrel.ingest.signal_sources import known_signal_kinds

CATEGORY_ATS = "ATS board"
CATEGORY_AGGREGATOR = "job board aggregator"
CATEGORY_FILING = "public filing"
CATEGORY_PROCUREMENT = "procurement"
CATEGORY_FEED = "feed"
CATEGORY_PE = "PE portfolio"
CATEGORY_MANUAL = "manual capture"

# The shared HttpClient default (settings.yml ingest.default_rate_per_sec) and the User-Agent
# policy every adapter inherits unless the kind says otherwise below.
_SHARED_UA = (
    "Descriptive Kestrel User-Agent from settings.yml (ingest.user_agent); robots.txt respected"
)

REGION_NOTE = (
    "The shipped configuration is US-centric. JobSpy defaults to country USA, SEC EDGAR and "
    "SAM.gov are US federal sources, the geography rules in filters.yml match US municipality "
    "names from the profile's geo_rules, and the example profile is a US city. Indeed, "
    "Glassdoor and every ATS board work worldwide: change the JobSpy search location (and "
    "country_indeed), and replace the municipality aliases in your profile's geo_rules, and the "
    "same pipeline runs for any region. There is no geocoder; geography is name matching only."
)

REQUEST_NEW_PROVIDER = (
    "A new ATS or job board is a code adapter, not a configuration entry. Each adapter owns one "
    "platform's field shapes behind the SourceAdapter contract in kestrel/ingest/base.py and is "
    "tested against a captured fixture, never a live endpoint. The steps and the file list are in "
    "docs/SOURCES.md under \"Adding an adapter\"."
)


@dataclass(frozen=True)
class SourceKindInfo:
    kind: str
    name: str
    category: str
    track: str  # "employment" | "consulting" | "either"
    fetches: str  # the public endpoint, in plain words
    provide: str  # what the operator must supply to add one
    config_example: dict = field(default_factory=dict)
    rate_limit: str = ""
    user_agent: str = _SHARED_UA
    region: str = ""
    how_to_add: str = ""
    scheduled: bool = True  # False: the kind exists but the scheduler never fetches it
    can_add_on_sources_page: bool = True
    documented: bool = True


_US_FEDERAL = "US federal source. Only meaningful for a US consulting search."
_WORLDWIDE = "Worldwide. The board's own location strings feed the geo rules unchanged."


_DESCRIPTIONS: dict[str, dict] = {
    "greenhouse": {
        "name": "Greenhouse",
        "category": CATEGORY_ATS,
        "track": "employment",
        "fetches": (
            "The public job-board JSON for one company: "
            "boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true. One request per scan, "
            "no pagination."
        ),
        "provide": (
            "The board token, which is the last path segment of the company's careers page "
            "(boards.greenhouse.io/{token})."
        ),
        "config_example": {"token": "examplecompany"},
        "rate_limit": "Shared default, 2 requests/second per host; one request per scan.",
        "region": _WORLDWIDE,
    },
    "lever": {
        "name": "Lever",
        "category": CATEGORY_ATS,
        "track": "employment",
        "fetches": (
            "The public postings JSON for one company: "
            "api.lever.co/v0/postings/{company}?mode=json. One request per scan. An unknown slug "
            "is a 404, reported as a partial run."
        ),
        "provide": "The company slug from the careers URL (jobs.lever.co/{company}).",
        "config_example": {"company": "examplecompany"},
        "rate_limit": "Shared default, 2 requests/second per host; one request per scan.",
        "region": _WORLDWIDE,
    },
    "ashby": {
        "name": "Ashby",
        "category": CATEGORY_ATS,
        "track": "employment",
        "fetches": (
            "The public job-board JSON for one organization: "
            "api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true. One request "
            "per scan."
        ),
        "provide": (
            "The organization's board id from its careers URL (jobs.ashbyhq.com/{token}). It is "
            "case sensitive. Optionally company_name for display."
        ),
        "config_example": {"token": "ExampleOrg"},
        "rate_limit": "Shared default, 2 requests/second per host; one request per scan.",
        "region": _WORLDWIDE,
    },
    "workday": {
        "name": "Workday",
        "category": CATEGORY_ATS,
        "track": "employment",
        "fetches": (
            "The public CXS job list (a POST, 20 items per page) and each posting's detail page "
            "under {tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/. One list request per "
            "20 jobs plus one detail request per job, up to max_results (default 200)."
        ),
        "provide": (
            "The tenant, the data-center segment (wd1, wd3, wd5, ...) and the site name, all read "
            "from the careers URL {tenant}.{dc}.myworkdayjobs.com/{site}. Optionally company and "
            "max_results."
        ),
        "config_example": {"tenant": "examplecorp", "dc": "wd5", "site": "ExampleCareers"},
        "rate_limit": "1 request/second (list and detail alike).",
        "user_agent": (
            "A browser User-Agent for Workday only, because many tenants refuse non-browser "
            "agents; robots.txt is still respected."
        ),
        "region": _WORLDWIDE,
    },
    "smartrecruiters": {
        "name": "SmartRecruiters",
        "category": CATEGORY_ATS,
        "track": "employment",
        "fetches": (
            "The public postings list, 100 per page, and each posting's detail under "
            "api.smartrecruiters.com/v1/companies/{company}/postings. One list request per 100 "
            "jobs plus one detail request per job, up to max_results (default 200)."
        ),
        "provide": (
            "The company identifier from the careers URL (careers.smartrecruiters.com/{company}). "
            "It is case sensitive. Optionally max_results."
        ),
        "config_example": {"company": "ExampleCo"},
        "rate_limit": "Shared default, 2 requests/second per host.",
        "region": _WORLDWIDE,
    },
    "rippling": {
        "name": "Rippling",
        "category": CATEGORY_ATS,
        "track": "employment",
        "fetches": (
            "The public job-board JSON for one company: "
            "api.rippling.com/platform/api/ats/v1/board/{slug}/jobs, plus one detail request per "
            "posting under .../jobs/{id} for the description and date, up to max_results "
            "(default 200). A role open in several places is one posting with all its locations."
        ),
        "provide": (
            "The board name from the careers URL (ats.rippling.com/{slug}/jobs). Optionally "
            "company_name and max_results."
        ),
        "config_example": {"slug": "examplecompany"},
        "rate_limit": "1 request/second (list and detail alike).",
        "region": _WORLDWIDE,
    },
    "adp": {
        "name": "ADP Workforce Now",
        "category": CATEGORY_ATS,
        "track": "employment",
        "fetches": (
            "The public career-center JSON for one employer: workforcenow.adp.com/mascsr/default/"
            "careercenter/public/events/staffing/v1/job-requisitions, 20 per page, plus one "
            "detail request per posting for the description, up to max_results (default 200). "
            "ADP publishes no robots.txt for this host, so Kestrel stays at one request a second."
        ),
        "provide": (
            "The employer's cid (a UUID) from its career-center link "
            "(workforcenow.adp.com/.../recruitment.html?cid={cid}). Optionally company_name, "
            "cc_id, lang and max_results."
        ),
        "config_example": {"cid": "00000000-0000-4000-8000-000000000000"},
        "rate_limit": "1 request/second (list and detail alike).",
        "region": _WORLDWIDE,
    },
    "jobspy": {
        "name": "Job boards via JobSpy",
        "category": CATEGORY_AGGREGATOR,
        "track": "either",
        "fetches": (
            "Public search results from Indeed and Glassdoor, plus ZipRecruiter, Google Jobs and "
            "Dice when they answer (ZipRecruiter and Google Jobs currently return nothing), "
            "through the python-jobspy library, run off the event loop. Nothing is fetched "
            "behind a login."
        ),
        "provide": (
            "A site list (site_name), a search term (search_term or google_search_term) and a "
            "location. Optional: distance, results_wanted, hours_old, job_type, is_remote, "
            "country_indeed. Indeed allows only one of hours_old, job_type+is_remote, or "
            "easy_apply per source."
        ),
        "config_example": {
            "site_name": ["indeed", "glassdoor"],
            "search_term": '"operations analyst"',
            "location": "Columbus, OH",
            "results_wanted": 100,
        },
        "rate_limit": (
            "Each board's own limits apply. A 429 "
            "makes the run partial and never aborts the batch."
        ),
        "user_agent": "JobSpy's own request headers; the shared Kestrel agent is not used here.",
        "region": (
            "Defaults to country USA (country_indeed). Set location and country_indeed for any "
            "other region; Indeed is worldwide."
        ),
    },
    "sec_edgar": {
        "name": "SEC EDGAR",
        "category": CATEGORY_FILING,
        "track": "consulting",
        "fetches": (
            "The EDGAR full-text search (efts.sec.gov/LATEST/search-index) for each query phrase, "
            "then the matching public filing documents under www.sec.gov/Archives. 8-K Item 5.02 "
            "(officer departures) and Item 1.05 (cybersecurity incidents) are the trigger events."
        ),
        "provide": (
            "A list of search phrases (queries). Optional: forms (default 8-K), max_results per "
            "query, and a user_agent override. A real contact address in the User-Agent is "
            "mandatory; a placeholder is refused before the first request."
        ),
        "config_example": {"queries": ['"completion of acquisition"'], "forms": ["8-K"]},
        "rate_limit": (
            "SEC caps at 10 requests/second per IP; Kestrel sends at most 5 per second to each of "
            "the two hosts and backs off on 403."
        ),
        "user_agent": (
            "Descriptive agent with a real contact address, required by SEC policy (source "
            "user_agent, else settings.yml ingest.user_agent)."
        ),
        "region": _US_FEDERAL,
    },
    "sam_gov": {
        "name": "SAM.gov",
        "category": CATEGORY_PROCUREMENT,
        "track": "consulting",
        "fetches": (
            "The SAM.gov Opportunities API (api.sam.gov/opportunities/v2/search), one request per "
            "NAICS code over the last lookback_days (default 30). Each notice becomes a consulting "
            "posting directly, with no model involved."
        ),
        "provide": (
            "A free SAM.gov API key. Sign in at sam.gov with a Login.gov account, open Account "
            "Details (or Workspace), request a Public API Key, and paste it under Sources > "
            "Advanced > SAM.gov; it is saved as SAM_GOV_API_KEY in Kestrel's .env. NAICS codes "
            "(naics) default to 541512, 541611, 541519. Optional: limit, lookback_days, ptype."
        ),
        "config_example": {"api_key_env": "SAM_GOV_API_KEY", "naics": ["541512", "541611"]},
        "rate_limit": "2 requests/second; the key travels in a header, never in a URL.",
        "region": _US_FEDERAL,
    },
    "rss": {
        "name": "RSS and Atom feeds",
        "category": CATEGORY_FEED,
        "track": "consulting",
        "fetches": (
            "Each configured public feed (company newsrooms, layoff trackers). Every entry is a "
            "trigger candidate the model classifies. A feed that declares a DOCTYPE or ENTITY is "
            "refused; a broken feed is skipped, never fatal."
        ),
        "provide": "A list of feed URLs (feeds). Optional: rate_per_sec.",
        "config_example": {"feeds": ["https://example.com/newsroom/rss"]},
        "rate_limit": "2 requests/second per host by default.",
        "region": "Worldwide; any public feed.",
    },
    "pe_portfolio": {
        "name": "PE portfolio pages",
        "category": CATEGORY_PE,
        "track": "consulting",
        "fetches": (
            "One public portfolio page per configured private-equity firm, read for the list of "
            "portfolio companies. Nothing beyond the configured page is fetched, and a robots.txt "
            "disallow skips the firm."
        ),
        "provide": "A list of firms, each with a name and the portfolio page url.",
        "config_example": {
            "firms": [{"name": "Example Capital", "url": "https://example-capital.com/portfolio"}]
        },
        "rate_limit": "1 request/second per firm host.",
        "region": "Worldwide; any public portfolio page.",
    },
    "manual": {
        "name": "Manual capture",
        "category": CATEGORY_MANUAL,
        "track": "either",
        "fetches": (
            "Nothing on its own. The browser extension's capture, the MCP ingest tool and pasted "
            "text land here as one opportunity each, deduplicated against everything already seen."
        ),
        "provide": "Nothing to configure. Kestrel creates the row on the first capture.",
        "rate_limit": "Not applicable; no requests are made.",
        "user_agent": "Not applicable.",
        "region": "Any.",
        "scheduled": False,
    },
    "import": {
        "name": "Applied history import",
        "category": CATEGORY_MANUAL,
        "track": "either",
        "fetches": (
            "Nothing on its own. Rows from Settings > Applied (a CSV or Markdown table of "
            "applications made before Kestrel) land here, one hidden opportunity and one pursuit "
            "each, so re-posts of an applied role are recognized."
        ),
        "provide": "Nothing to configure. Kestrel creates the row on the first import.",
        "rate_limit": "Not applicable; no requests are made.",
        "user_agent": "Not applicable.",
        "region": "Any.",
        "scheduled": False,
    },
    "page": {
        "name": "Single page (reserved)",
        "category": CATEGORY_MANUAL,
        "track": "either",
        "fetches": (
            "Nothing. The kind is accepted by the source table for dedup precedence but no adapter "
            "fetches it in this build; use Manual capture to bring in a page you are looking at."
        ),
        "provide": "Nothing. Adding a page source has no effect on scans.",
        "rate_limit": "Not applicable.",
        "user_agent": "Not applicable.",
        "region": "Any.",
        "scheduled": False,
    },
}


def _how_to_add(kind: str, info: dict, can_add: bool) -> str:
    if not info.get("scheduled", True):
        return (
            "Not added by hand. "
            + ("The Sources page accepts the kind but nothing scans it." if can_add else "")
        ).strip()
    if not can_add:
        return (
            f"The adapter is registered but the Sources page cannot add kind '{kind}' because it "
            "is missing from SOURCE_KINDS in kestrel/db/enums.py."
        )
    example = info.get("config_example") or {}
    keys = ", ".join(example) if example else "no keys"
    lead = _EASY_ADD.get(kind, "")
    return (
        f"{lead}Sources > Add source (under Advanced): name it, pick the track, choose kind "
        f"'{kind}', set a cadence in hours, and paste a JSON config with {keys}. The same "
        "validator the scan runner uses checks it before the row is written."
    )


# The plain-words way to add each kind on the Sources page (D-SRC-3), ahead of the manual form.
_LOOKUP = (
    "Easiest: Sources > Advanced > Watch a company's job board, type the company name, and "
    "Kestrel finds the board. By hand: "
)
_EASY_ADD = {
    "greenhouse": _LOOKUP,
    "lever": _LOOKUP,
    "ashby": _LOOKUP,
    "smartrecruiters": _LOOKUP,
    "jobspy": (
        "Usually nothing to do: your searches are built from Profile > Employment and Location "
        "and listed under Sources > Your searches. For an extra search of your own: "
    ),
    "sec_edgar": (
        "Usually nothing to do: with consulting on and an email on About you, the filings watch "
        "is one of your searches. By hand: "
    ),
    "sam_gov": (
        "Easiest: Sources > Advanced > SAM.gov, paste your API key, and it is switched on. "
        "By hand: "
    ),
    "rss": "Easiest: Sources > Advanced > News feeds, one address per line. By hand: ",
    "pe_portfolio": (
        "Easiest: Sources > Advanced > PE portfolio page, the firm and its page. By hand: "
    ),
}


def _ordered_kinds() -> list[str]:
    """Registered adapter kinds, then registered signal kinds, then the table-only kinds, each in
    a stable operator-facing order (ATS boards before the aggregator; triggers before feeds)."""
    preferred = [
        "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "rippling", "adp", "jobspy",
        "sec_edgar", "sam_gov", "rss", "pe_portfolio", "manual", "import", "page",
    ]
    registered = set(known_kinds()) | set(known_signal_kinds())
    table_only = [k for k in SOURCE_KINDS if k not in registered]
    live = [k for k in preferred if k in registered or k in table_only]
    live += sorted(k for k in registered if k not in live)
    live += [k for k in table_only if k not in live]
    return live


def source_catalog() -> list[SourceKindInfo]:
    """Every kind Kestrel knows, built from the registries. A registered kind without a description
    is still listed, flagged `documented=False`, so a new adapter is never silently omitted."""
    rows: list[SourceKindInfo] = []
    for kind in _ordered_kinds():
        can_add = kind in SOURCE_KINDS
        info = _DESCRIPTIONS.get(kind)
        if info is None:
            rows.append(
                SourceKindInfo(
                    kind=kind,
                    name=kind,
                    category="undocumented",
                    track="either",
                    fetches="No description yet; see the adapter module docstring.",
                    provide="See the adapter's validate_config.",
                    how_to_add=_how_to_add(kind, {}, can_add),
                    can_add_on_sources_page=can_add,
                    documented=False,
                )
            )
            continue
        rows.append(
            SourceKindInfo(
                kind=kind,
                name=info["name"],
                category=info["category"],
                track=info["track"],
                fetches=info["fetches"],
                provide=info["provide"],
                config_example=dict(info.get("config_example") or {}),
                rate_limit=info.get("rate_limit", ""),
                user_agent=info.get("user_agent", _SHARED_UA),
                region=info.get("region", ""),
                how_to_add=_how_to_add(kind, info, can_add),
                scheduled=bool(info.get("scheduled", True)),
                can_add_on_sources_page=can_add,
            )
        )
    return rows


def described_kinds() -> frozenset[str]:
    """The kinds this module has prose for; tests compare it against the registries."""
    return frozenset(_DESCRIPTIONS)
