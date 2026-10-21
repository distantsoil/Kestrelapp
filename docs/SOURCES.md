# Where the opportunities come from

Kestrel scans public sources only. Each source has a *kind*, and the kinds are fixed in code: the
employment adapters in `kestrel/ingest/adapters/` and the consulting signal sources in
`kestrel/ingest/signal_sources/`. The Help page in the app renders this same list live from the
registries (`kestrel/ingest/catalog.py`), so if this document and the app disagree, the app is
right and this file needs updating.

Every kind is added the same way on the Sources page: a name, a track, the kind, a cadence in
hours, and a JSON config. The config is checked with the adapter's own `validate_config`, the same
one the scan runner calls, so a config that would fail at scan time is refused before it is saved.
The only row shipped in `kestrel/resources/config/sources.yml` is SAM.gov (disabled, and it needs a
free key). Your job searches and SEC filings watch are generated from your profile, and company
boards, feeds and portfolio pages you add yourself; the config shapes are listed below.

## Region

The shipped configuration is US-centric. JobSpy defaults to country USA; SEC EDGAR and SAM.gov are
US federal sources; the geography rules in `filters.yml` match US municipality names from the
profile's `geo_rules`; and the example profile is a US city. Indeed, Glassdoor and every ATS
board work worldwide. To run the same pipeline elsewhere, change the JobSpy `location` and
`country_indeed`, and replace the municipality aliases in your profile's `geo_rules`. There is no
geocoder and there will not be one: geography is name matching, never distance.

## The kinds

| Kind | Category | Track | What it scans | What you provide | Rate limit and User-Agent | Region |
|---|---|---|---|---|---|---|
| `greenhouse` | ATS board | employment | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`, one request per scan | `token`: the board token from `boards.greenhouse.io/{token}` | Shared default 2/s per host; Kestrel UA | Worldwide |
| `lever` | ATS board | employment | `api.lever.co/v0/postings/{company}?mode=json`, one request per scan; a 404 is a partial run | `company`: the slug from `jobs.lever.co/{company}` | Shared default 2/s; Kestrel UA | Worldwide |
| `ashby` | ATS board | employment | `api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` | `token` (case sensitive) from `jobs.ashbyhq.com/{token}`; optional `company_name` | Shared default 2/s; Kestrel UA | Worldwide |
| `workday` | ATS board | employment | CXS job list (POST, 20 per page) plus one detail GET per job under `{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/` | `tenant`, `dc` (wd1, wd3, wd5, ...), `site`; optional `company`, `max_results` (default 200) | 1/s; a browser UA because many tenants refuse others; robots.txt respected | Worldwide |
| `smartrecruiters` | ATS board | employment | `api.smartrecruiters.com/v1/companies/{company}/postings` (100 per page) plus one detail GET per job | `company` (case sensitive) from `careers.smartrecruiters.com/{company}`; optional `max_results` | Shared default 2/s; Kestrel UA | Worldwide |
| `rippling` | ATS board | employment | `api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` plus one detail GET per posting; a role open in several places is one posting | `slug` from `ats.rippling.com/{slug}/jobs`; optional `company_name`, `max_results` (default 200) | 1/s; Kestrel UA | Worldwide |
| `adp` | ATS board | employment | the public ADP Workforce Now career-center JSON (`workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions`, 20 per page) plus one detail GET per posting | `cid` (UUID) from the career-center link `recruitment.html?cid={cid}`; optional `company_name`, `cc_id`, `lang`, `max_results` (default 200) | 1/s; Kestrel UA; the host publishes no robots.txt | Worldwide |
| `paylocity` | ATS board | employment | the employer's public careers page `recruiting.paylocity.com/recruiting/jobs/All/{board_id}` (its embedded page data lists the board) plus one posting-page GET per job | `board_id` (GUID) from the careers link; optional `company_name`, `max_results` (default 200) | 1/s; Kestrel UA; the host publishes no robots.txt rules | Worldwide |
| `workable` | ATS board | employment | `apply.workable.com/api/v3/accounts/{slug}/jobs` (POST) plus one detail GET per posting under `/api/v2/accounts/{slug}/jobs/{shortcode}` | `slug` from `apply.workable.com/{slug}/`; optional `company_name`, `max_results` (default 200) | 1/s; Kestrel UA; robots.txt checked | Worldwide |
| `jobspy` | job board aggregator | either | Indeed and Glassdoor (ZipRecruiter and Google Jobs currently return nothing) through `python-jobspy`, run off the event loop | `site_name`, `search_term` or `google_search_term`, `location`; optional `distance`, `results_wanted`, `hours_old`, `job_type`, `is_remote`, `country_indeed`. Indeed takes only one of `hours_old`, `job_type`+`is_remote`, `easy_apply`. LinkedIn is not offered and a source naming it is refused (D-SRC-5) | Each board's own limits; a 429 is a partial run. JobSpy's own headers | Defaults to USA via `country_indeed`; set `location` for any region |
| `sec_edgar` | public filing | consulting | EDGAR full-text search `efts.sec.gov/LATEST/search-index` per query, then the matching documents under `www.sec.gov/Archives`. 8-K Items 5.02 and 1.05 are the triggers | `queries` (list of phrases); optional `forms` (default 8-K), `max_results`, `user_agent` | SEC caps at 10/s per IP; Kestrel sends at most 5/s per host and backs off on 403. A descriptive UA with a real contact address is mandatory; a placeholder is refused | US federal |
| `sam_gov` | procurement | consulting | `api.sam.gov/opportunities/v2/search`, one request per NAICS code over `lookback_days` (default 30); each notice becomes a consulting posting with no model involved | a free SAM.gov key as `api_key` or the `SAM_GOV_API_KEY` variable in `.env`; `naics` (default 541512, 541611, 541519); optional `limit`, `lookback_days`, `ptype` | 2/s; the key travels in a header, never a URL | US federal |
| `rss` | feed | consulting | each configured public RSS or Atom feed; every entry is a trigger candidate. Feeds declaring a DOCTYPE or ENTITY are refused; a broken feed is skipped | `feeds` (list of URLs); optional `rate_per_sec` | 2/s per host; Kestrel UA | Worldwide |
| `pe_portfolio` | PE portfolio | consulting | one public portfolio page per configured firm, read for the company list; nothing beyond that page is fetched and a robots.txt disallow skips the firm | `firms`: list of `{name, url}` | 1/s per firm host; Kestrel UA; robots.txt enforced | Worldwide |
| `manual` | manual capture | either | nothing on its own; the browser extension, the MCP ingest tool and pasted text land here one opportunity at a time | nothing; Kestrel creates the row on first capture | none | any |
| `import` | manual capture | either | nothing on its own; rows from Settings > Applied (a CSV or Markdown table of applications made before Kestrel) land here as one hidden opportunity and one pursuit each, so re-posts of an applied role are recognized | nothing; Kestrel creates the row on the first import | none | any |
| `page` | manual capture | either | nothing; the kind exists in the source table for dedup precedence but no adapter fetches it in this build | nothing; adding one has no effect | none | any |

The Sources page can add every kind above, because all of them are in `SOURCE_KINDS`
(`kestrel/db/enums.py`). Adding `manual`, `import` or `page` by hand is accepted but pointless: nothing
scans them.

Minimal configs that pass validation:

```json
{"token": "examplecompany"}                                        // greenhouse, ashby
{"company": "examplecompany"}                                      // lever, smartrecruiters
{"tenant": "examplecorp", "dc": "wd5", "site": "ExampleCareers"}  // workday
{"site_name": ["indeed", "glassdoor"], "search_term": "\"vice president\" IT", "location": "Columbus, OH"}
{"queries": ["\"chief information officer\""], "forms": ["8-K"]}   // sec_edgar
{"api_key_env": "SAM_GOV_API_KEY", "naics": ["541512", "541611"]}  // sam_gov
{"feeds": ["https://example.com/newsroom/rss"]}                    // rss
{"firms": [{"name": "Example Capital", "url": "https://example-capital.com/portfolio"}]}
```

## What every source respects

Hard rule 7 of `CLAUDE.md`: public filings and public pages only, never behind a login. Every
request carries the descriptive User-Agent from `settings.yml` (`ingest.user_agent`) unless the kind
says otherwise above, is rate-limited per host through one shared HTTP client, and honors
`robots.txt`. A fetch failure makes the run `partial` and never aborts the batch; three consecutive
failures disable the source until you re-enable it.

Hard rule 1: no source ever acts for you. Scanning reads; it never applies, messages, or submits.

## Adding an adapter

A new ATS or job board is a code adapter, not a configuration entry. The work is one module, one
fixture, one test, and a registry line.

### The contract (`kestrel/ingest/base.py`)

```python
class SourceAdapter(Protocol):
    kind: str
    def validate_config(self, config: dict) -> None: ...        # pure, raises SourceConfigError
    async def fetch(self, config: dict, client: Any) -> list[RawPosting]: ...
```

- `kind` is the string stored in `source.kind`. It must also be added to `SOURCE_KINDS` in
  `kestrel/db/enums.py` (otherwise the Sources page cannot offer it) and to the dedup precedence
  table in `kestrel/ingest/dedup.py`.
- `validate_config` is structural only: required keys present, parameter combinations legal. It
  raises `SourceConfigError` and never touches the network. The Sources page calls it before saving.
- `fetch` does the I/O through the shared `client` (`kestrel/ingest/http.py`: `get_json`,
  `get_text`, per-host rate limit, robots.txt, the configured User-Agent) and returns
  `RawPosting` objects. Fill what the payload provides and leave the rest `None`; never guess a
  salary, a date, or a remote flag. A recoverable failure (HTTP error, bad slug, shape change) raises
  `AdapterError`, which makes the run `partial`. Nothing in an adapter touches the database;
  `normalize.py` owns canonicalization, dedup, and persistence.
- Consulting trigger sources implement the parallel `SignalSource` protocol in
  `kestrel/ingest/signal_base.py` and return `RawSignal` objects instead.

### Fixture first

Tests never call a live endpoint. Capture one real payload, replace identifying data, save it as
`tests/fixtures/<kind>.json`, and test the adapter against that with a fake client that returns the
fixture. `tests/test_lever_adapter.py` is the pattern. Assert the field mapping, the gotchas below,
and that `validate_config` refuses each missing required key.

### Field-shape traps already paid for

- Lever's job title is `text`, not `title`; `createdAt` is epoch milliseconds.
- Greenhouse's `location` is an object `{"name": ...}`; `content` is HTML-escaped HTML.
- Workday's job list is a POST capped at 20 per page with no description; the detail is a
  separate GET, and `postedOn` is a human string, not a date.
- SmartRecruiters' title is `name`; the list carries no description; identifiers are case
  sensitive.
- Rippling's list repeats a posting once per location (same `uuid`); the title is `name` with stray
  spaces; the description and date are only in the detail.
- ADP's career-center pages hold 20 rows and `$skip` is 1-based (1, 21, 41); its location list
  repeats and holds blank entries; the payload names no employer.
- Paylocity has no JSON list: the board is JSON inside the careers page (`window.pageData`), the
  list's description is only a snippet, and the posting page's `datePosted` has a wrong UTC offset.
- Workable's list is a POST with body `{}`; internal and unpublished results are skipped; the body is
  `description`, `requirements` and `benefits` from the detail. Its pagination is unverified.
- Ashby's board token is case sensitive; compensation is absent, not zero, unless the board shows
  it.
- JobSpy is synchronous: always `asyncio.to_thread()`. Indeed allows one filter group per search.
- SEC EDGAR blocks requests without a descriptive User-Agent that carries a contact address, and
  caps at 10 requests per second.

### Files to touch

1. `kestrel/ingest/adapters/<kind>.py` (or `kestrel/ingest/signal_sources/<kind>.py`): the adapter.
2. `kestrel/ingest/adapters/__init__.py` (or `signal_sources/__init__.py`): register an instance.
3. `kestrel/db/enums.py`: add the kind to `SOURCE_KINDS`.
4. `kestrel/ingest/dedup.py`: add the kind's precedence.
5. `kestrel/ingest/catalog.py`: add the operator-facing description (the Help page test fails
   until you do, by design).
6. Add the config shape to the table above. Do not add a row to
   `kestrel/resources/config/sources.yml` unless the kind needs one shipped (only SAM.gov does).
7. `tests/fixtures/<kind>.json` and `tests/test_<kind>_adapter.py`.
8. This file, and `SPEC.md` Appendix A with the verified payload shape.

Run `ruff check` and the test suite, then record the decision in the private decision log.
