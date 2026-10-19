# CLAUDE.md — Standing Orders (Kestrel)

Read this before doing anything in this repository. These instructions override default behavior.

> **The build tool is Claude Code.** This file is auto-loaded from the repo root. `AGENTS.md`
> is a verbatim copy for Warp compatibility; if you change one, change both.
>
> **Rules can be dropped after context summarization.** The phase prompts in `BUILD_PROMPT.md`
> therefore restate the non-negotiable constraints inline. If the agent starts violating a hard
> rule mid-build, compaction has probably occurred; re-paste the constraints block and continue.

---

## What this is

Kestrel is a free, local-first job search app for **any job seeker**, delivered as a
**double-clickable desktop application for Windows, macOS and Linux**. It is **app first**: a
user never has to type a command; every setup step has a button, and raw commands live behind a
"Show details" toggle. It runs AI models on the user's own machine by default (Ollama) or, by the
user's choice, on an online model with their own key (OpenRouter, OpenModel, Anthropic, OpenAI or
any OpenAI-compatible service). It finds openings, scores them for fit, tailors the user's own
resume text per role, tracks applications to outcome, and learns from the user's decisions. A
second track for consulting and contract work exists but is optional and off by default.

The public promises (kestrelapp.co) are binding on the code: free and "you are not the product";
never applies, emails or messages anyone; asks before anything goes to an online model; source
public under PolyForm Noncommercial 1.0.0; built with Claude, ChatGPT, Qwen, DeepSeek, NVIDIA
Nemotron, Space Bunny and LongCat. Do not assume the original operator's hardware, seniority,
location or field anywhere in shipped defaults, copy or examples.

## Source of truth

| File | Authority over |
|---|---|
| `SPEC.md` | Principles, stack, packaging, data model, ingestion, pipeline, scoring |
| `SPEC-MODULES.md` | Documents, research, interview, negotiation, learning loop, intelligence, analytics |
| `SPEC-INTERFACES.md` | Desktop shell, web UI, REST API, MCP server, extension, notifications, CLI |
| `PROMPTS.md` | Every LLM prompt, schema, and failure mode |
| `REFERENCES.md` | Every external API and doc, with verification status |

If this file, the code, and a spec disagree, **the spec wins**. If the spec is wrong, fix the
spec first, then the code. Build history and decision records are kept privately by the maintainer.

---

## Session start

1. Read this file.
2. Build history and decision records are kept privately by the maintainer. Assumptions already
   made are settled; do not relitigate them.
3. Read only the spec sections relevant to the current phase. They are long by design.
4. State in a few lines: current phase, what is done, the single next action. Then work.

---

## Build discipline

Build history and decision records are kept privately by the maintainer. Decide, document
the assumption in the PR description, continue. Do not stop to ask.

**Every phase ends with a verification gate.** Run it. If it fails, fix it. If it genuinely
cannot pass (a model not pulled, no network, no GPU in a cloud environment), log exactly why,
mark the phase `BLOCKED`, and continue with work that does not depend on it.

**Tests alongside code, never after.**

**Never call a live job board, ATS, or SEC endpoint from a test.** Fixtures live in
`tests/fixtures/`, captured from real payloads with identifying data replaced. Some
signal-source tests build their payloads inline in the test instead; they still never touch the
network.

**No scope creep.** This specification is complete; there is no v2 backlog. If something is not
in a spec file, it is deferred, not put into the code.

**Commit at every phase boundary**, conventional-commit style (`feat:`, `fix:`, `test:`, `chore:`).

**CI minutes are metered (D-CI-1, D-CI-2).** Windows bills 2x and macOS 10x, and every push to a
PR branch runs CI. So CI confirms; it does not discover.
- Batch work: finish a coherent unit locally, then push once. Never push work in progress, and
  never push just to "see what CI says".
- Before every push run `uv run --extra dev python scripts/check.py` (lint plus the full suite, including
  the workflow, installer-script and notices guards) and push only when it ends "Ready to push."
  `--quick` is for iterating, not for pushing. The opt-in hook: `git config core.hooksPath .githooks`.
- A warning in any CI or release log (a deprecated runtime, an annotation, a resource warning)
  becomes a failing local test in the same PR, not a note for later.
- Windows-only behavior is the one thing the Windows CI job still has to prove; say so in the PR.

---

## Subagent strategy

The main agent's context window is the scarcest resource across eighteen phases, more than time
or credits. A subagent has its own context and returns only its conclusion, so the parent never
ingests what it read. Use subagents for that first and for cost second.

**Mandatory: a reviewer subagent before any gate is declared passed.** Fresh context, given the
phase's diff and that phase's constraints, asked only *what is wrong with this*. Never asked to
fix anything; the parent acts on the findings and re-verifies. The author of a change is blind to
it in ways a fresh reader is not, and this is the single highest-return pattern available here.
Use the same model class as the parent. A cheap reviewer finds only cheap bugs.

**Fan out only after the contract exists.** Phases 3, 4, 9, 13, and 16 contain genuinely
independent implementations. The parent writes the shared interface and **one reference
implementation** first, then spawns the siblings **in a single message** so they run
concurrently. Fanning out into empty space produces N subtly different conventions, and
reconciling those costs more than the parallelism saved.

**Never fan out phases -1, 0, 1, 2, 5, 7, 8, or 14.** Each holds many interlocking decisions
simultaneously. Splitting them yields inconsistent interfaces and a slower net result.

**Test-writing subagents must not see the implementation.** Give them the spec section and the
public interface only. This prevents tests that merely mirror whatever the code happens to do,
which is the standard failure mode of tests written by the implementer.

**Lookup subagents for pure context savings.** "Read `SPEC-MODULES.md` §5 and return the reranker
feature list and promotion criteria as a compact spec" costs the parent 500 tokens instead of
15,000.

**Model selection.** Parent-class for review, and for anything touching scoring, the learning
loop, or document selection. Mid-tier for ATS adapters, UI pages, and tests. Cheap only for
documentation and boilerplate.

**Brief cost is real.** A subagent cannot see the parent's context, so every brief must be
self-contained. When writing the brief would cost more than doing the work, do the work.

---

## Hard rules

These are the rules that, if broken, make the system actively harmful rather than merely
imperfect. They are worth restating in every phase prompt.

**1. The system never acts externally on the operator's behalf.** It never submits an
application, never sends an email or message, never clicks apply, never fills and submits an ATS
form, never automates LinkedIn or any social platform. It drafts, recommends, and hands over.
This boundary was decided deliberately. Do not relax it, and if you find yourself writing code
that transmits something to a third party on the operator's behalf, stop and treat it as out of scope.

**2. The model selects, it never invents.** Every bullet that reaches a PDF is the operator's
own text, byte-for-byte, from `doc_variant.text`. A model-hallucinated variant key is a
validation failure that must be surfaced, **never silently defaulted away**. This is the single
most important correctness property in the system.

**3. Structured output or failure.** Every LLM call is schema-validated. One repair attempt,
then quarantine with the raw response preserved. Never regex-parse a model response. Never
accept prose where JSON was specified. Never let a parse failure crash a run.

**4. Uncertainty escalates, it never rejects.** A low-confidence Tier 1 verdict becomes "keep".
A quarantined Tier 1 item is promoted to Tier 2, not dropped. An ambiguous title passes Tier 0.
A wrongly rejected opportunity is invisible and unrecoverable; a wrongly kept one costs seconds.

**5. Resumability is not optional.** Every unit of work is a `task` row. Killing the process at
any moment and restarting must continue cleanly. No in-memory-only state that matters.

**6. Sample sizes are always stated.** Any number derived from fewer than five observations is
labeled as insufficient rather than rendered as if it were solid. This applies to comp
intelligence, conversion analytics, and every generated summary.

**7. Only public data, politely.** Public filings and public pages only. Respect `robots.txt`.
Send a descriptive User-Agent (mandatory for SEC EDGAR). Rate-limit every source. Never scrape
behind a login. Contact records hold only publicly listed business contact information.

---

## Code conventions

- Python 3.12. Type hints everywhere. `from __future__ import annotations` at the top of every
  module.
- `ruff` for lint and format, line length 100. Run before declaring a phase done.
- Async throughout ingestion, LLM, and web layers. **JobSpy is synchronous; always
  `asyncio.to_thread()`.** Never put sync `requests` on the event loop.
- No bare `except:`. Catch specific exceptions. Every caught exception either recovers
  meaningfully or is logged with context and re-raised.
- Pydantic v2 at every boundary: config, LLM responses, API bodies.
- SQLAlchemy 2.0 async style (`select()`, not `Query`) with `aiosqlite`. Alembic runs a **sync**
  engine against the same file. Every connection sets `PRAGMA journal_mode=WAL` and
  `PRAGMA busy_timeout=5000`.
- No secrets in code, config YAML, logs, or committed files. `.env` only.
- `structlog` for logging. No bare `print()` outside the CLI's own output.
- **All paths resolve through `paths.py`.** Never hardcode a relative path; the packaged app runs
  from a read-only directory.
- **All package data via `importlib.resources`.** Never `__file__`-relative arithmetic.

---

## Privacy

- **Never put organization names in file metadata, PDF properties, headers, footers, or
  generator strings.** Playwright PDF generation must set empty `header_template` and
  `footer_template` explicitly.
- The operator's search is confidential. Confidential mode redacts the current employer
  everywhere in generated output and list views.
- `config/profile.yml`, the data directory, logs, and `.env` are never committed. The shipped
  example profile is fictional.
- All inference is local by default. A hosted Tier 3 backend is the only path that can transmit
  operator data externally; it is opt-in per opportunity and the UI states exactly what will be
  sent before the operator confirms.

---

## Output style

- The brand is the **Paper and Indigo** scheme (indigo `#4F5BD5`, dark-mode indigo `#7B86F0`),
  matching kestrelapp.co, including the app icon, tray icon and in-app mark (D-UI-53). Every UI
  color comes from theme tokens so the operator's chosen scheme applies; never hardcode a color.
  Orange `#D4622A` is retired as a brand color.
- **No em dashes** in generated prose or UI copy.
- American English.
- Executive register in anything the operator reads: lead with the decision, stay concise,
  separate analysis from recommendation, state risk proportionately without catastrophizing.

---

## Token and context discipline

- Targeted `grep` and partial reads over whole files. The specs are long; read the section you
  need.
- Warn before any action consuming a lot of context.
- **If context is filling: finish the current phase, summarize the state in the PR description,
  commit, and say explicitly that a fresh session should start.** Do not degrade. On Warp,
  prefer a deliberate `/compact` at a phase boundary over letting it fire mid-derivation.

---

## Platform traps — learn these once

These were verified against live documentation and live APIs. Do not rediscover them.

- **Context window silently truncates.** The OpenAI-compatible endpoint cannot set `num_ctx`.
  Ollama applies the model default, far below what this system sends. A long description is cut
  in half with no error and the evaluation is quietly wrong. Build derived models with
  `PARAMETER num_ctx` **sized per tier** (SPEC §8.2) and fail preflight if the effective value is
  short of that tier's own budget. A single global threshold is wrong and would make tier 1 fail
  a check it has no reason to pass. Verify with a needle test, not just `/api/show`; metadata can
  lie. **This is the most likely cause of "runs fine, results are bad".**
- **Disabling thinking is backend-specific.** Ollama: `reasoning_effort: "none"`. oMLX and vLLM:
  `extra_body.chat_template_kwargs.enable_thinking = false`. Sending the wrong one is silently
  ignored and you find out via a 5x token bill. Strip `<think>` spans defensively regardless.
- **Structured output on Ollama goes through `response_format`**, not the native `format`
  parameter, because every call targets `/v1/chat/completions`.
- **JSON Schema subset.** Constrained decoding supports `type`, `properties`, `required`,
  `enum`, `items`, `$ref`/`$defs`. `patternProperties`, `oneOf`, `allOf`, `if/then` produce an
  unsatisfiable grammar, not a warning. `maxLength` and numeric bounds are advisory: **truncate
  overlong strings and clamp out-of-range numbers, never reject them.** Union types
  `["X","null"]` are normalized to plain `"X"` plus removal from `required`, once in the client.
  **P3's document-selection schema is generated dynamically per `doc_master`** with explicit
  properties and per-slot enums, so a hallucinated variant key is structurally impossible rather
  than merely detectable.
- **`simpleeval` cannot evaluate generator expressions** (`GeneratorExp`), and supports
  comprehensions only under `EvalWithCompoundTypes`. Every Tier 0 rule therefore uses a
  whitelisted null-safe helper function. An AST guard in `kestrel/rules/helpers.py`
  (`_assert_no_comprehensions`) rejects generator expressions, comprehensions and lambdas at
  config load; `simpleeval` is not what stops them. That guard is load-bearing: do not remove
  or loosen it. Reintroducing `any(x for y in z)` into `filters.yml` makes the shipped config
  fail to load on first run. There is a test guarding this; keep it.
- **There is no geocoder in this stack and there will not be one.** `geo_allows` matches
  municipality names and operator-maintained aliases, never distance.
- **`webview.start()` blocks the main thread and runs once per process.** Close-to-tray is the
  `closing` event returning `False` plus `window.hide()`. Never `destroy()` except on quit.
- **`uvicorn.Server.run()` off the main thread** needs `install_signal_handlers = False`.
- **The worker, scheduler, and every DB session live on uvicorn's loop**, started from the
  FastAPI lifespan. A second loop gives cross-loop aiosqlite errors that look like corruption.
- **In a `--windowed` PyInstaller build `sys.stdout` and `sys.stderr` are `None`.** Repair them
  before configuring logging, and call `multiprocessing.freeze_support()` first.
- **Single-instance mutex is `Local\`, not `Global\`.** Global needs a privilege and blocks a
  second Windows user. A mutex detects; it does not focus. Focus is `POST /api/focus` to the
  running instance found via `runtime.json`.
- **sklearn needs `n_jobs=1`** inside a frozen app, because joblib's loky backend re-executes
  `sys.executable` and relaunches the whole application. Persist the fitted PCA beside the model
  and record `sklearn_version`; pickles are version-locked.
- **`tiktoken` downloads its encoding on first use.** Vendor `cl100k_base`, set
  `TIKTOKEN_CACHE_DIR`, or the packaged app fails offline.
- **Alembic in a frozen app:** `versions/*.py` as `datas` not analyzed modules, absolute
  `script_location`, no `__file__` arithmetic in `env.py`, and a `sqlite://` URL for the sync
  engine, never `sqlite+aiosqlite://`.
- **Model swap thrash.** With 16GB VRAM, alternating tier 1 and tier 2 models forces constant
  unload and reload. The worker **drains all pending tasks for one tier before starting
  another**, always, with no exception when two tiers share a model. Set `OLLAMA_KEEP_ALIVE` generously.
- **`OLLAMA_NUM_PARALLEL`** must be at least the max configured concurrency or requests
  serialize regardless of how many are sent.
- **Lever's job title field is `text`, not `title`.** Greenhouse's `location` is an object.
  Workday's jobs endpoint is a **POST** with a 20-item cap. SmartRecruiters' title is `name`.
  Full shapes in `SPEC.md` Appendix A, all verified live.
- **SEC EDGAR requires a descriptive User-Agent** with a contact address, and caps at 10
  requests/second. Requests without one are blocked.
- **LinkedIn is not searched (D-SRC-5).** Its terms forbid automated access, and the public site
  says Kestrel does not search it. A JobSpy source naming LinkedIn is refused at config load; a
  LinkedIn posting can only be pasted by hand. A 429 from any board is a `partial` run, never a
  `failed` one, and never aborts the batch.
- **Indeed permits only one** of `hours_old`, `job_type`+`is_remote`, or `easy_apply` per search.
  Validate at config load and refuse with a clear message.
- **Playwright uses `channel="msedge"`**, driving the Edge already on the machine. Do not bundle
  Chromium; it makes the installer 150MB larger for no benefit.
- **PyInstaller one-folder, never one-file.** One-file extracts to temp on every launch, is slow,
  and trips antivirus heuristics.

---

## Environment split — matters if you are a cloud agent

Some phases require the operator's actual hardware and cannot be verified remotely.

**Cloud-safe:** anything that is pure code plus mocked tests. Data layer, schema definitions,
API surface, web templates, extension, CLI structure, unit tests against fixtures.

**Local-required:** anything touching a real model (Ollama on the user's GPU or CPU), Playwright with real
Edge, PDF generation, the packaged desktop app, or a live external API.

If you are running in a cloud environment and hit a local-required gate, **mark it
`BLOCKED — requires local` in the PR description with the exact command to run, and continue.** Do
not fake the verification and do not mark the phase complete.

---

## Definition of done

1. Ingestion working across JobSpy, all five ATS platforms, SEC EDGAR triggers, and manual paste,
   deduped and normalized, for both tracks.
2. Tiers 0 through 3 running to completion on a real batch, resumable, with quarantine handling,
   for both tracks.
3. Document engine: slot and variant editing, model-driven selection with strict validation, PDF
   render, operator override, for resume and capability statement.
4. Pursuit tracker with status history, next actions, and outcome capture.
5. Research, contacts, and warm-path finding.
6. Story bank, prep packs, mock interview, negotiation, offer comparison.
7. Learning loop: capture, calibration, few-shot injection, reranker with promotion criteria,
   active learning queue, drift detection.
8. Intelligence: comp corpus, ghost detection, liveness.
9. Analytics with the honest summary.
10. Web UI covering every page in `SPEC-INTERFACES.md` §2.1.
11. MCP server and browser extension.
12. The installers (Windows `Kestrel-Setup-x.y.z.exe`, macOS, Linux AppImage) install and launch to
    a working window on a clean machine, and first-run setup completes without a command line.
13. Preflight passes clean. `ruff check` and the full `pytest` suite pass.
14. Documentation and specs are complete and current.
