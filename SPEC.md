# Kestrel — Core Specification

> A local-LLM career operations engine, packaged as a double-clickable Windows desktop
> application. It ingests opportunities across two tracks (permanent employment and fractional
> or interim consulting), filters and scores them in tiers, tailors parametric documents per
> opportunity, tracks everything through to outcome, and learns from the operator's own
> decisions.
>
> Named for the bird that hovers, scans a wide field, and strikes rarely.

**Version:** 1.0 — complete scope. There is no "v2 backlog" in this specification. Everything
intended to exist is described here.
**Target host:** Windows 11 desktop. a 16GB-VRAM NVIDIA GPU, a modern multi-core CPU, 64GB+ RAM,
Ollama installed.
**Spec authored:** 2026-07-30. All external API shapes cited were verified live on that date.

## Document set

| File | Contains |
|---|---|
| **`SPEC.md`** (this) | Principles, stack, packaging, data model, ingestion, pipeline, scoring |
| **`SPEC-MODULES.md`** | Documents, research, interview, negotiation, learning loop, intelligence, analytics |
| **`SPEC-INTERFACES.md`** | Desktop shell, web UI, REST API, MCP server, browser extension, notifications, CLI |
| **`PROMPTS.md`** | Every LLM prompt, schema, and failure mode |
| **`CLAUDE.md`** | Standing orders for the build agent |
| **`BUILD_PROMPT.md`** | Phased build instructions |

---

## 0. Design principles

Non-negotiable. Every downstream decision defers to these.

1. **Throughput over latency.** Nothing here is interactive-realtime. A run may take hours.
   Optimize for jobs processed per run and for resumability, never for time-to-first-token.

2. **Spend compute where it changes a decision.** A filter costing 0.1ms runs before a model
   call costing 10 seconds. Tiering is the architecture, not an optimization.

3. **The model selects, it does not invent.** Every claim about the operator that reaches a PDF
   traces to text the operator wrote. The LLM chooses among pre-authored variants and reorders
   emphasis. It never generates a new achievement, metric, title, or date.

4. **Structured output or failure.** Every model call returns schema-validated JSON. One repair
   attempt, then quarantine with the raw output preserved. Never regex-parse a model response.

5. **Resumable by construction.** Every unit of work is a queue row. Kill the process at any
   moment, restart, continue. No in-memory-only state that matters.

6. **The system never acts externally on the operator's behalf.** It never submits an
   application, never sends an email or message, never clicks apply, never automates a social
   platform. It drafts, recommends, and hands over. This is a hard boundary, decided
   deliberately, and it is not to be relaxed.

7. **Model backend is a URL.** One OpenAI-compatible client, configured per tier by base URL,
   model, and key. Ollama, oMLX, llama.cpp, or a hosted API are all valid targets.

8. **Single user, single machine.** No multi-tenancy, no accounts, no horizontal scaling.
   SQLite is correct here. Do not introduce Postgres, Redis, Celery, or Kubernetes.

9. **It is an application, not a script.** The operator double-clicks an icon. Everything is
   reachable from the window. The CLI exists for debugging and for the build agent's own
   verification, not as the product.

10. **The system improves from use.** Every override, every correction, every outcome is
    captured and fed back. A career tool that scores the same on day 200 as on day 1 has wasted
    two hundred days of signal.

---

## 1. Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | JobSpy is Python; one toolchain end to end |
| Desktop shell | **`pywebview`** over Edge WebView2 | WebView2 ships with Windows 11. No Node, no Rust, no Electron. Same codebase serves a browser |
| Packaging | **PyInstaller** one-folder + **Inno Setup** installer | One-folder avoids the antivirus false positives that plague onefile |
| Tray | `pystray` + `Pillow` | Worker keeps churning with the window closed. Essential for multi-hour runs |
| Web framework | FastAPI + Uvicorn, bound to loopback on a free port | Already the house pattern |
| Templating | Jinja2, server-rendered | No SPA build toolchain |
| Interactivity | HTMX 2.x + Alpine.js 3.x, **vendored locally** | Must work offline inside a packaged app. Do not use CDNs |
| Styling | Tailwind CSS, **prebuilt stylesheet vendored** | Play CDN needs network. Generate once at build time, commit the CSS |
| Database | SQLite WAL, SQLAlchemy 2.x async (`aiosqlite`) + Alembic | Alembic runs a sync engine against the same file |
| Queue | DB-backed `task` table, in-process async worker | Restart-safe, inspectable with SQL |
| Rule expressions | `simpleeval` | Tier 0 `when:` clauses. Sandboxed, never `eval()`. **No generator expressions; see §6.1** |
| PDF verification | `pypdf` | Phase gates assert page count, text layer, and empty metadata |
| Scheduler | `APScheduler` (`AsyncIOScheduler`) | Recurring scans, in-process |
| Board scraping | `python-jobspy` | **Synchronous / `requests`-based. Always `asyncio.to_thread()`** |
| API scraping | `httpx` | Six ATS platforms plus signal sources, all public JSON |
| Browser automation | Playwright, **`channel="msedge"`** | Drives the Edge already on the machine. Avoids bundling a 150MB Chromium |
| HTML to markdown | `markdownify` | ATS descriptions arrive as HTML |
| PDF | Playwright `page.pdf()` from rendered HTML | ATS-safe selectable text layer |
| Inference | `httpx` against `/v1/chat/completions` | Provider-agnostic |
| Embeddings | Ollama `nomic-embed-text` via `/v1/embeddings`, **or in-process**: bundled `all-MiniLM-L6-v2` ONNX on `onnxruntime` CPU + `tokenizers` (§8.1.1) | Dedup, retrieval, few-shot selection, reranker features. Never hosted; the in-process engine is what a BYOK install with no Ollama uses |
| Learned reranker | **`scikit-learn` `HistGradientBoostingRegressor`** | No torch. Small, fast, bundles cleanly, correct tool for tabular plus embedding features |
| MCP server | `fastmcp` | Mirrors the operator's existing `copilot-mcp` pattern |
| Notifications | `win11toast` locally, optional `ntfy` webhook | Native toast plus phone push for long runs |
| Token counting | `tiktoken` with a **vendored** `cl100k_base` encoding file | First use otherwise downloads from Azure blob storage, which breaks the offline requirement. Ship the file and set `TIKTOKEN_CACHE_DIR` |
| Config | `pydantic-settings`, `.env` + YAML + DB overrides | |
| Logging | `structlog`, rotating JSON lines | |
| Tests | `pytest`, `pytest-asyncio`, `respx`, `playwright` | |
| Dependency mgmt | `uv` | Fast and Windows-clean |

**Explicitly excluded:** React/Vue/Svelte, Node build tooling, Electron, Tauri, Rust, Redis,
Celery, Postgres, LaTeX, PyTorch, any hosted vector database, any auth provider, any CDN
dependency at runtime.

---

## 2. Packaging and distribution

The deliverable is `Kestrel-Setup-x.y.z.exe`. The operator runs it once, then launches Kestrel
from the Start menu or a desktop shortcut. Full detail is in `SPEC-INTERFACES.md` §1; the
constraints that shape the whole codebase are here.

**Paths.** Nothing writable may live beside the executable. Program Files is read-only.

| Purpose | Location |
|---|---|
| Application code | wherever the installer put it, read-only |
| Database, PDFs, raw payloads, models metadata | `%LOCALAPPDATA%\Kestrel\data\` |
| Config (`settings.yml`, `profile.yml`, `sources.yml`, `filters.yml`) | `%LOCALAPPDATA%\Kestrel\config\` |
| Logs | `%LOCALAPPDATA%\Kestrel\logs\` |
| Secrets (`.env`) | `%LOCALAPPDATA%\Kestrel\.env` |

Resolve these through one `paths.py` module from day one. Never hardcode a relative path.
On first run, seed the config directory from packaged defaults if it is empty.

**Frozen-app constraints the code must respect:**

- Alembic migration scripts are **package data**. They must be bundled and migrations run
  programmatically at startup, not via a `alembic` shell command.
- Jinja templates, static assets, vendored HTMX/Alpine/Tailwind, prompt files, and Modelfiles
  are all package data. Access them through `importlib.resources`, never `__file__`-relative
  path arithmetic.
- `sys.frozen` detection selects packaged vs. development paths.
- Playwright uses `channel="msedge"`. If Edge is somehow absent, fall back to prompting a
  one-time `playwright install chromium` with a progress dialog. Do not bundle Chromium.
- Single-instance lock via a named mutex. A second launch focuses the existing window.
- Graceful shutdown: closing the window hides to tray (default; optional, SPEC-INTERFACES 1.3); quitting from the tray stops the worker
  cleanly, leaving all in-flight tasks re-leasable.

**Preflight on first launch.** A guided setup window, not a stack trace:
Ollama reachable, required models present (offer to pull with progress), derived `num_ctx`
models built, Edge present, database migrated, profile configured. Each step passes, warns with
a remediation, or offers a one-click fix.

---

## 3. The two tracks

The operator runs two parallel searches with different economics and different signals.

| | **Employment track** | **Consulting track** |
|---|---|---|
| Seeking | Permanent roles in your field | Contract or consulting engagements |
| Unit | A job posting | An engagement opportunity |
| Found by | Posted openings | Posted openings **and inferred trigger events** |
| Buyer | HR then hiring manager | CEO, CFO, PE operating partner, board |
| Economics | Base, bonus, equity, total comp | Day or monthly rate × duration, renewal probability |
| Cycle | 6-14 weeks | 2-6 weeks |
| Output | Tailored resume, cover letter | Capability statement, engagement outline, positioning one-pager |
| Volume | High ingest, aggressive filtering | Low ingest, high per-item effort |

**The core asymmetry, and the thing that makes the consulting track worth building:** posted
fractional roles are a small and heavily contested market. The larger opportunity is inferring
demand from **trigger events** — a company that just closed an acquisition, just lost its IT
leader, just took PE investment, or just announced a compliance obligation has a need that is
not yet a posting. Reaching that company before a posting exists is the entire advantage.

Every entity in the data model carries a `track`. Filters, prompts, scoring dimensions, document
templates, and pipeline stages are all track-aware. The UI can show one track or both.

---

## 4. Data model

SQLAlchemy 2.x async declarative. Timestamps UTC ISO8601 text. JSON blobs as `JSON` columns.

### 4.1 Profile

**`profile`** — one row per track. The operator has two.

| column | type | notes |
|---|---|---|
| id | int pk | |
| track | enum | `employment` \| `consulting` |
| is_active | bool | both may be active |
| full_name, email, phone, location | text | |
| headline | text | |
| narrative | text | 300-600 words, first person, track-specific framing |
| work_auth | text | |
| requires_sponsorship | bool | true if the operator needs visa sponsorship; the Tier 0 sponsorship blocker (§6.1) keys off it |
| geo_rules | json | see §6.1 |
| must_have, must_avoid | json | weighted statements |
| proof_points | json | `{key, claim, evidence, metric}` — the only achievements any prompt may cite |
| employment_terms | json | employment track: `target_titles`, `anti_titles`, `seniority_floor`, `comp_floor_usd`, `comp_target_usd`, `excluded_employment_types` |
| consulting_terms | json | consulting track: `day_rate_floor`, `day_rate_target`, `min_engagement_days`, `max_concurrent`, `service_lines`, `target_org_size`, `target_sectors`, `excluded_sectors`, `travel_tolerance` |
| updated_at | ts | |

### 4.2 Sources and ingestion

**`source`**

| column | type | notes |
|---|---|---|
| id | int pk | |
| track | enum | which pipeline its output enters |
| kind | enum | `jobspy` \| `greenhouse` \| `lever` \| `ashby` \| `workday` \| `smartrecruiters` \| `page` \| `rss` \| `sec_edgar` \| `sam_gov` \| `manual` |
| name | text | |
| config | json | kind-specific |
| enabled | bool | |
| cadence_hours | int | |
| last_run_at | ts | |
| consecutive_failures | int | auto-disable at 3 |
| health | json | last status, error, counts |

**`scrape_run`** — `id`, `source_id`, `started_at`, `finished_at`, `status`
(`running`/`ok`/`partial`/`failed`), `raw_path`, `counts` json, `error`.

### 4.3 Opportunities

**`opportunity`** — the central table. Replaces what a single-track design would call `job`.

| column | type | notes |
|---|---|---|
| id | int pk | |
| track | enum | `employment` \| `consulting` |
| opp_type | enum | `job_posting` \| `contract_posting` \| `rfp` \| `trigger_event` \| `referral` \| `manual` |
| org_id | fk | resolved during normalization, so signals accumulate per organization |
| source_id, scrape_run_id | fk | |
| external_id, url | text | |
| title | text | for a trigger event, a synthesized descriptor |
| org_name, org_url | text | |
| location_raw, country, state, city | text | |
| is_remote | bool nullable | |
| remote_type | enum | `remote` \| `hybrid` \| `onsite` \| `unknown` |
| employment_type | text | |
| comp_min, comp_max, comp_currency, comp_interval | | employment: annual. consulting: day or hourly rate |
| comp_source | enum | `structured` \| `parsed` \| `inferred` \| `none` |
| description_md | text | |
| description_tokens | int | |
| date_posted | date nullable | |
| event_date | date nullable | trigger events: when the event occurred |
| trigger_data | json nullable | see §5.3 |
| first_seen_at, last_seen_at | ts | |
| seen_count | int | increments on every re-observation. Ghost-job signal |
| ghost_score | float | 0.0-1.0, computed at ingest from history. See `SPEC-MODULES.md` §6.2 |
| version | int | increments when the description materially changes |
| previous_description_md | text nullable | |
| content_hash, dedup_group_id | text | |
| is_canonical | bool | |
| liveness | enum | `live` \| `closed` \| `unknown` \| `unchecked` |
| liveness_checked_at | ts | |
| liveness_strikes | int | consecutive expired reads; a posting closes on the second (SPEC-MODULES 6.3) |
| closed_reason | enum? | `http_404` \| `http_410` \| `page_text` \| `ats_missing` (reserved) |
| raw | json | untouched source payload |
| stage | enum | `new` \| `t0_pass` \| `t0_reject` \| `t1_pass` \| `t1_reject` \| `t2_done` \| `t3_done` \| `error` \| `quarantined` |
| stage_reason | text | |
| pre_score | float | Tier 0 soft-signal sum, used for processing order |

**`opportunity_embedding`** — `opportunity_id` pk, `model`, `vector` blob (float32
`numpy.tobytes()`), `dims`, `text_hash`. Vectors from different models are not comparable, so
every reader uses only rows whose `model` equals the current `llm.tiers.embed.model`, and a model
change re-embeds the corpus (§8.1.1).

**`org`** — deduplicated organizations, so signals accumulate across opportunities.

| column | type | notes |
|---|---|---|
| id | int pk | |
| name, normalized_name (unique), domain, url | text | |
| size_band, sector, ownership | text | `ownership`: `public`/`pe_backed`/`vc_backed`/`private`/`nonprofit`/`government` |
| hq_location | text | |
| signals | json | accumulated trigger events and research findings |
| first_seen_at, last_seen_at | ts | |

### 4.4 Evaluation

**`evaluation`**

| column | type | notes |
|---|---|---|
| id | int pk | |
| opportunity_id | fk | |
| tier | int | 1, 2, 3 |
| track | enum | |
| model, prompt_version | text | |
| verdict | enum | the **system's** final verdict after post-processing |
| verdict_model | enum nullable | what the model itself returned |
| score | float nullable | the model's holistic, 1.0-5.0 |
| weighted_score | float nullable | Σ(dimension × weight) |
| needs_review | bool | true when the two diverge by more than 0.75 |
| forced_by | text nullable | `blocker` \| `legitimacy` \| `work_auth` \| null |
| dimensions | json | track-specific, see §7.1 |
| blocks | json | the structured evaluation output |
| blockers, legitimacy | json | |
| few_shot_ids | json | which past evaluations were injected as examples |
| raw_response | text | |
| prompt_tokens, completion_tokens, latency_ms | int | |
| created_at | ts | |

**`quarantine`** — `id`, `opportunity_id`, `tier`, `model`, `prompt`, `raw_response`, `error`,
`attempts`, `created_at`. Retryable from the UI.

### 4.5 Tracking

**`pursuit`** — the operator's pipeline entry. Track-aware; an employment pursuit is an
application, a consulting pursuit is a business-development thread.

| column | type | notes |
|---|---|---|
| id | int pk | |
| opportunity_id | fk unique | |
| track | enum | |
| status | enum | employment: `shortlisted`/`preparing`/`applied`/`screening`/`interviewing`/`final`/`offer`/`accepted`/`rejected`/`withdrawn`/`ghosted`. consulting: `shortlisted`/`researching`/`outreach_sent`/`conversation`/`scoping`/`proposal_sent`/`negotiating`/`won`/`lost`/`no_response` |
| document_id | fk nullable | the resume or capability statement actually sent |
| cover_letter_id | fk nullable | |
| priority | int | operator-set |
| applied_at, last_activity_at, next_action_at | ts | |
| next_action | text | |
| notes_md | text | |
| outcome, outcome_stage, outcome_reason | text | for the learning loop |
| closed_at | ts | |

**`status_event`** — append-only history. `id`, `pursuit_id`, `from_status`, `to_status`,
`note`, `occurred_at`. Never update a status in place without writing one of these.

### 4.6 Infrastructure

**`task`** — the queue. `id`, `type`, `payload` json, `status`
(`pending`/`leased`/`done`/`failed`/`dead`), `priority`, `attempts`, `max_attempts`,
`lease_until`, `worker_id`, `last_error`, timestamps.

Task types: `scrape`, `normalize`, `embed`, `t1`, `t2`, `t3`, `doc_select`, `render`,
`research`, `contacts`, `liveness`, `calibrate`, `train_reranker`, `consolidate_stories`,
`prep_pack`, `negotiation`, `trigger_classify`, `pursuit_sweep`.

**`embed` is enqueued by `normalize`, unconditionally, for every opportunity.** Two separate
subsystems depend on vectors existing: the reranker's PCA features
(`SPEC-MODULES.md` §5.4), and MCP semantic search. If nothing enqueues `embed`, both fail
silently and late.

**`pursuit_sweep`** runs daily from the scheduler and is what makes next-action and ghosting
notifications possible: it finds pursuits with a `next_action_at` due today and pursuits whose
`last_activity_at` has passed the empirically-derived ghosting threshold.

**`run`** — a named batch. `id`, `name`, `track`, `started_at`, `finished_at`, `status`
(`running`/`stopping`/`done`/`failed`), `counts` json, `notes`.

**`setting`** — key/value UI overrides.
**Precedence, highest first:** `setting` row → environment variable → YAML → code default.
Re-read on every request and every task lease, so a UI change takes effect on the next unit of
work with no restart. Only keys declared in the typed settings model are overridable.

**`saved_view`** — `id`, `name`, `track`, `filters` json, `sort` json, `is_default`.

Tables belonging to specific modules (documents, contacts, research, stories, offers, labels,
overrides, models) are defined in `SPEC-MODULES.md`.

---

## 5. Ingestion

### 5.1 Employment track sources

**JobSpy.** Config per source:

```yaml
- kind: jobspy
  track: employment
  name: "Indeed: Operations Analyst, Columbus OH"
  cadence_hours: 12
  config:
    site_name: [indeed, glassdoor]
    search_term: '"vice president" OR "head of" (IT OR technology OR infrastructure) -sales -intern'
    location: "Columbus, OH"
    distance: 100
    results_wanted: 200
    hours_old: 168
    country_indeed: USA
    description_format: markdown
    enforce_annual_salary: true
```

Guardrails, all of which must be enforced rather than discovered:

- Indeed has no meaningful rate limiting. It is the backbone.
- LinkedIn is never searched (D-SRC-5): it does not allow automated access. A source naming it
  is refused at config load, and a pasted LinkedIn posting is handled like any manual capture.
  A 429 from any other board is `partial`, never `failed`, and never aborts the batch.
- Indeed permits **only one** of `hours_old`, `job_type`+`is_remote`, or `easy_apply` per
  search. Validate at config load and refuse with a clear message.
- All boards cap near 1000 results. Split broad searches by location or title.
- JobSpy is synchronous. Always `asyncio.to_thread()`.
- ZipRecruiter answers JobSpy with a Cloudflare 403 on every search, and JobSpy reports that as
  zero results with zero errors. Profile-generated searches therefore ask Indeed and Google only;
  a custom source may still name `zip_recruiter` (D-SRC-4).
- Generated searches ask Indeed and Glassdoor. Google Jobs is available to custom sources but
  currently returns nothing through JobSpy (D-SRC-4 amended). Every run records its postings per
  board, so a board that found nothing is shown, not hidden in the total.
- Profile-generated searches also ask for near-title variants of each target title (D-TTL-2):
  up to 3 per title, deduplicated, 9 in all, OR-joined into the same title queries. A variant
  only swaps a searchable abbreviation for its other form (IT and Information Technology, VP and
  Vice President, RN and Registered Nurse) or a curated family spelling (Technology and IT,
  People Operations and HR); word order is never changed, since boards handle it, and every
  variant normalizes to its title's exact token set. Variants are dropped from the end before
  they could add more than half again the searches the plan has without them (rounded down),
  push the searches past `MAX_GENERATED`, or cost a metro. The operator can remove any on
  Profile > Employment.

**ATS platforms.** All verified live 2026-07-30. Exact field shapes in Appendix A.

| Platform | Endpoint | Verified |
|---|---|---|
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | 200, 401 jobs |
| Lever | `GET https://api.lever.co/v0/postings/{company}?mode=json` | 200, bare array |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` | 200 |
| **Workday** | `POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` body `{"limit":20,"offset":0,"searchText":""}` | 200, 2000 postings |
| **SmartRecruiters** | `GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=0` | 200 |

Workday matters disproportionately: it is the dominant enterprise ATS, and enterprise is where
VP of IT roles live. It paginates via `offset`/`limit` and needs a second call per job for the
full description (`/wday/cxs/{tenant}/{site}/job/{path}`).

**Company boards found in the searches (D-SRC-7).** These platforms cannot be searched across
companies; each company has its own board. A JobSpy search run therefore reads each posting's
link (`job_url_direct`, then `job_url`), and a link to a company board on one of the five
platforms becomes a source for that company: `origin: discovered`, `cadence_hours: 24`, on or off
with the search that found it. Hosts match exactly (`boards.greenhouse.io`,
`job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `{tenant}.{dc}.myworkdayjobs.com`,
`jobs.smartrecruiters.com`, `careers.smartrecruiters.com`); EU hosts are not matched, since they
use APIs the adapters do not call. A board already among the sources, in any state, is never
added again, so one the operator turned off stays off. At most 40 found boards per track. A
discovery failure is logged and never changes the run's status.
Greenhouse short links (`grnh.se`) are followed with one request each, redirects off, at most 20
per run. A role from a found board is scored at once when its level is unknown or at or above the
floor and its title shares a field word with the target titles or with the titles of at least two
of the operator's pursuits (D-SRC-8). Any other role from a found board waits in a background
lane: stored, never rejected, and scored only when no other work is waiting, up to
`pipeline.lane_daily_cap` (200) a day. "Score everything now" frees the whole lane once, after a
confirmation that states the count, the time and, on an online model, the cost (D-SRC-9).

**Manual.** Paste a URL (httpx, escalating to Playwright on a JavaScript shell) or paste raw JD
text. Both produce a normal `opportunity` and run the identical pipeline.

### 5.2 Consulting track sources

**Posted fractional and interim work.** JobSpy with `job_type: contract` and search terms for
"fractional CIO", "interim CTO", "interim IT director", "IT transformation consultant", plus the
same ATS platforms filtered for contract employment type.

**Public sector RFPs.**
- **SAM.gov Opportunities API** — `https://api.sam.gov/opportunities/v2/search`. Requires a
  **free API key** from sam.gov. **Not verified live** because no key was available at spec
  time; the adapter must handle a missing key by disabling the source with a clear message
  rather than failing. Filter by NAICS 541512 (computer systems design), 541611 (management
  consulting), 541519.
- State and municipal procurement portals as configured `rss` or `page` sources. These vary too
  much to generalize; treat each as a configured source with its own parser.

**Trigger events — the differentiating source.** See §5.3.

**Referral and manual.** A conversation, an introduction, a lead the operator enters by hand.
First-class, because in consulting it is the highest-converting channel by a wide margin.

### 5.3 Trigger-event detection

A trigger event is a public fact implying a likely near-term need for fractional IT leadership.
It becomes an `opportunity` with `opp_type = trigger_event` and no URL to apply to.

**SEC EDGAR full-text search.** Verified live: `GET https://efts.sec.gov/LATEST/search-index`
with `q`, `forms`, `dateRange` parameters returns JSON hits. **A descriptive `User-Agent` header
identifying the operator is required by SEC policy** — send
`Kestrel/1.0 (mark@example.com)` and respect a 10 request/second ceiling.

Queries that produce real signal, run against `forms=8-K`. The generated watch never ships a
job title or a field (D-SRC-6). By default it sends field-neutral events only: SEC sees every
query beside the contact email its User-Agent rule requires, so the operator's own target titles
(up to three, each its own quoted phrase, the service lines when there are no titles) join the
queries only after the operator ticks "Use my target titles in SEC filing searches" on the
Sources page (off by default):

| Query | Implies |
|---|---|
| A target title, quoted (`"operations director"`), opt-in only | A filing that names the role, such as an Item 5.02 departure |
| `"completion of acquisition"` | Integration work, 90-day window |
| `"departure of directors or certain officers"` (Item 5.02) | Leadership vacancy in any function |

An operator may add field-specific phrases (`"material weakness"`, `"cybersecurity incident"`)
to a custom EDGAR source; the shipped defaults never assume one.

Item 5.02 of an 8-K covers officer departures specifically. Item 1.05 covers material
cybersecurity incidents and has been mandatory since 2023, which makes it a reliable feed.

**Other signal sources**, each a configured source with its own adapter:
- PE firm portfolio pages: crawl a configured list of PE firms, extract portfolio companies,
  monitor for new additions. A newly acquired portfolio company is a prospect on day one.
- Company newsroom and blog RSS: funding, leadership change, expansion, compliance milestones.
- The ATS boards themselves: a company suddenly posting five IT roles is a signal even if none
  of them is the right role.
- Layoff trackers as configured RSS where available.

**Classification.** Each raw signal runs through the `trigger_classify` prompt
(`PROMPTS.md` P10), producing `trigger_data`:

```json
{
  "event_type": "cio_departure|acquisition|pe_investment|security_incident|control_weakness|rapid_hiring|funding|expansion|other",
  "org_name": "...",
  "event_date": "2026-07-15",
  "evidence_quote": "verbatim from the filing or page",
  "source_url": "...",
  "confidence": 0.0,
  "implied_need": "what an IT leader would be needed for",
  "urgency_window_days": 90,
  "reasoning": "..."
}
```

Signals below `settings.consulting.min_trigger_confidence` are stored but not promoted to the
evaluation pipeline. They still accumulate on the `org` record, and **three weak signals on one
org within 90 days promotes it** regardless of individual confidence. Compounding weak evidence
is the point.

**Legal and ethical constraints, non-negotiable:** only public filings and public pages. Respect
`robots.txt`. Send an identifying User-Agent. Rate-limit every source. Never scrape behind a
login. Never use personal data beyond publicly listed business contact information.

### 5.4 Normalization

`normalize.py` maps any payload to the canonical `opportunity` shape.

- **Location:** parse to country/state/city; always retain `location_raw`.
- **Compensation:** prefer structured fields. Otherwise regex the description
  (`$180,000 - $220,000`, `180k-220k`, `$95/hr`, `$1,200/day`). Set `comp_source` honestly.
  Employment converts hourly to annual at 2080h when `enforce_annual_salary`. Consulting
  normalizes to a **day rate**, converting hourly × 8 and monthly ÷ 21.
- **Remote type:** keyword classification over title plus the first 500 characters. Default
  `unknown`, never guess `onsite`.
- **Organization:** resolve or create an `org` row by normalized name and domain, and set
  `opportunity.org_id`. **Infer and persist `size_band`, `sector`, and `ownership` here**, from
  whatever the payload offers: Greenhouse `metadata[]`, Indeed's
  `company_employees_label`/`company_revenue_label`/`company_industry` (JobSpy exposes all
  three), Ashby `department`, and the description text. Leave unknown rather than guessing.
  **Four consulting Tier 0 rules key off these fields**, so if nothing populates them the
  consulting track silently degrades to no filtering at all.
- **Compensation observations:** every parsed or structured figure writes a `comp_observation`
  row immediately (`SPEC-MODULES.md` §6.1). This is pure arithmetic with no model involved, it
  costs nothing, and it must start on day one because the corpus is only useful once it has
  history.
- **Ghost score:** recompute `opportunity.ghost_score` from `seen_count`, the span between
  `first_seen_at` and `last_seen_at`, and version history (`SPEC-MODULES.md` §6.2). Also pure
  arithmetic, also starts on day one.
- **`description_tokens`:** count with `tiktoken` `cl100k_base` as a portable approximation.
  **The encoding file must be vendored and `TIKTOKEN_CACHE_DIR` set** before first use, or
  tiktoken downloads it from Azure blob storage and the packaged application fails offline.

### 5.5 Deduplication

Three passes, cheapest first.

1. **Exact.** `content_hash` = sha256 of `lower(strip(org)) + "|" + lower(strip(title)) + "|" +
   normalized_description`, where normalization collapses whitespace and strips URLs and
   tracking parameters.
2. **Near-exact.** Within a bucket keyed by normalized org plus the first four significant title
   tokens, token-set Jaccard above 0.85 groups them.
3. **Semantic (not built).** An embedding pass (`nomic-embed-text` cosine above 0.93) over
   remaining same-org candidates was specified but no code does it. Dedup today is the hash of
   org, title and normalized description (pass 1) plus the title-token Jaccard grouping (pass 2).
   A same role on two boards with different boilerplate is therefore not merged unless the titles
   match closely.

**Canonical priority:** company ATS (Greenhouse/Lever/Ashby/Workday/SmartRecruiters) > company
page > Indeed > Google > ZipRecruiter > LinkedIn > Glassdoor. Apply direct where possible, and
the ATS copy is authoritative. Non-canonical members keep their URLs and show as "also seen on".

**Re-seen handling.** Same hash: increment `seen_count`, update `last_seen_at`, no
re-evaluation. Materially changed (Jaccard below 0.9 against stored): increment `version`, store
`previous_description_md`, re-queue Tier 2 with a `changed` flag.

`seen_count` combined with the span between `first_seen_at` and `last_seen_at` is the raw
material for ghost-job detection (`SPEC-MODULES.md` §6).

### 5.6 Applied-history import

The operator arrives with applications already made elsewhere. Settings > Applied takes that
history as a CSV or a Markdown table, shows a review pane, and on confirm writes one
opportunity plus one pursuit per row. Nothing is written before the confirm.

**Format.** A header row names the columns, in any order and any case; the review pane says
which column it read as which. Required: company (`Company`, `Organization`, `Org`,
`Employer`), title (`Role`, `Title`, `Position`, `Job`), applied date (`Date`, `Applied`,
`Applied on`) and status (`Status`). Optional: `Source` (the board or site), `URL` (`Link`),
`Location`, `Notes`, `Track` (`employment` or `consulting`; default employment). Dates are
`YYYY-MM-DD`; a row whose date does not parse is listed with the error and skipped unless
corrected in the pane. Unknown columns are kept in the row's notes, never dropped silently.

**Status vocabulary.** Kestrel's own pursuit statuses are accepted as written. The common
tracker words are mapped and the mapping shown per row: `no response`, `acknowledged`,
`viewed`, `submitted` map to `applied`; `in conversation`, `screening call`, `phone screen` to
`screening`; `interview`, `interviewing` to `interviewing`; `final`, `onsite` to `final`;
`offer` to `offer`; `rejected`, `declined by them`, `no` to `rejected`; `withdrawn`,
`declined` to `withdrawn`; `ghosted`, `no answer` to `ghosted`. Any other word leaves the row
with a status select in the pane; the row is not imported until one is chosen.

**Non-application rows.** A tracker often logs activities that are not applications (a resume
update, a networking call, a workshop). A row whose company starts with `Self`, `Networking`,
`Workshop` or `Event`, or whose title or notes carry `activity type:`, is listed under "Not an
application" and skipped. The operator can tick one to import it anyway.

**What is written.** For each confirmed row: an `org` (resolved like any scraped posting), an
`opportunity` on the `import` source with `title`, `org_name`, `url`, `location_raw`, a
one-line `description_md` built from the row ("Imported from <source> on <date>. <notes>"),
`date_posted` = the applied date, `stage = "new"`, and `archived_at` set with
`archive_reason = "applied"` so it never enters triage or the list; and a `pursuit` with the
mapped status, `applied_at`, `notes_md`, one `status_event` (`None -> status`), and
`allow_reposts` from the row's checkbox (default off). A row that matches an existing pursuit
(same org and a title with Jaccard above 0.85, or the same URL) is shown as "already tracked"
and skipped unless ticked; ticking updates that pursuit's status and applied date instead of
making a second one.

**Suppression of re-posts.** Dedup (§5.5) now consults pursuits. When a NEW opportunity lands
in a dedup group that holds an opportunity whose pursuit has status `applied` or later, or whose
`archive_reason` is `applied`, and no such pursuit has `allow_reposts` on, the new row is
written with `archived_at` set, `archive_reason = "applied"` and `stage_reason = "Re-post of
pursuit #<id>"`. It is counted in the scrape run as `suppressed`, visible under the Hidden chip,
and Unhide puts it back like any hidden row. With `allow_reposts` on, the new row surfaces as
usual: this is for aggregators that re-list the same title (Jobgether, for one) where the
operator wants to see each listing. The pursuit's detail and the Applied tab both carry the
toggle. The suppression applies to every applied pursuit, imported or not.

---

## 6. The tiered pipeline

An opportunity flows strictly downward and may exit at any tier.

### 6.1 Tier 0 — deterministic, no LLM

Microseconds. Target: eliminate 60-80% of raw ingest. Rules live in
`kestrel/resources/config/filters.yml`, keyed by track, so they are tunable without a code
change. Every rejection records the rule that fired.

A rejection is not final when the inputs change. A profile save that changes what the rules read
(target or excluded titles, seniority floor, pay floor, geography, sponsorship) queues one
resumable, rules-only `t0_recheck` task per track. It re-runs the rules on roles set aside by
Tier 0 and returns the ones that now pass to `new`. A role the operator scored anyway, labeled,
put in a pursuit or hid is never touched (D-RULES-1).

> **Critical implementation constraint, and the reason these rules look the way they do.**
> `simpleeval` does **not** support generator expressions (`GeneratorExp`) at all, and supports
> comprehensions only under `EvalWithCompoundTypes`. A rule written as
> `any(t.lower() in opp.title.lower() for t in profile.target_titles)` would make the shipped
> `filters.yml` fail on first run. Every list operation is therefore expressed as a **whitelisted helper function**.
> Do not reintroduce generator expressions into this file. The load-bearing guard is an AST check
> in `kestrel/rules/helpers.py` (`_assert_no_comprehensions`, banning generator expressions,
> comprehensions and lambdas), which rejects such a rule at config load with a clear message
> naming the rule. It is not left to `simpleeval` to raise. Do not remove or loosen it; a test
> guards it.

```yaml
employment:
  hard_blockers:
    - id: comp_floor
      when: "comp_max_below(opp, profile.employment_terms.comp_floor_usd)"
      reason: "Comp ceiling below floor"
    - id: sponsorship
      when: "profile.requires_sponsorship and regex(opp.description_md, '(?i)(cannot|unable to|will not) (provide|offer) (visa )?sponsor')"
      reason: "Explicit no-sponsorship"
    - id: clearance
      when: "regex(opp.description_md, '(?i)(active|current) (TS/SCI|top secret|secret) clearance')"
      reason: "Requires clearance not held"
    - id: anti_title
      when: "title_contains_any(opp, profile.employment_terms.anti_titles)"
      reason: "Excluded title"
    - id: seniority_floor
      when: "seniority_rank(opp.title) >= 0 and seniority_rank(opp.title) < seniority_rank(profile.employment_terms.seniority_floor)"
      reason: "Below seniority floor"
    - id: geo
      when: "not geo_allows(opp)"
      reason: "Location outside acceptable set"
    - id: stale
      when: "opp.date_posted is not None and age_days(opp.date_posted) > settings.max_posting_age_days"
      reason: "Posting too old"
  soft_signals:
    - { id: title_exact,       when: "title_near_equals_any(opp, profile.employment_terms.target_titles)", weight: +2.0 }
    - { id: title_contains,    when: "title_near_any(opp, profile.employment_terms.target_titles)", weight: +1.0 }
    - { id: comp_above_target, when: "comp_max_at_least(opp, profile.employment_terms.comp_target_usd)", weight: +1.0 }
    - { id: direct_ats,        when: "source_kind_in(source, ['greenhouse','lever','ashby','workday','smartrecruiters'])", weight: +0.8 }
    - { id: fresh,             when: "age_days(opp.date_posted) <= 3", weight: +0.5 }
    - { id: must_have_hit,     when: "desc_contains_any(opp, profile.must_have)", weight: +0.5 }
    - { id: comp_unknown,      when: "opp.comp_source == 'none'", weight: -0.3 }
    - { id: must_avoid_hit,    when: "desc_contains_any(opp, profile.must_avoid)", weight: -1.0 }
    - { id: staffing_agency,   when: "regex(opp.description_md, '(?i)(staffing|recruiting|talent) (agency|partner|solutions)|our client is seeking')", weight: -1.0 }
    - { id: reposted_often,    when: "opp.seen_count >= 5", weight: -0.8 }
    - { id: likely_ghost,      when: "opp.ghost_score >= 0.6", weight: -1.2 }

consulting:
  hard_blockers:
    - id: rate_floor
      when: "comp_max_below(opp, profile.consulting_terms.day_rate_floor)"
      reason: "Day rate below floor"
    - id: too_short
      when: "trigger_int_below(opp, 'engagement_days', profile.consulting_terms.min_engagement_days)"
      reason: "Engagement shorter than minimum"
    - id: excluded_sector
      when: "value_in(org.sector, profile.consulting_terms.excluded_sectors)"
      reason: "Excluded sector"
    - id: geo
      when: "not geo_allows(opp)"
      reason: "Outside travel tolerance"
    - id: stale_trigger
      when: "opp.event_date is not None and age_days(opp.event_date) > settings.consulting.max_trigger_age_days"
      reason: "Trigger event window closed"
  soft_signals:
    - { id: pe_backed,        when: "org.ownership == 'pe_backed'", weight: +1.5 }
    - { id: target_size,      when: "value_in(org.size_band, profile.consulting_terms.target_org_size)", weight: +1.0 }
    - { id: target_sector,    when: "value_in(org.sector, profile.consulting_terms.target_sectors)", weight: +1.0 }
    - { id: recent_trigger,   when: "age_days(opp.event_date) <= 30", weight: +1.2 }
    - { id: multi_signal,     when: "org_signal_count(org, 90) >= 2", weight: +1.5 }
    - { id: no_url,           when: "opp.url is None", weight: -0.2 }
```

`geo_rules` on the profile:

```yaml
geo_rules:
  remote_ok: true
  remote_country_allowlist: [US]
  onsite_states: [OH]
  onsite_metros:                       # each entry lists the municipality names that count
    - { name: "Columbus",    aliases: ["Columbus", "Dublin", "Westerville", "Hilliard", "Grove City", "Gahanna"] }
    - { name: "Cleveland",    aliases: ["Cleveland", "Parma", "Lakewood", "Euclid", "Strongsville"] }
    - { name: "Cincinnati",  aliases: ["Cincinnati", "Mason", "Blue Ash", "Covington"] }
  hybrid_requires_metro: true
  relocation_acceptable: false
  travel_days_per_month_max: 6         # consulting track
```

**`geo_allows` uses name matching, not distance.** A radius in miles would require geocoding,
and no geocoder, no offline place dataset, and no network dependency belongs in this stack. The
alias list is explicit and the operator edits it. Logic, in order:

1. `remote_type == "remote"` and the country is in `remote_country_allowlist`, **or the country is
   unknown/unparsed** → allow. An unparsed country on a remote role escalates rather than rejects
   (hard rule 4); ingestion often leaves `country` empty on "Remote" listings.
   Then, for a posting that is not remote: when the profile lists **no** `onsite_states` and
   **no** `onsite_metros`, allow. An operator who gave no on-site location filters nothing by
   location; empty lists never mean "nowhere is acceptable" (release round review, finding 1).
   The shipped example profile lists none.
2. Parsed `state` in `onsite_states` → allow.
3. Parsed `city` matches any alias in any `onsite_metros` entry, case-insensitive → allow.
4. `location_raw` contains any alias as a substring → allow. This catches the many postings whose
   location string never parses cleanly.
5. `remote_type == "unknown"` and nothing matched → **allow**, and record `geo_uncertain`. Tier 0
   biases toward false positives; an unparseable location is not evidence of a bad location.
6. Otherwise → deny.

**Expression evaluation.** `simpleeval`, never `eval()`. The evaluator is constructed once.

Names available: `opp`, `org`, `profile`, `source`, `settings`.

Helper functions, the complete whitelist. **Every one is null-safe**: a `None` input returns the
safe answer (`False` for predicates, a large sentinel for `age_days` so a missing date never
trips a staleness rule) rather than raising.

| Helper | Returns |
|---|---|
| `regex(text, pattern)` | bool, case handled in the pattern |
| `age_days(date)` | int days, `999999` when the date is None |
| `seniority_rank(title)` | int 0-8, or `-1` for unknown, which **passes** every floor check |
| `geo_allows(opp)` | bool, see below |
| `title_contains_any(opp, terms)` | bool, **strict** substring match, lowercased both sides (anti_title) |
| `title_near_any(opp, terms)` | bool, substring match or near-title containment (D-TTL-1) |
| `title_equals_any(opp, terms)` | bool, **strict** exact match, lowercased both sides |
| `title_near_equals_any(opp, terms)` | bool, exact match or near-title equality (D-TTL-1), soft signals only |
| `desc_contains_any(opp, terms)` | bool, substring over `description_md` |
| `value_in(value, list)` | bool, None-safe membership |
| `source_kind_in(source, list)` | bool |
| `comp_max_below(opp, n)` | bool, False when `comp_max` is None |
| `comp_max_at_least(opp, n)` | bool, False when `comp_max` is None |
| `trigger_int_below(opp, key, n)` | bool, False when the key is absent |
| `org_signal_count(org, days)` | int |

Builtins: `len`, `min`, `max`, `abs`. **No `any`, no `all`**, because their useful form requires
a generator expression, which the AST guard in `kestrel/rules/helpers.py` rejects.

A rule referencing anything outside this set fails at **config load** with a message naming the
rule and the unknown symbol, not at runtime. Ship a test that loads the packaged `filters.yml`
and evaluates every rule against a fixture opportunity. That test is the guard against this
whole class of failure.

**Near titles (D-TTL-1).** `kestrel/rules/titles.py` reduces a title to canonical tokens:
lowercase, punctuation stripped, filler words (of, the, and, &, for, a, an) dropped, abbreviations
expanded from a small, field-agnostic table (IT, VP, SVP, HR, CIO, RN, LPN, UX, QA and the like;
ambiguous ones such as PM, PR and UI are left out), a light plural fold, multi-word concepts
joined ("information technology" is one token), and a small explicit family table (people
operations counts as HR, software developer as software engineer). "Technology" counts as IT only
in a bare level title ("<level> of Technology", "<level>, Technology", "<level> Technology"),
never in a longer one ("Technology Sales", "Educational Technology"). Equality is the same token
set in any order ("IT Director" equals "Director of IT"); containment is the term's set inside
the title's. Levels are never folded: "Head of IT" is not "VP of IT". Only the soft signals
(`title_near_equals_any`, `title_near_any`) and generated searches use it. `title_equals_any`
stays strict exact and `anti_title` keeps `title_contains_any`, strict substring, so every hard
blocker reads as written: a broader match there would reject more roles (hard rule 4). A seeded
`filters.yml` whose `title_exact` or `title_contains` soft signal is still the exact
pre-D-TTL-1 line is read as `title_near_equals_any` or `title_near_any` at load; any edited rule
is read as written.

**A rule's why (D-UI-73).** `stage_reason` stays `blocker:<rule>: <reason>`. The operator's
sentence (the title's level and the floor, the pay and the floor, the matched excluded title, the
place, the days) is computed at render time from the opportunity and the current profile
(`kestrel.web.fit.explain_rule`); nothing more is stored.

**Score this role anyway (D-UI-74).** A role the operator asked to score despite a rule has a
`tier0_override.<opportunity id>` setting row once Tier 1 has run it. Tier 0 evaluates its soft signals but no hard
blocker for it, and appends `; tier0_override` to its pass reason.

`seniority_rank`: ic(0), lead(1), manager(2), senior_manager(3), director(4), senior_director(5),
vp(6), svp(7), c_level(8), derived from title regexes. **An ambiguous title ranks `unknown` and
passes.** Bias toward false positives at Tier 0; a wrong rejection here is invisible and
unrecoverable.

**Ordering.** Survivors sort by `pre_score` descending, so the best opportunities get model time
first and an interrupted run has already done the work that mattered.

### 6.2 Tier 1 — fast triage

Small model. Input: structured fields plus the first 1200 tokens of description plus a compact
profile (under 400 tokens). Output: `keep`/`kill` with confidence, reason, and signal tags.

Target: 2-5 seconds per item, eliminating 50-70% of Tier 0 survivors.

**Bias rule:** confidence below `settings.tier1_min_confidence` (default 0.6) is forced to
`keep`. Uncertainty escalates, it never rejects. Record that the escalation was forced.

**Once the learned reranker meets its promotion criteria (`SPEC-MODULES.md` §5.4), it replaces
this tier entirely** and Tier 1 becomes a millisecond operation. The LLM path remains as the
cold-start behavior and as the fallback when the reranker's confidence is low.

### 6.3 Tier 2 — deep evaluation

Workhorse model. Track-specific prompts (`PROMPTS.md` P2 for employment, P2C for consulting).
Full description truncated to `settings.max_jd_tokens` (default 6000, middle elided if over),
full profile for the relevant track, and **two to three few-shot examples selected from the
operator's own confirmed past evaluations** by embedding similarity
(`SPEC-MODULES.md` §5.3).

Employment output: **six blocks plus a top-level legitimacy assessment.** The legitimacy result
is a sibling of `blocks` in the schema, not a seventh block, because post-processing acts on it
directly and nesting it inside `blocks` would make that path awkward. Prose elsewhere referring
to "Block G" for the employment track means this top-level `legitimacy` object. The consulting
track does nest its equivalent as `blocks.g_risk` **and** carries the top-level object; on that
track the top-level object is authoritative for post-processing and `g_risk` is narrative detail.

Blocks:

- **A — Role summary.** What the job actually is, and whether title and body disagree.
- **B — Fit analysis.** Matches with evidence; gaps classified fatal / addressable / noise.
- **C — Level and scope.** Above, at, or below level. Reporting line, team size, budget.
- **D — Compensation.** Stated band, or a market estimate with explicit confidence and basis,
  **grounded in the operator's own accumulated comp corpus** (`SPEC-MODULES.md` §6.1).
- **E — Personalization angle.** What a cover letter should lead with, citing JD text.
- **F — Interview prep.** Likely questions from the Block B gaps, mapped to proof points in
  STAR+Reflection shape.
Plus, as a sibling of `blocks`:

- **`legitimacy`.** Ghost-job and scam signals. **The prompt receives `ghost_score` and the
  repost history as explicit input** (§5.4 computes them at ingest), so the model reasons from
  observed history rather than guessing from one snapshot.

Consulting output blocks:

- **A — Situation read.** What is actually going on at this organization and why now.
- **B — Engagement thesis.** The engagement that fits: scope, likely duration, phases.
- **C — Buyer map.** Who signs, who influences, who blocks. Access difficulty.
- **D — Economics.** Rate range this org supports, engagement value, renewal likelihood.
- **E — Approach.** The opening move. What to say, to whom, referencing what public fact.
- **F — Delivery feasibility.** Can this be delivered solo, and what would need partnering out.
- **G — Legitimacy and fit risk.** Procurement barriers, incumbent advisors, pure fishing.

Target: 15-40 seconds per item.

### 6.4 Tier 3 — escalation, opt-in per opportunity

Never automatic. The operator escalates from the shortlist. May use a stronger model, including
a hosted API. Produces cover letter or engagement outline, negotiation framing, research
synthesis, and a refined document selection. The UI states exactly what will be transmitted and
the estimated cost before the operator confirms.

**Bring your own key (BYOK).** The hosted model is the operator's own account at a provider they
pay directly; Kestrel ships no key and no proxy. The shipped external backends are:

| Backend | Provider | Protocol | Key (in `.env`) |
|---|---|---|---|
| `frontier` | OpenRouter, `https://openrouter.ai/api/v1` | chat completions | `OPENROUTER_API_KEY` |
| `openmodel` | openmodel.ai, `https://api.openmodel.ai/v1` | Responses (`/v1/responses`; its chat-completions endpoint was removed) | `OPENMODEL_API_KEY` |
| `anthropic` | Anthropic (Claude API direct), `https://api.anthropic.com/v1` | Messages (`/v1/messages`, `x-api-key` + `anthropic-version`); structured output is `output_config.format` json_schema, never a forced tool call (current Claude models reject one); no sampling parameter is sent (the Claude 5 family rejects `temperature`); `max_tokens` has a floor of 8192 because default thinking counts against it; `stop_reason: refusal` or `max_tokens` quarantines at once with the reason, no repair, no retry | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI (API direct), `https://api.openai.com/v1` | chat completions with `max_completion_tokens` and a strict `response_format` json_schema; reasoning families (o-series, gpt-5) get no `temperature` and take `reasoning_effort`; the model list is filtered to chat families by id because it carries no capability or price data | `OPENAI_API_KEY` |

Any OpenAI-compatible chat-completions server can be added as `provider: generic`. Rules:

- The `frontier` **tier** names exactly one external backend and is reachable only through the
  per-item preview-and-confirm escalation. The worker never passes `allow_external`.
- **Hosted mode** (`llm.hosted_mode`, off by default) is the only other way an external backend
  is ever used. Any tier (`tier1`, `tier2`, `docsel`; never `embed`, which runs on Ollama or
  in-process, §8.1.1) may name an external backend, but the client refuses the call
  (`ExternalBackendError`) until the operator has turned Hosted mode on **and** accepted the
  disclosure under Settings > Hosted, which writes `enabled: true`, `acknowledged_at` and
  `acknowledged_terms` (the Tier 1 head, the Tier 2 ceiling and the per-tier "sends" lines as
  accepted). A bare `enabled: true` in YAML without the acknowledgement does nothing, and an
  acknowledgement whose terms differ from the current disclosure is void: hosted sends stop
  (the client refuses, saying the disclosure changed) until the operator accepts it again. It
  exists for machines too weak for local inference and for OpenRouter's free models. Under Hosted mode:
  - The disclosure states exactly what each hosted tier sends: Tier 1 the title, organization,
    location and the first N tokens of the description; Tier 2 the description slice, the org
    context, the confirmed labels used as few-shot examples and the full profile; document
    selection the resume variant texts and the opportunity summary. The provider's retention
    rules apply, and OpenRouter free models may train on inputs unless `data_collection: deny`
    excludes them (which may leave no free model).
  - A hosted Tier 2 sized call (tier2, the frontier escalation, which `tier3.py` sizes through
    the same `jd_budget`) gets a description slice sized to the chosen model's context window as
    the provider lists it (`llm.hosted_model_caps`): context minus a 6,000-token prompt overhead
    and the tier's output budget, less a 15% margin for the gap between cl100k counts and the
    model's own tokenizer, clamped to [0, `hosted_mode.jd_ceiling_tokens`] (16000, the figure
    the disclosure states). Below `hosted_mode.max_jd_tokens` (the floor, 3000) the floor is used
    only when the window holds it (context >= floor + overhead + output), else the smaller
    figure; with the context unknown, the local `pipeline.max_jd_tokens`, at least the floor
    and at most the ceiling. A provider 400 on a structured call quarantines with its text.
    The middle is elided, so the head and a tail window (where comp and location usually sit) both survive (D-LLM-3). Tier 1 and the
    trigger classifier keep their head-only slice, `min(1200, hosted_mode.max_jd_tokens)`.
  - With confidential mode on, the current employer is redacted from every message before the
    send (`hosted_mode.redact_confidential`, default true); with confidential mode on and no
    redaction map the send is **refused**: the item is quarantined with the reason, never
    rejected (hard rule 4). This is the same rule Tier 3 escalation applies.
  - `kestrel doctor` reports Hosted mode off or on (with the routed tiers), warns about a tier
    that names an external backend while the mode is off, and checks the key for each external
    backend in use. It no longer fails on a non-frontier external tier.
  - Turning Hosted mode off keeps the per-tier choices and stops using them.
  - **Two tier sets and a switch.** `llm.tiers` is the local set (the hardware profile's
    derived models) and `llm.hosted_tiers` the hosted assignments, same shape, written by
    Settings > Hosted and `kestrel setup --hosted` as `llm.hosted_tiers.<tier>.backend` and
    `.model`. `llm.mode` (`local`, the default, or `hosted`) is the standing choice: under
    `hosted`, settings resolution overlays each assigned tier onto `llm.tiers` (backend and
    model; the local tier's other fields survive except `max_tokens`, which takes the hosted
    budget, §8.1, D-LLM-1) and every unassigned tier, `embed`
    always, stays local. The pipeline keeps reading `llm.tiers`. `hosted` is honored only with
    the acknowledgement and tier1 and tier2 assigned; otherwise the switch, the CLI and Run now
    refuse it with the reason, and a stored `hosted` that can no longer run resolves to local.
    One run can differ from the standing choice: `Worker.run(mode=)`, `kestrel run --hosted` or
    `--local`, and `POST /api/run/now?mode=` (the Runs page form), which queues a one-shot
    `pending_run_mode` row the serve loop consumes on exactly one cycle. Each `run` row records
    its `mode`; each evaluation already records its model. The `frontier` tier is hosted in both
    modes and stays in `llm.tiers`.
- Keys live only in `<data home>/.env`, entered from **Settings > Hosted (BYOK)** or
  `kestrel provider set-key`. Never in YAML, never in a backup, never echoed, never logged.
- Before a model is offered for Tier 3 the provider is asked which models can honor a JSON schema
  (`supported_parameters` on OpenRouter, `supported_protocols` on OpenModel); a model whose
  capability is unknown is offered with that label, never hidden.
- OpenRouter requests to a schema-capable model send `provider.require_parameters: true` (never
  route to an endpoint that ignores the schema); a JSON-mode or prompt-only model (§8.5, D-UI-48)
  is sent with it false, since there is no schema to ignore. `provider.data_collection` defaults
  to `"deny"`; `zdr` and `reasoning.effort` are operator options. `usage.include: true` returns
  the USD cost.
- A hosted request sends the schema in the **strict** dialect: every property required, optionals
  as `anyOf [X, null]`, `additionalProperties: false`. Without this the strict endpoints reject the
  Tier 2 schema after the payload has already left the machine.
- Every transmission is recorded in `frontier_transmission` with the provider's own token counts
  and cost where reported (NULL renders as unknown, never as free). One confirmation authorizes
  one send: any prior record for the same payload hash, whatever its status, refuses a replay.

### 6.5 Recurring scans

`source.cadence_hours` and `last_run_at` drive an `AsyncIOScheduler` inside the worker. Every
five minutes it enqueues `scrape` tasks for due, enabled sources.

Guards: never enqueue for a source with a pending or leased scrape; stagger enqueues by 60
seconds; auto-disable and surface a source after three consecutive failures. A fresh install
does not start scraping unattended because all sources ship disabled; the operator enables
sources explicitly or sets a run schedule to start automatic scans.

**Run schedule.** Beside the per-source cadence, the Runs page offers one schedule for the
whole scan, in the operator's local time: off (default), daily at a time, or weekly on a
weekday at a time. Stored as setting rows (`scheduler.run_schedule`: `{"kind": "off" |
"daily" | "weekly", "time": "HH:MM", "weekday": 0-6}` and `scheduler.last_scheduled_run`,
the local date or ISO week it last fired). When the schedule is due and the app is running
(the window or the tray), the scheduler enqueues a scrape for every enabled source, cadence
or not, with the same guards as above, and records the firing so a restart inside the same
day or week does not fire twice. A due time that passed while the app was closed fires once
at the next start that same day (daily) or week (weekly), never for earlier periods. The
schedule is independent of every source's cadence; it
changes neither. It runs in the desktop app (and `kestrel serve` only when started with the
scheduler), never from a one-shot CLI run.

### 6.6 Concurrency and the model-thrash trap

- `settings.concurrency.tier1` and `.tier2` (defaults 4 and 2) parallel requests.
  `OLLAMA_NUM_PARALLEL` must be at least the maximum, or requests serialize regardless.
- **Never interleave tiers that use different models.** With 16GB of VRAM, alternating a 14B and
  an 8B forces Ollama to unload and reload weights on every task. The worker **drains all pending
  tasks for one tier before starting another**, unless the models are identical. Set
  `OLLAMA_KEEP_ALIVE` generously (2h).
- A run reports live per-tier counts, throughput, elapsed, and an ETA extrapolated from the
  trailing 20 completions.
- Graceful stop: setting a run to `stopping` lets in-flight tasks finish and leaves the rest
  `pending`, resumable later.

---

## 7. Scoring

### 7.1 Dimensions

Each returned on 1.0-5.0 with a one-line justification.

**Employment track**

| key | dimension | default weight |
|---|---|---|
| `role_fit` | Does the actual scope match the target role? | 0.30 |
| `domain_match` | Technology, industry, functional overlap | 0.20 |
| `company_context` | Stage, ownership, size, trajectory, culture | 0.20 |
| `comp_logistics` | Band, location, remote, travel, work auth | 0.15 |
| `trajectory` | Does this advance the stated direction? | 0.15 |

**Consulting track**

| key | dimension | default weight |
|---|---|---|
| `engagement_fit` | Does the need match the service lines offered? | 0.30 |
| `buyer_access` | Can the decision maker realistically be reached? | 0.20 |
| `economics` | Rate × duration against floor and target | 0.20 |
| `delivery_feasibility` | Deliverable solo or with the existing network? | 0.15 |
| `strategic_value` | Reference-ability, sector entry, recurring potential | 0.15 |

Weights live in settings, are editable from the UI, and are **recalibrated from the operator's
own judgments** once enough labels exist (`SPEC-MODULES.md` §5.2).

### 7.2 Holistic vs. weighted

The model returns its own `holistic`. The system computes `weighted = Σ(dimension × weight)`.
Both are stored and both are shown.

The **displayed** score is the model's holistic, because a well-prompted model captures
interactions a linear sum cannot: a perfect functional fit at an organization the operator would
be miserable in is not a 4.0.

**When `abs(holistic - weighted) > 0.75`, flag `needs_review`.** This divergence is the single
most useful quality signal in the system. It catches model sloppiness and mis-specified weights
alike, and it is the primary input to the active-learning queue.

### 7.3 Thresholds

| score | verdict | treatment |
|---|---|---|
| ≥ 4.0 | `apply` (employment) / `pursue` (consulting) | Shortlist. Auto-queue document selection. |
| 3.0 - 3.99 | `consider` | Review queue. No automatic work. |
| < 3.0 | `pass` | Archived, still searchable |

Overrides, applied system-side after the model responds:

1. Any hard blocker forces `pass`, with the blocker shown and the model's score preserved.
2. `legitimacy.status == reject` forces `pass`.
3. `legitimacy.status == suspect` caps the displayed score at 3.5 with a warning banner.
4. `work_auth_signal == explicit_no_sponsorship` where the profile requires sponsorship adds a
   blocker.
5. The threshold-derived verdict is authoritative for filtering; the model's own verdict is
   stored in `verdict_model` and any disagreement is logged and surfaced in analytics.

**The system never applies, never sends, never submits.** Its value is that it says no to
hundreds of opportunities so the operator can say yes to five.

---

## 8. Models

### 8.1 Configuration

```yaml
llm:
  backends:
    local:                                 # Ollama
      base_url: "http://127.0.0.1:11434/v1"
      api_key: "ollama"                    # ignored by Ollama, required by the client
      json_mode: json_schema               # response_format: {type: json_schema, ...}
      thinking_control: reasoning_effort   # reasoning_effort: "none"
      requires_num_ctx_modelfile: true     # see §8.2 — CRITICAL
    inprocess:                             # §8.1.1: bundled CPU embedding model, embed tier only
      engine: inprocess                    # no URL; nothing leaves the machine
    mini:                                  # the operator's Mac Mini oMLX stack, optional
      base_url: "http://192.168.2.25:8000/v1"
      api_key_env: OMLX_API_KEY
      json_mode: json_object
      thinking_control: chat_template_kwargs
    frontier:                              # optional: OpenRouter, operator's own key (§6.4); Tier 3, or any tier under Hosted mode
      base_url: "https://openrouter.ai/api/v1"
      api_key_env: OPENROUTER_API_KEY
      provider: openrouter                 # openrouter | openmodel | anthropic | openai | generic
      protocol: chat_completions           # chat_completions | responses | messages
      json_mode: json_schema               # strict dialect on hosted paths
      thinking_control: none
      external: true                       # the only kind of backend allowed off-machine
      hosted: { require_parameters: true, data_collection: deny, zdr: false, reasoning_effort: null,
                repetition_penalty: 1.05, frequency_penalty: null,     # penalties: §8.5, D-LLM-2
                repair_frequency_penalty: 0.3, tier1_reasoning_off: true }   # D-LLM-5
    openmodel:                             # optional: openmodel.ai (Responses protocol); same rule as frontier
      base_url: "https://api.openmodel.ai/v1"
      api_key_env: OPENMODEL_API_KEY
      provider: openmodel
      protocol: responses
      external: true
    anthropic:                             # optional: Claude API direct (Messages protocol); same rule
      base_url: "https://api.anthropic.com/v1"
      api_key_env: ANTHROPIC_API_KEY
      provider: anthropic
      protocol: messages
      external: true
    openai:                                # optional: OpenAI API direct (chat completions); same rule
      base_url: "https://api.openai.com/v1"
      api_key_env: OPENAI_API_KEY
      provider: openai
      protocol: chat_completions
      external: true
  tiers:
    embed:   { backend: local, model: "nomic-embed-text" }  # or { backend: inprocess, model: "all-MiniLM-L6-v2" } (§8.1.1)
    tier1:   { backend: local, model: "kestrel-tier1", temperature: 0.0, max_tokens: 300 }
    tier2:   { backend: local, model: "kestrel-tier2", temperature: 0.2, max_tokens: 4000 }
    tier3:   { backend: local, model: "kestrel-tier3", temperature: 0.3, max_tokens: 6000 }  # own model: §8.2 needs num_ctx 20480, above kestrel-tier2's 16384
    docsel:  { backend: local, model: "kestrel-docsel", temperature: 0.0, max_tokens: 1500 }
    trigger: { backend: local, model: "kestrel-tier1", temperature: 0.0, max_tokens: 600 }
    frontier: { backend: frontier, model: "anthropic/claude-sonnet-4.6", temperature: 0.3, max_tokens: 6000, timeout_s: 300 }  # §6.4 escalation only
  hosted_mode:                             # §6.4: off by default; Settings > Hosted writes enabled + acknowledged_at
    enabled: false
    acknowledged_at: null                  # ISO timestamp of the accepted disclosure; required for any hosted tier to be used
    redact_confidential: true              # redact the current employer before a hosted send; refuse when there is no map
    max_jd_tokens: 3000                    # floor of the hosted description slice (§6.4, D-LLM-3)
    jd_ceiling_tokens: 16000               # ceiling of the context-sized hosted slice; the disclosure states it
```

Every tier accepts `timeout_s` (per-request HTTP timeout; default 180). A timeout is a retryable
request error, never a quarantine.

Any tier except `embed` may name an external backend (`frontier`, `openmodel`, `anthropic`,
`openai`, or a `generic` one). Such a tier is only used under Hosted mode (§6.4); until the operator turns it on and
acknowledges the disclosure, the client refuses the call and `kestrel doctor` warns that the tier
will not be used. The frontier tier is the exception: it is used through per-item escalation
regardless, and it must stay on an external backend.

**Precedence for `temperature` and `max_tokens`:** the value declared on the individual prompt
in `PROMPTS.md` **wins**. The tier entry above is the fallback for any prompt that does not
declare one. Several prompts legitimately differ from their tier default (P4 is warmer than
tier3, P9 shorter than tier2), and the prompt is the more specific statement of intent.

**Hosted output budgets (D-LLM-1).** The prompt budgets are sized for the local models. Under
Hosted mode a tier assigned in `llm.hosted_tiers` takes `llm.hosted_tiers.<tier>.max_tokens`, or
when that is unset a hosted default (`tier1` and `trigger` 1200, `tier2` and `tier3` 8000,
`docsel` 4000; `config.HOSTED_MAX_TOKENS`), and that value is a **floor** under the prompt's own: a hosted
reasoning model spends output tokens thinking before it writes, and the local 300 and 4000
quarantined every reply with `finish_reason: length`. A cap bounds the bill, it does not set it.
Local tier budgets and Tier 3 escalation (the `frontier` tier) are unchanged. **The floor follows
the backend, not the mode (D-LLM-7):** a tier whose backend is external gets the same floor
(and its own `llm.tiers.<tier>.max_tokens`, if larger) even while the run mode is local, so a
tier pointed at a paid model by a settings edit is not left on the local 300 and 4000. A length
stop on a request that already had reasoning off says the reply needs more output tokens and
names the setting; it never suggests lowering the reasoning effort.

### 8.1.1 Embeddings: two engines, both on the machine

Embeddings carry the operator's text and have **no hosted path**: `LLMClient.embed` refuses an
external backend whatever the Hosted mode or Tier 3 state. The embed tier runs on one of two
engines, chosen by the backend `llm.tiers.embed.backend` names:

- **Ollama** (`backend: local`, `model: nomic-embed-text`, the shipped default): `/v1/embeddings`.
  nomic-embed-text is prefix conditioned (`search_document: ` / `search_query: `).
- **In-process** (`backend: inprocess`, `model: all-MiniLM-L6-v2`): the backend entry carries
  `engine: inprocess` and no URL. A bundled ONNX export of sentence-transformers'
  `all-MiniLM-L6-v2` (Apache-2.0, 384 dimensions, 256-token window, mean pooling, L2 normalized,
  no prefixes) runs on `onnxruntime`'s CPU provider with `tokenizers`. No torch, no network, no
  Ollama. Inference runs in a worker thread (`asyncio.to_thread`), one run at a time on at most
  four intra-op threads, so the UI stays responsive. Any other `model` value is refused.

The **`hosted_only` profile** (models.yml `embed_backend: inprocess`) writes `llm.tiers.embed`
to the in-process engine when hosted setup completes (`kestrel setup --hosted` or Connect under
Settings > Hosted with that profile active), so a BYOK install needs nothing local beyond Kestrel.
It is written to the local tier set, never `hosted_tiers`: the engine serves both modes.

**Packaging.** The model files are never committed and never downloaded at run time.
`scripts/fetch_embed_model.py` fetches the pinned archive at release-build time and verifies the
archive and every extracted file by SHA-256 (pins in `kestrel/llm/inprocess_embed.py`), writing
`kestrel/resources/embed_model/` plus a license `NOTICE.txt`; the PyInstaller spec refuses to
build without them, and the third-party notices include the model's section. At run time the
files are resolved through `paths.packaged_embed_model()` (`importlib.resources`) and re-verified
by hash on first load; a missing or altered file is `EmbedModelMissing` with the fix, and
`kestrel doctor` reports it as a WARN (few-shot and the reranker off, nothing else affected).

**Switching engines.** Each stored vector records its `model`. Readers (few-shot, reranker gate
and training, active-learning novelty, semantic search) compare only current-model rows, and the
reranker records the embed model it was trained on and is not loaded under another. Before each
embed drain, the worker enqueues an `embed` task for every opportunity whose vector is missing or
from another model, once per model (recorded as the `embed_swept_model` setting row, which is
state and excluded from the settings layer).

### 8.2 Context length — read this before anything else

**The OpenAI-compatible endpoint provides no way to set the context window.** Ollama applies the
model's default, which is far below what this system sends. A 6000-token job description sent to
a model at a 4096-token default is **silently truncated**. No error. The evaluation reasons about
half a description and every score is quietly wrong.

This is the single most likely cause of "it runs fine but the results are bad."

**Fix:** derived models with an explicit context window.

```
# kestrel/resources/models/Modelfile.tier2
FROM qwen3:14b
PARAMETER num_ctx 16384
```

```
ollama create kestrel-tier2 -f kestrel/resources/models/Modelfile.tier2
```

Ship `kestrel/resources/models/` with a Modelfile per tier and a `kestrel models build`
command (also a button in the setup window) that creates them. Preflight must:

1. Verify each configured model exists.
2. Read its effective `num_ctx` via the **native** `/api/show` endpoint, which does expose
   parameters, and **fail** if it is below `max_jd_tokens + prompt overhead + max_tokens`.
3. Print the required value in the failure message.

**Sizing is per tier, against that tier's own input budget.** A single global rule is wrong and
would make the tier 1 model fail a check it has no reason to pass.

| Tier | Input budget | `max_tokens` | Required `num_ctx` | Ship |
|---|---|---|---|---|
| tier1 | 1200 description + 400 profile + 400 instructions | 350 | ~2400 | **4096** |
| tier2 | 6000 description + 2500 profile + 1500 few-shot + 1000 instructions | 4000 | ~15000 | **16384** |
| tier3 | 6000 + 2500 + 2000 research + 1000 | 6000 | ~17500 | **20480** |
| docsel | 2000 catalog + 1500 evaluation extract + 500 | 1500 | ~5500 | **8192** |
| trigger | 1500 filing excerpt + 400 | 600 | ~2500 | **4096** |

Preflight computes the requirement from the live settings for each tier and compares against
that tier's actual `num_ctx`. Larger `num_ctx` costs VRAM for KV cache, which is exactly why this
is per-tier rather than one large default.

**Verify empirically, not just by reading `/api/show`.** Preflight also runs a needle test: place
a distinctive token near the start of a filled-to-budget prompt and ask the model to return it.
A model whose context is quietly smaller than reported fails this and passes the metadata check.

### 8.3 Hardware guidance

a 16GB-VRAM NVIDIA GPU, 64GB system RAM.

- **Fully resident:** a 14B at Q4_K_M is roughly 9GB of weights, leaving 6-7GB for KV cache. The
  sweet spot for tier 2.
- **MoE alternative:** a 30B-A3B class model at Q4 is around 18GB and will partially offload,
  but with ~3B active parameters per token the penalty is far smaller than for a dense model of
  the same size, and the 7950X3D handles the CPU-side layers well. Benchmark it; it may beat the
  dense 14B at similar speed.
- **Avoid dense models above ~20B** at usable quantization. The offload penalty dominates a
  multi-hundred-item run.
- Tier 1 should be a genuinely small model (4B-8B) and its `num_ctx` kept low, so it costs
  almost nothing while the reranker is still training.

**The local-default floor (D-SETUP-6).** Setup recommends a local hardware profile only where
the models run at a usable speed: an NVIDIA GPU with at least 11 GB of VRAM, or Apple silicon
with at least 14 GB of unified memory. Every other machine (a smaller NVIDIA card, integrated
graphics, CPU only at any RAM, an AMD or Intel GPU, which detection does not identify, or
nothing detected) is recommended `hosted_only`: a Tier 2 evaluation on a CPU takes minutes per
job, and a hosted provider under the operator's own key uses no local memory. The reason is
stated plainly and specifically (for example "Apple silicon with 8 GB unified memory: too little
to run the models alongside the OS"). Local profiles stay available to choose; only the default
changes. When Hosted is recommended and the operator chooses Local or Both, the Setup page shows
a prominent warning that leads with the decision (D-SETUP-7).

**Ollama only when a tier uses it (D-SETUP-8).** Ollama is needed iff a tier in the effective
`llm.tiers` names the `local` backend. When none does, preflight reports every Ollama check
(reachable, models present, `num_ctx`, needle, thinking, schema, `OLLAMA_NUM_PARALLEL`,
`OLLAMA_KEEP_ALIVE`) as information, "not needed: no tier runs on Ollama", never as a warning or
a blocking failure, and the Today page and status rail do not report Ollama as missing.

### 8.4 Disabling thinking — backend-specific and silently wrong if crossed

Reasoning models multiply tokens per item by five to ten. Across 500 items that is a 30-minute
run becoming a 5-hour one, and reasoning traces leak into structured output.

| Backend | Mechanism |
|---|---|
| **Ollama** (`/v1/chat/completions`) | `reasoning_effort: "none"`, or `reasoning: {effort: "none"}`. Both are listed as supported request fields in Ollama's OpenAI-compatibility documentation. |
| **oMLX / vLLM / SGLang** | `extra_body: {chat_template_kwargs: {enable_thinking: false}}` |
| Anything else | Check its docs. Assume nothing. |

`chat_template_kwargs` is **not** an Ollama parameter and is silently dropped there. A top-level
`enable_thinking` flag and a `/no_think` prefix are ignored almost everywhere.

Regardless of backend, strip `<think>…</think>` spans before parsing, and have preflight run a
one-shot probe per model asserting no thinking span survives.

### 8.5 Structured output

- `json_mode: json_schema` → `response_format: {"type": "json_schema", "json_schema": {"name":
  ..., "schema": {...}}}`. Ollama supports structured outputs on the OpenAI-compatible endpoint
  via `response_format`. (The bare `format` parameter belongs to Ollama's **native** `/api/chat`.
  Do not confuse them.)
- `json_mode: json_object` → `response_format: {"type": "json_object"}` plus the schema in the
  system prompt.
- **Constrained decoding supports only a subset of JSON Schema.** Use `type`, `properties`,
  `required`, `enum`, `items`, `$ref`/`$defs`. Do **not** use `patternProperties`, `oneOf`,
  `allOf`, `if/then`, or rely on `additionalProperties: false` for correctness. An unsupported
  keyword produces an unsatisfiable grammar, not a warning.
- **`maxLength` and numeric bounds are advisory.** They are ignored by the grammar and enforced
  by pydantic afterward, which turns ordinary verbosity into a repair call. **Truncate overlong
  strings, never reject them.** Reserve validation failure for structural problems: missing
  fields, wrong types, invalid enum values.
- Validate with pydantic. On failure, one repair call at temperature 0 quoting the error. On
  second failure, quarantine with the full prompt and raw response. Never crash a run.
- **Hosted JSON mode is per model (D-UI-48).** On OpenRouter the model list's
  `supported_parameters` decides, and is recorded per model under `llm.hosted_model_caps` when
  the model is assigned (Connect, Models page overrides): `structured_outputs` gets the strict
  `json_schema` with `provider.require_parameters: true`; `response_format` alone gets
  `json_object` with the schema in the system prompt and `require_parameters: false`; neither
  gets no `response_format`, the schema and a reply-only-JSON instruction in the system prompt,
  and the picker label "no JSON guarantee". Labels: "structured", "JSON mode", "no JSON
  guarantee". Validation and the repair-then-quarantine rule are identical for all three.
  Connect and `kestrel setup --hosted` pick automatic defaults only from schema-capable models.
- **Enum lists are deduplicated, never rejected (D-LLM-2).** A `signals`-style enum array keeps
  first-seen order, drops repeats, and is capped at its PROMPTS.md `maxItems`, which the schema
  also emits (advisory; dropped for the Claude API, which rejects array bounds).
- **Generation loops are a failed attempt (D-LLM-2).** A reply in which one JSON string value
  occurs more than 20 times is degenerate. At a `length` stop it is reported as a loop and gets
  the one repair (quoting only its first 600 characters back), then quarantine with the raw reply
  kept; it is never waited out or accepted. On OpenRouter a mild `repetition_penalty` (1.05,
  `llm.backends.<name>.hosted.repetition_penalty`; `frequency_penalty` beside it, off by
  default) rides on the request where the model lists the field, or where nothing is known and
  `require_parameters` is off, so it can never route a request to "no endpoint". The Tier 1
  prompts ask for a reason under 20 words and each signal once.
- **The repair after a loop resists looping (D-LLM-5).** It runs with reasoning off, names the
  looping field and value, asks for each value once (evaluation prompts: "at most 3 legitimacy
  flags") and to stop after the closing brace, and runs at the prompt's `repair_max_tokens`
  (front matter: P2 2,500, P2C 3,000, P1/P1C 600) when reasoning is off for it, so a second loop
  fails fast. On a hosted tier that cap is never below the prompt's own `max_tokens` (P2 4,000,
  D-LLM-6): a legitimate P2 reply runs 2,500 to 3,000 tokens, and a 2,500 cap cut it off. On OpenRouter that repair alone adds `frequency_penalty`
  (`hosted.repair_frequency_penalty`, 0.3) when the model lists it and not
  `repetition_penalty`; neither listed, nothing is added, and `require_parameters` is never
  turned off for a penalty.
- **Every repair after a loop or a reasoning overrun runs with reasoning off (D-LLM-5).**
- **Hosted Tier 1 starts with reasoning off (D-LLM-5).** Triage is a short classification: on
  an OpenRouter model whose record lists reasoning and marks it optional, with no operator effort
  and no prompt hint, the first Tier 1 attempt sends `reasoning: {effort: "none"}`
  (`hosted.tier1_reasoning_off`, default true).
- **Reasoning overruns re-ask once with reasoning off.** A `length` stop with an empty answer, or
  with at least half of the output tokens spent reasoning, is re-asked once with OpenRouter's
  `reasoning: {enabled: false}` (when the operator has set no effort); a second overrun
  quarantines with the D-LLM-1 message naming the setting that raises the budget.
- **A reply cut off at its cap is asked again once, larger (D-LLM-7).** A `length` stop that is not
  a loop and not a reasoning overrun (a long legitimate answer cut mid-object) on an **external**
  backend is retried once
  with twice the output budget the request carried, capped at the model's context window minus the
  input it was billed for (`hosted_model_caps`); with no room to grow it quarantines at once. A
  second cut-off quarantines with the raw reply kept, the limit it hit named (`max_tokens N`,
  which the Pipeline reads as "the reply ran out of room") and a note that it was retried. This
  retry is separate from the schema repair: it neither uses nor is counted against it, a repair
  after it keeps the larger budget, and both attempts' tokens and cost go in the record. The
  reasoning-off re-ask and this retry are each taken at most once per call, so a call makes at
  most three attempts before the one repair. A local backend is never retried larger: its
  `num_ctx` is sized per tier (8.2), and a doubled output budget could push the prompt past it,
  which Ollama truncates silently; a local cut-off quarantines at once.

### 8.6 Benchmark harness

`kestrel bench`, also a UI page. Accepts `--models` and `--prompt-version` so exactly one
variable changes per comparison. Reports per model and per track:

- First-attempt JSON validity rate (a target above 98%; the code measures and reports it but does
  not enforce it)
- Mean tokens in and out, mean latency, items per minute at configured concurrency
- Agreement with the gold set: exact-verdict match rate and mean absolute score error
- Projected wall-clock for a 500-item run

Ship `evals/goldset.employment.jsonl` and `evals/goldset.consulting.jsonl` with 20 synthetic but
realistic entries each, clearly marked as placeholder data. **The gold set is replaced by the
operator's own labeled decisions as they accumulate** (`SPEC-MODULES.md` §5.5). Model selection
is an empirical question and this is how it gets answered.

---

## 9. Privacy and data handling

- **Never put organization names in file metadata, PDF properties, headers, footers, or
  generator strings.** Playwright PDF generation must set empty header and footer templates
  explicitly, and the HTML must embed no generator string.
- Document roles support **redaction** (for example "a regional logistics company,
  ~300 employees"). A per-render flag toggles real names versus redacted labels.
- **Confidential search mode**, a setting: generated documents and drafts omit the current
  employer entirely, and list views hide it.
- All processing is local by default. Tier 3 against a hosted backend is the only path that can
  transmit operator data to a third party. It is opt-in per opportunity and the UI states
  plainly what will be sent before the operator confirms.
- The operator's real `profile.yml` (it resolves through `paths.config_file()`, so normally
  `%LOCALAPPDATA%\Kestrel\config\profile.yml`), the data directory, logs, and `.env` are never
  committed. The shipped example profile (`profile.example.yml`) is fictional.
- Contact records hold only publicly listed business contact information. No personal addresses,
  no personal phone numbers, no inferred emails.
- **Local backup:** a `kestrel backup` command and a UI button produce a single timestamped zip
  of the database, config, and generated documents. Restore from the setup window.

---

## Appendix A — ATS API shapes, verified live 2026-07-30

Build adapters against these, not against assumptions. Preflight re-checks each shape so a
silent upstream change is caught early: a missing field is a **warn** that names the field, not a
crash.

### Greenhouse

`GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` → `200`
Detail: `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` → `200`

Verified against `token=anthropic`: 401 jobs, **5.8MB in a single response**.

```
{ "jobs": [ ... ], "meta": { ... } }
```

Per job: `absolute_url`, `internal_job_id`, `id`, `requisition_id`, `title`, `company_name`,
`updated_at`, `first_published`, `application_deadline`, `language`, `content`, `departments[]`,
`offices[]`, `metadata[]` (`{name, value}`), `data_compliance[]`.

**Gotchas:** `location` is an **object** `{"name": "..."}`, not a string. `content` is
**HTML-escaped HTML** — unescape, then convert to markdown. No pagination; a large board is one
huge payload, so stream it with a 60s timeout and do not hold two boards in memory at once.
`metadata[]` often carries useful custom fields such as "Location Type".

### Lever

`GET https://api.lever.co/v0/postings/{company}?mode=json` → `200`, a **bare JSON array**.

Verified against `company=ro`: 51 postings. An unknown slug returns **404**, so validate at
source-creation time rather than storing a dead source.

Per posting: `id`, `text`, `categories`, `createdAt`, `country`, `workplaceType`, `hostedUrl`,
`applyUrl`, `description`, `descriptionPlain`, `descriptionBody`, `descriptionBodyPlain`,
`opening`, `openingPlain`, `additional`, `additionalPlain`, `lists[]`, `salaryRange`.

**Gotchas:** **the job title is `text`, not `title`** — the most common Lever integration bug.
Location is `categories.location`, with `categories.allLocations[]` for multi-site;
`categories.commitment` is employment type; `categories.team` is the department. `createdAt` is
epoch **milliseconds**. `salaryRange` is frequently `null`. `descriptionPlain` is cleaner than
`description`. `lists[]` holds structured requirement and responsibility sections worth keeping
separately for the fit analysis.

### Ashby

`GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` → `200`

Verified against `token=Ashby`: 58 jobs. `{ "jobs": [...], "apiVersion": "..." }`

Per job: `id`, `title`, `department`, `team`, `employmentType`, `location`,
`secondaryLocations[]`, `isRemote`, `workplaceType`, `address`, `publishedAt`, `isListed`,
`jobUrl`, `applyUrl`, `descriptionHtml`, `descriptionPlain`, `compensation`.

**Gotchas:** the token is **case sensitive**. `compensation` only populates when
`includeCompensation=true` **and** the job's `shouldDisplayCompensationOnJobPostings` is true;
absence means unknown, not zero. Filter on `isListed`. `descriptionPlain` is preferred.

### Workday

`POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`
Headers: `Content-Type: application/json`, `Accept: application/json`
Body: `{"limit": 20, "offset": 0, "searchText": "", "appliedFacets": {}}` → `200`

Verified against `nvidia.wd5.myworkdayjobs.com` site `NVIDIAExternalCareerSite`:
`{"total": 2000, "jobPostings": [...]}`

Per posting: `title`, `externalPath`, `locationsText`, `postedOn`, `bulletFields[]`
(usually the requisition id).

**Gotchas:** this is a **POST**, unlike every other ATS here. `limit` maxes at 20, so paginate
with `offset` and respect `total`. The list response has **no description**; fetch it per job
from `GET .../wday/cxs/{tenant}/{site}/job{externalPath}`, which returns `jobPostingInfo` with
`jobDescription` (HTML), `startDate`, `timeType`, `jobRequisitionId`, and often
`hiringOrganization`. `postedOn` is a human string like "Posted Today", so prefer the detail
call's `startDate`. The tenant, data-center segment (`wd1`, `wd3`, `wd5`…), and site name all
vary per employer and must be config, not derived. Rate-limit to roughly 1 request/second and
send a normal browser User-Agent.

### SmartRecruiters

`GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=0` → `200`

Verified against `company=Visa`. `{"offset":0,"limit":n,"totalFound":n,"content":[...]}`

Per posting: `id`, `name`, `uuid`, `jobAdId`, `refNumber`, `company`, `location`, `industry`,
`department`, `function`, `typeOfEmployment`, `experienceLevel`, `customField[]`, `ref`,
`releasedDate`, `creator`.

**Gotchas:** the title field is `name`. The list has **no description**; fetch
`GET https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}` for `jobAd.sections`
containing `companyDescription`, `jobDescription`, `qualifications`, `additionalInformation`,
each with `title` and `text` (HTML). Company identifiers are case sensitive.

### SEC EDGAR full-text search — consulting trigger events

`GET https://efts.sec.gov/LATEST/search-index?q={query}&forms=8-K&dateRange=custom&startdt=&enddt=`
→ `200`

Verified: `q="chief information officer"&forms=8-K` returned 8,436 hits. Response is
Elasticsearch-shaped: `{"took":n,"hits":{"total":{"value":n},"hits":[{"_id":..., "_source":
{...}}]}}`. `_id` encodes accession number and filename; build the document URL as
`https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{filename}`.

**Gotchas:** SEC policy **requires a descriptive User-Agent** identifying the requester with a
contact address. Requests without one are blocked. Respect the 10 requests/second ceiling and
back off on 403. Item 5.02 covers officer departures; Item 1.05 covers material cybersecurity
incidents and has been mandatory since 2023, making it a reliable feed.

### SAM.gov — not verified

`GET https://api.sam.gov/opportunities/v2/search` requires a **free API key**, which was not
available at spec time; an unauthenticated request returns 404. The adapter must detect a missing
key and disable the source with a clear message rather than failing the run. Relevant NAICS
codes: 541512, 541611, 541519.
