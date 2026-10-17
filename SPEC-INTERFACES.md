# Kestrel — Interface Specification

> The desktop application, the web UI it hosts, the REST API, the MCP server, the browser
> extension, notifications, and the CLI. Read `SPEC.md` §0-§2 first.

| § | Interface |
|---|---|
| 1 | Desktop application shell |
| 2 | Web UI |
| 3 | REST API |
| 4 | MCP server |
| 5 | Browser extension |
| 6 | Notifications |
| 7 | CLI |

---

## 1. Desktop application shell

The product is `Kestrel.exe`. The operator double-clicks it. Everything is reachable from the
window. The CLI exists for debugging and for the build agent's verification gates, not as the
user interface.

### 1.1 Startup sequence

**The process model is the hardest part of this whole build. Get it exactly right or Phase 14
will consume a week.** Build the ~100 line spike described in `BUILD_PROMPT.md` Phase -1 before
anything else, and freeze the result.

```
main thread                          background threads
───────────                          ──────────────────
multiprocessing.freeze_support()
fix sys.stdout/stderr (None in --windowed builds)
acquire mutex "Local\Kestrel..." ── already held? POST /api/focus, exit
resolve paths, seed config
configure structlog to files
run Alembic (SYNC engine, sqlite://)
                                     ├─ thread: uvicorn.Server.run()
                                     │    install_signal_handlers=False
                                     │    worker + scheduler started from
                                     │    FastAPI lifespan, on uvicorn's loop
wait for /health 200 (30s)
                                     └─ thread: pystray icon.run()
run preflight
webview.create_window(...)
webview.start()  ◄── BLOCKS FOREVER
```

**Five things that will each cost you a day if missed:**

1. **`webview.start()` blocks the main thread and can be called only once per process.** There is
   no restarting the GUI loop. "Close hides to tray, reopen shows progress" is implemented by
   subscribing to `window.events.closing`, returning `False` to cancel the close, and calling
   `window.hide()`. Reopening calls `window.show()` from the tray thread. **Never call
   `webview.destroy()`** unless you are quitting the process. **Verified in Phase -1:** on quit
   you *do* call `window.destroy()`, and pywebview (6.2.1) **re-fires the same `closing` event**
   for that programmatic destroy. A `closing` handler that unconditionally returns `False` cancels
   its own quit and the process hangs alive forever. Guard it: a module-level `_quitting` flag,
   handler returns `True` (allow close) when quitting else hides and returns `False`, and
   `do_quit()` sets the flag before calling `destroy()`.
2. **`uvicorn.Server.run()` off the main thread.** Historically this raised `ValueError: signal
   only works in main thread`. **Verified in Phase -1 against uvicorn 0.52.0:** modern uvicorn no
   longer has an `install_signal_handlers` attribute at all — `Server.capture_signals()` already
   skips signal installation when not on the main thread, so off-thread serving works with no extra
   step. On OLDER uvicorn `install_signal_handlers` is a *method* that raises off-thread; override
   it with a no-op **callable** (`server.install_signal_handlers = lambda: None`). Do **not** set it
   to `False` (a bool that gets called and raises `TypeError` on those versions), and do not treat
   the assignment as load-bearing protection on current uvicorn.
3. **The worker, the scheduler, and every DB session must live on uvicorn's event loop.** Start
   them from the FastAPI `lifespan` context. A second loop means `aiosqlite` and SQLAlchemy throw
   cross-loop errors that look like random corruption.
4. **In a `--windowed` PyInstaller build `sys.stdout` and `sys.stderr` are `None`.** structlog,
   uvicorn's logger, and anything that prints will raise `AttributeError` on first write. Replace
   both with file handles or `io.StringIO` before configuring logging.
5. **`multiprocessing.freeze_support()` must be the first statement** in the entry point, or any
   library that spawns a process relaunches the whole application.

**Single instance:** use `Local\Kestrel` as the mutex name, not `Global\`. The global namespace
requires `SeCreateGlobalPrivilege` and would block a second Windows user on the same machine.
`CreateMutexW` then `GetLastError() == ERROR_ALREADY_EXISTS (183)`; never close the handle.

**A mutex gives detection, not focus.** `SetForegroundWindow` from a non-foreground process
silently fails on Windows. The second instance instead reads `runtime.json` and issues
`POST /api/focus`, which the running instance handles by calling `window.show()` and
`window.restore()` on the GUI thread.

**Free port, not a fixed one.** A hardcoded port collides eventually, and the operator already
runs a dozen local services. **Verified in Phase -1:** bind a socket to `("127.0.0.1", 0)`
in-process, read the OS-assigned port off it, then hand it to `uvicorn.Server.run(sockets=[sock])`.
This yields the port *before* serving, which is cleaner than reading it back from the started
server. Write the chosen port and the process id to `%LOCALAPPDATA%\Kestrel\runtime.json` so the
MCP server, browser extension, and a second launch can find the running instance.

### 1.2 Preflight

A guided setup window, not a stack trace. Each check reports pass, warn, or fail, with a
remediation and, where possible, a one-click fix.

| Check | On failure |
|---|---|
| Ollama reachable at the configured base URL | "Ollama is not running." Button: open the Ollama download page. Retry button. |
| Required base models pulled | List missing. Button: pull each with a live progress bar. |
| Derived `num_ctx` models built | Button: build them (runs `ollama create` per Modelfile). |
| Effective `num_ctx` adequate per tier | Show required versus actual, offer rebuild. **Blocking.** |
| Thinking-suppression probe returns no `<think>` span | Warn with the correct parameter for the backend. |
| `OLLAMA_NUM_PARALLEL` at least max concurrency | Warn with the value to set and how. |
| Edge / WebView2 present | Almost always true on Windows 11. Offer `playwright install chromium` fallback. |
| Database migrated | Auto-fix. |
| Profile configured for at least one track | Open the profile editor. **Blocking.** |
| At least one enabled source | Open the sources page. Warn only. |

### 1.3 Tray behavior

Multi-hour runs are the normal case, so the window is not the process.

- **Closing the window hides to tray. The worker keeps running.** A first-time toast says so.
  This is the default and is optional: Settings > Appearance > Desktop window turns it off
  (`ui.close_to_tray`), after which closing the window is a graceful quit, the same as the tray's
  Quit. With no tray available, closing always quits, so the app is never left running unseen.
- Tray tooltip shows live state: `Kestrel — run 7: 412/900, 18/min, ~27m left`.
- Tray menu: Open, Pause worker, Resume worker, Run scan now, Recent notifications, Open data
  folder, Open logs, Quit.
- Quit stops the scheduler, signals the worker to stop, waits up to 30 seconds for in-flight
  tasks, releases leases so nothing is stranded, then exits.
- Icon state: idle, working, attention needed.

### 1.4 Packaging

**PyInstaller one-folder**, not one-file. One-file extracts to a temp directory on every launch,
is slow, and triggers antivirus heuristics. One-folder plus an installer is faster and quieter.

**Inno Setup** produces `Kestrel-Setup-x.y.z.exe`: installs to `%LOCALAPPDATA%\Programs\Kestrel`
(no admin rights needed), creates Start menu and optional desktop shortcuts, registers an
uninstaller, and offers a "launch on login (minimized to tray)" checkbox. The installer never
writes `%LOCALAPPDATA%\Kestrel`; after removing the program, the uninstaller asks whether to delete
that data folder too (default No; a silent uninstall always keeps it).

Bundle as package data: Alembic migrations, Jinja templates, static assets, **vendored HTMX,
Alpine, and the prebuilt Tailwind stylesheet**, prompt files, Modelfiles, default config, the
vendored `cl100k_base` tiktoken encoding, the tray icons, and the in-process embedding model
(`kestrel/resources/embed_model/`, fetched at build time). Access all through
`importlib.resources`.

**PyInstaller specifics that are not obvious and will each cost a build cycle:**

| Dependency | What is needed |
|---|---|
| **Alembic** | `versions/*.py` must be `datas`, **not** analyzed modules, because `ScriptDirectory` imports them from disk at runtime. Set `script_location` to the resolved absolute path. `env.py` must contain no `__file__` arithmetic. `command.upgrade()` is synchronous and the URL must be `sqlite://`, **not** `sqlite+aiosqlite://` |
| **Playwright** | `--collect-all playwright`. The `playwright/driver` directory (`node.exe` plus `package/`) is not collected by default and nothing works without it |
| **Playwright fallback** | If Chromium is ever installed, set `PLAYWRIGHT_BROWSERS_PATH` to `%LOCALAPPDATA%\Kestrel\browsers`. The install directory is read-only |
| **scikit-learn** | `collect_submodules('sklearn')` plus explicit hidden imports for `sklearn.utils._typedefs`, `_heap`, `_sorting`, `_vector_sentinel`, and `scipy.special.cython_special`. Adds roughly 150-250MB to the bundle |
| **pywebview** | The EdgeChromium backend needs `pythonnet`/`clr`. Historically the most fragile hook in this stack; verify it in the Phase -1 spike, not in Phase 14 |
| **tiktoken** | Ship the encoding file as data and set `TIKTOKEN_CACHE_DIR`, **and add `tiktoken_ext.openai_public` (with `tiktoken_ext`) as hidden imports.** Verified in Phase -1: the encoding constructor is a namespace-package plugin invisible to the module graph, so without the hidden import `get_encoding("cl100k_base")` fails in the frozen app even with the cache vendored. The `blobfile` missing-module warning is benign (tiktoken's *remote* loader, unused offline). **The vendored cache file must keep its exact SHA-1 name** (e.g. `9b5ad71b…`, the hash of the blob URL); tiktoken locates the cache by hashing the URL, so renaming it forces a network fetch. Vendor by running tiktoken once online with `TIKTOKEN_CACHE_DIR` set and copying the cache dir verbatim |

Yes, sklearn costs more bundle size than the Chromium being avoided. The Chromium decision is
about avoiding a **download and a second browser engine on the machine**, not about bytes.

**Do not bundle Chromium.** Playwright uses `channel="msedge"`.

**Do not bundle Ollama or any Ollama model.** They are external dependencies preflight
verifies. The one exception is the in-process embedding model (`SPEC.md` §8.1.1): about 90MB of
ONNX weights plus `onnxruntime` and `tokenizers`, fetched and hash-verified at build time by
`scripts/fetch_embed_model.py`, so a hosted-only install needs no Ollama for embeddings.

Version stamping: a single `__version__` feeds `pyproject.toml`, the installer, the window
title, the `/health` response, and the About page.

### 1.5 Update check

On startup, optionally check a configured URL for a newer version and show a non-blocking banner
with a download link. **No auto-update, no silent download.** Disabled by default; the operator
opts in.

---

## 2. Web UI

Server-rendered Jinja, HTMX for partial updates, Alpine for local state, vendored Tailwind.
Paper and Indigo (D-UI-53): indigo `#4F5BD5` on white paper, with a dark variant of the same
palette. Every color comes from the theme tokens in `kestrel.css`, never a hardcoded hex in a
template. Everything works offline.

**Global chrome:** a track switcher that persists and scopes every page: when both tracks are in
use, two tabs (Employment | Consulting, no "Both" view), each with a badge counting what waits
for the operator on that track (Worth a look plus follow-ups due, hidden at zero); with one
track in use, no switch (D-UI-57); a run status strip when a run is active; a notification bell; a command palette
(Ctrl+K) for jump-to-opportunity and quick actions. The Pipeline nav badge counts the active
tab's roles not yet rated (its Being scored group), in the same badge style as Calibrate, hidden
at zero, with a small spinning ring beside it, and on the Being scored header, only while a run
is active (the worker not paused and work waiting on a track that is switched on); reduced
motion stops the spin (D-UI-61). Every badge counts exactly the rows of the page or chip it
opens, on the active tab's track. A switched-off track does not scan or score (its tasks wait,
unclaimed), and its open pursuits are never hidden: Today's Act card and Applications name the
track and link to its board (D-UI-68). Work the operator asked for (Score again, a Retry, an MCP
scan) is never held by the switch.

### 2.1 Pages

**`/` Dashboard**
A persistent run panel, full width at the top and always present (D-UI-59): searching ("Searching
3 of 5 searches", a breathing bar, the search running now, last activity), rating (the segmented
bar of scored, failed, rating now and waiting, the counts, the model, the time left when an
estimate exists), "Finishing up" while the run is still open with no search or scoring step
live, paused ("Pausing: finishing N" while leased tasks finish, then "Paused"; a Resume button;
nothing pulses; no stall warning), or the last run (its local
date and time, "finished" or "finished, 1 source had a problem" with a tooltip naming the source
and that it is retried next run, a dim bar, then two lines at one size: "107 new roles found, 24
rated. This run added 8 to Worth a look (now 19 in Review), 4 to Maybe and 12 to Not a fit." and
"83 set aside by your rules (location 50, title 33) Review rules · 3 failed Score again"). The run
is global, one figure for every track, and the panel's headline is the very string the status
rail prints; only the additions are the active tab's, naming the track when both are in use, and
a track that never ran says "No runs yet on this track". "Now N in Review" is the Review card's
own number. Below it three cards: **Review** (wider: the Worth a look count, the top three roles
with fit word, title, org and fit number, redacted under confidential mode as every list is,
Open Pipeline and "N more"), **Act**, and **Schedule** (mode in words, a thin status bar whose
tooltip is the current step, the next scheduled run, the last scheduled run and how it went;
D-UI-60). The honest line on whether the score discriminates sits under the cards with its n.
Shortlist awaiting review. Pursuits with a
next action due this week, overdue highlighted. The Calibrate queue count (§5.5 of
`SPEC-MODULES.md`) with a prominent entry point. Recent trigger events for the consulting track.
Errors and quarantine count. Source health grid. The honest-summary narrative from analytics.
A fresh install (no completed onboarding, no `profile.yml`, no stored `llm.mode`) is redirected
from `/` to `/onboarding`; an install whose onboarding is not complete gets a "Finish setup"
banner that links there (D-UI-30).

**`/onboarding` Onboarding wizard** (D-UI-30; replaces the Setup page)
One self-contained flow from model setup to the first search, for an operator who has never
seen Kestrel. Full-page, with "Step N of M", a progress row whose ticks are read from the real
state on every render, Back and Next on every step, and a help panel on every step (what this
is, why it matters, what happens next). The current step, the steps passed and the steps
skipped persist in the setting table (`onboarding.state`), so a killed app resumes at the same
step. Steps:
1. **Welcome**: what Kestrel does and never does (it never applies or sends anything), privacy
   (local by default; a hosted key sends only what the operator accepts), time to complete.
2. **How Kestrel will think**: detected hardware, the recommendation and the low-spec warning,
   and Local, Hosted or Both completed inside the step. Hosted: a plain explanation of bring your
   own key, where each provider's keys page is, provider, key, the disclosure, an "I understand"
   box, and Connect (the same connect service as Settings > Hosted). Local: Ollama status with the
   install command, pull, build and verify with their live log. Next needs a choice; the models
   may finish later.
3. **What you are looking for**: employment, consulting or both, consulting explained in plain
   words. Choosing one track removes the other's terms step from the flow and the count.
4. **About you**: the Profile page's About you form, saved for every track in use; Next needs a
   name.
5. **Your resume**: the Documents import (read, proposal, accept) embedded; skippable, with the
   note that no tailored documents exist until one is imported.
6. **What you want in a job** and 7. **in consulting work**: the Profile forms, each shown only
   for a track in use; Next needs the step saved, so example values never become searches.
8. **Where you will work**: the Location form, saved for every track in use, with the plain
   explanation that matching is by place name.
9. **Your searches**: what Kestrel will search, from the generated-source dry run, in plain
   English, or which earlier step to fill. **Start searching** switches the generated searches
   on, commits, and queues the first scan (the tray's Run scan now path). It is the only control
   in the wizard that turns scraping on.
10. **Done**: what happens next (results in Pipeline in minutes, Today as the daily start) and
   how to restart onboarding. Reaching it marks onboarding complete.

Each step has one primary button ("Save and continue", "Save job terms and continue", "Save
resume and continue", "Start searching", "Finish") and, where the step can wait, a quiet "Skip
for now"; there is no second Save. A primary that needs something first is disabled with a hint
("Add your full name first", "Approve your resume text first", "Choose Local, Hosted or Both
first"). The resume step's proposal carries the approval box and the save (D-UI-65).
Every save inside the wizard returns the whole step, so no indicator is ever stale; the local
model box polls itself while an action runs and reloads the step when it finishes. `/setup`
redirects to `/onboarding` until onboarding is complete, then to `/settings#system`.

**`/opportunities` Pipeline**
The main working surface. Server-side pagination and filtering over several thousand rows.

- Filters: track, stage, verdict, score range, org, source, remote type, comp range, date
  posted, has-blocker, legitimacy status, ghost score, liveness, needs-review, trigger type.
- Sort: score, date posted, first seen, pre-score, org, ghost score.
- Saved views, backed by `saved_view`, with a default per track.
- HTMX inline row expansion showing the evaluation blocks without navigating away.
- Bulk actions: shortlist, archive, re-evaluate, escalate to Tier 3, mark reviewed.
- Row badges: ghost score, legitimacy, needs-review divergence, liveness, prompt-version staleness.
- Fit groups in order (Worth a look, Maybe, Being scored, Scoring failed) page together. **Not a
  fit is its own section below them** (D-UI-62): "Not a fit · N" with a note over the whole
  filter set ("49 set aside by your rules before any model ran (location 31, title 18); 0 scored
  low by the model."), a Show N button that loads its rows with their own paging
  (`/opportunities/not-fit?nf_page=`), and its first page open in place when nothing else
  matches. It never takes a page slot from the groups above.

**`/opportunities/{id}` Detail**
Full description in one pane, evaluation in the other. Dimension bars with the weighted-versus-
holistic comparison. Blockers and legitimacy panel. All dedup-group members with source links.
Comp corpus context for this title and geography. Actions: shortlist, tailor document, research
org, find contacts, generate prep pack, escalate, override verdict, archive.

A role a Tier 0 rule set aside says why in one sentence, on the decision panel and the
Evaluation tab: the rule, the concrete detail computed from the role and the current profile, and
a link to where that rule is changed ("Set aside by your level rule: the title reads as Director,
and your lowest level is Vice President. Change it in Profile > Employment."). Destinations:
pay, excluded titles and level go to Profile > Employment; sponsorship to Profile > About you;
location to Profile > Location; the consulting rate, engagement length and sector to Profile >
Consulting; posting and event age to Settings > Core; the built-in clearance rule, and any rule
id Kestrel does not know, to Settings > Filters (D-UI-73). The Score again dialog says the same
for each selected role so set aside, and offers **Score this role anyway**: Tier 0 is skipped for
that role only, Tier 1 then Tier 2 run on the picked model as an operator request (never held by
the track switch), and when Tier 1 runs it the choice is stored (`tier0_override.<id>` setting
row, written in Tier 1's own commit, so a task that dies stores nothing) so a later Tier 0 pass
keeps the role, and the role says "You asked to score this despite your rule on seniority". The
operator's rules never change. Unticked, the submit is refused with the same sentence and link
(D-UI-74).

The **override control is one click and always visible.** Agree, adjust score, or full override
with a reason. This is the primary label-capture surface and its friction determines whether the
learning loop ever gets data.

**`/calibrate` Active learning queue**
Ten highest-information unlabeled items. Description left, system evaluation right, one-click
agree / adjust / override. Keyboard-driven. Progress toward the next calibration and reranker
thresholds shown, so the operator can see the payoff accruing.

**`/profile` Profile** (D-UI-21; `/documents` opens its Documents tab)
Who the operator is, what they want, and the documents that say it, as tabs. **Documents**:
per track and doc kind, section and slot tree, inline variant editing, tag editor, add and
remove variants and slots, add and remove roles and bullets in the operator's own words (slot and
variant keys are tooltips, never visible text; D-UI-29), layout, font and header alignment
pickers (D-DOC-1), live preview, test render, "Assess this resume" (P13, advisory and displayed
only; D-DOC-2), and the import flow with its reviewable proposal and verbatim flagging. **About you**, **Employment**, **Consulting**
and **Location** edit the matching blocks of `profile.yml` (the personal block and the lists,
`employment_terms`, `consulting_terms`, `geo_rules`), which the Tier 0 rules and the Tier 2
context read. The Employment and Consulting tabs, and the track picker on About you and Location, are the
track switch: they write the same remembered tab (localStorage and the `kestrel-track` cookie)
as the nav switch, and the page always names its track, so the nav and the page never disagree
(D-UI-66). Saves are per tab and per track, atomic, with the previous file kept as
`profile.yml.bak` and the result confirmed through the real loader. The operator never edits
`profile.yml` by hand; the file stays the storage format so backup and restore are unchanged.
Under the Employment tab, **Other ways your titles are searched** lists the near-title variants
the generated searches also ask for (D-TTL-2: up to 3 per target title, 9 in all, at most half again
the searches without them and never past the generated-search cap), states that each variant is searched on every board, and lets each one be
removed or searched again (`GET /profile/title-variants`, `POST /title-variants/remove|restore`;
removals are the `sources.title_variants_removed` setting row).

**`/renders/{id}` Render review**
Chosen variant beside alternatives per slot with the model's rationale, one-click swap writing a
`selection_override`, live preview, page-count warning, download, attach to pursuit.

**`/pursuits` Applications (Leads in consulting mode)**
The route keeps its name; the page and the nav label read Applications in employment mode and
Leads in consulting mode (D-UI-46). The lede states that Kestrel never applies or contacts anyone.
Four columns per track over the fine statuses. Employment: To apply (shortlisted, preparing),
Applied (applied, screening), Interviewing (interviewing, final), Offer / Closed (offer, accepted,
rejected, withdrawn, ghosted). Consulting: To contact (shortlisted, researching), In conversation
(outreach_sent, conversation), Proposal (scoping, proposal_sent, negotiating), Won / Closed (won,
lost, no_response). Each card carries a Stage select for the fine status; every change, by select,
button or drag (a drop sets the column's first status), writes a `status_event`.
Buttons record what the operator did and move the card: To apply has Tailor resume (a link) and
I applied (which resume was sent, an applied date defaulting to today behind a disclosure arrow,
a follow-up date defaulting to 7 days out; it sets `applied_at`, the "Follow up" next action and
`document_id`); Applied has Heard back, No reply (follow up again, +7 days) and Interview scheduled
(date and time become the next action); Interviewing has Offer received, Rejected and Withdrew;
Offer / Closed has outcome capture (result, furthest stage, reason, days to first reply) on the
existing outcome fields. Consulting uses the equivalent verbs (Reached out, They replied, Proposal
sent, Won, Lost). A card whose follow-up date has passed shows "N days quiet" and sorts first in
its column. "Add a role I found elsewhere" takes a link (fetched once, politely, robots.txt
respected; a failure is reported and pasted text is kept) and/or pasted text, ingests it through
the manual path so it is scored through the normal queue, and puts the card in the first column at
once. A Table view lists every pursuit. Interview debrief capture stays on the opportunity page.

**`/orgs/{id}` Organization**
Accumulated signals over time, research, contacts with drafted outreach, warm-path finder
results, every opportunity ever seen at this org, comp observations.

**`/stories` Story bank**
Canonical stories with theme coverage and gap analysis. Consolidation proposals awaiting review.
Usage counts. Editor.

**`/prep/{opportunity_id}` Prep pack** and **`/mock/{opportunity_id}` Mock interview**
The prep document, and the conversational practice loop with per-answer critique and history.

**`/offers` Negotiation**
Offer records, benchmark against the comp corpus with sample size, generated scripts, comparison
matrix when more than one is open.

**`/sources`**
CRUD, health, last run, counts, errors, run-now, cadence. JobSpy parameter-conflict validation
before save. Slug validation for ATS sources (a Lever 404 is caught here, not silently later).

**`/settings`**
Models and backends per tier with a test button. Concurrency. Thresholds. The oldest posting
and the oldest consulting event Tier 0 keeps (`max_posting_age_days`,
`consulting.max_trigger_age_days`, on Core, D-UI-73). Scoring weights per
track, with proposed recalibration when available. Profile editors are on `/profile`. Filter-rule editor
**with a "test against the last 200 opportunities" preview** showing how many each rule would
reject. Learning-loop settings and current model status. Notification settings. Confidential
mode, which hides the employer the operator marks as current on Documents ("This is my current
employer" in a role's details, D-PRIV-1); Settings names it ("Hiding X as Y", with Change) or,
when none is marked, links to the first role to choose one. Backup and restore. **Applied**
tab (SPEC.md §5.6): the format instructions with the accepted column names and status words, a paste box or file field for a CSV or Markdown
table, a Review button that renders the parsed rows with what each column was read as, each
row's mapped status (a select where the word was unknown), the "already tracked" and "not an
application" groups unticked, a per-row "allow re-posts" box, and an Import button that
writes only the ticked, valid rows and reports what it wrote. **System** tab (D-UI-31; opens on
`/settings#system`): the machine checks that were the Setup page (Local, Hosted or Both, the
detected hardware and the low-spec warning, Ollama status, the hardware profile with a Change
control, pull, build, verify and the doctor result, the hosted key status) and **Restart
onboarding** with two choices. "Keep my details" reopens step 1 with every form prefilled.
"Clear everything and start over" needs RESET typed and offers "keep my API keys" (on by
default); it writes a backup zip beside the data home first (never deleted), then, with the app
running, pauses the worker, empties every table except `alembic_version` in one transaction,
removes `profile.yml` and the seeded config, clears renders, prep packs, the reranker and the
in-process caches, re-seeds the config, shipped sources and layouts, resumes the worker and
lands on step 1. The machine's model setup (the hardware profile, the Modelfiles, the doctor
report) is kept: the models are still in Ollama.

**`/runs` and `/runs/{id}`**
Batch history, live log tail, per-tier throughput chart, quarantine list with retry. Retry on a
scored tier opens the Score again dialog on the model that failed, to keep it or pick another
(D-UI-71). A quarantine row is resolved once a later evaluation of the same role at the same or
a higher tier exists; it stays as history and no count calls it failed (D-UI-70). A
Schedule panel (SPEC.md §6.5): off, daily at a time, or weekly on a weekday at a time, in local
time, with the next firing shown and a note that it fires only while Kestrel is running.
Latest run is a live panel (D-UI-58): the current step in words ("Searching 3 of 5 searches:
Operations Analyst on Indeed · 42 found so far", "Quick screen 18 of 42", "Rating 3 of 12 · 1
failed", where done is rated plus failed), a bar per stage, the last ten events newest first,
and "Last activity N seconds ago", which becomes "No activity for N minutes" with a Diagnostics
link after 10 minutes while the run is active (never while paused). A partial search (a 429, a
server error) reads amber, "was rate-limited; it is retried next run", never red; a search the
app stopped mid-way reads "cut off". The status rail's worker line prints the same step words
as Today's run panel. The toast at the end of the search step says "Search done: 43 new, 1 source
had a problem. Still scoring 4."; only the run panel says a run finished, when it closes
(D-UI-63).

**`/analytics`**
The funnel, breakdowns, cost and throughput, quality metrics, time metrics, honest summary.

**`/models`**
Which model has done the work at each tier and how it agrees with the operator's labels, with
every rate beside its n. A "Hosted models per tier" panel (D-UI-42) lists each hosted tier
(tier1, tier2, tier3, docsel, trigger, frontier) with its model and a "Default" (Connect's
automatic pick) or "(Override)" label. "Override models" fetches the provider's model list with
the stored key (only models that honor a JSON schema; cached in-process for ten minutes; a
failure is an inline error) and gives each tier a picker showing name, context length and price.
A "Free only" filter (`:free` id, or prompt and completion both priced zero) starts on when the
standing choice is free and can be cleared to mix in paid models. A model whose context is below
the tier's num_ctx budget (§8.2 floor; trigger uses tier1's, frontier tier2's) is shown disabled
with the reason. Picking a model reveals a Save box for that row; "Save all" writes every ticked
row in one request, refusing any id not in the provider's list or below the budget. "Reset to
default" restores Connect's pick. Overrides are stored apart from the defaults, so connecting
again keeps them; a pick changed under Settings > Hosted > Advanced counts as an override too.
Tier 3 hosted escalation stays opt-in per opportunity whatever model it uses.

**`/bench`**
The benchmark harness as a UI: pick models and prompt versions, run against the gold set, see
the comparison table, promote a model to a tier. The page opens with two plain sentences on what
it is for and points to Profile > Documents for the resume assessment, as `/calibrate` does.
Models are chosen from the same picker as `/models` (D-UI-44), never typed: local Ollama models in
one group, the hosted provider's list (loaded on request) in another. Promoting a hosted model
saves it as that tier's hosted override, validated as on `/models`.

**`/about`**
Version, data folder link, log folder link, license, dependency versions, update check.

### 2.2 Responsive and PWA

Every page is responsive. A web manifest and a service worker caching the vendored static assets
make it installable from a browser on the LAN, which gives a usable phone view for reviewing the
shortlist and updating pursuit status. **Read and light-write only on mobile**; document editing
and settings are desktop surfaces.

### 2.3 Access

Binds `127.0.0.1` by default.

A setting allows binding `0.0.0.0`. When it is on, a shared secret is **generated automatically**
and required on every request via an `X-Kestrel-Key` header, checked by FastAPI middleware that
exempts only `/health`. Browser access uses a cookie set by a one-time
`/auth?key=...` link the settings page renders as a QR code, which is how the phone gets in
without typing a secret. The browser extension sends the header.

The settings page states plainly that there is no real authorization model, that the secret is a
single shared credential, and that this should not be exposed beyond a trusted LAN or VPN.

---

## 3. REST API

Everything the UI does is available under `/api`. JSON in, JSON out, FastAPI OpenAPI docs at
`/docs`. The UI is a consumer of this API, not a privileged path, so the CLI, MCP server, and
extension all have full capability.

Two endpoints that are easy to miss because they serve other components rather than the UI:

- **`POST /api/opportunities/lookup`** — body `{"urls": [...]}`, returns for each known URL its
  id, score, verdict, pursuit status, and ghost score. This is what powers the browser
  extension's seen-badge.
- **`POST /api/focus`** — brings the existing window forward. Called by a second launch that
  found the mutex already held.

Resource groups: `/api/opportunities`, `/api/evaluations`, `/api/pursuits`, `/api/orgs`,
`/api/contacts`, `/api/documents`, `/api/renders`, `/api/stories`, `/api/offers`,
`/api/sources`, `/api/runs`, `/api/tasks`, `/api/settings`, `/api/learning`, `/api/analytics`,
`/api/stats`, `/health`.

Conventions: cursor pagination on collections; filters as query parameters mirroring the UI;
`POST /api/opportunities/ingest` accepting either a URL or raw text plus metadata; long
operations return a task id and a poll URL rather than blocking; every mutating endpoint is
idempotent where it reasonably can be.

---

## 4. MCP server

Exposes the pipeline to the operator's existing Claude and Cowork sessions, mirroring the
`copilot-mcp` pattern already running on their Mac Mini.

**Transport:** `fastmcp` over SSE, on a **distinct, configurable port** (default 9110).
The operator has been bitten before by two MCP servers sharing a port and one silently serving
the other's tools; the port must be unique and preflight must verify it is free.

**Construction:** `FastMCP(name, transport_security=TransportSecuritySettings(
enable_dns_rebinding_protection=False))` when exposed beyond loopback. FastMCP fixes its
allowed-hosts list at construction time, so setting `host` afterward does not widen it and a
forwarded Host header returns `421 Invalid host header` from uvicorn, not from any proxy.

**Lifecycle:** starts with the app, stops with it, toggleable in settings. Disabled by default.
The switch is on Settings > LAN & backup (D-MCP-1): on or off, the port, the address to paste
into an MCP client (`http://127.0.0.1:<port>/sse`, plus the LAN address under `bind_all`), and,
with `bind_all` on, the note that another device needs the shared secret. It writes the
`mcp.enabled` setting row and starts or stops the server at once in the running app, with the
same Host/Origin pinning and shared-secret rule; a busy port is refused with a reason. A server
that stopped on its own reads "On, stopped: <reason>" with Start again. A process
that cannot host it says "Restart Kestrel to apply".

**Read tools**

| Tool | Purpose |
|---|---|
| `list_opportunities` | Filter by track, stage, verdict, score, date. Returns summaries. |
| `get_opportunity` | Full record with its latest evaluation. |
| `search_opportunities` | Semantic search over descriptions using the embedding index. |
| `get_shortlist` | Everything at `apply`/`pursue` awaiting action, ranked. |
| `get_pursuits` | Pipeline with statuses and next actions. |
| `get_org` | Organization with accumulated signals, research, and contacts. |
| `get_story_bank` | Canonical stories with themes and coverage gaps. |
| `get_comp_intel` | Comp corpus query by title, geography, size, **with sample size**. |
| `get_analytics` | Funnel and conversion summary. |
| `get_run_status` | Live progress of the active run. |

**Action tools** (deliberately few, and none of them external)

| Tool | Purpose |
|---|---|
| `ingest_opportunity` | Add a URL or pasted text to the pipeline. |
| `trigger_scan` | Run a named source now. |
| `trigger_evaluation` | Force re-evaluation of an opportunity. |
| `set_pursuit_status` | Move a pursuit, writing a `status_event`. |
| `add_note` | Append to a pursuit's notes. |

**No MCP tool sends anything externally, submits anything, or generates a final document
without the operator seeing it.** The boundary in `SPEC.md` §0.6 applies identically here.

Verification, learned from the operator's prior experience: an SSE endpoint returning 200 is
**not** proof it is the right server. Confirm via an MCP `tools/list` that the expected tool
names come back.

---

## 5. Browser extension

Manifest V3, Chrome and Edge. Talks to the local API at the port from `runtime.json`, falling
back to a port configured in the extension options.

**Capture.** A toolbar button and a right-click context item send the current page URL plus its
extracted readable text to `POST /api/opportunities/ingest`. Works on any site, including ones
with no API adapter and ones behind a login the operator is already viewing. **The extension
reads the page the operator is already looking at; it does not crawl, does not navigate, and
does not act.**

**Seen-badge.** On supported job boards, the content script collects the visible posting URLs
and asks `POST /api/opportunities/lookup` which are already known. Each known posting gets a
small inline badge showing its score and status: already evaluated 4.2, already applied, passed
because comp below floor, flagged as a likely ghost job.

This is quietly the most useful feature in the whole application. It puts the system's judgment
directly into the moment of browsing, which is where the operator actually loses time.

**Options page:** API port and shared secret, which boards to badge, badge style.

**Privacy:** the extension talks only to `127.0.0.1` (or the configured LAN host). It sends
nothing anywhere else. It requests the narrowest host permissions that work, and the README says
exactly what it transmits and to where.

---

## 6. Notifications

Multi-hour runs mean the operator is not watching. Notification quality determines whether the
system is useful or ignored.

**Channels:** Windows toast via `win11toast` (default on), optional `ntfy` topic webhook for
phone push, optional generic webhook.

**Events, each individually toggleable:**

| Event | Default |
|---|---|
| Run finished, with a summary count | on |
| A new opportunity scored at or above `settings.notify.high_score` (default 4.5) | on |
| A shortlisted opportunity went `closed` | on |
| A pursuit next-action is due today | on |
| A pursuit has had no response past the ghosting threshold | on |
| A source auto-disabled after three failures | on |
| Quarantine count crossed a threshold | on |
| Reranker met its promotion criteria and is awaiting approval | on |
| Calibration is available with enough new labels | on |
| Drift detected, reranker demoted | on |
| Every individual scored item | off, and it must stay off by default |

**Batching:** never more than one notification per minute. Overflow collapses into a single
summary. A tool that pings forty times during a run gets muted permanently, and then none of the
useful notifications land either.

---

## 7. CLI

`kestrel <command>` via typer. For debugging, automation, and the build agent's verification
gates. The application is the product; this is the service entrance.

| Command | Purpose |
|---|---|
| `doctor` | Full preflight as a text report. Non-zero exit on a blocking failure. |
| `models build` | Create the derived `num_ctx` models from the Modelfiles. |
| `scan [--source NAME] [--track T] [--all]` | Run ingestion. |
| `run [--tier N] [--track T] [--limit N]` | Process the queue. |
| `evaluate <id>` | Force a single evaluation. |
| `render <id>` | Produce a tailored document. |
| `research <org>` | Run company research. |
| `calibrate` | Recompute weight proposals from labels. |
| `train` | Train and evaluate the reranker, reporting promotion criteria. |
| `bench [--models a,b] [--prompt-version v]` | The benchmark harness. |
| `export [--format csv\|json] [--track T]` | Dump the pipeline. |
| `backup [--out PATH]` | Timestamped zip of database, config, and documents. |
| `restore <PATH>` | Restore from a backup. |
| `serve [--headless]` | Start the API and worker without the desktop window. |
| `worker` | Start the worker alone. |
| `mcp` | Start the MCP server alone. |

Every command works against the same `%LOCALAPPDATA%\Kestrel` data directory the application
uses, so CLI and GUI are never looking at different databases.
