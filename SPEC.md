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
| Database | SQLite WAL, SQLAlchemy 2.x async (`aiosqlite`) + Alembic | Every connection uses `synchronous=NORMAL`; Alembic runs a sync engine against the same file (D-UI-174) |
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
| Tests | `pytest`, `pytest-asyncio`, `pytest-xdist`, `respx`, `playwright` | Isolated processes, temporary per-test data and ports; serial parity required (D-UI-172) |
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

**No profile, no persona (D-PROF-4, issue #70).** The only profile the app reads is the operator's
own `profile.yml`. When there is none (a fresh install before setup writes it, or an unreadable
file), every consumer sees an **empty** profile: no proof points, narrative, target or excluded
titles, pay floor or must-haves; the prompts render "unspecified" and Tier 0 filters nothing on
it (empty lists and no floor never reject; hard rule 4). The shipped fictional
`profile.example.yml` is documentation, placeholder hints and test data; it is never the
fallback, so no role is scored, sketched or storied against a fictional person. One loader,
`kestrel.pipeline.profile`, reads the file for every caller.

| column | type | notes |
|---|---|---|
| id | int pk | |
| track | enum | `employment` \| `consulting` |
| is_active | bool | both may be active |
| first_name, last_name, email, phone, location | text | `first_name` is required and used to greet the operator; `last_name` may be empty. `full_name` is derived (`first_name last_name`, trimmed) and is what a resume header prints. A migration (0023) splits an existing `full_name` at its first space and keeps the original text (D-UI-117) |
| headline | text | |
| narrative | text | 300-600 words, first person, track-specific framing |
| work_auth | text | |
| requires_sponsorship | bool | true if the operator needs visa sponsorship; the Tier 0 sponsorship blocker (§6.1) keys off it |
| geo_rules | json | see §6.1 |
| must_have, must_avoid | json | weighted statements |
| proof_points | json | `{key, claim, evidence, metric}` — the only achievements any prompt may cite |
| employment_terms | json | employment track: `target_titles`, `anti_titles`, `seniority_floor`, `comp_floor_usd`, `comp_target_usd`, `comp_floor_margin_pct` (0, 2, 5, 10 or 15; default 5; 0 = exact; the near-floor margin, D-RULES-2), `excluded_employment_types` |
| consulting_terms | json | consulting track: `day_rate_floor`, `day_rate_floor_margin_pct` (same meaning as the employment margin), `day_rate_target`, `min_engagement_days`, `max_concurrent`, `service_lines`, `target_org_size`, `target_sectors`, `excluded_sectors`, `travel_tolerance` |
| updated_at | ts | |

**`title_suggestion`** (P15, D-TTL-3) — job titles the model proposes for `employment_terms.target_titles`,
for the operator to accept or dismiss. **Nothing is ever added to `target_titles` without an
explicit Accept.**

| column | type | notes |
|---|---|---|
| id | int pk | |
| title | text | unique case-insensitively across every status, so a title never comes back once seen |
| why | text | one sentence on why it fits, from the operator's own document |
| evidence | json | `[{slot_key, text}]`: the operator's own bullets the title rests on (display only) |
| status | enum | `suggested` \| `accepted` \| `dismissed` |
| prompt_version | text | e.g. `title_suggest/v1` |
| created_at, decided_at | ts | `decided_at` is set on Accept or Dismiss |

A queued `title_suggestions` task writes the rows: after a resume import, about once a week, and
after every 10 roles newly marked applied (a manual "Suggest again" also queues one). Accept adds
the title through the same profile edit as the Employment form (so the generated searches and the
Tier 0 re-check follow); Dismiss records it so it is never suggested again.

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
`research`, `org_summary`, `contacts`, `liveness`, `calibrate`, `train_reranker`,
`consolidate_stories`, `prep_pack`, `negotiation`, `follow_up`, `trigger_classify`,
`pursuit_sweep`, `bench`.
(`org_summary` is the company summary of `SPEC-MODULES.md` §2.1; `bench` is the Model check run.)

**`embed` is enqueued by `normalize`, unconditionally, for every opportunity.** Two separate
subsystems depend on vectors existing: the reranker's PCA features
(`SPEC-MODULES.md` §5.4), and MCP semantic search. If nothing enqueues `embed`, both fail
silently and late.

**`pursuit_sweep`** runs daily from the scheduler and is what makes next-action and ghosting
notifications possible: it finds pursuits with a `next_action_at` due today and pursuits whose
`last_activity_at` has passed the empirically-derived ghosting threshold.

**`run`** — a named batch. `id`, `name`, `track`, `started_at`, `finished_at`, `status`
(`running`/`stopping`/`done`/`failed`), `counts` json, `notes`.

**`notification`** — `id`, `kind`, `severity`, `title`, `body`, `opportunity_id`, `pursuit_id`,
`track`, `created_at`, `read_at`, `dismissed_at`, `resolved_at`, `delivered_channels` json. The
bell counts rows with `read_at` null and `dismissed_at` null; the Dismissed tab shows rows with
`dismissed_at` set; a problem kind stays under Needs attention while `resolved_at` is null; a
dismissed row is deleted 30 days after `dismissed_at` (D-UI-94). `SPEC-INTERFACES.md` §6.

**`setting`** — key/value UI overrides.
**Precedence, highest first:** `setting` row → environment variable → YAML → code default.
Re-read on every request and every task lease, so a UI change takes effect on the next unit of
work with no restart. Only keys declared in the typed settings model are overridable.

**`saved_view`** — `id`, `name`, `track`, `filters` json, `sort` json, `is_default`.

**State the interface owns (D-UI-92 to D-UI-129).** Where each piece of overhaul state lives:

| State | Storage |
|---|---|
| Save for later | `opportunity.saved_at` (nullable ts); the Saved chip lists rows where it is set |
| Not for me and its reason | The existing hidden state of the role, plus a `label` (`source` `manual`, `dimensions` for role fit, comp and logistics, or career direction, `rationale` the chosen reason); the role stays recoverable under Hidden |
| Company hidden | `org.hidden_at` (nullable ts); roles of a hidden org are left out of Pipeline and Today, never deleted |
| Scene, touches, avatar | `setting` rows `ui.scene`, `ui.scene_daily` (default true), `ui.scene_rotation`, `ui.scene_animate`, `ui.touches_enabled`, `ui.touches_off` (single touches), `ui.touch_groups_off` (a whole group: `seasons`, `civic` or `lights`), `ui.avatar` (`initial`, six marks or `photo`; the photo file lives in the data folder). Values are validated in `kestrel/ui_settings.py`; a stored value that no longer validates reads as its default |
| Appearance and window | `ui.theme`, `ui.scheme`, `ui.text_size`, `ui.reduce_motion`, `ui.classic_nav`, `ui.close_to_tray` |
| Region | `region.date_format` (`MM/DD/YYYY` default, `MM/DD/YY`, `DD/MM/YYYY`, `DD/MM/YY`, `YYYY-MM-DD`, `Mon D, YYYY`), `region.time_format` (`12h` default, `24h`) (country and currency are fixed: US, USD) |
| Notification sound | `notify.sound` (default on), `notify.sound_name` (`chime` default, `soft_click`, `bell`), `notify.sound_volume` (0 to 1, clamped), `notify.quiet_hours` (`{enabled, start, end}`, `HH:MM`, default 21:00 to 07:00, off) |
| Score cutoffs | `thresholds.apply`, `thresholds.consider` |
| Onboarding | `onboarding.state` (step, visited, skipped, completed) |
| Contacts import | `contact_import`, `connection_position`, and `contact.source` = `linkedin_import` (SPEC-MODULES.md §2.3) |
| Company summary | `org_summary` (SPEC-MODULES.md §2.1) |
| Near-floor margin | `profile.employment_terms.comp_floor_margin_pct`, `consulting_terms.day_rate_floor_margin_pct` (§4.1) |

Key names are the contract for the build; the exact `ui.*` spellings may be adjusted in the code
with a matching spec edit.

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
| **Paylocity Recruiting** | `GET https://recruiting.paylocity.com/recruiting/jobs/All/{id}` (JSON in `window.pageData`) | 200, HTML (verified 2026-10-21) |
| **Workable** | `POST https://apply.workable.com/api/v3/accounts/{slug}/jobs` body `{}` | 200, `{results, total}` (verified 2026-10-21 from the maintainer's network) |
| **Recruitee** | `GET https://{slug}.recruitee.com/api/offers/` | 200, `{offers}` with full descriptions (verified 2026-10-29) |
| **Careers page** (generic) | `GET {the pasted careers page}`, then at most 25 job pages | 200 HTML with schema.org `JobPosting` JSON-LD (job page shape verified 2026-10-29 on one public page; list, `@graph` and stub shapes from schema.org's documentation) |
| **Teamtailor** | `GET https://{domain}/jobs.json` (JSON Feed; fallback `/jobs.rss`) | 200, `application/feed+json`, full descriptions (verified 2026-10-29) |
| **Personio** | `GET https://{slug}.jobs.personio.de/xml` (also `.jobs.personio.com`) | 200, `text/xml`, `<workzag-jobs>` (verified 2026-10-29) |
| **Breezy HR** | `GET https://{slug}.breezy.hr/json` (list) and `GET https://{slug}.breezy.hr/p/{friendly_id}` (description) | 200 (verified 2026-10-29) |
| **ADP Workforce Now** | `GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions?cid={cid}&$top=20&$skip=1` | 200, no auth (verified 2026-10-21) |
| **Rippling** | `GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` | 200, bare array (verified 2026-10-21) |

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
per run. All fetched roles, from searches and company boards alike, pass the existing smart
filter before storage (D-SRC-13, superseding D-SRC-8 and D-SRC-12): a rejected title is skipped
and counted as `dropped_unrelated_title` on its scrape run. The matching rule is unchanged.
The first 25 such titles of each run are kept as a sample (D-SRC-17, below).
Hand-pasted roles bypass this filter. The background lane remains only for previously stored
rows. Before release and scoring, those rows are rechecked against the current country rule and
smart filter; clear mismatches become `t0_reject` with an explanation, without a model call.
Survivors score only on local tiers, when idle up to `pipeline.lane_daily_cap` (200) a day, or
through "Score everything now" after confirmation. Hosted mode or an external scoring tier
holds the surviving backlog, even after confirmation; model overrides cannot bypass this.
Historical precision is measured, not assumed: `python -m kestrel.evals.smart_filter_report`
reads the database (SELECT only) and reports per lane the roles, scored, fits and not-fits, the
fast lane's precision (fits over scored roles) and the lane miss rate (fits that sat in the
background lane over all fits). A figure from fewer than five observations prints as insufficient.

**USAJobs (US federal jobs; D-SRC-15).** A search board with its own adapter (`usajobs`), added from
the job-board sheet (group "Official public access") or the USAJobs card on the Sources page. It
asks `GET https://data.usajobs.gov/api/search` with the user's own free key (`Authorization-Key`)
and the email they requested it with (`User-Agent`, which USAJobs requires). The key is saved in the
app and stored in `.env` as `USAJOBS_API_KEY` exactly as an online-model key is; the email is an
ordinary setting (`sources.usajobs_email`) copied into each USAJobs source as `contact_email`. Neither
the key nor any secret is in a config row, the database, a log or diagnostics. Search terms are the
source's `keyword`, else the employment profile's target titles (up to 5, one search each) with an
optional `location`; a place of "Remote" asks for remote roles only. Each search reads up to
`max_pages` pages (default 3) of `results_per_page` (default 100, at most 500) posted in the last
`date_posted_days` (default 7); a run makes at most 12 requests at 2 a second, robots.txt checked.
Mapping: title, agency as organization, the apply link (`ApplyURI`, else the announcement), one
place or the display text ("Multiple Locations"), pay with its interval (`PA` year, `PH` hour,
`PM` month, `PW` week, `PD` day; unpaid or fee-basis roles carry none), the posting date, the
closing date (in the description and the raw record), and the summary, duties, qualifications,
requirements, education and benefits. `RemoteIndicator` true is remote, `TeleworkEligible` true is
hybrid, both explicitly false is on-site, anything else stays unknown. A missing key or email is a
`partial` run with the sentence "USAJobs needs your API key and email" (no request is made, no
failure counted); a 429 or 5xx is `partial` and keeps the pages already read; a 401 or 403 is a
failed attempt that says the key was not accepted. Every role goes through the same ingest path as
any other source, so the D-SRC-13 area and title drop applies. Kestrel never applies: the role links
to the official announcement, and it is described as not run or endorsed by OPM.

**Adzuna (US job ads; D-SRC-15).** A search board with its own adapter (`adzuna`), added from the
job-board sheet (group "Official public access") or the Adzuna card on the Sources page. It asks
`GET https://api.adzuna.com/v1/api/jobs/us/search/{page}` (country fixed to `us`) with the user's own
free `app_id` and `app_key` as query parameters. Both are saved in the app and stored in `.env` as
`ADZUNA_APP_ID_KEY` and `ADZUNA_APP_KEY`, as an online-model key is; neither is in a config row, the
database, a log or diagnostics (the log file masks credential-like query values). Search terms are
the source's `keyword`, else the employment profile's target titles (up to 5, one search each) with
an optional `location` (a place of "Remote" adds the word to the search; Adzuna has no remote
filter). Budget: the free key allows about 250 calls a day, so a check reads at most `max_pages`
pages (default 2, at most 5) of 50 ads posted in the last `date_posted_days` (default 7), and at
most 8 requests in all, at 1 a second, never retried on a 429; the card says so. The robots check is
skipped for this API call (D-SRC-15). Mapping: title, company, `location.display_name` (else the
area as "City, State"), the redirect URL as the posting link, `created`, contract time and type, and
`salary_min`/`salary_max` as annual USD pay only when `salary_is_predicted` is 0 (a predicted salary
stays in the raw record). Adzuna sends a description snippet only, so the body ends with the
sentence "Adzuna supplies only the start of this posting. Open the original posting for the full
text." and `raw.description_partial` is true. A missing key is a `partial` run with the sentence
"Adzuna needs your app ID and app key"; a 429 or 5xx is `partial` and keeps the pages already read;
a 401 or 403 is a failed attempt that says the keys were not accepted. Every role goes through the
same ingest path, so the D-SRC-13 drop applies. Credit: the source is named "<search> on Adzuna" and
the Posting tab links to the Adzuna page, where the user applies.

**Remote boards: Himalayas and We Work Remotely (D-SRC-15).** Two keyless job-feed adapters
(`himalayas`, `weworkremotely`), added from the job-board sheet (group "Remote jobs"). The existing
`rss` kind is a consulting signal source and is not reused. Himalayas asks
`GET https://himalayas.app/jobs/api/search?q=<keyword>&country=US&sort=recent&page=N` (20 a page; the
source's `keyword`, else the profile's target titles; `max_pages` default 3; at most 12 requests, 1
a second, robots.txt checked); its terms require that Apply uses the API's `applicationLink`
(`apply_url`) and that Himalayas is credited with a link, and that expired jobs are dropped
(`expiryDate`). We Work Remotely reads its public feed `https://weworkremotely.com/remote-jobs.rss`
once a check (or one category feed, `category` such as `remote-programming-jobs`); a role the
board limits to another region ("Europe Only") is not passed on, anything unclear is kept, an
expired role is dropped, and each role links to its page on the board. Both mark roles remote, map
the place as "United States", "Remote (worldwide)" or the board's own words, and run through the
D-SRC-13 drop. A 429, 5xx or (for We Work Remotely) its 403 bot check is a `partial` run that keeps
what was read. Credit: the source is named after the board and the Posting tab links to it. Remotive
is not built (D-SRC-15: its robots.txt disallows its API path and its terms could not be read).

**Careers page (`careers_page`; B4, D-SRC-15, D-UI-102).** The operator pastes any employer's careers
page link on the Sources sheet. Many employers (and sites on iCIMS, Taleo, Paycor, UKG or a custom
build) embed each opening as schema.org `JobPosting` JSON-LD for search engines, and Kestrel reads
only that, with `html.parser` and `json` (never a regular expression over the page): a single
object, a list, an `@graph` container, or items inside an `ItemList`; a block that is not valid
JSON is skipped. Mapped: title, hiringOrganization (name, `sameAs`), jobLocation (locality, region,
country), `jobLocationType` `TELECOMMUTE` (remote; the place falls back to
`applicantLocationRequirements`), baseSalary (`minValue`/`maxValue`/`value`, currency and
`unitText` hour, day, month, year; a weekly figure is stated per year), datePosted, description
(HTML), employmentType, identifier and url. A role whose `validThrough` date has passed is skipped.
The run, through the shared client (descriptive User-Agent, 1 request a second, robots.txt checked,
public addresses only, 2 MB a page, the same guarded fetch as the link check): (1) a link that is a
hiring system Kestrel already reads (`board_discovery.board_from_url`, or markup that
`board_from_markup` recognizes) is read by that system's adapter instead; (2) otherwise every
JobPosting on the page is mapped, and a posting that is only a stub (no description) with its own
page on the same site is read from that page; (3) a page with no JobPosting but links to job pages
(a path segment such as `/job/`, `/jobs/` or `/position/` with something after it, or a job id in
the query) is followed one level, at most 25 job pages a run, and the run says when links were left
for later; (4) a page that yields nothing is a `partial` run with "This page lists its jobs with
JavaScript, which Kestrel cannot read yet.", never a silent zero. A 429 is partial, a 404 or a
robots.txt refusal is a failed run with a plain sentence. Every role then goes through the D-SRC-13
area and title drop like any other source. The company page is the most direct copy of a posting
(dedup rank 0). A page that renders its list in the browser with no JobPosting in its HTML is not
readable; the card says so.

**RemoteRocketship (optional, paid, discovery only; D-SRC-14).** For people who already pay for
RemoteRocketship API access, a `remoterocketship` source asks its jobs API for postings and keeps
**only each posting's own employer link** (`url`). It stores no title, description, company data
or copy of the reply, and holds the reply in memory for the run only (their terms limit caching to
24 hours and forbid archiving). Each link goes the way a JobSpy posting link goes (D-SRC-7): a
recognized company board (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Rippling, ADP,
Paylocity, Workable, Recruitee, Teamtailor, Personio, Breezy HR) becomes a found board read by its own adapter; any other link is read from the
employer's public page (schema.org `JobPosting` data, robots.txt checked, one polite request, at
most 20 pages a run). A link nothing can read is dropped and counted in the run ("N links could
not be read from the employer"). Off by default; the key is `REMOTEROCKETSHIP_API_KEY` (or the
`api_key_env` named in the config) in `.env` and never in a config row. A run makes at most
`max_pages` requests (default 2, 50 jobs each) against the provider's 500 requests or 3,000 jobs
per UTC day; a 429 makes the run partial (never a source failure, so it cannot auto-disable the source) and
keeps the links already collected, which on the first page is none. A link on the provider's own
site is dropped and counted unreadable, never fetched. With no `filters` on the source, the
employment profile supplies `jobTitleFilters` (target titles) and `locationFilters` (onsite metros);
filters set on the source replace them.

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

The operator arrives with applications already made elsewhere. Settings > Your data > Import past applications takes that
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
operator wants to see each listing. The pursuit's detail and the Applications page both carry the
toggle. The suppression applies to every applied pursuit, imported or not.

---

## 6. The tiered pipeline

An opportunity flows strictly downward and may exit at any tier.

### 6.1 Tier 0 — deterministic, no LLM

Microseconds. Target: eliminate 60-80% of raw ingest. Rules live in
`kestrel/resources/config/filters.yml`, keyed by track, so they are tunable without a code
change. Every rejection records the rule that fired.

**Ingest skip accounting (D-SRC-13).** Each scrape run records `dropped_outside_area`
and `dropped_unrelated_title` in its counts. The Runs history drawer and full run detail
sum those counts across the searches in the run's existing time window and, when either
is positive, show one muted summary: "Skipped 42: 30 outside your area, 12 unrelated titles".
Under that summary, when the searches recorded a title sample (D-SRC-17), a "Show details"
disclosure (the app's `data-k-fold` fold: animates open and closed, off under reduced motion)
lists the sampled titles as "Title, Org", interleaving the searches (first of each, then second
of each) up to 25, with "Showing X of N" when X is less than the summed
`dropped_unrelated_title`. Runs recorded before the sample existed show no disclosure.
Zero or missing counts show no summary. The summary uses all matching searches, including
those beyond the source list's display limit. Diagnostics exports both counts on each
scrape-run row, with missing counts exported as zero. This accounting does not change
location or title filtering, including the rule that genuine ambiguity is kept.

A rejection is not final when the inputs change. A profile save that changes what the rules read
(target or excluded titles, seniority floor, pay floor, pay margin, geography, sponsorship) queues one
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
      when: "comp_max_below(opp, margin_floor(profile.employment_terms.comp_floor_usd, profile.employment_terms.comp_floor_margin_pct))"
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
    - { id: near_floor,        when: "comp_near_floor(opp, profile.employment_terms.comp_floor_usd, profile.employment_terms.comp_floor_margin_pct)", weight: 0.0 }
    - { id: comp_unknown,      when: "opp.comp_source == 'none'", weight: -0.3 }
    - { id: must_avoid_hit,    when: "desc_contains_any(opp, profile.must_avoid)", weight: -1.0 }
    - { id: staffing_agency,   when: "regex(opp.description_md, '(?i)(staffing|recruiting|talent) (agency|partner|solutions)|our client is seeking')", weight: -1.0 }
    - { id: reposted_often,    when: "opp.seen_count >= 5", weight: -0.8 }
    - { id: likely_ghost,      when: "opp.ghost_score >= 0.6", weight: -1.2 }

consulting:
  hard_blockers:
    - id: rate_floor
      when: "comp_max_below(opp, margin_floor(profile.consulting_terms.day_rate_floor, profile.consulting_terms.day_rate_floor_margin_pct))"
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
    - { id: near_floor,       when: "comp_near_floor(opp, profile.consulting_terms.day_rate_floor, profile.consulting_terms.day_rate_floor_margin_pct)", weight: 0.0 }
```

**The near-floor margin (D-RULES-2).** A
profile sets `comp_floor_margin_pct` (employment) or `day_rate_floor_margin_pct` (consulting) to
0 (Exact), 2, 5, 10 or 15, default 5 (the Profile control "Keep near-floor roles", `SPEC-INTERFACES.md` §2.1). The
`comp_floor` and `rate_floor` blockers then fire only when the ceiling is below the floor lowered
by that percentage, so a role that tops out a little under the floor passes Tier 0 and reaches the
models. A passing role inside the margin matches the zero-weight `near_floor` soft signal; the
signal is a label, it changes no score, and the interface shows "Just under your pay floor" (a
rate: "Just under your rate floor") on cards, in the pay box and on the role page, so the operator
decides with the gap in view. A role with no posted pay is unaffected: it was already kept
(`comp_max_below` is False when `comp_max` is None). With the margin at 0 (Exact) both rules behave
exactly as before. The helpers are null-safe and contain no comprehension, so the AST guard is
untouched. Changing the margin queues the same `t0_recheck` as changing the floor. This is hard
rule 4 applied to pay: a small gap is uncertainty, and uncertainty escalates.

`geo_rules` on the profile:

**Country screening at ingest (D-SRC-13).** Before storing a fetched role, parse its location.
When its country is known and outside the source track's profile `remote_country_allowlist`,
drop it with reason `outside_area` and increment `dropped_outside_area` in the scrape run's
counts. Store no opportunity or related rows for a dropped posting. An absent or empty country
list applies no country restriction. Unknown, blank, vague and counted locations such as
"Remote" and "2 Locations" are kept (hard rule 4), as are countries in the list. This screening
does not decide work mode: "New York - Remote" is kept for a US search, with remote or hybrid
fit left to scoring. Hand-pasted roles bypass this screening because the operator chose them.
Existing stored roles are not removed by this ingest screen; a fetched posting it drops does
not update an existing row either.

**Title screening at ingest (D-SRC-13).** Build `smart_filter.build_context` once per source run
and track from the profile's target titles, their phrasings, seniority floor and words learned
from at least two pursuits. If the unchanged `smart_filter.keep(title, context)` returns false,
skip the posting before storage and increment `dropped_unrelated_title` on the scrape run.
**Dropped-title sample (D-SRC-17, amends D-SRC-13).** The first 25 postings a run skips for an
unrelated title are recorded, in the order seen, in that scrape run's `counts` as
`dropped_title_sample`: a list of `{"title", "org"}`, each string cut at 200 characters. No
posting text, URLs or other fields; no migration. The sample is written only to the stored
counts, never to the run outcome the CLI, Run now and the worker progress line print, so it never
reaches a log. Diagnostics bundles strip it (they never carry opportunity titles). The filter
rule itself is unchanged; it is tuned only after the operator reviews real samples. Signal
sources have no title drop and keep no sample.
The country check runs first, so a posting skipped for both reasons is counted outside the area
only. Every fetched source uses this screen; manual paste bypasses it. Titles the filter cannot
judge keep its existing treatment: blank titles, no field words and nothing to compare against
are retained, and unknown seniority never fails the floor test. Tier 0 creates no new lane tasks.
Existing lane rows are rechecked before release and before scoring, rejected with an
`outside_area` or `unrelated_title` stage reason on a clear mismatch, and otherwise remain
local-only through Tier 2. Normal Tier 0 rules and explicit operator overrides remain available.

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

Location parsing recognizes all ISO 3166-1 countries from a vendored package table of alpha-2,
alpha-3 and three-digit numeric codes, English short names and common alternative names. A country
can stand alone or end a comma-separated location; US remote phrases also resolve to US. For a
two-letter code shared with a US state, uppercase `City, XX` means the state; `City, Region, XX`
or a lowercase code means the country. Full US state names retain their existing meaning.
Foreign locations keep the first part as the city and leave state empty (D-SRC-13, step 1).

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
5. `remote_type == "unknown"`, nothing matched, the posting **names a specific place** (a parsed
   state, or a city that is not a vague phrase such as "Multiple locations" or "United States"),
   and its title, location and description never mention remote or hybrid work → **deny**: a
   posting that names one place and never offers remote work is on-site there (owner ruling
   D-RULES-9, 2026-10-27). The role stays findable under the rules' set-aside list with the reason
   and one click to restore, so a rare remote role that names only a home state is not lost.
6. `remote_type == "unknown"` and nothing matched otherwise → **allow**, and record
   `geo_uncertain`. Tier 0 biases toward false positives; an unparseable location is not evidence
   of a bad location.
7. Otherwise → deny.

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
| `margin_floor(n, pct)` | the floor lowered by `pct` percent (`n * (1 - pct/100)`); returns `n` when `pct` is None or 0 and returns None when `n` is None, so the rule then has nothing to check and the role passes |
| `comp_near_floor(opp, n, pct)` | bool, True when `comp_max` is below `n` but at or above `margin_floor(n, pct)`; False when `comp_max`, `n` or `pct` is None or `pct` is 0 |
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

**Stub postings are not scored.** A scraped job or contract posting with under 40 words of
description (after whitespace normalization; text in an unspaced script such as Chinese, Japanese
or Thai is counted by characters, under 120) and no trigger event is not sent to the model. It is
not a failure: no quarantine row, no Retry. It stays at `t1_pass` with `stage_reason` "t2 posting
has no description" and reads **Not scored: the posting has no description** in Pipeline and on the
role page (the D-UI-225 pattern). A manual paste, an RFP, a referral and a trigger event are
exempt. A re-scrape that brings a body re-queues Tier 2 (`changed`); a re-scrape of a role already
scored to a stub keeps its score and stage. A Glassdoor pay band (JobSpy's p10-p90
`payPeriodAdjustedPay`) is stored with `comp_source = inferred`, reaches the model labelled as a
job-board estimate, and is not posted pay: no Tier 0 pay rule compares it with the floor
(uncertainty escalates, never rejects), and the company pay range and the comp corpus leave it out.

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

- Settings > This computer offers Other with a base URL and key. Both are stored only in
  the data home's `.env` (`OTHER_SERVICE_URL_SECRET`, `OTHER_SERVICE_API_KEY`), never in YAML,
  setting rows, backups or logs. The URL must be HTTPS (HTTP is allowed only for loopback),
  without credentials, a query or a fragment. Test lists models and writes nothing. Save makes
  a generic external backend available to the existing Models connection and consent flow;
  it does not enable Hosted mode, acknowledge a disclosure or assign a model.

- The `frontier` **tier** names exactly one external backend and is reachable only through the
  per-item preview-and-confirm escalation. The worker never passes `allow_external`.
- **Hosted mode** (`llm.hosted_mode`, off by default) is the only other way an external backend
  is ever used. Any tier (`tier1`, `tier2`, `docsel`; never `embed`, which runs on Ollama or
  in-process, §8.1.1) may name an external backend, but the client refuses the call
  (`ExternalBackendError`) until the operator has turned Hosted mode on **and** accepted the
  disclosure when the operator chooses Online or Both under Settings > This computer, which writes `enabled: true`, `acknowledged_at` and
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
    Settings > This computer, Models and `kestrel setup --hosted` as `llm.hosted_tiers.<tier>.backend` and
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
- Keys live only in `<data home>/.env`, entered from **Settings > This computer** or **Models** (the same key store; D-UI-106) or
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
- **Balance in the status rail** (#29, D-BYOK-12). Only OpenRouter can say what is left on a key:
  `GET /key` returns `usage`, `usage_monthly`, `limit` and `limit_remaining`. The rail shows one short line either way,
  "$Y spent this month" (the key's `usage_monthly`), with or without a key cap (owner decision);
  the account balance needs a management key and is never fetched. The request is made only when Hosted mode is on
  and acknowledged, the key is OpenRouter's and saved, and the user did not choose free models only
  (unless they ticked Always show my OpenRouter balance, the `ui.show_balance` row). It carries the
  Authorization header and nothing else, is cached for five minutes under a digest of the key (a
  failure for one minute, shown as "Balance unavailable"), and the key is never logged. Anthropic and
  OpenAI have no balance endpoint for an ordinary key and Kestrel does not ask for an admin key, so
  every provider but OpenRouter makes **no request**: the rail shows this month's deeper reads from
  `frontier_transmission.cost_usd` (a send with no reported cost is unknown, never $0). Tier runs on a
  hosted model are not metered locally, so that line says "on deeper reads".

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
  Each concurrency setting, including `embed` (default 4), is a whole number from 1 to 16,
  validated at load from every configuration source. Models saves Tier 1 and Tier 2 together
  only after both values pass validation. These limits bound requests, not a guarantee of
  available memory; start low on smaller computers.
  `OLLAMA_NUM_PARALLEL` must be at least the maximum, including `embed`, or requests queue.
  Set it in Ollama's environment and restart Ollama. Kestrel cannot verify another process's
  environment.
- **Never interleave tiers that use different models.** With 16GB of VRAM, alternating a 14B and
  an 8B forces Ollama to unload and reload weights on every task. The worker **drains all pending
  tasks for one tier before starting another**, even when the models are identical. Set
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

The two cutoffs are `thresholds.apply` (default 4.0, shown to the operator as **Strong match**) and
`thresholds.consider` (default 3.0, shown as **Worth a look**). They are editable in Settings >
Which roles you see (D-UI-107): each is 0.0 to 5.0 in steps of 0.1, `consider` must be lower than
`apply`, and Restore defaults returns 4.0 and 3.0. The "new strong match" notification uses the
`apply` cutoff rather than a separate `notify.high_score` setting, which the interface no longer
exposes. A change applies to new evaluations and to how existing ones are grouped; stored scores
are never rewritten. (The earlier code read threshold keys that did not exist; the cutoffs are
read from `thresholds` and fixed under D-UI-107.)

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
  hosted_mode:                             # §6.4: off by default; choosing Online or Both under Settings > This computer writes enabled + acknowledged_at
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
Settings > This computer with that profile active), so a BYOK install needs nothing local beyond Kestrel.
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

**Base tags are pinned, never bare (D-LLM-9, 2026-11-01).** Tier 1 uses
`qwen3:4b-instruct-2507-q4_K_M`; document selection calls the profile's own `kestrel-tier2` derived model
(D-LLM-10: the 4B model is too small for resume import, and a separate `kestrel-docsel` with a
smaller `num_ctx` would load as a second runner; an existing `kestrel-docsel` is left alone); the 24 GB and 64 GB profiles' Tier 2 and 3 use
`qwen3:30b-a3b-instruct-2507-q4_K_M`. The bare tags `qwen3:4b` and `qwen3:30b-a3b` now resolve to
Thinking-2507 builds that cannot stop thinking, so every call spent its output budget on hidden
reasoning. The rebuild of an existing install writes a clean Modelfile from `render_modelfile`
(FROM plus PARAMETER lines only) and never copies a TEMPLATE, SYSTEM or stop line from the old
model, and never deletes or untags the person's existing models.

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
  the same size, and a modern multi-core CPU handles the CPU-side layers well. Benchmark it; it may beat the
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
changes. When Hosted is recommended and the operator chooses Local or Both, onboarding step 2 and Settings > This computer show
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
- **An online key that has run out of credit is said plainly (D-LLM-8).** HTTP 402 from any
  online provider, OpenModel's `INSUFFICIENT_BALANCE`, OpenAI's `credit_balance_exhausted` (or
  `insufficient_quota`), and Anthropic's `billing_error` are classified in `kestrel/llm/credit.py`
  as a distinct, non-retryable failure: not a schema failure (no repair, no larger retry, no
  quarantine) and not a transient error. The call raises `OutOfCredit`, a `UserFacingError` whose
  sentence Kestrel writes ("Your OpenModel credit has run out. Top up at console.openmodel.ai,
  then use Score again."); no provider text reaches a page. The backend is then held for that key:
  nothing more is sent to it, and no other online provider is chosen in its place. The hold ends
  when the key changes, when the operator starts a run (Run now, Score again, Score everything
  now, a Failed scoring retry), or after 30 minutes. The worker puts the leased task back
  unstarted with its attempt refunded (resumable, hard rule 5), leases nothing more for a tier
  routed to the held backend, and opens no empty Run row; tiers on a local model carry on. The
  status rail and the Runs page each show one notice per held provider.
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

`kestrel bench`, and the **Model check** page, which queues the same run as a background task of
type `bench` so it never blocks the web and worker loop and never needs the command line
(D-UI-105; D-DRIFT-11 reverses the Bench line of D-DRIFT-6). The task is a `task` row, resumable,
with progress and Cancel, and each finished check is stored so earlier checks can be listed on the
page (the storage shape is the build's to choose). Accepts
`--models` and `--prompt-version` so exactly one variable changes per comparison. Reports per
model and per track:

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
  no personal phone numbers, no inferred emails. The one other thing Kestrel stores about people is
  the operator's own LinkedIn connections file, which they download themselves and import by hand:
  name, company, position and connected date only, email addresses dropped at import, kept only on
  this computer (`SPEC-MODULES.md` §2.3, D-UI-115).
- The operator's profile photo (the avatar, D-UI-112) stays on this computer and never reaches a
  resume, an export or a diagnostics file. A diagnostics file (D-UI-114) holds the app version,
  recent logs, settings without keys and health check results; it is made only on request, never
  holds the profile, resumes, keys, roles or applications, and is never sent by Kestrel.
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

### Rippling

`GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` → `200`, a **bare JSON
array**, no auth. Verified 2026-10-21 against the public board behind `ats.rippling.com/{slug}/jobs`
(667 rows, 381 distinct postings). `ats.rippling.com/robots.txt` disallows only `/internal/`;
`api.rippling.com/robots.txt` is a 404 (no rules). The `page` and `pageSize` query parameters are
ignored: the whole board is one response.

Per row: `uuid`, `name`, `department` (`{id, label}`), `url` (the public posting page
`https://ats.rippling.com/{slug}/jobs/{uuid}`), `workLocation` (`{id, label}`).

**Gotchas:** the title is `name`, and it carries leading and trailing spaces. A posting open in
several places is **one row per location with the same `uuid`**, so the adapter merges rows by
`uuid` and joins the labels. The list has no description and no date; the detail does:
`GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uuid}` → `200`, an object with
`uuid`, `name`, `description` (`{company, role}`, both HTML), `workLocations[]` (strings),
`department` (`{name, base_department, department_tree[]}`), `employmentType` (`{label, id}`,
`label` is an enum such as `SALARIED_FT` or `TEMP`, `id` is the readable text),
`createdOn` (ISO datetime with offset), `payRangeDetails[]` (empty in every sample, so no
compensation is read), `url`, `board` (`{slug, companyName, ...}`), `companyName`,
`unlistedFromSearch`, `activeJobApplication` (form questions, ignored). `description.company` is
the employer boilerplate and is left out; `description.role` is the body.

### ADP Workforce Now (career center)

`GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions?cid={cid}&ccId={ccId}&lang=en_US&locale=en_US&$top=20&$skip=1`
→ `200`, `application/json`, no auth. Verified 2026-10-21 against one public career center
(`recruitment.html?cid={uuid}&ccId=19000101_000001`; 85 postings). `cid` is the employer's UUID from
the career-center link; `ccId` is `19000101_000001` on every link seen. `workforcenow.adp.com`
publishes no `robots.txt` (the path redirects to the ADP sign-in page), so there is no rule to
follow or break; the endpoint is the one the public career-center page itself calls.

```
{ "jobRequisitions": [ ... ], "meta": { "startSequence": n, "totalNumber": n, "links": [ ... ] } }
```

Per requisition: `itemID` (e.g. `9207316549520_1`, the id the detail endpoint takes),
`requisitionTitle`, `postDate` (ISO datetime with offset), `workLevelCode` (`{shortName}`, absent
on some rows), `clientRequisitionID`, `requisitionLocations[]` (`{address: {cityName,
countrySubdivisionLevel1: {codeValue}, postalCode}, nameCode: {shortName}, aliasNames}`),
`customFieldGroup` (`stringFields[]` holds `ExternalJobID`, the public job number), plus empty
`links`, `organizationalUnits`, `screeningRequirements`, `postingInstructions`.

**Gotchas:** pages are capped at **20** whatever `$top` says. `$skip` is **1-based**: `$skip=1`
is the first page, `$skip=21` the second (`$skip=0` returns 19 rows and `startSequence: 0`, so it
is never sent). Stop at `meta.totalNumber`. `meta.links` is a facet list and is ignored. A posting
open in several places repeats `requisitionLocations` entries, some duplicates and some blank
(`nameCode: {}`, empty `address` strings); blanks are dropped and duplicates merged. The title is
`requisitionTitle` and carries a trailing space on some rows. The list has no description; the
detail `GET .../job-requisitions/{itemID}?cid={cid}&ccId={ccId}&lang=en_US&locale=en_US` → `200`
returns the same object plus `requisitionDescription` (HTML). The public posting page is
`https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&ccId={ccId}&jobId={ExternalJobID}&lang=en_US`
(a script-rendered page; its content was not fetched, only the JSON above).

### Paylocity Recruiting

No JSON API was found for a job list. `GET https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{id}`
answers `200` with `{"displayName", "vidGridId", "showVideo", "jobs": []}` for every public board
tried (three), including one that lists 11 postings on its page, so it is not used. The public
careers page is the source: `GET https://recruiting.paylocity.com/recruiting/jobs/All/{id}` → `200`
HTML (no login, no challenge, no script needed) whose inline script assigns the board to
`window.pageData = {...};`. Verified 2026-10-21 against one public board (11 postings).
`recruiting.paylocity.com/robots.txt` is a 404 (no rules). `{id}` is the board GUID in the page link
(`.../recruiting/jobs/All/{id}` or `.../All/{id}/{Name}`).

```
window.pageData = { "ModuleTitle": "...", "ModuleId": "...", "Jobs": [ ... ], "Locations": [...],
                    "Departments": [...], "LeadJoinUrl": ..., ... }
```

Per job in `Jobs[]`: `JobId` (integer), `JobTitle`, `LocationName`, `ShouldDisplayLocation`,
`PublishedDate` (ISO datetime with the correct offset), `Description` (a ~110 character plain
snippet, **not** the body), `IsInternal`, `HiringDepartment`, `JobLocation` (`{LocationId, ModuleId,
Name, Address, Address2, Metro, City, State, Zip, Country, County, SmartyAddressId}`), `IsRemote`,
`IndeedRemoteType`. `ModuleTitle` is the employer name.

**Gotchas:** the data is JSON inside an HTML script, read by locating `window.pageData = ` and
decoding one JSON value from there (never by a regex over the JSON). The list's `Description` is a
truncated snippet; the body comes from the posting page
`GET https://recruiting.paylocity.com/Recruiting/Jobs/Details/{JobId}` → `200` HTML with one
`<script type="application/ld+json">` schema.org `JobPosting` (`title`, `datePosted`,
`hiringOrganization.name`, `description` as HTML, `jobLocation.address`). **Its `datePosted` has a
wrong UTC offset (the local clock time labelled `-05:00` after adding five hours), so the date is
taken from the list's `PublishedDate`.** The posting link carries only `JobId`, not the board id, so
board discovery can follow an `.../Jobs/All/{id}` link but not a `.../Jobs/Details/{JobId}` one.

### Workable

`POST https://apply.workable.com/api/v3/accounts/{slug}/jobs` with the JSON body `{}` → `200`
`{"results": [...], "total": N}`, no auth. Captured 2026-10-21 by the maintainer from one public
board (6 postings); the build environment was rate-limited by Cloudflare on this host, so nothing
here was read from it. `apply.workable.com/robots.txt` is `User-agent: *` / `Disallow:` (allows
everything) with `Content-Signal: search=yes, ai-input=yes, ai-train=no`. `www.workable.com/robots.txt`
disallows `/j/`, but the adapter calls only the `apply.workable.com` API.

Per result: `id`, `shortcode`, `title`, `remote` (bool), `location` (`{city, country, countryCode,
region}`; `city` can be `""` and `region` null), `locations[]` (same fields plus `hidden`), `state`,
`isInternal`, `published` (ISO datetime), `type`, `language`, `department[]`, `accountUid`,
`workplace`.

**Pagination is UNVERIFIED.** The captured board had `total` equal to the number of results. The
adapter follows a `nextPage` token if the response carries one (POST again with `{"token": <nextPage>}`,
up to 20 pages). If `total` exceeds what was collected and there is no token, it returns what it
has and logs a warning; it never raises for that.

Detail: `GET https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}` → `200`, no auth,
the same fields plus `description` (HTML), `requirements` (HTML) and `benefits` (HTML, or `""`).
The public posting page is `https://apply.workable.com/{slug}/j/{shortcode}/`; the board is
`https://apply.workable.com/{slug}/`.

**Gotchas:** skip a result whose `isInternal` is true or whose `state` is not `published`. The list
has no description, so each posting needs a detail request. The body is `description`,
`requirements` and `benefits` joined in that order (blank sections skipped). The location text is
built from `city`, `region` and `country`, dropping empty parts.

### Recruitee

`GET https://{slug}.recruitee.com/api/offers/` → `200` `{"offers": [...]}`, no auth, no pagination, and every offer carries its full HTML `description` and `requirements`, so a board is one request. Verified 2026-10-29 against one public board (24 offers, about 700 KB). An unknown `{slug}` answers `404` `{"error": "Not Found"}`. `{slug}.recruitee.com/robots.txt` redirects to the company's own careers domain, whose file allows everything; the shared client follows it.

Per offer: `id` (integer), `guid`, `slug`, `title`, `status` (`published`), `description` and `requirements` (HTML), `location` (one display string, "Amsterdam, Noord-Holland, Netherlands"), `locations[]` (`city`, `state`, `country`, `country_code`), `city`, `country`, `remote`, `hybrid`, `on_site` (booleans), `employment_type_code` (`fulltime_permanent`), `department`, `tags`, `published_at` and `created_at` (`2026-10-28 13:21:58 UTC`), `careers_url` and `careers_apply_url` (on the company's own domain when it has one), `salary` (`{min, max, period, currency}`, all null when not shown), `company_name`, `mailbox_email` (an employer mailbox, never stored by Kestrel), `open_questions` (the application form, not read).

**Gotchas:** custom careers domains are common, but the API stays on `{slug}.recruitee.com`, so the slug is the only thing needed; a pasted link on `{slug}.recruitee.com` is recognized by its host, and a careers page on another domain by markup that names `{slug}.recruitee.com` (the API address, an `/o/` link or the widget). A role open in several places has several `locations[]` entries, joined with "; ". The `salary.period` values are mapped when they are `hour`, `day`, `month` or `year`; the salary shape is from Recruitee's public documentation and the live board showed no pay, so it is **documented, not verified live**.

### Teamtailor

`GET https://{domain}/jobs.json` → `200` `application/feed+json`, a JSON Feed 1.1 document `{"version", "title", "home_page_url", "feed_url", "items": [...]}`, no auth. `{domain}` is the career site's own host (`{name}.teamtailor.com` or the employer's own career domain); Teamtailor has no shared API host. Verified 2026-10-29 against one public site (15 roles, about 220 KB, one document, no `next_url`). `GET https://{domain}/jobs.rss` → `200` RSS 2.0 with the same roles. A site that has the feeds off answers `404` (an empty `application/json` body for `jobs.json`, an HTML page for `jobs.rss`); some sites behind a firewall answer `403`. `robots.txt` on the verified site disallows only `/app/`, `/messages/`, `/messenger/`, `/facebook/tab/` and `/jobs/internal/`.

Per JSON item: `id` (a UUID), `title`, `url`, `date_published` (ISO with offset), `content_html` (the full description), and `_jobposting`, a schema.org `JobPosting` with `identifier.value` (the numeric job id), `hiringOrganization.name`, `datePosted` and `jobLocation[]` (`address.addressLocality`, `addressCountry` as an ISO code, `addressRegion` as a Teamtailor region such as "North America", `streetAddress`, `postalCode`). Per RSS item: `title`, `description` (HTML), `pubDate` (RFC 2822), `link`, `guid` (the same UUID), `remoteStatus` (`none`, `hybrid` or `remote`), and in the `https://teamtailor.com/locations` namespace `location` (`name`, `address`, `zip`, `city`, `country` as a name), `department`, `role`, `division`.

**Gotchas:** the JSON feed has no remote flag (the RSS does), so remote stays unknown unless `jobLocationType` is `TELECOMMUTE`. `addressCountry` is an ISO code, and a bare `CA` would read as California, so Kestrel turns it into the country's name. A site with both feeds off is a `partial` run with a plain sentence, never a failure. The domain is operator-supplied, so every request is the guarded kind (public addresses only, robots.txt checked, 2 MB cap). A career site on the employer's own domain is recognized from the page loading Teamtailor's `assets-aws.teamtailor-cdn.com/assets/careersite-*.js`.

### Personio

`GET https://{slug}.jobs.personio.de/xml` → `200` `text/xml`, a `<workzag-jobs>` root with one `<position>` per role, no auth. Accounts on the other domain use `https://{slug}.jobs.personio.com/xml` (also `200`). Verified 2026-10-29 against Personio's own public demo board (4 positions) and one company board (2 positions). An unknown `{slug}` answers `404` with an HTML page. The `robots.txt` of the company board checked allows everything.

Per `<position>`: `id`, `name`, `office` (a place name with no country, "München", "Remote", "Frankfurt am Main (hybrid)"), `subcompany` (optional), `department` (optional), `recruitingCategory`, `jobDescriptions/jobDescription[]` (`name`, `value` as HTML in CDATA), `employmentType` (`permanent`, `intern`), `seniority`, `schedule` (`full-time`, `part-time`, `full-or-part-time`), `yearsOfExperience`, `occupation`, `occupationCategory`, `createdAt` (ISO), and optionally `salaryInformation` (`min`, `max`, `currencyCode`, `currencySymbol`, `type` yearly). The documented `additionalOffices/office` list was not on any board seen. The feed names no employer.

Detail: `GET https://{slug}.jobs.personio.de/job/{id}` → `200` HTML with one schema.org `JobPosting` in `<script type="application/ld+json">`: `title`, `description` (HTML), `hiringOrganization.name`, `jobLocation.address` (`addressLocality`, `addressRegion`, `addressCountry` as an ISO code), `datePosted` (a date), `employmentType`.

**Gotchas:** descriptions are often empty in the feed (`<jobDescriptions/>` or blank values; the demo board has them, a real board often does not), so the job page is read for those roles, 2 seconds apart, up to `max_results` pages a run (default 100). Roles past the cap are kept without a description and the run is `partial` with a sentence saying how many. The page's place and country replace the feed's bare office name, because only the page has a country. Personio rate-limits hard: a 429 on the list stops the run, and on a page it stops reading pages and keeps the rest. Positions are read with ElementTree after any DOCTYPE or ENTITY declaration is refused.

### Breezy HR

`GET https://{slug}.breezy.hr/json` → `200` `application/json`, a bare array of positions, no auth, no pagination, **no description**. Verified 2026-10-29 against Breezy's own public trial board (3 positions, placeholder data). An unknown `{slug}` answers `404` with an HTML page; a board with no openings answers `[]` (`acme.breezy.hr` did). `breezy.breezy.hr/robots.txt` disallows only `/css`, `/fonts`, `/stylesheets` and `/javascripts` (and AhrefsBot).

Per position: `id` (also the `friendly_id` prefix), `friendly_id` ("98323abf2296-employee-12"), `name` (the title), `url` (`https://{slug}.breezy.hr/p/{friendly_id}`), `published_date` (ISO), `type` (`{id, name}`: `fullTime`, `contract`, `other`), `location` (`{country: {name, id}, state: {id, name}, city, primary, is_remote, name}`; `name` is "Chaos, FL" with no country), `locations[]` (empty for a one-place role, else every place), `department`, `salary` (display text, "$1 – $5 / year"), `company` (`{name, logo_url, friendly_id, isMultipleLocationsEnabled}`).

Detail: `GET https://{slug}.breezy.hr/p/{friendly_id}` → `200` HTML; the description is the page's `<div class="description">`. The verified page carried no `ld+json`; a schema.org JobPosting in one is read as the fallback.

**Gotchas:** the description needs one page request per role, so Kestrel reads at most `max_results` pages a run (default 100), one a second; roles past the cap are kept without a description and the run is `partial` with a sentence saying how many. The page address is built from `friendly_id` on `{slug}.breezy.hr`, never taken from the payload. The place is built from city, state name and country name, because the payload's own `name` has no country. `is_remote` is an explicit boolean (`null` on most rows) and is the only thing that sets the remote flag. `salary` is display text and is not parsed into numbers.

### USAJobs (search API; keyed)

`GET https://data.usajobs.gov/api/search?Keyword=..&LocationName=..&RemoteIndicator=True&DatePosted=N
&ResultsPerPage=<=500&Page=N&WhoMayApply=Public&Fields=Full` with headers `Authorization-Key: <key>`
and `User-Agent: <the email the key was requested with>` (host is always `data.usajobs.gov`).
Source: developer.usajobs.gov (Search API reference, authentication and rate-limiting guides).
**Shape documented, not yet verified live** (no key in the build environment); the fixture
`tests/fixtures/usajobs_search.json` is synthetic from the documented shape.

`200` reply: `{"LanguageCode", "SearchParameters", "SearchResult": {"SearchResultCount",
"SearchResultCountAll", "SearchResultItems": [...], "UserArea": {"NumberOfPages" (a string),
"IsRadialSearch"}}}`. Per item: `MatchedObjectId` (a string), `RelevanceRank`, and
`MatchedObjectDescriptor` with `PositionID`, `PositionTitle`, `PositionURI`, `ApplyURI` (a list),
`PositionLocationDisplay`, `PositionLocation[]` (`LocationName`, `CountryCode`,
`CountrySubDivisionCode`, `CityName`), `OrganizationName`, `DepartmentName`, `PositionSchedule[]`,
`QualificationSummary`, `PositionRemuneration[]` (`MinimumRange` and `MaximumRange` as strings,
`RateIntervalCode`, `Description`), `PositionStartDate`, `PositionEndDate`, `PublicationStartDate`,
`ApplicationCloseDate`, and `UserArea.Details` (`JobSummary`, `MajorDuties`, `Requirements`,
`Education`, `Benefits`, `HowToApply`, `RemoteIndicator`, `TeleworkEligible`, ...). Fields the
reference lists with a different type than a sample shows (`MajorDuties` as text or a list) are
read as either. Limits: 500 rows a page, 10,000 rows a query; the rate limit is not published.
Terms (developer.usajobs.gov/guides/terms-of-service): public data may be stored and used, source
attribution and a link to the official announcement are encouraged, an unofficial product should say
it is not run or endorsed by OPM, credentials must not be shared, and applications follow the
official announcement. `robots.txt` was not readable from the build environment (the edge answered
403); the adapter checks it and fails open when none is served.

### Adzuna (search API; keyed)

`GET https://api.adzuna.com/v1/api/jobs/us/search/{page}?app_id=..&app_key=..&results_per_page=<=50
&what=..&where=..&max_days_old=N&sort_by=date&content-type=application/json`. Source:
developer.adzuna.com (Overview, Search ads, Terms of Service), read 2026-10-29. **Shape documented,
not yet verified live** (no key in the build environment; an unkeyed request answers 401); the
fixture `tests/fixtures/adzuna_search.json` is synthetic from the documented shape.

`200` reply: `{"__CLASS__", "count", "mean", "results": [...]}`. Per ad: `id` (a string), `title`,
`description` (a snippet: "we currently only provide a snipped of the job description"), `created`
(ISO), `redirect_url`, `location` (`display_name`, `area` as country, state, county, city),
`company.display_name`, `category` (`label`, `tag`), `salary_min`, `salary_max`,
`salary_is_predicted` ("0" or "1", number or string), `contract_time` (`full_time`, `part_time`),
`contract_type` (`permanent`, `contract`), `latitude`, `longitude`. Terms: "Personal job search" is
a permitted use; each displayed advert is labeled "Adzuna" linked to Adzuna; do not contact content
providers through Adzuna. `api.adzuna.com/robots.txt` is `Disallow: /`.

### Himalayas (search API; no key)

`GET https://himalayas.app/jobs/api/search?q=..&country=US&sort=recent&page=N` (20 a page; the browse
feed `/jobs/api` pages by `nextCursor`). Source: himalayas.app/docs/remote-jobs-api and
himalayas.app/terms (clauses 30A and 113). **Verified live 2026-10-29** from the build environment
(200, no key; the fixture `tests/fixtures/himalayas_search.json` is a trimmed capture with three
edited copies). Reply: `{comments, updatedAt, offset, limit, totalCount, jobs[]}` (`nextCursor` on
the browse feed only). Per job: `title`, `excerpt`, `companyName`, `companySlug`, `companyLogo`,
`employmentType`, `minSalary`, `maxSalary` (numbers or null), `salaryPeriod` (hourly, weekly,
fortnightly, monthly, annual), `currency`, `seniority[]`, `locationRestrictions[]` (empty means
worldwide), `timezoneRestrictions[]`, `categories[]`, `parentCategories[]`, `description` (HTML),
`pubDate` and `expiryDate` (Unix seconds), `applicationLink`, `guid` (the job's Himalayas URL). Terms
for API users: Apply must use `applicationLink`; name Himalayas with a visible link; show jobs only in
your own app; cache and drop expired jobs; stay within request limits (an anonymous caller shares one
public allowance; the data refreshes every 24 hours). Job pages are protected against automation:
only the API is requested. `robots.txt` disallows only paging query strings on site pages.

### We Work Remotely (RSS; no key)

`GET https://weworkremotely.com/remote-jobs.rss` (about 100 recent jobs; category feeds at
`/categories/remote-<name>-jobs.rss`). **Verified live 2026-10-29** (200 for the feed and
`robots.txt`, which allows the feeds; every other page answers a 403 bot check, so the terms are
unconfirmed). Fixture: `tests/fixtures/weworkremotely_feed.rss`, trimmed capture plus three edited
items. RSS 2.0 items hold `title` ("Company: Job title"), `region` ("Anywhere in the World", "North
America Only", ...), `country` and `state` (the employer's base, may carry a flag emoji), `skills`,
`category`, `type` ("Full-Time", "Contract"), `description` (HTML, escaped), `pubDate`,
`expires_at`, `guid` and `link`.

### RemoteRocketship (paid API; discovery only)

`POST https://www.remoterocketship.com/api/openclaw/jobs/` with `Authorization: Bearer <key>` and the
JSON body `{"filters": {..., "page": N, "itemsPerPage": <=50}, "includeJobDescription": false}`.
**The trailing slash is required**: without it the server answers `308`. Captured 2026-10-21 by the
maintainer from their own key and scrubbed (`tests/fixtures/remoterocketship_jobs.json`).

`200` reply: `{"jobOpenings": [...], "totalCount": N, "pagination": {"page", "itemsPerPage",
"totalCount", "totalPages", "hasNextPage", "hasPreviousPage"}}`. Per job: `id`, `roleTitle`, `url`
(the employer's own posting, the only field Kestrel keeps), `created_at`, `validUntilDate`,
`employmentType`, `salaryRange` (null or an object), `company` (object) and more. Kestrel reads
nothing else.

Errors carry `{"message", "code", "hint"}` (`tests/fixtures/remoterocketship_401.json`): `400` bad
request, `401` missing or invalid key, `403` active subscription required, `413`, `429` daily request
limit reached, `500`. Kestrel never shows the provider's `message` or `hint`; it says each case in
its own words. Limits: 500 requests or 3,000 jobs per UTC day, 50 jobs per request. Their terms
(section 4) allow caching responses for 24 hours only and prohibit archiving, bulk export and
resale; section 5 forbids scraping the website and bursts. `robots.txt` has `Disallow: /api` for all
agents, a crawler rule; this user-keyed API is published for programmatic use, so the API call alone
is exempt from the robots check (D-SRC-14). Every employer page it leads to is checked.

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
