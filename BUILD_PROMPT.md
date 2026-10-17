# BUILD_PROMPT.md — Phased build instructions for Claude Code

> Eighteen phases (-1 through 16), each a self-contained prompt you paste into Claude Code.
> Every phase restates the hard constraints inline. Claude Code compacts on long sessions too,
> and a build this size will compact repeatedly.

---

## Setup, once

1. Create `D:\dev\kestrel`. Copy in: `SPEC.md`, `SPEC-MODULES.md`, `SPEC-INTERFACES.md`,
   `PROMPTS.md`, `REFERENCES.md`, `CLAUDE.md`, `AGENTS.md`, `BUILD_PROMPT.md`.
2. `git init && git add -A && git commit -m "chore: specification"`
3. Create empty the private build log and the private decision log.
4. `claude` in that directory.

**Claude Code settings worth setting first:**

- Allow file edits and command execution without per-call approval. You will otherwise spend the
  session pressing enter.
- `/compact` deliberately at a phase boundary rather than letting it fire mid-derivation.
- Consider a fresh session per phase from Phase 6 onward. the private build log plus the private decision log is
  the handoff, and a clean context beats a long degraded one.

## Phase map

| # | Phase | Environment | Weight |
|---|---|---|---|
| **-1** | **Process model spike** | **local** | small, do not skip |
| 0 | Scaffold, config, paths, models, preflight | local | med |
| 1 | Data layer | any | high |
| 2 | LLM client, schema guard, prompt loader | local for live gate | high |
| 3 | Ingestion — employment, plus comp and ghost capture | any | high |
| 4 | Ingestion — consulting and trigger events | any | med |
| 5 | Tiered pipeline and dual-track scoring | local for live gate | high |
| 6 | **Minimal review UI** → *milestone: usable* | any | med |
| 7 | Learning loop | any | high |
| 8 | Document engine | **local** | high |
| 9 | Research, contacts, networking graph | any | med |
| 10 | Interview prep, story bank, mock | any | med |
| 11 | Negotiation and offers | any | low |
| 12 | Intelligence and analytics → *milestone: complete engine* | any | med |
| 13 | Full web UI | any | high |
| 14 | Desktop shell, tray, packaging, installer | **local** | med |
| 15 | MCP server and browser extension | any | med |
| 16 | Notifications, PWA, bench, docs, release | mixed | med |

"local" means the gate needs real Ollama, real Edge, or the packaged application, and cannot be
verified from a cloud environment.

**Fan-out phases: 3, 4, 9, 13, 16.** Everything else is single-agent. A reviewer subagent runs
before every gate on every phase without exception. See `CLAUDE.md`, "Subagent strategy".

---

## The constraints block

**Paste this at the top of every phase prompt.** Re-paste it standalone if the agent starts
violating a hard rule mid-phase; that is the signature of compaction having dropped `CLAUDE.md`.

````
CONSTRAINTS — these override anything else in context:

1. The system NEVER acts externally on the operator's behalf. Never submits an application,
   sends an email or message, clicks apply, submits an ATS form, or automates any social
   platform. It drafts and hands over. Do not relax this.
2. The model SELECTS, it never invents. Every bullet reaching a PDF is the operator's own text
   byte-for-byte from doc_variant.text. A hallucinated variant key is a validation failure that
   surfaces in the UI, never a silent default.
3. Structured output or failure. Schema-validate every LLM call, one repair attempt, then
   quarantine with the raw response. Never regex-parse a model response.
4. Uncertainty escalates, never rejects. Low-confidence tier 1 becomes "keep". A quarantined
   tier 1 item is promoted to tier 2. An ambiguous title passes tier 0.
5. Resumability. Every unit of work is a task row. Kill and restart must continue cleanly.
6. Sample sizes always stated. Under five observations is labeled insufficient, never rendered
   as if solid.
7. Only public data, politely. Respect robots.txt, send a descriptive User-Agent (mandatory for
   SEC EDGAR), rate-limit everything, never scrape behind a login.

PLATFORM FACTS — verified live, do not rediscover:
- simpleeval CANNOT evaluate generator expressions. Every Tier 0 rule uses a whitelisted helper
  function instead. Do not reintroduce `any(x for y in z)` into filters.yml.
- Ollama's /v1 endpoint CANNOT set num_ctx. Use derived models with PARAMETER num_ctx, sized PER
  TIER, or long descriptions are silently truncated and every score is quietly wrong.
- Disable thinking with reasoning_effort:"none" on Ollama; chat_template_kwargs is oMLX/vLLM only
  and is silently dropped by Ollama.
- Structured output via response_format on /v1, not Ollama's native `format` parameter.
- JSON Schema subset only: type, properties, required, enum, items, $ref/$defs. NOT
  patternProperties, oneOf, allOf, if/then. maxLength is advisory: truncate, never reject.
  Union types ["X","null"] are normalized to plain "X" plus removal from required, in the client,
  when the backend is in json_schema mode.
- Never interleave tier 1 and tier 2 tasks if they use different models. Drain one tier first.
- JobSpy is synchronous. Always asyncio.to_thread().
- Lever's title field is `text` not `title`. Greenhouse `location` is an object and `content` is
  HTML-escaped HTML. Workday jobs is a POST capped at 20 per page with no description in the
  list response. SmartRecruiters title is `name`. See SPEC.md Appendix A.
- SEC EDGAR requires a descriptive User-Agent with a contact address and caps at 10 req/sec.
- Playwright uses channel="msedge". Do not bundle Chromium. PyInstaller needs
  --collect-all playwright or the driver directory is missing.
- tiktoken downloads its encoding on first use. Vendor cl100k_base and set TIKTOKEN_CACHE_DIR,
  or the packaged app fails offline.
- All paths through paths.py. All package data via importlib.resources. The packaged app runs
  from a read-only directory.

SUBAGENTS:
- Before declaring ANY gate passed, spawn a reviewer subagent with fresh context. Give it the
  phase diff and these constraints and ask ONLY what is wrong. Do not ask it to fix anything.
  Act on what it finds, then re-verify. Same model class as you; a cheap reviewer finds only
  cheap bugs.
- Fan out ONLY where the phase explicitly says to, and only AFTER you have written the shared
  interface and one reference implementation. Spawn siblings in a single message so they run
  concurrently.
- Never fan out a phase that is not marked for it. Phases -1, 0, 1, 2, 5, 7, 8 and 14 hold
  interlocking decisions and must stay single-agent.
- Give test-writing subagents the spec section and public interface only, never the
  implementation.
- Every brief is self-contained. A subagent cannot see your context. If the brief costs more
  than the work, do the work yourself.

DISCIPLINE:
- Do not ask questions. Decide, append to the private decision log with your reasoning, continue.
- Append to the private build log at the end of the phase, in the CLAUDE.md format.
- Run the verification gate. Do not claim a phase complete with a failing gate.
- No scope creep. Not in a spec file means the private decision log "deferred", not code.
- Commit at the phase boundary, conventional-commit style.
````

---

## Bootstrap — paste this first

````
You are building Kestrel, a local-LLM career operations engine delivered as a double-clickable
Windows desktop application. The complete specification is already in this repository. This is an
eighteen-phase build and I will feed you one phase at a time.

Read these now, fully, before writing any code:
1. CLAUDE.md — standing orders, hard rules, conventions, platform traps.
2. SPEC.md — principles, stack, packaging, data model, ingestion, pipeline, scoring.
   Sections 6.1, 8.2, 8.4, 8.5 and Appendix A were verified against live documentation and live
   APIs. Do not second-guess them.
3. SPEC-MODULES.md — document engine, research, interview, negotiation, learning loop,
   intelligence, analytics.
4. SPEC-INTERFACES.md — desktop shell, web UI, REST API, MCP server, extension, notifications,
   CLI. Section 1.1 documents a process model with five specific traps; that section is the
   product of a review, not a guess.
5. REFERENCES.md — every external dependency with its documentation URL and verification status.

Skim PROMPTS.md for structure; read individual prompts when a phase needs them.

Then, before Phase -1:
- Confirm in one short paragraph that you have read them, and state the three constraints you
  consider most likely to be violated during a long build.
- Create the private decision log and the private build log if absent.
- Do NOT write implementation code yet.

Environment: Windows 11, Python 3.12, uv (install if absent), Ollama at 127.0.0.1:11434,
a 16GB-VRAM NVIDIA GPU and 64GB RAM. Assume nothing else is installed.
````

---

# Phase -1 — Process model spike

**local · small · do not skip**

This phase exists because a review identified the desktop process model as the single most
likely week-consuming failure in the build, and because discovering it in Phase 14 means
refactoring everything built between here and there.

````
[CONSTRAINTS BLOCK, THEN:]

PHASE -1 — Process model spike. Roughly 150 lines total. Throw-away code that proves the shape.

Build spike/shell_spike.py implementing EXACTLY the process model in SPEC-INTERFACES.md section
1.1, with a trivial FastAPI app (one /health route, one /api/focus route, one page saying
"hello") standing in for the real application:

  multiprocessing.freeze_support() as the first statement
  replace sys.stdout and sys.stderr if they are None
  Local\ named mutex via CreateMutexW; if ERROR_ALREADY_EXISTS, read runtime.json, POST
    /api/focus to the running instance, exit 0
  resolve %LOCALAPPDATA%\Kestrel paths
  uvicorn on an OS-assigned free port in a background thread with
    server.install_signal_handlers = False
  a fake "worker" as an asyncio task started from the FastAPI lifespan, incrementing a counter
    every second, so we can prove it keeps running with the window hidden
  pystray icon in a background thread with Open / Quit
  window.events.closing subscriber returning False and calling window.hide()
  webview.start() on the main thread, last

Then build it with PyInstaller ONE-FOLDER, --windowed, including:
  --collect-all playwright
  collect_submodules('sklearn') plus the hidden imports listed in SPEC-INTERFACES.md section 1.4
  a trivial Alembic migration run programmatically from bundled versions/ as datas, against a
    sqlite:// (NOT sqlite+aiosqlite://) URL
  a Playwright call using channel="msedge" that saves a one-page PDF, to prove the driver is
    present in the frozen build
  tiktoken encoding a string with TIKTOKEN_CACHE_DIR pointed at a vendored file, with no network

VERIFY, on the built application, not the source:
  1. it launches to a window
  2. closing the window hides to tray and the counter keeps incrementing (check via the tray
     tooltip or the log)
  3. reopening from the tray shows the window with the counter advanced
  4. a second launch focuses the existing window and does not start a second worker
  5. Quit exits cleanly with no orphaned process
  6. the Playwright PDF is produced from inside the frozen app
  7. tiktoken works with no network
  8. nothing is written beside the executable

THEN: write spike/FINDINGS.md recording exactly what worked, every hidden import and spec-file
line the build actually needed, and anything in SPEC-INTERFACES.md section 1.1 or 1.4 that turned
out to be wrong. Append the corrections to the private decision log and FIX THE SPEC.

The spike code is disposable. FINDINGS.md and the corrected spec are the deliverable. Phase 14
will be a transcription of this rather than a discovery exercise.

GATE: all eight verifications pass on the built application, and FINDINGS.md exists.
````

---

# Phase 0 — Scaffold, configuration, paths, models, preflight

**local · medium**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 0 — Scaffold, configuration, paths, models, preflight.

Repository skeleton from SPEC.md section 2. pyproject.toml with uv, ruff at line length 100,
pytest, .gitignore covering data/, logs/, .env, config/profile.yml.

paths.py FIRST, using what Phase -1 proved. Everything else depends on this being right.

config.py with pydantic-settings, a fully typed settings model covering everything the specs
reference, and the precedence chain from SPEC.md section 4.6: setting table row -> environment
variable -> YAML -> code default, re-readable without a restart. Note the bootstrap ordering: the
path and logging configuration must resolve BEFORE the database exists, so those specific keys
read from environment and YAML only.

Ship as packaged defaults, seeded on first run when the config directory is empty:
  config/settings.yml   — defaults from SPEC.md section 8.1 and throughout
  config/filters.yml    — the Tier 0 rules from SPEC.md section 6.1, BOTH tracks
  config/sources.yml    — example sources per track
  config/profile.example.yml — a FICTIONAL executive candidate, both tracks populated

Implement the Tier 0 helper functions from SPEC.md section 6.1 NOW, even though the rules engine
is Phase 5, and write the test that loads the packaged filters.yml and evaluates every rule
against a fixture opportunity. simpleeval cannot evaluate generator expressions, so this test is
the guard against the whole config being dead on arrival. Every helper is null-safe.

models/ with a Modelfile per tier setting PARAMETER num_ctx to the PER-TIER values in SPEC.md
section 8.2 (tier1 4096, tier2 16384, tier3 20480, docsel 8192, trigger 4096), and a
`kestrel models build` command running ollama create for each.

cli.py with typer and the full command surface from SPEC-INTERFACES.md section 7, doctor fully
implemented, the rest stubbed with clear "not yet implemented" messages.

doctor checks, each pass / warn / fail with a specific remediation, never raising:
  Python version, uv present
  Ollama reachable
  configured models present
  each model's effective num_ctx via Ollama's NATIVE /api/show, compared against THAT TIER'S
    computed requirement, failing if short
  a needle test per tier: a distinctive token near the start of a filled-to-budget prompt,
    returned correctly. Metadata can lie; this cannot
  a thinking-suppression probe per model asserting no <think> span survives
  Edge present for Playwright channel="msedge"
  OLLAMA_NUM_PARALLEL and OLLAMA_KEEP_ALIVE (warn only)
  config validity, including the filters.yml load test above
  database presence and migration state — reported as "not yet initialized" rather than a
    failure until Phase 1 exists

DO NOT put the live ATS shape check in doctor yet. The adapters are Phase 3; it is added there.

GATE:
  uv run kestrel doctor produces a readable report
  uv run kestrel models build creates the derived models
  doctor's per-tier num_ctx and needle checks then pass for every tier including tier1
  the filters.yml rule-evaluation test passes
  ruff check clean
````

---

# Phase 1 — Data layer

**any · high**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 1 — Data layer.

Implement EVERY model across all three spec files with SQLAlchemy 2.0 async declarative and
aiosqlite. Do not skip tables that look peripheral; later phases depend on all of them.

SPEC.md section 4: profile, source, scrape_run, opportunity, opportunity_embedding, org,
evaluation, quarantine, pursuit, status_event, task, run, setting, saved_view.

SPEC-MODULES.md: doc_master, doc_layout, doc_section, doc_slot, doc_variant, doc_render,
org_research, contact, outreach_draft, connection, interaction, story, story_usage, prep_pack,
mock_session, offer, engagement_offer, verdict_override, selection_override, label, outcome,
calibration, ml_model, comp_observation.

Plus one not yet named elsewhere:
  notification — id, kind, severity, title, body, opportunity_id nullable, pursuit_id nullable,
  created_at, read_at, delivered_channels json. Phases 7 and 12 raise notifications long before
  Phase 16 builds the delivery channels, so the table and a NotificationService that writes rows
  exist from here. Delivery is a later concern; recording is not.

Details that are easy to get wrong:
- opportunity is the central abstraction across BOTH tracks, not a "job" table. It carries
  org_id, track, opp_type, trigger_data, seen_count, ghost_score, version,
  previous_description_md, liveness, and pre_score.
- evaluation carries verdict AND verdict_model, score AND weighted_score, needs_review,
  forced_by, and few_shot_ids.
- pursuit status enums differ per track.
- status_event is append-only. A status change without one is a bug.
- ml_model carries pca_artifact_path, sklearn_version, and feature_names.

Alembic runs a SYNC engine (sqlite://, not sqlite+aiosqlite://) against the same file the async
app uses. Migration scripts are package data run programmatically, per the Phase -1 findings.

Every connection: PRAGMA journal_mode=WAL, PRAGMA busy_timeout=5000.

Indexes at minimum: opportunity.content_hash, .dedup_group_id, .stage, .track, .org_id,
.ghost_score; evaluation.opportunity_id; task.status+priority; pursuit.status,
pursuit.next_action_at; org.normalized_name unique; comp_observation.normalized_title+geo_bucket.

Task queue in kestrel/queue/: enqueue, lease with expiry, heartbeat, complete, fail with retry,
dead-letter after max_attempts, reclaim expired leases.

Seed the five doc_layout rows from SPEC-MODULES.md section 1.2.

GATE:
  alembic upgrade head creates the full schema, and the same migration runs programmatically
  tests: lease expiry reclaim, retry increments, dead-lettering, two concurrent workers never
    lease the same task, settings precedence at all four levels, status_event written on every
    pursuit status change, NotificationService writes a row
  ruff check clean
````

---

# Phase 2 — LLM client, schema guard, prompt loader

**local for the live gate · high**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 2 — LLM client, schema guard, prompt loader, few-shot and embedding infrastructure.

kestrel/llm/client.py — async OpenAI-compatible client over httpx, driven entirely by config,
handling the per-backend differences in SPEC.md sections 8.1, 8.4, 8.5:
  json_mode json_schema  -> response_format {"type":"json_schema","json_schema":{...}}
  json_mode json_object  -> response_format {"type":"json_object"} + schema in the system prompt
  thinking_control reasoning_effort     -> reasoning_effort: "none"
  thinking_control chat_template_kwargs -> extra_body chat_template_kwargs.enable_thinking=false
  thinking_control none                 -> neither
Crossing these is SILENT. Write a test per combination asserting the exact request body.

Schema normalization, applied once in the client, not per prompt: when the backend is in
json_schema mode, rewrite every union type ["X","null"] to plain "X" and drop that field from
required. Pydantic still models it Optional so a returned null and an absent key behave
identically.

Temperature and max_tokens: the value declared on the individual prompt wins; the tier entry is
the fallback.

kestrel/llm/schemas.py — a pydantic model per schema in PROMPTS.md, all sixteen prompts. String
length limits TRUNCATE via field validators, they never reject. Numeric bounds that the grammar
drops (confidence, holistic, dimension scores) are CLAMPED, not rejected. Validation failure is
reserved for structural problems: missing required fields, wrong types, invalid enums.

kestrel/llm/guard.py — strip <think>/<thinking> unconditionally, parse, validate, one repair call
at temperature 0 quoting the error, then quarantine with the full prompt and raw response and
return a typed failure. Never raise past the caller.

Every prompt from PROMPTS.md into kestrel/llm/prompts/{id}.{version}.md, loaded by version.
Never inline prompt text in Python.

Dynamic schema builder for P3 per SPEC-MODULES.md section 1.3: given a doc_master, emit a schema
with explicit properties per slot key and an enum of that slot's real variant keys, plus "hide"
where optional. This makes hallucination structurally impossible rather than merely detectable.

Few-shot assembly per SPEC-MODULES.md section 5.3: same track, ranked by embedding cosine,
DIVERSIFIED across the verdict range. Gate at settings.learning.min_labels_for_few_shot.

Embedding client against /v1/embeddings with nomic-embed-text, storing float32 bytes.

GATE (any environment):
  respx-mocked tests: valid parsed; malformed repaired on retry; malformed twice quarantined;
    <think> stripped; overlong string truncated not rejected; out-of-range number clamped; all
    four backend request-body variants exact; union-type normalization correct; few-shot
    diversity enforced; P3 dynamic schema contains the right enums for a fixture doc_master
GATE (local):
  a real call to kestrel-tier2 returning schema-valid output for a fixture description, with zero
  thinking tokens in the response
````

---

# Phase 3 — Employment ingestion, plus comp and ghost capture

**any · high**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 3 — Employment ingestion, normalization, dedup, scheduler, and the pure-arithmetic
intelligence capture.

Every employment source in SPEC.md section 5.1 using the EXACT field mappings in Appendix A,
verified live 2026-07-30. The counter-intuitive ones you will get wrong if you assume:
  Greenhouse: location is an OBJECT; content is HTML-ESCAPED HTML; no pagination, one huge
    payload, 60s timeout
  Lever: title field is `text` NOT `title`; createdAt is epoch MILLISECONDS; location is
    categories.location; unknown slug returns 404 so validate at source creation
  Ashby: token is CASE SENSITIVE; respect isListed
  Workday: a POST not a GET; limit caps at 20, paginate on offset against total; the list has NO
    description, fetch per job from the detail path; tenant, data-center segment and site name
    are all config
  SmartRecruiters: title field is `name`; fetch the posting detail for jobAd.sections

JobSpy adapter with parameter-conflict validation enforced at CONFIG LOAD. Always
asyncio.to_thread().

Manual URL and raw-text paths with Playwright escalation on a JavaScript shell. ONE shared
Playwright browser with an async lifecycle owned by the app. channel="msedge".

normalize.py per SPEC.md section 5.4, and note what that section now requires beyond field
mapping, because three later phases depend on it starting here rather than in Phase 12:
  - resolve or create the org row, set opportunity.org_id, and INFER AND PERSIST org.size_band,
    org.sector, org.ownership from the payload. Four consulting Tier 0 rules key off these; if
    nothing populates them the consulting track silently degrades to no filtering
  - write a comp_observation row for every parsed or structured figure
  - compute opportunity.ghost_score from seen_count, the first-to-last-seen span, and version
    history
  - ENQUEUE AN `embed` TASK for every opportunity. Semantic dedup, the reranker's PCA features,
    and MCP semantic search all depend on vectors existing. If nothing enqueues embed, all three
    fail silently and late
All of this is arithmetic and field extraction with no model involved. It costs nothing and the
corpus is worthless without history.

All three dedup passes and the canonical-source priority from section 5.5, plus re-seen handling:
unchanged increments seen_count; materially changed increments version, stores
previous_description_md, and re-queues tier 2.

Scheduler per SPEC.md section 6.5 with every guard, the settings.scheduler.enabled kill switch
defaulting to FALSE, and the daily `pursuit_sweep` task that finds next actions due and pursuits
past the ghosting threshold, writing notification rows.

NOW add the live ATS shape check to doctor, per SPEC.md Appendix A: a missing field is a WARN
naming the field, never a crash.

Real payload fixtures for tests. Never call a live endpoint from a test.

FAN OUT, in this order and not before:
  1. YOU write normalize.py, the source-adapter protocol, the shared HTTP client with rate
     limiting and User-Agent handling, and the Greenhouse adapter as the reference
     implementation. Commit that.
  2. THEN spawn four subagents in a single message, one each for Lever, Ashby, Workday, and
     SmartRecruiters. Each brief contains: the adapter protocol verbatim, that platform's full
     Appendix A entry, the Greenhouse adapter as the worked example, and the instruction to
     write the adapter plus its fixture test. They cannot see your context, so include
     everything.
  3. YOU write the JobSpy adapter yourself; its parameter-conflict validation is subtle and does
     not fan out cleanly.
  4. YOU reconcile, then run the reviewer subagent.

GATE:
  uv run kestrel scan --source <a configured ATS source> fetches real jobs, normalized, with
    org attributes set, comp observations written, ghost score computed, and embed enqueued
  dedup tests on fixtures with the same role from three sources group correctly and select the
    ATS copy as canonical
  a deliberately broken source config produces a `partial` run with a clear error and does NOT
    abort the batch
  scheduler tests: cadence enqueue, no double-enqueue, auto-disable after three failures,
    pursuit_sweep raises notification rows for a due and an overdue fixture pursuit
````

---

# Phase 4 — Consulting ingestion and trigger events

**any · medium**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 4 — Consulting ingestion and trigger-event detection. Read SPEC.md section 5.3 carefully;
this has no equivalent in any existing tool.

The thesis: posted fractional roles are a small contested market. The real opportunity is
inferring demand from public trigger events before a posting exists.

SEC EDGAR full-text search adapter. VERIFIED LIVE:
  GET https://efts.sec.gov/LATEST/search-index?q={query}&forms=8-K
  Elasticsearch-shaped. _id encodes accession number and filename; build the document URL as
  https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{filename}
  A DESCRIPTIVE User-Agent with a contact address is MANDATORY. Requests without one are
  blocked. Cap at 10 req/sec, back off on 403.
  Item 5.02 covers officer departures. Item 1.05 covers material cybersecurity incidents,
  mandatory since 2023.

SAM.gov: requires a free API key, unauthenticated returns 404, NOT verified. Detect a missing key
and disable the source with a clear message rather than failing the run.

PE portfolio crawler over a configured firm list, detecting new portfolio additions.
RSS adapter for company newsrooms and layoff trackers.
Consulting JobSpy sources with job_type contract and fractional/interim terms.

Trigger classification via P10. The post-processing matters as much as the prompt: below
min_trigger_confidence the signal is stored on the org record but not promoted; THREE weak
signals on one org within 90 days promote it regardless of individual confidence. Compounding
weak evidence is the entire mechanism.

Every signal writes to org.signals so it accumulates across opportunities.

Legal constraints enforced in code, not merely documented: public filings and public pages only,
robots.txt respected, identifying User-Agent on every request, rate limiting per source, never
behind a login.

FAN OUT, in this order:
  1. YOU write the signal-source protocol, the trigger classification pipeline with P10, the
     org-signal accumulation and three-weak-signals promotion rule, and the SEC EDGAR adapter as
     the reference. That promotion rule is the whole mechanism; do not delegate it.
  2. THEN spawn three subagents in one message: SAM.gov, the PE portfolio crawler, and the RSS
     adapter. Each brief contains the protocol, the EDGAR adapter as the worked example, and the
     legal constraints verbatim.
  3. YOU reconcile, then run the reviewer subagent.

GATE:
  a live EDGAR query returns parseable hits and the constructed document URLs resolve
  trigger classification on fixture filings produces correct event types, and a filing with no
    supporting sentence yields confidence below 0.4 with event_type "other"
  the three-weak-signals promotion rule is covered by a test
  a missing SAM.gov key disables that source cleanly without failing the batch
  robots.txt compliance and rate limiting are exercised by tests
````

---

# Phase 5 — Tiered pipeline and dual-track scoring

**local for the live gate · high**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 5 — Tiers 0 through 3, both tracks.

Tier 0 as a rules engine over config/filters.yml, per track, using the helper functions built in
Phase 0. SIMPLEEVAL, never eval(), constructed once with the whitelist in SPEC.md section 6.1.
Builtins are len, min, max, abs ONLY — no any, no all, because their useful form needs a
generator expression simpleeval cannot parse. A rule referencing anything outside the whitelist
fails at CONFIG LOAD naming the rule and the unknown symbol.

geo_allows uses name and alias matching per SPEC.md section 6.1, NOT distance. There is no
geocoder in this stack and there will not be one. An unparseable location with unknown remote
type ALLOWS and records geo_uncertain.

seniority_rank returns -1 for unknown, which passes every floor check. Record which rule fired on
every rejection. Compute pre_score and order survivors by it descending.

Tier 1: P1 (employment) and P1C (consulting). Confidence below threshold is FORCED to keep, and
record that the escalation was forced.

Tier 2: P2 and P2C with few-shot injection from Phase 2. The Phase 3 work means the ghost score
and comp corpus inputs are already available, so send them; the prompt shape never changes and an
insufficient corpus is marked "insufficient data (n<5)" rather than omitted.

Every post-processing rule:
  weighted = sum(dimension * weight) per that track's weight set
  needs_review when abs(holistic - weighted) > 0.75
  any blocker forces pass, preserving the model's score, setting forced_by
  legitimacy reject forces pass; suspect caps the displayed score at 3.5
  work_auth explicit_no_sponsorship adds a blocker when the profile requires sponsorship
  store verdict_model alongside the threshold-derived verdict and log disagreement
  for P2C the TOP-LEVEL legitimacy object is authoritative; blocks.g_risk is narrative detail
  P2C writes situation facts and identified buyers back to org.signals

Tier 3 as an explicitly invoked escalation. Never automatic.

Worker per SPEC.md section 6.6 with the TIER-DRAINING RULE: never interleave tasks across tiers
using different models. Graceful stop. Run progress with throughput and an ETA from the trailing
20 completions.

GATE (any environment):
  integration test: 30 fixture opportunities per track through tiers 0, 1, 2 with a mocked LLM,
  asserting stage transitions, rule attribution, weighted score computation, needs_review
  flagging, blocker and legitimacy forcing, quarantine promotion of a failed tier 1, and that
  consulting evaluations write back to org.signals
  a test proving the packaged filters.yml evaluates cleanly under simpleeval for both tracks
GATE (local):
  uv run kestrel run --limit 10 against real ingested opportunities and the real local model
  kill the worker mid-run, restart, confirm it resumes without duplicating or losing work
  report observed items-per-minute for tier 1 and tier 2 in the private build log
````

---

# Phase 6 — Minimal review UI · MILESTONE: USABLE

**any · medium**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 6 — Minimal review UI. After this the system is usable and starts generating the labels
every later phase depends on, which is why it comes before the learning loop rather than after.

FastAPI app bound to 127.0.0.1 on an OS-assigned free port, written to runtime.json. Server-
rendered Jinja, HTMX, Alpine. VENDOR HTMX, Alpine, and a PREBUILT Tailwind stylesheet. No CDN at
runtime; the packaged application must work offline. Orange #D4622A and white with a dark variant.

Four surfaces:
1. /opportunities — server-side paginated and filtered table, track switcher, core filters
   (stage, verdict, score range, org, source, needs-review, ghost score), sort by score and date,
   HTMX inline row expansion showing the evaluation blocks.
2. /opportunities/{id} — full description beside the evaluation, dimension bars showing weighted
   versus holistic, blockers and legitimacy panel, dedup-group members, comp corpus context.
3. THE OVERRIDE CONTROL, one click and always visible on both surfaces: agree / adjust score /
   full override with a reason, writing a verdict_override row. This is the primary label capture
   surface and its friction determines whether the learning loop ever gets data. Keyboard-driven.
   This is built HERE and nowhere else; later phases consume it, they do not rebuild it.
4. /runs — active run progress with live tier counts, throughput, ETA.

REST API under /api alongside each page, including POST /api/opportunities/lookup and
POST /api/focus, so the CLI, extension, and MCP server have full capability later. The UI is a
consumer of the API, not a privileged path.

GATE:
  uv run kestrel serve starts the API and worker
  a Playwright test you write and run: seed fixture data, load /opportunities, filter, open a
  detail, submit an override, confirm the verdict_override row exists and the UI reflects it
  no console errors on any page
  MILESTONE: append to the private build log that the system is usable end to end for review and labeling,
  and state exactly what the operator can do with it today
````

---

# Phase 7 — Learning loop

**any · high**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 7 — The learning loop. Read SPEC-MODULES.md section 5 in full. The most conceptually dense
phase in the build.

Capture: verdict_override already exists from Phase 6, do not rebuild it. Wire up
selection_override (consumed in Phase 8), label, and outcome.

Weight recalibration (5.2): at min_labels_for_calibration (default 30), fit dimension weights
minimizing error against the operator's own scores using ridge regression with non-negativity and
sum-to-one. Report current versus proposed with cross-validated MAE for each. THE OPERATOR
ACCEPTS OR REJECTS; never applied silently. Store a calibration row. Per track independently.

Learned reranker (5.4):
  features: opportunity embedding reduced to 64 dims by PCA fitted on the corpus, plus
    seniority_rank, comp min/max/known-flag, pre_score, each soft signal as binary, source kind
    one-hot, remote_type, description token count, org size band, org ownership, seen_count,
    ghost_score, days since posted, and for consulting the trigger event type and confidence
  target: operator score where labeled, else the tier 2 holistic where not overridden
  model: sklearn HistGradientBoostingRegressor with n_jobs=1, because joblib's loky backend
    re-executes sys.executable and inside a frozen app that relaunches the whole application
  PERSIST THE FITTED PCA alongside the model and record sklearn_version and feature_names.
    Without the PCA the 64-dim features are not reproducible and the model silently scores
    garbage after a restart. Refuse to load a model trained under a different sklearn version,
    falling back to LLM tier 1 and raising a notification
  PROMOTION CRITERIA, all four on a held-out split:
    at least min_labels_for_reranker (default 200)
    held-out MAE below reranker_max_mae (default 0.6)
    keep/kill agreement with the operator at the tier 1 threshold BEATS the tier 1 LLM's
      agreement on the same held-out set
    recall on apply-verdict items above 0.95, because missing a good opportunity is the one error
      that matters and false positives merely cost a tier 2 call
  once promoted tier 1 is a millisecond operation. Items within reranker_uncertain_band of the
    threshold still escalate to the LLM. The LLM path is NEVER deleted
  drift: rolling MAE over the last 50 labeled items; rising past drift_threshold DEMOTES the
    reranker automatically and writes a notification row

Active learning (5.5): rank unlabeled evaluated opportunities by uncertainty combining proximity
to the 4.0 threshold, holistic-versus-weighted divergence, reranker prediction variance,
embedding distance from existing labels, and recency. Build /calibrate HERE: description left,
evaluation right, one-click agree / adjust / override, keyboard-driven, under 30 seconds per
item, showing progress toward the next calibration and reranker thresholds. Phase 13 styles it;
it does not rebuild it.

Outcome-driven calibration (5.6): response rate by score band, source, org size, ownership,
remote type, and track; furthest stage by score band; response rate by document configuration
labeled correlational and small-sample; time-to-first-response distribution setting the ghosting
threshold empirically.
IF THE SCORE IS NOT DISCRIMINATING, SAY SO PLAINLY AND FIRST.

GATE:
  a synthetic label set of 250 items exercises the full path: calibration proposes sane weights;
  the reranker trains and its promotion criteria are correctly evaluated in BOTH a passing and a
  failing case; a model saved and reloaded through the PCA path scores identically; a
  version-mismatched artifact is refused; active learning ranks a known-uncertain item first;
  drift detection demotes on injected degradation and writes a notification; outcome analysis
  correctly reports non-discrimination on a synthetic dataset where score does not predict
  response
  /calibrate loads and a label round-trips
````

---

# Phase 8 — Document engine

**local · high**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 8 — Document engine. Read SPEC-MODULES.md section 1.

Parametric model for BOTH kinds: resume (employment) and capability statement (consulting).
Sections, slots, variants, layouts, render configs.

HARD RULE restated because it is the most important correctness property in the system: the
renderer emits doc_variant.text BYTE-FOR-BYTE. No model output ever reaches a PDF.

Selection via P3 using the DYNAMIC schema builder from Phase 2 — explicit properties per slot
key, enum of that slot's real variant keys, "hide" only where optional. Strict validation remains
as the second line of defense for json_object backends. A hallucinated key surfaces in the UI. On
repair failure render with defaults and set defaults_used, flagged prominently. NEVER silently
substitute.

Rendering: Jinja2 semantic HTML with a print stylesheet, Playwright page.pdf with
print_background and prefer_css_page_size and EXPLICITLY EMPTY header_template and
footer_template, channel="msedge".

ATS safety: single column body, no text in images, no tables for layout, real Unicode bullets,
selectable text, standard section headings.

Privacy: no generator string in the HTML, no organization name in PDF metadata, redact_orgs swaps
org_label for org_label_redacted everywhere.

Page-count guard using pypdf, ?preview route rendering identical HTML, filename
{LastName}_{Org}_{RoleSlug}.pdf.

Import via P7: verbatim check on every derived:false variant, at most two derived:true per slot
never changing a number or scope, proof point extraction, NOTHING written until accepted.

Cover letters via P4 and engagement outlines via P4C with claims_used verification against real
proof point and variant keys, banned-phrase checks, and a minimum of three exclusions for P4C.

Because /documents is Phase 13, ship a `kestrel doc import <file>` CLI command and a seed fixture
so this phase is verifiable now rather than blocked on a UI that does not exist yet.

GATE (local, cannot be verified in a cloud environment):
  uv run kestrel render <id> produces a PDF for each document kind
  assert with pypdf: selectable text layer present; within the page limit; NO generator or
  organization string anywhere in the metadata; and EVERY rendered bullet string exactly matches
  a doc_variant.text row in the database
  a hallucinated variant key is rejected rather than defaulted
  an import proposal containing a non-verbatim derived:false variant is flagged
  the dynamic P3 schema for a fixture doc_master contains exactly the right enums
````

---

# Phase 9 — Research, contacts, networking graph

**any · medium**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 9 — Research, contacts, warm paths. SPEC-MODULES.md section 2.

Research via P5 with the five-tier source acquisition: direct fetch of conventional company
paths, the ATS board itself, SEC EDGAR for public companies, optional SearXNG when configured,
and OPERATOR PASTE. Operator paste is the guaranteed path and must work with zero network.

The model synthesizes supplied excerpts and does not browse. Every claim carries a source_index
resolving to a supplied excerpt. Fewer than two sources returns insufficient_sources rather than
a synthesized answer. Signals write back to org.signals, ENRICHING the size_band, sector, and
ownership fields that normalization set provisionally in Phase 3.

Contacts via P6 with rules ENFORCED IN VALIDATION, not merely stated in the prompt:
  every person must carry a valid source_index resolving to supplied text
  any returned email must appear VERBATIM in a source or be nulled. No pattern-guessing from a
    domain, ever
  confidence must be honest
  contact_type includes board and pe_operator, which the consulting buyer map needs

Outreach drafts only, LinkedIn capped at 300 characters. THE SYSTEM NEVER SENDS.

Networking graph: connection and interaction tables, LinkedIn connections CSV import (a
first-party export the operator downloads themselves, never scraped), and warm-path finding
ranked as: direct connections at the org, formerly at the org, at a company sharing an investor,
sharing a former employer with a decision maker. A graph query, no ML, and frequently the
highest-value screen in the application for the consulting track.

FAN OUT, in this order:
  1. YOU write the shared excerpt-acquisition layer (the five-tier source fetch, excerpt
     structuring with source indices, and the validation helpers that resolve a source_index).
     Every downstream piece depends on that contract.
  2. THEN spawn three subagents in one message: company research over P5, contact discovery over
     P6, and the networking graph with CSV import and warm-path queries. The research and
     contacts briefs must both carry the validation rules verbatim, because those are enforced
     in code and not merely in the prompt.
  3. YOU reconcile, then run the reviewer subagent.

GATE:
  offline is the primary gate: adversarial mocked responses containing a fabricated person, a
  fabricated email, and an out-of-range source_index are ALL correctly rejected
  a LinkedIn CSV export fixture imports and warm paths compute correctly against seeded orgs
  research enriches org attributes without overwriting a more specific value with a vaguer one
  if network permits, one live run against a real company's public pages
````

---

# Phase 10 — Interview preparation

**any · medium**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 10 — Story bank, prep packs, mock interview. SPEC-MODULES.md section 3.

Story consolidation via P8: embed every Block F sketch since the last consolidation, cluster by
cosine above merge_threshold, propose one merged canonical story per cluster showing its sources.
AN OPERATOR-EDITED STORY IS NEVER OVERWRITTEN by a later merge; new sketches attach as usage.
Absolute. The operator's own words about their own career outrank any model synthesis.

Target 8-12 canonical stories. Show THEME COVERAGE AND GAPS. Knowing you have no good failure
story is more useful than having forty mediocre ones.

Prep packs: role summary, research signals, Block B gaps and mitigations, mapped canonical
stories, questions to ask drawn from research UNKNOWNS (which is what makes them specific rather
than generic), known panel members. Renders to PDF via the Phase 8 pipeline.

Mock interview via P9: one question at a time, operator answers in text, critique against the
rubric, and the critical check — every substantive claim must appear in the operator's own proof
points or canonical stories. Flag any that does not. A claim that drifts in practice will drift
in a real interview. Store as mock_session.

Debrief capture writing interaction rows, updating the pursuit, adding discovered contacts, and
feeding outcome data. Compare questions actually asked against questions predicted and report the
hit rate, a direct measure of whether Block F is any good.

GATE:
  consolidation on fixture sketches produces sensible merges and NEVER overwrites an
  operator_edited story
  theme coverage correctly identifies a deliberately missing theme
  mock critique correctly flags a claim not present in the proof points
  prep pack renders to PDF
````

---

# Phase 11 — Negotiation and offers

**any · low**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 11 — Negotiation and offer analysis. SPEC-MODULES.md section 4.

offer and engagement_offer records. Benchmarking against the operator's OWN comp corpus (populated
since Phase 3), the posting's stated band, open competing offers, and their own history. Output is
a range with an EXPLICIT SAMPLE SIZE. Below five observations say "insufficient data" rather than
presenting a precise-looking number from three points.

Scripts via P11 for: deflecting the first-number question, countering below target, geographic
discount pushback, using a competing offer without bluffing, equity versus cash, start date, title
and level (frequently worth more than base for a senior-track candidate), and for consulting rate
versus scope, payment terms, renewal option, exclusivity.

HARD RULES in the prompt AND enforced in review:
  the walk-away number is supplied by the operator; the model never proposes, questions, or
    adjusts it
  never draft a bluff, never suggest implying a competing offer that does not exist
  every justification rests on a supplied proof point or a supplied market data point WITH its
    sample size

Offer comparison matrix across total first-year comp, four-year value, level and scope, reporting
line, location and travel, organizational risk from research signals, and trajectory fit, with
operator-adjustable weights. Presented as ANALYSIS, never as a recommendation to accept.

GATE:
  benchmarking correctly reports insufficient data at n<5 and a sensible range at n>=20
  a script generated with no supporting proof point is flagged
  comparison matrix computes correctly across two seeded offers
````

---

# Phase 12 — Intelligence and analytics · MILESTONE: COMPLETE ENGINE

**any · medium**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 12 — Intelligence querying, liveness, analytics. SPEC-MODULES.md sections 6 and 7.

NOTE: comp_observation writing and ghost_score computation already happen at ingestion from Phase
3. This phase builds the QUERY, AGGREGATION, and PRESENTATION layers over that accumulated data,
plus liveness, plus analytics. Do not rebuild the capture.

Comp corpus queries by normalized title, geo bucket, and size band returning median, p25, p75,
AND SAMPLE SIZE. Sample size always shown; below five the UI says insufficient data rather than
rendering a chart from three points.

Ghost detection: the score exists; add the presentation and the aggregate signals that need
cross-opportunity context — an org with 20+ simultaneous generic postings, and a requisition id
reappearing after being marked closed. Badge it. Confirm it reaches Block G input.

Liveness: re-fetch shortlisted and pursued URLs every 3 days. 404/410, or page text matching "no
longer accepting" / "position filled" / "this job is closed", or the ATS no longer returning the
id, all mark closed and write a notification row.

Analytics per section 7: the funnel per track with conversion at each step, breakdowns by source
and score band and org size and ownership and remote type and trigger type each with sample size,
cost and throughput including reranker savings, quality metrics including override rate and
score-versus-outcome correlation and Block F prediction hit rate, and time metrics.

The honest summary via P12. It must be willing to say the system is not discriminating when the
data shows that. An analytics page that always reports good news is worse than none.

GATE:
  comp corpus returns correct percentiles and refuses at n<5
  ghost scoring correctly flags a fixture posting reposted six times with an identical hash
  liveness correctly marks a 404 fixture closed and writes a notification
  the funnel computes correctly on seeded data and the honest summary correctly reports
  non-discrimination on a synthetic dataset where score does not predict response
  MILESTONE: append to the private build log that the engine is complete. Everything from here is interface
  and packaging.
````

---

# Phase 13 — Full web UI

**any · high**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 13 — Every remaining page from SPEC-INTERFACES.md section 2.1, on the Phase 6 foundation.

Do NOT rebuild the override control (Phase 6) or /calibrate (Phase 7). Style and integrate them.

Global chrome: track switcher persisting across pages and scoping everything, run status strip,
notification bell reading the notification table, Ctrl+K command palette.

Pages, in this order so an interruption leaves the most valuable done:
  /documents      — section and slot tree, inline variant editing, tags, layouts, live preview,
                    test render, markdown import with reviewable proposal and verbatim flagging
  /renders/{id}   — chosen variant beside alternatives with rationale, one-click swap WRITING a
                    selection_override, live preview, page-count warning, download, attach
  /pursuits       — Kanban plus table, track-aware statuses, drag writes status_event, next
                    actions with overdue highlighting, outcome and debrief capture
  /orgs/{id}      — accumulated signals over time, research, contacts, warm paths, every
                    opportunity seen there, comp observations
  /stories        — canonical stories, theme coverage and gaps, consolidation proposals
  /prep/{id} and /mock/{id}
  /offers
  /sources        — CRUD, health, run-now, cadence, JobSpy conflict validation and ATS slug
                    validation BEFORE save
  /settings       — models per tier with a test button, concurrency, thresholds, weights with
                    proposed recalibration, profile editors per track, learning settings and model
                    status, notifications, confidential mode, LAN binding with generated shared
                    secret and QR code, backup and restore, and the filter-rule editor WITH the
                    "test against the last 200 opportunities" preview
  /analytics, /bench, /about
  / dashboard     — last, since it aggregates everything else

Extend /opportunities with the full filter set, saved views backed by saved_view, bulk actions,
and row badges.

Implement confidential-mode redaction in list views and generated documents, not just the toggle.

Responsive throughout, plus the web manifest and service worker caching vendored assets.

Implement `kestrel export` and `kestrel worker`, and the optional update check from
SPEC-INTERFACES.md section 1.5, disabled by default.

FAN OUT, and this is the phase where it pays most:
  1. YOU build the global chrome (track switcher, run strip, notification bell, command palette),
     the shared Jinja base template and partial conventions, and TWO complete pages: /documents
     and /pursuits. Those two between them exercise every pattern the rest need: a nested
     editing tree, a drag-and-drop board, HTMX partial updates, and a form that writes an audit
     row. Commit that.
  2. THEN fan out the remainder in batches of three or four subagents per message:
       batch A: /renders/{id}, /orgs/{id}, /stories
       batch B: /prep/{id}, /mock/{id}, /offers
       batch C: /sources, /analytics, /bench, /about
     Each brief contains the base template, the partial conventions, ONE of your two reference
     pages in full, that page's section of SPEC-INTERFACES.md 2.1, and the relevant API
     endpoints. Mid-tier model is appropriate here.
  3. YOU build /settings and the dashboard yourself. Settings carries the filter-rule preview and
     the weight recalibration approval, both of which touch correctness. The dashboard aggregates
     everything and must come last.
  4. YOU reconcile, then run the reviewer subagent.

GATE:
  a Playwright sweep loads every page with zero console errors
  the filter preview returns correct counts against seeded data
  a slot override on /renders writes a selection_override row
  confidential mode actually redacts in both a list view and a rendered PDF
  the mobile viewport renders /opportunities and /pursuits usably
````

---

# Phase 14 — Desktop shell, tray, packaging, installer

**local · medium**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 14 — Turn it into a double-clickable application.

READ spike/FINDINGS.md FROM PHASE -1 FIRST. This phase is a transcription of a process model you
already proved, not a discovery exercise. If FINDINGS.md contradicts SPEC-INTERFACES.md section
1.1 or 1.4, FINDINGS.md wins, because it was empirically verified on this machine.

Startup sequence exactly as proven: freeze_support first, stdout/stderr repair, Local\ mutex with
POST /api/focus on collision, paths, config seeding, logging, programmatic Alembic on a sync
sqlite:// URL, uvicorn in a thread with install_signal_handlers=False, worker and scheduler from
the FastAPI lifespan on uvicorn's loop, health wait, tray thread, preflight, then
webview.start() on the main thread last and forever.

Setup window with every preflight check from SPEC-INTERFACES.md section 1.2, each reporting
pass/warn/fail with a remediation and a one-click fix where possible: pull missing models with a
live progress bar, build the derived num_ctx models, open the profile editor, open sources.

Tray behavior, which matters because multi-hour runs are the normal case:
  CLOSING THE WINDOW HIDES TO TRAY VIA THE closing EVENT RETURNING FALSE, and the worker keeps
    running, with a first-time toast saying so. Never call webview.destroy() except on quit
  tooltip shows live state: "Kestrel — run 7: 412/900, 18/min, ~27m left"
  menu: Open, Pause worker, Resume worker, Run scan now, Recent notifications, Open data folder,
    Open logs, Quit
  Quit stops the scheduler, signals the worker, waits up to 30s, RELEASES LEASES, exits
  icon states: idle, working, attention

Packaging: PyInstaller ONE-FOLDER with every hidden import and data entry FINDINGS.md recorded.
DO NOT bundle Chromium, Ollama, or any model.

Inno Setup producing Kestrel-Setup-x.y.z.exe: installs to %LOCALAPPDATA%\Programs\Kestrel with NO
admin rights, Start menu and optional desktop shortcuts, uninstaller, optional launch-on-login
minimized to tray.

Single __version__ feeding pyproject.toml, the installer, the window title, /health, /about.

GATE (local, cannot be verified in a cloud environment):
  the built application launches to a working window
  closing hides to tray and a run continues; reopening shows correct progress
  a second launch focuses the existing instance rather than starting a second worker
  Quit releases leases; restarting resumes cleanly
  the installer produces a working installation ON A PATH CONTAINING A SPACE, and the app writes
  nothing beside its executable
  uninstall leaves the data directory intact
  a PDF renders from inside the frozen application
````

---

# Phase 15 — MCP server and browser extension

**any · medium**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 15 — MCP server and browser extension. SPEC-INTERFACES.md sections 4 and 5.

MCP server via fastmcp over SSE on a DISTINCT configurable port (default 9110). Preflight
verifies the port is free. Construct with
FastMCP(name, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
when exposed beyond loopback, because FastMCP fixes allowed_hosts AT CONSTRUCTION TIME and setting
host afterward does not widen it; a forwarded Host header then returns 421 from uvicorn.

Read tools: list_opportunities, get_opportunity, search_opportunities (semantic, over the
embedding index built since Phase 3), get_shortlist, get_pursuits, get_org, get_story_bank,
get_comp_intel (WITH sample size), get_analytics, get_run_status.
Action tools, deliberately few and none of them external: ingest_opportunity, trigger_scan,
trigger_evaluation, set_pursuit_status, add_note.
NO MCP TOOL SENDS ANYTHING EXTERNALLY.

Starts with the app, stops with it, toggleable in settings, DISABLED BY DEFAULT.

Browser extension, Manifest V3, Chrome and Edge, reading the port from runtime.json with an
options-page fallback and sending the shared secret header when LAN mode is on.
  Capture: toolbar button and right-click context item send the current page URL plus extracted
    readable text to POST /api/opportunities/ingest. It reads the page the operator is already
    looking at. IT DOES NOT CRAWL, NAVIGATE, OR ACT.
  Seen-badge: the content script collects visible posting URLs and asks
    POST /api/opportunities/lookup which are known, badging each with score and status. This puts
    the system's judgment into the moment of browsing, which is where the operator loses time.
  Options: API port, shared secret, which boards to badge, badge style.
  Privacy: talks ONLY to 127.0.0.1 or the configured LAN host, narrowest host permissions,
    README stating exactly what it transmits and where.

GATE:
  MCP tools/list returns the expected tool names. A 200 from an SSE endpoint is NOT proof it is
  the right server; assert on the tool list
  each read tool returns correct data against seeded fixtures
  the extension loads unpacked in Edge, captures a page into the pipeline, and badges a seeded
  known URL correctly
````

---

# Phase 16 — Notifications, bench, docs, release

**mixed · medium**

````
[CONSTRAINTS BLOCK, THEN:]

PHASE 16 — Finish it.

Notification DELIVERY channels over the notification table and NotificationService that have
existed since Phase 1: win11toast locally, optional ntfy topic, optional generic webhook. Every
event from SPEC-INTERFACES.md section 6 individually toggleable. BATCHING IS MANDATORY: never
more than one per minute, overflow collapses into a summary, per-item notifications default OFF.
A tool that pings forty times during a run gets muted permanently and then none of the useful
notifications land either.

Benchmark harness: kestrel bench and a /bench page accepting --models and --prompt-version so
exactly ONE variable changes per comparison. Report per model and per track: first-attempt JSON
validity rate, mean tokens in and out, latency, items per minute at configured concurrency,
exact-verdict agreement and mean absolute score error against the gold set, and projected
wall-clock for a 500-item run. Promote a model to a tier from the UI.

Gold set: ship evals/goldset.employment.jsonl and evals/goldset.consulting.jsonl with 20 synthetic
but realistic entries each, clearly marked placeholder. Implement regeneration from accumulated
real labels HERE (Phase 7 captures the labels; this phase turns them into a gold set).

Backup and restore: kestrel backup produces a timestamped zip of database, config, and generated
documents; restore from the setup window.

README.md covering: what it is, install from the installer, first run and the setup window, THE
MODELS BUILD STEP AND WHY IT MATTERS, configuring sources for both tracks, importing a resume and
a capability statement, picking a model with bench, the learning loop and what it needs from the
operator, and the operational traps from CLAUDE.md.

FAN OUT: notifications, the bench harness, gold-set regeneration, backup and restore, and the
README are mutually independent. Write the notification batching yourself, because getting it
wrong makes the whole feature counterproductive, then spawn the other four in one message. A
cheap model is fine for the README.

Final pass: ruff check clean, full pytest suite passing, preflight clean, version stamped,
installer built, the private build log and the private decision log complete.

GATE:
  uv run kestrel bench --models <two you have pulled> produces the comparison table
  notification batching collapses 20 events in 10 seconds into one
  gold set regeneration from a synthetic label set produces a valid file
  backup and restore round-trips a populated database
  the installer builds and installs cleanly on a fresh path
  ruff check and pytest both pass

THEN REPORT, briefly:
1. What works end to end and what does not.
2. The five most significant decisions I should review, with file and line.
3. Anything in the specs that turned out wrong, impractical, or underspecified, and what you did.
4. Measured items-per-minute at tier 1 and tier 2, and which model you would benchmark first.
5. The single highest-value thing to do next.
Do not write a long summary. I will read the code and the private decision log.
````

---

## After the build

Replace the fictional profile with your real one for both tracks. Import your actual resume and
draft a capability statement. Configure your real sources. Run `bench` to pick models. Run a first
real batch.

Then spend ten minutes a week in `/calibrate`. Everything the learning loop does depends on
labels, and nothing else in the system generates them for you.
