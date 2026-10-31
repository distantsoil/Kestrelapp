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
| `recruitee` | ATS board | employment | `{slug}.recruitee.com/api/offers/`, one request per scan (full descriptions included); a 404 is a failed run, a 429 a partial one | `slug` from `{slug}.recruitee.com`; optional `company_name` | 1/s; Kestrel UA; robots.txt checked | Worldwide |
| `teamtailor` | ATS board | employment | the career site's public `{domain}/jobs.json` feed (or `/jobs.rss` when there is no JSON), one request per scan, full descriptions included; a site with the feeds off is a partial run | `domain`: the career site's address (`example.teamtailor.com` or the employer's own); optional `company_name` | 1/s; Kestrel UA; robots.txt checked; public addresses only | Worldwide |
| `personio` | ATS board | employment | `{slug}.jobs.personio.de/xml` (or `.com`), one request for the list, plus one job page GET per role whose feed description is empty, up to `max_results` (default 100); a 429 is a partial run | `slug` from `{slug}.jobs.personio.de`; optional `tld` (`com`), `company_name`, `max_results` | 1 request per 2 s; Kestrel UA; robots.txt checked | Worldwide |
| `breezy` | ATS board | employment | `{slug}.breezy.hr/json` (one request, no descriptions) plus one posting-page GET per role (`/p/{friendly_id}`) for the description, up to `max_results` (default 100); a 429 is a partial run | `slug` from `{slug}.breezy.hr`; optional `company_name`, `max_results` | 1/s; Kestrel UA; robots.txt checked | Worldwide |
| `careers_page` | ATS board | employment | one company's careers page, read for the schema.org `JobPosting` JSON-LD it publishes for search engines (a list, an `@graph` or a single posting); a list page that only links to its job pages is followed one level, at most 25 job pages a run (the run says when links were left); a link that is a known hiring system is read by that system's reader instead; a page that builds its list with JavaScript is a partial run that says so | `url`: the careers page link; optional `company_name` | 1/s; Kestrel UA; robots.txt checked; public addresses only; 2 MB a page | Worldwide |
| `usajobs` | job board aggregator | employment | `data.usajobs.gov/api/search` (GET): one search per keyword (your profile's target titles when none is set), up to `max_pages` pages (default 3) of `results_per_page` (default 100, at most 500), posted in the last `date_posted_days` (default 7). Public announcements only; each role links to the official announcement | your own free USAJobs key (saved from the Add sheet or the USAJobs card; stored as `USAJOBS_API_KEY` in `.env`, never in the config) and the email you requested it with (`contact_email`); `keyword`, `location` (a place of "Remote" asks for remote roles), optional `remote_only` | 2/s and at most 12 requests a run; your email is the User-Agent, as USAJobs requires; robots.txt checked; a 429 or 5xx is a partial run, a missing key is a partial run that says so | US federal jobs, overseas postings included (your Region screens them) |
| `adzuna` | job board aggregator | employment | `api.adzuna.com/v1/api/jobs/us/search/{page}` (GET): one search per keyword (your profile's target titles when none is set), up to `max_pages` pages (default 2, at most 5) of `results_per_page` (default 50) posted in the last `date_posted_days` (default 7). The description is a snippet only and is marked partial; a predicted salary is not shown as posted pay; each role links to Adzuna | your own free Adzuna app ID and app key (saved from the Add sheet or the Adzuna card; stored as `ADZUNA_APP_ID_KEY` and `ADZUNA_APP_KEY` in `.env`, never in the config); `keyword`, `location` | 1/s and at most 8 requests a run (the free key allows about 250 calls a day); the API call skips robots.txt (D-SRC-15); a 429 or 5xx is a partial run, missing keys are a partial run that says so | US only |
| `himalayas` | job board aggregator | employment | `himalayas.app/jobs/api/search` (GET): one search per keyword (your profile's target titles when none is set), up to `max_pages` pages (default 3) of 20, only roles open to the US; expired jobs are skipped; the apply link is Himalayas' own and Himalayas is credited | nothing (no key); `keyword`, optional `max_pages` | 1/s and at most 12 requests a run; robots.txt checked; a 429 or 5xx is a partial run | Remote roles open to the US |
| `weworkremotely` | job board aggregator | employment | the public RSS feed `weworkremotely.com/remote-jobs.rss` (or one category feed): one request a check. Roles limited to another region and expired roles are skipped; each role links to its page on the board, which is credited | nothing (no key); optional `category` such as `remote-programming-jobs` | one request a check at 1/s; robots.txt checked; a 403 (the site's bot check), 429 or 5xx is a partial run | Remote roles open to the US |
| `remoterocketship` | paid API, discovery only | employment | `www.remoterocketship.com/api/openclaw/jobs/` (POST), at most `max_pages` requests (default 2, 50 jobs each). Keeps only each posting's employer link; a recognized company board becomes a found board, any other link is read from the employer's public page, and a link nothing can read is dropped and counted | your own paid RemoteRocketship API subscription; the key, saved from the RemoteRocketship card on the Sources page (stored as `REMOTEROCKETSHIP_API_KEY` in `.env`, never in the config); optional `max_pages`, `items_per_page`, `filters` (when none are set, your profile's target titles and onsite metros are used) | 1/s; Kestrel UA; the API call skips the robots check (D-SRC-14), employer pages are checked | Worldwide |
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
- Recruitee's list carries the full description and requirements, so a board is one request. A careers
  page on the company's own domain still has its `{slug}.recruitee.com` API; a role open in several
  places has several locations, joined with "; ".
- A careers page is only readable when the HTML itself carries the postings as `JobPosting` JSON-LD.
  Pages that fill their list in the browser (no JobPosting in the HTML) are reported as "lists its
  jobs with JavaScript" and read nothing. An expired `validThrough` role is skipped; a weekly pay
  figure is shown per year.
- Teamtailor has no shared API host, so the career site's own domain is the board. Some sites switch
  the feeds off (a 404) or refuse automated reads (a 403): the run is partial with a plain sentence, not
  failed. The JSON feed has no remote flag and gives the country as an ISO code, which Kestrel turns
  into a name.
- Personio's feed often has empty descriptions, so those roles' job pages are read (2 seconds apart, up to
  `max_results`, default 100); the rest are kept without a description and the run says how many. The
  feed's `office` has no country, so the page's place replaces it where the page was read. A 429 is a
  partial run, never a failure.
- Breezy HR's list has no descriptions, so each role's posting page is read (1 per second, up to
  `max_results`, default 100); the rest are kept without a description and the run says how many. The
  page address is built on the board's own host, and a 429 is a partial run, never a failure.
- RemoteRocketship's endpoint needs its trailing slash (`/api/openclaw/jobs/`; without it, a 308); the
  reply is read for each job's `url` only, and nothing else of it is stored.
- USAJobs wants the email you requested the key with as its User-Agent; the key goes in
  `Authorization-Key`. `NumberOfPages` is a string, `MatchedObjectId` is a string, pay amounts are
  strings, and `ApplyURI` is a list. Remote and telework are separate flags under `UserArea.Details`.
- Adzuna takes its keys in the query string (`app_id`, `app_key`), returns `salary_is_predicted` as
  "0" or "1", sends a description snippet only, and its robots.txt disallows everything (it is the
  published API). Himalayas wants `applicationLink` used for Apply, a visible credit, and expired
  jobs dropped. We Work Remotely's item title is "Company: Job title", its `country` is the
  employer's base (not who may apply), and every page but the feed answers a bot check.
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
