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

**What the operator sees first (D-UI-89).** The diagram shows the order of dependencies, not the
order of what is visible. The window is created first, at its final size (§1.7), and shows a
loading screen while the steps above run behind it: the Windhover mark hovering, one line of
status, a progress bar. The loading page is a local file shipped in the package, so it needs no
server. Heavy imports are deferred so the window appears quickly. When setup finishes the screen
fades into the app. A startup failure appears in the same window (§1.8).

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

The same checks reach the operator in plain words as the Health check on Models, the Health check
article in Help, and step 2 of onboarding (D-UI-105); a failed check offers **Fix it**, never a
command.

### 1.3 Tray behavior

Multi-hour runs are the normal case, so the window is not the process.

- **Closing the window hides to tray. The worker keeps running.** A first-time toast says so.
  This is the default and is optional: Settings > This computer > Everyday options > "Keep running
  when I close the window" turns it off (`ui.close_to_tray`), after which closing the window is a graceful quit, the same as the tray's
  Quit. With no tray available, closing always quits, so the app is never left running unseen.
- Tray tooltip shows live state: `Kestrel — run 7: 412/900, 18/min, ~27m left`.
- Tray menu: Open, Pause worker, Resume worker, Run scan now, Recent notifications, Open data
  folder, Open logs, Quit. The labels read in plain words ("Run a search now", "Pause searching",
  "Resume searching"); the set of items is unchanged.
- Quit stops the scheduler and the worker, stops in-flight tasks and exits within one to two
  seconds (D-UI-90, which revises D-UI-17). It never waits on a running model call. A stopped
  task keeps its row and its lease expires, so it resumes at the next launch (hard rule 5);
  nothing durable is lost.
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
Alpine, and the prebuilt Tailwind stylesheet**, the Manrope font files (SIL OFL, listed in the
third-party notices, D-UI-127), the scene and seasonal-touch art (§2.5) and the notification
sounds (D-UI-95), prompt files, Modelfiles, default config, the
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

### 1.5 Update checker

Kestrel tells the operator when a newer release exists and shows what changed. **It never
downloads or installs anything.** The operator downloads and runs the installer themselves. Two
public promises bind this subsection, and every rule below keeps them:

- "Asking is fine; installing on its own is not." Checking for a newer release is a request out
  to the internet. Kestrel says it does this, lets the operator turn it off, and never downloads or
  installs anything without the operator's click. "It never acts on your behalf" covers this.
- "Send nothing about the user." The check is a plain request for the latest release version. It
  sends no ID, no profile details and no usage data. "You are not the product" covers this.

**When it runs.** About 60 seconds after startup, then every 24 hours while Kestrel runs. On by
default. A switch in Settings turns it off, and while it is off no request is made at all,
including the startup check.

**Where it goes.** `https://kestrelapp.co/latest.json`, not the GitHub API (D-UPD-4). The check
goes to kestrelapp.co rather than GitHub; the rules above apply unchanged.

**What the request carries.** These headers and nothing else:

- `User-Agent: Kestrel-Updater`
- `Accept: application/json`
- `If-None-Match: <cached ETag>`, only when a cached copy exists

No version, no query string, no cookies. The User-Agent is fixed so the running version never
leaves the machine through it (D-UPD-3).

**What the response carries.** The feed, schema 1:

```json
{"schema":1,"version":"0.4.0","published_at":"2026-11-02T15:00:00Z",
 "download_page":"https://kestrelapp.co/download",
 "downloads":{"windows":{"file":"Kestrel-Setup-0.4.0.exe","url":"<asset url>","sha256":"...","signed":false},
              "macos":{...},"linux":{...}},
 "releases":[{"version":"0.4.0","date":"2026-11-02","notes_md":"### New\n- ..."}]}
```

- `releases` holds the last 10 CHANGELOG sections, newest first. Prerelease versions are never
  written to the feed.
- A feed whose `schema` is not 1 is ignored quietly: no banner, no error.
- `downloads.<platform>.sha256` is published so the operator can check the file they downloaded
  against it. Kestrel does not download the file, so it does not check it.

**What the operator sees.** A newer version is one that is higher than `kestrel.__version__` by
semantic version comparison. Then:

- A non-blocking banner on every page that reads "Kestrel 1.5.0 is available. Get it from
  kestrelapp.co." (the new version number), with "What's new" and "Get the update". The banner
  and every other surface never say "restart to update" or "install now": Kestrel does not
  update itself, and wording that implies it does breaks the promise above (D-UI-93).
- "What's new" opens a dialog with the `releases` sections newer than the running version, newest
  first.
- One tray and bell notification per new version. The daily check does not repeat it.
- "Get the update" opens `download_page` in the default browser. Kestrel opens only an https
  address on kestrelapp.co and ignores any other value.
- The first launch after the operator has installed a newer version shows a one-time "Kestrel is
  now on version X" dialog: the version they came from, the `releases` sections since then from
  the cached feed, "See every change", "Got it", and the line "Nothing about you was sent to
  check for updates." With no cached notes the dialog is skipped quietly.
- Settings > This computer > Everyday options has the switch ("Check for updates automatically"),
  the running version with the time of the last check, and a "Check now" button. Behind Show
  details: "Updates download from kestrelapp.co. Nothing about you is sent." The same switch is
  the one described under "When it runs".
- **A self-updater is deferred.** Downloading, verifying a signature and installing without the
  operator running the installer is a future, separately planned feature (the private decision log, Deferred)
  that would also need the public promise wording reviewed. Nothing in this section may be read
  as allowing it.

**When it fails.** A network error, a non-200 response or malformed JSON is logged with context
and tried again at the next interval. A failed check never blocks startup, never shows an error to
the operator, and changes nothing on screen unless a newer version is confirmed. The ETag is kept
on the operator's machine, so an unchanged feed costs a 304.

The release side that publishes the feed is in `docs/RELEASING.md`. The decisions are D-UPD-1 to
D-UPD-5 in the private decision log.

### 1.6 In-app announcements

A dismissible notice on Today can tell the operator about a change. The shipped list is empty;
the mechanism stays for a future notice. **It is local.** The text is fixed in the build
(`kestrel/web/announcements.py`), nothing is fetched, and there is no remote announcement feed:
Kestrel never phones home for this.

- Each announcement has an id. Today carries one slot (D-UI-157) that loads `GET /announcements`
  and shows the first announcement not yet dismissed.
- "Got it" posts `POST /announcements/{id}/dismiss`, which stores the id in the
  `ui.announcements_dismissed` setting row on this install (not the browser), then empties the
  slot. A later announcement has a new id and shows again. An unknown id is ignored with a 200; a
  failed write is logged and answered with a plain line, never a 500.
- UI only: it never appears in a PDF, export or generated document.
- It reuses the update banner's pattern (an htmx slot and the `warnbox` token styling). There is
  no second banner system.

### 1.7 Window, frame and controls

**Size (D-UI-87; revises D-UI-14 and the 90 percent rule recorded under D-UI-21).** The window
opens at one fixed design size of about 1680x1000 logical pixels, never larger than a 15-inch
laptop's default scaled resolution (about 1710x1107). It is not maximized and it is not a
percentage of the screen, because a percentage looks wrong on an ultrawide display. On a smaller
screen it is clamped to the usable area (the screen minus the taskbar or dock and a margin).
The minimum stays 960x640 and the operator can resize freely. The fallback when screen size is
unavailable is 1440x900.

**Reflow, not scroll.** The layout is fluid (grids that drop columns, container queries), so a
smaller window rearranges its content instead of adding scroll bars or a horizontal page scroll.
The design range is 1280 to 1680 wide. Examples: Today shows fewer cards with the next one
peeking from the edge; below 1100 the sidebar collapses to an icon rail rather than hiding;
Settings' section list becomes a top bar at 1300 and below so help cards stay on the right.

**Frameless window with controls in the sidebar (D-UI-88).** The window uses
`webview.create_window(frameless=True)`; the operating system title bar is not shown. The window
controls sit at the top of the left sidebar on every page, above the Kestrel mark:

- **macOS:** the real traffic lights (close, minimize, full screen) kept over a transparent,
  full-size title bar through a small pyobjc setting, which keeps native behavior, including the
  full-screen tile menu. They are not drawn by the app.
- **Windows and Linux:** minimize, maximize or restore, and close, drawn by the app, with a red
  hover on close.
- **Close means close to tray** (§1.3), or a graceful quit when close-to-tray is off or no tray
  exists. The controls call a small JS API on the window object; the `closing` guard of §1.1 is
  unchanged.
- **Drag and resize.** The top of the sidebar and the top row of the page are the drag region;
  a double click on it toggles maximize. Edge and corner resizing must work on every platform. It
  is not optional, and a build that cannot resize from the edges does not ship. Keyboard window
  management (Win+Arrow snap, Alt+Space menu on Windows) must keep working.
- **Known risks, to verify on real hardware:** a frameless Windows window may lose Snap Layouts on
  the maximize hover and may lose edge-resize unless the window procedure is handled; macOS
  full-screen behavior depends on the pyobjc tweak. These are local-required gates
  (logged as blocked, requires local, in a cloud session).
- The operating-system button colors are the one place a non-token color is allowed (they mimic
  the system).

The earlier idea of tinting the native Windows title bar to match the theme is superseded by this
design; frameless is no longer a later trial behind a setting.

### 1.8 Launch, quit and relaunch

- **Launch** (§1.1): window first, loading screen, then the app. The status line names what is
  happening in plain words ("Opening your data"). The first launch after an update shows
  "Updating your data to the new version" while migrations run.
- **Startup failure** (D-UI-89): the same window shows "Kestrel couldn't start" with one plain
  reason sentence (for example, another app is using the connection Kestrel needs), the buttons
  Try again, Open logs folder and Quit, and a Show details block with the startup step and the
  cause (ports tried, log file name). Never a stack trace. The reassurance line says the operator's
  roles and settings are safe on this computer.
- **Relaunch while the old copy is still closing:** the new launch finds the mutex held, reads
  `runtime.json`, and instead of failing or opening a second window shows "Finishing closing the
  last session" and opens by itself when the old process has exited. The operator never has to
  click again.
- **Power button** (D-UI-197): the top row ends with a round power button beside the bell, theme
  toggle and avatar, tooltip "Quit Kestrel", in the window and in the browser. It opens the same
  Quit dialog as the palette's Quit entry.
- **Quit** (D-UI-90): a short closing screen ("Closing Kestrel. Your work is saved and picks up
  where it left off next time."), in-flight tasks are stopped (their rows resume at the next
  launch), and the process exits in one to two seconds. Quit never waits up to 30 seconds.
- **Measure.** Launch and quit times are measured before and after on real Windows and macOS
  machines; this cannot be verified in a cloud session.

---

## 2. Web UI

Server-rendered Jinja, HTMX for partial updates, Alpine for local state, vendored Tailwind.
Paper and Indigo (D-UI-53): indigo `#4F5BD5` on white paper, with a dark variant of the same
palette. Every color comes from the theme tokens in `kestrel.css`, never a hardcoded hex in a
template. Everything works offline.

**Global chrome:** a track switcher that persists and scopes every page: when both tracks are in
use, two tabs (Employment | Consulting, no "Both" view), each with a badge counting what waits
for the operator on that track (Worth a look plus follow-ups due, hidden at zero); with one
track in use, no switch (D-UI-57); the two-line status readout at the foot of the sidebar (below);
a notification bell; a command palette (below). The Pipeline nav badge counts the active
tab's roles not yet rated (its Being scored group), in the same badge style as Calibrate, hidden
at zero, with a small spinning ring beside it, and on the Being scored header, only while a run
is active (the worker not paused and work waiting on a track that is switched on); reduced
motion stops the spin (D-UI-61). Every badge counts exactly the rows of the page or chip it
opens, on the active tab's track. A switched-off track does not scan or score (its tasks wait,
unclaimed), and its open pursuits are never hidden: Today's Applications card and Applications name the
track and link to its board (D-UI-68). Work the operator asked for (Score again, a Retry, an MCP
scan) is never held by the switch.

**Frame and navigation (D-UI-91).** The left sidebar holds, top to bottom: the window controls
(§1.7), the Kestrel mark, the track switcher (when two tracks are in use), the page list in three
groups, and the status readout. Help and About sit at the foot. Where a track changes a label,
the nav follows it (Applications reads Leads in consulting mode).

| Group | Entries |
|---|---|
| Work | Today, Pipeline, Applications (Leads), Profile, Interview prep, Offers |
| Learn | Calibrate, Analytics |
| Setup | Sources, Models, Runs, Settings |
| Foot | Help, About |

There is **no Bench entry**: the model check lives under Models (§2.1 `/models`). Pages with no
nav entry are reached from where they are needed: a role, the company page, the tailor-resume
page, Needs another try and the interview-prep pack for one role. Every nav item has an icon.
Below 1100 px the sidebar collapses to an icon rail; it never disappears.

There is no fixed top band. The search field, the bell, the theme toggle and the avatar sit at
the top of the page and scroll with it, with the page title and today's date under them. The
avatar is a link to Profile; Kestrel has no accounts. Company logos and job-board logos are not
used anywhere (D-UI-126): a company is shown as a lettered monogram badge.

**Status readout (D-UI-91).** Two short lines, never wrapping (an ellipsis if too long):

1. Activity: "Idle · next run 8:00 AM", "Working · search 3 of 5" (live, in the same step words
   as the Runs page), or "Problem · 2 failed scoring". The problem form is a link to Needs
   another try (§2.1 `/quarantine`).
2. The AI in use: "Local AI ready · <model>", "OpenRouter key · $4.82 left", or, in Both mode,
   "Local + OpenRouter · $4.82 left". A provider that does not report a balance shows no amount.
   Low credit turns the line amber (default threshold $1) and raises a "needs attention"
   notification (§6).

The balance comes from the provider's key endpoint (OpenRouter `/key`, `limit_remaining_usd`
through `providers.verify_key`). It is refreshed on a timer and after online work, never on a
page view, and a failed refresh keeps the last value and says nothing.

**Command palette (D-UI-91).** The search field opens a palette (Ctrl+K on Windows and Linux,
Cmd+K on macOS; the label in the field follows the operating system). With an empty query it
shows Recent entries and two suggested actions. With a query it groups matches under Roles,
Companies, Settings and Actions, with the matched text highlighted, keyboard movement (up, down,
Enter, Esc) and a result count. Actions include Run a search now (opens the Run now dialog),
Open Calibrate, Turn on dark mode (it reads Turn off dark mode when dark), and Pause searching.
Settings entries are searchable by plain words and synonyms (for example "ollama", "backup",
"linkedin", "dates"). An empty result says what to try, in one line. The palette only navigates
and runs the actions above; it never sends anything.

**Error and notice text (D-SEC-2).** A response never carries exception text, a file path or a
URL taken from an exception. The route logs the exception (`error_type`, `error`, the ids) and
shows an authored sentence plus `plain_error(exc)` (`kestrel/web/errors.py`), which returns an
exception's own message only when it is a `UserFacingError` (`kestrel/errors.py`), a type whose
text Kestrel wrote. A redirect notice travels as `?msg=<code>`, never as text: the page looks
the code up in `kestrel/web/flash.py` and shows nothing for a code it does not know, so a
crafted link cannot display a message of its own.

### 2.1 Pages

**`/` Today** (D-UI-92; replaces the Dashboard and the run panel of D-UI-59 and D-UI-60 on this
page)
The daily start. It opens with the page's scene (§2.5) behind a time-based greeting by first name
("Good morning, Jordan.", "Good afternoon", "Good evening") and one line of purpose. Below it, top
to bottom:

- **Status strip**, three clickable segments that stack vertically below about 1300 px.
  1. *Last run* (its local time and outcome), which cross-fades to *Next run* every few seconds
     and holds on Last run while a problem exists. It carries the Run now action.
     Its hover background follows the strip's outer radius only (square on the inner edges,
     clipped inside the strip).
  2. *New roles found*, with live counts while a search is running ("12 rated · 2 failed · 2
     pending").
  3. *The model*: on this computer, "Model on this computer · <model> · local and private"; with
     the operator's own key, "Online model, your key · <provider> · <model> · BYOK · $4.82
     credit left"; in Both mode, "<local model> · <provider> · $4.82 left". The run is global,
     one figure for every track, as before.
- **The retry note**, shown only when something needs it: "N roles need another try. The AI could
  not read its own answer. They are kept in Pipeline." with Review, which opens Needs another try
  (§2.1 `/quarantine`). It is not shown on the first-run Today that carries the walkthrough
  (D-UI-116), and it never shows a past run's problems.
- **New signals** (Consulting only, above Top matches): public trigger events with their fit,
  and Research, Add to leads and Not relevant. Trigger events appear only here.
- **Top matches today**: the best of today's newly found roles, ranked by fit, whatever the score
  (a day that only produced 1s and 2s still shows them). Today's roles only, not the backlog.
  Only roles with a full review and a fit score become cards (D-UI-211). Eligible roles still
  waiting for that review count as "N more being reviewed" under the heading, never as cards.
  The count is absent at zero; its line reserves space so completing a review does not shift
  the screen. The existing refresh inserts the finished role with the arrival animation and
  updates the count in place. The first reviewed card replaces the empty state with a smooth
  height transition. The heading's found count still includes all roles found today.
  A horizontal carousel of fixed-width cards (about 270 to 300 px): a wider window shows more
  cards, a narrower one fewer, with the next card peeking from the edge. Arrows, keys, the page
  dots (large hit area), and touch, pen or mouse dragging move it (a drag never presses the button
  under it). A tap or click on the active card's body opens its summary, as Review does, with a
  brief press; a drag never opens it, a tap on the peeking card moves to it, and the card's own
  controls do only their own job (D-UI-223). A card shows the company monogram, the title (two lines reserved so every card has
  one height), company and place, the fit word and score out of 5, pay (an estimate is labeled
  "est." with a hover disclaimer and its sample size, hard rule 6), skill tags (the operator's own skills, from their resume skill groups and profile must-haves, that
  the posting mentions, matched by text with no model call), and a short "why it fits" of two to
  three sentences (clamped to three lines, labeled as model-written): the evaluation's
  `headline_reason` followed by the evidence line of its strongest `b_fit.matches` entry, so
  it adds no model call and no new evaluation field (D-UI-92). Actions: a bookmark
  (Save for later, distinct from Shortlist, retrievable under the Saved chip in Pipeline), Shortlist
  (Add to leads in consulting), and Review. Shortlist or Save for later removes the card
  (it fades, collapses, and the shortlisted
  role appears in Decisions waiting); Review alone does not. When every card is actioned a single
  dashed card says "You're done for now! Run another scan or check out your pipeline." with Run a
  scan and Open Pipeline. With nothing yet, one dashed card says "No matches yet" with Run a scan.
  A new match during a scan opens a gap at its ranked position and the card rises in with a brief
  brand ring and a NEW tag; if the operator has scrolled the carousel the view stays anchored.
  Review opens the role in a slide-over panel on the right (§2.4) with "Open full role".
- **Decisions waiting**: roles the operator has not decided (shortlist, applied or set aside)
  plus shortlisted roles not yet applied to. In Consulting: undecided work plus leads not yet
  contacted (Draft outreach, I reached out). Rows slide out when actioned.
- **Applications needing action** (Leads needing action in Consulting): follow-ups and interviews
  coming up (Consulting: proposal due, follow-up, call), with the same verbs as Applications.

The page no longer carries the Calibrate count (the nav badge does), the errors and quarantine
count (the sidebar status and the retry note do), the source health grid (Sources), or the honest
summary (Analytics, "Summary"). Confidential mode redacts the current employer as on every list.
The launch entrance is a short stagger (sidebar, nav items, header, greeting, scene, strip, cards,
panels, in about 1.5 seconds, 8 to 10 px of movement); it is off under reduced motion.
A fresh install (no completed onboarding, no `profile.yml`, no stored `llm.mode`) is redirected
from `/` to `/onboarding`; an install whose onboarding is not complete gets a banner ("Finish
setting up Kestrel: 2 steps left. Add your resume text and choose how scoring runs.") that links
there (D-UI-30, D-UI-116).

**`/onboarding` Onboarding wizard and walkthrough** (D-UI-30 as revised by D-UI-116; replaces
the Setup page and the earlier ten-step list)
One self-contained flow from model setup to the first search, for an operator who has never seen
Kestrel, followed by a short tour of the real app. Full-page (no sidebar) with the scene band,
"Step N of 6", a progress row whose ticks are read from the real state on every render, Back and
Next on every step, and a help panel on every step (what this is, why it matters, what happens
next). It is meant to take about five minutes. The current step, the steps passed and the steps
skipped persist in the setting table (`onboarding.state`), so a killed app resumes at the same
step ("Picked up where you left off"). Steps:
1. **Welcome**: what Kestrel does (finds roles, scores the fit, tailors the operator's own resume
   lines, tracks applications) and never does (never applies, emails or messages anyone, never
   logs in to a site as you; it drafts and hands over), privacy (local by default; online only if
   chosen, and it asks first and shows what would be sent), free and "you are not the product".
2. **How Kestrel will think**: detected hardware, "Our pick" and the low-spec warning, and On this
   computer, Online model or Both completed inside the step. Online: a plain explanation of
   bring your own key, where each provider's keys page is, provider, key, "I understand what gets
   sent" box, and Connect (the same connect service as Settings > This computer). Local: install
   the local AI helper, models download in the background and the operator may continue while
   they finish; Kestrel keeps trying if a download fails. Next needs a choice (and a connected
   key for Online).
3. **About you**: first name (required, so Kestrel can greet by it), last name, email and city
   (these fill the top of the resume and stay on this computer), and "What are you looking for?":
   Employment, Consulting and contract work, or both (at least one). Headline, phone and
   LinkedIn link sit behind "Add more details". Choosing one track leaves the other's questions
   out of step 5 and out of the count. Saved for every track in use.
4. **Your resume**: the Documents import (read, check what Kestrel read, accept) embedded. Word,
   PDF, plain text or pasted text. A line that does not match the file is flagged; the operator
   uses the original or leaves it out, and Next is blocked until flagged lines are resolved. It
   is skippable ("Skip for now: no tailored resumes until you add one"); Kestrel still finds and
   scores roles without it. The proposal carries the approval and the save (D-UI-65).
5. **What you want**: a short section per track in use, then where. Employment: target titles
   (with Suggest titles), pay minimum, "Keep roles a little under your minimum" with how far
   under (Exact, 2, 5, 10 or 15 percent, default 5; the same control as Profile, §2.1 `/profile`), work mode
   (remote, hybrid, on-site). Consulting: work offered, project length in weeks, day-rate floor.
   Where: places by name plus nearby towns, or remote only (matching is by name, not distance).
   Level, deal-breakers, sectors to skip and similar sit behind "Add more details". Next needs at
   least one title for a track in use and a place or remote on; example values never become
   searches.
6. **Your searches**: a plain list of what Kestrel will search, from the generated-source dry run
   ("Operations jobs in Columbus, posted in the last 45 days"), where it will look ("public
   listings only", about 60 companies to start, LinkedIn is never searched but a posting can be
   pasted), with Edit on each line. "Nothing is searching yet" until **Start searching**, which
   switches the generated searches on, commits, queues the first scan (the tray's Run scan now
   path) and is the only control in the wizard that turns scraping on. It then opens the real Today
   (first-search state) and starts the walkthrough.

Each step has one primary button ("Save and continue", "Save resume and continue", "Start
searching") and, where the step can wait, a quiet "Skip for now"; there is no second Save. A
primary that needs something first is disabled with a hint ("Add your first name first", "Choose
how Kestrel should think first", "Connect your key first", "Add a place or turn on remote
first"). Every save inside the wizard returns the whole step, so no indicator is ever stale; the
local model box polls itself while an action runs and reloads the step when it finishes.
`/setup` redirects to `/onboarding` until onboarding is complete, then to `/settings`.

**Walkthrough.** After Start searching a spotlight tour runs over the real Today, never over a
second copy of it (D-UI-116). Six stops and a closing card, each a short title and two sentences,
with a dot per stop, Skip tour, Back, Next, Esc and the arrow keys: Today is the daily start;
Pipeline holds every role found; Your top matches; See why a role fits (Review opens the reasons,
tailoring uses only the operator's own lines, and the operator applies themselves); Teach Kestrel
your taste in Calibrate ("too few to tell until you rate 5"); Track what you applied to in
Applications; and "That's it". The tour is replayable from Help ("Replay the walkthrough"). The
first-run Today shown under it has no retry note and no problems from earlier runs. The old
Welcome-to-Done ten-step list is retired: the consulting-terms and Location steps are folded into
step 5, and Done is replaced by the tour. Restarting onboarding is Settings > Your data >
Restart setup (§2.6).

**`/opportunities` Pipeline** (D-UI-96)
The main working surface for decisions. Server-side pagination and filtering over several thousand
rows. Under the title sit a search field, a saved-view dropdown and a sort control; below it a row
of chips with counts: All, My shortlist, Your call (roles the operator has not decided), Scored,
New today, Saved (roles bookmarked with Save for later), Failed scoring (which opens Needs another
try), Hidden, and More filters. Sort is a Best fit | Newest toggle plus a menu (Date posted, First
seen, Oldest first, Company A to Z, Quick-screen score, Ghost posting risk); it sits on the chip row,
right-aligned.

- **More filters** slides open: fit score, pay against the floor and target, work type, posted,
  company, source, warnings, trigger type (Consulting), liveness, legitimacy and the rest of the
  earlier filter set (track, stage, verdict, score range, org, remote type, comp range, date
  posted, has-blocker, ghost score, needs-review), and "Save as a view". Saved views are backed by
  `saved_view`, with a default per track.
- **Rows** show the company monogram, title, fit pill, a shortlist tag, org · location · pay, the
  score, and how long ago the role was found ("Found 3h ago", in the brand color under six hours;
  hover shows found and posted age, from `first_seen_at` and `date_posted`). One warning icon
  with a tooltip appears only when something is wrong (ghost score, legitimacy, needs-review
  divergence, liveness, prompt staleness); a healthy row carries no badges.
- **The detail panel slides over the list** from the right (§2.4), top-aligned with the list and
  as tall as its content. It does not push or narrow the list. Switching roles slides the old
  content out and the new in. It holds the score ring, the five dimension bars (§2.1 detail), the
  pay and level boxes, why it fits, gaps with how to handle each, the model-written summary
  (labeled), and a footer of three buttons: Shortlist (Add to leads in Consulting), Not for me,
  Open full role. Each panel has its own URL (a link icon copies it) so back and deep links work;
  Close is always the rightmost control.
- **Not for me** swaps the footer in place to "Not for me because…" with four one-click reasons
  (Not my kind of role, Pay or location, Wrong level, Just not now; Consulting: Not my kind of
  work, Rate too low, Can't deliver it, Just not now). One click hides the role and records the
  reason as a label (`label.dimensions` for role fit, comp and logistics, or career direction;
  none for "just not now"); the hidden role stays recoverable under the Hidden chip. The detailed
  rating lives on the full role page and Calibrate. Pipeline is for decisions, and Kestrel learns
  from them implicitly (shortlisting a low score reads as "too low", hiding a high score for a
  fit reason as "too high").
- **Bulk actions**: a hover checkbox (or Shift or Ctrl click, or the x key) selects; a dark bar
  slides up under the list with Shortlist, Not for me, Score again, Deeper read (which says it
  needs a per-role OK because data leaves the machine) and Mark reviewed. Esc clears. Actions
  slide the rows out and show an Undo toast.
- **Consulting** shows two tabs under the title, Opportunities and Signals (a gliding
  underline; the list swaps with a slide). Opportunities show the type (contract posting, RFP,
  referral), day rate, engagement value, "Seen twice", "In my leads". Signals are trigger events
  grouped This week and Earlier, with type chips (Leadership change, Acquisition, Expansion,
  Funding) and Not relevant. A signal's panel shows what happened (with its source link), why it
  may need outside help, who to approach, the opening move, and the AI-written note ("Kestrel
  never contacts anyone"); its footer is Add to leads, Not relevant, Research company. Add to
  leads on a signal opens an inline form (next step, prefilled "Research the company"; due Today,
  This week or Next week) and creates a pursuit from existing fields only (status, `next_action`,
  `next_action_at`); there are no contacts, stages or amounts, and Kestrel is not a CRM.
- Fit groups in order (D-UI-198): Worth a look, Maybe, Applied, Not a fit, Being scored, Won't be
  scored unless you act, Scoring failed. All but Not a fit page together, 25 rows per group with "Show 25 more" and state kept in the URL. **Applied**
  (D-UI-51 amended, #37) holds a role with an open pursuit at `applied` or
  later (`APPLIED_OR_LATER`): it leaves Worth a look, Today's Decisions waiting and the Pipeline badge,
  and its row shows the pursuit status as a pill ("applied", "interviewing") instead of "on my
  shortlist". A merely shortlisted role stays in Worth a look. An unscored row's chip says why
  (D-UI-81, #34, issue #90, D-UI-236), one chip per reason and none of them says rejected or
  failed: **Waiting** (not queued yet or queued; "N ahead" when known; muted, dashed), **Paused**
  (its track is off, or it waits for a free slot in the background lane; action Turn track on),
  **Scoring** (a model is reading it), **No description**, **Hidden: applied** and **Taken off
  retry list**. The first three sit in **Being scored**. The last three sit in a second group,
  **Won't be scored unless you act** (it was "Not scored", which read as "not scored yet"), because
  nothing scores them until the operator acts: **Paste the description** (stores the text on the
  role, sends it back to Tier 0 and scores it through the normal pipeline) and **Open posting**
  for an empty posting, **Put it back** for a hidden-applied role, **Undo** for a retry removal.
  Hidden-applied roles are listed only in the hidden view. A role that stopped is a **Stopped** chip
  (muted, dashed, in Being scored) with Score again. A pasted description is capped at 50,000
  characters, is refused for a hidden role ("Unhide it first"), and the role page offers the same
  Paste the description for an empty posting. Chips fade when their state changes; reduced motion turns it off. Under the Being scored header a
  note counts legacy background roles and links to Sources to recheck and score current matches
  locally. When either scoring tier uses an online model, they wait for local models instead
  (D-SRC-13). The
  list refreshes every 10 seconds while a run is in flight (not while the search box has focus);
  a new role from a scan drops into its group with the fading ring. **Not a fit is its own
  section between Applied and Being scored** (D-UI-62, D-UI-198): a short line "Not a fit · 49 · Show" with the note over the
  whole filter set ("49 set aside by your rules before any model ran (location 31, title 18); 0
  scored low by the model."), and a Show button that expands its rows inside their own scroll box
  (about 300 px, "Showing 12 of 49 · Load more", paged by `/opportunities/not-fit?nf_page=`),
  each with "Keep it anyway" (moves the role to Being scored). Its panel says the AI never scored
  it, shows the rule that set it aside and the posting as posted, with Keep it anyway, Review your
  rules and Open full role. It never takes a page slot from the groups around it.
- Failed scoring also appears as the chip and the Scoring failed group; a failed role is kept
  (hard rule 4) and the group links to Needs another try.
- Empty list: "Select a role to see why it fits", with the j / k / Enter keyboard hint.

**`/opportunities/{id}` Role page** (D-UI-97)
The full page for one role. Breadcrumb, Previous and Next beside it ("3 of 11", the content
slides), the title with no fit pill beside it, a facts row (company, location, pay, type, posted,
live), and an "Open original posting" primary button that opens the posting in the system browser.
Tabs, each with its own URL and a gliding underline: **Evaluation**, **Posting** (the text as
posted, "Also posted on" the dedup-group members with source links), **Organization** (public
company facts, blank when unknown and never model-guessed, hard rule 7; the company page link),
**Documents** (count), and **History** (found, quick screen, scored with model and prompt version,
shortlisted, the operator's rating).

The Evaluation tab never shows less than the side panel does:

- **Score card**: the ring (colored by fit), the one fit word, the five dimension bars, and a
  model-written, labeled explanation of why it is a strong (or weak) match that names its
  evidence. The dimensions are role fit, domain match, company context, comp and logistics, and
  **career direction** (the label for the `trajectory` dimension, D-UI-98; the key is unchanged);
  Consulting shows engagement fit, buyer access, economics, delivery feasibility and strategic
  value.
- **Pay**: a band showing the floor and target, the similar-roles bracket with its count, and the
  role's marker, with "Above your target" and the similar-roles sentence. Fewer than five similar
  roles shows "Too few (n)" and no bracket (hard rule 6); an estimate is labeled. **Level**: a
  ladder (below, your level, above) with the role and the operator marked, plus reports to and
  team size where known. **Lead with** (`e_angle.lead_with`).
- **Match wheel**: one ring segment per thing the posting asks for, colored Met, Partly met, Not
  met, with "3 of 6" at the center; segments and legend chips are clickable to focus the
  matching rows below and dim the rest, and a click elsewhere clears it. Beside it, **In your
  favor** (green) and **Gaps** (amber), each gap with how to handle it (`b_fit.gaps`) and a plain
  link "I have a story for this". Only a `strong` match is Met. A match the profile neither
  shows nor rules out (`strength` `unverified`, PROMPTS.md P2) is Partly met and listed under
  Gaps with "Your profile does not show this yet, so Kestrel can't tell"; it never counts in
  the role's favor and never reaches Evidence (hard rule 4, O20).
- **Evidence**: a posting quote, the operator's matching proof, and a link to the exact resume
  bullet or story, with "+ I have a story that shows something else they ask for".
- **What the role is**: the model-written summary, labeled as AI-written, with tags and a "Read
  the full posting" link to the Posting tab.
- **Your view** (bottom): "Kestrel says 4.6, strong fit. Agree?" with Yes or Adjust. Adjust opens
  the Calibrate-style 1 to 5 picker ("Good match?"), optional nudges per dimension and an
  optional note, and saves a label (`label.source` `manual`). A compact company card sits
  beside it. This replaces the old always-visible override control; the primary label-capture
  surfaces are now Calibrate and this card.
- **Add a story** sheet (from a gap or from Evidence): it flies up from the bottom without
  leaving the page, with a one-line reason, the "For" chip, and three labeled boxes (What was the
  situation? What did you do? What changed?) because the story model stores situation, action and
  result separately. Copy: "Kestrel only knows what you write down." Buttons: Save, and Save and
  re-score this role, which queues a single-role re-score task (a resumable task row) using the
  new story. Kestrel does not rewrite the operator's words.

**Your decision** (right column): Shortlist (a toggle; a second click removes it, with Undo, and
drafts stay in Documents) and Not for me (the four reasons of Pipeline, inline, then "Next
role"). **Prepare**: Tailor resume, Cover letter (Consulting: Capability statement and
Engagement outline, §1.6 of `SPEC-MODULES.md`, and Discovery call prep), and Prep pack. These are
greyed until the role is shortlisted, with a tooltip and "Why?" that opens an in-app help card:
Kestrel runs the AI for these only on shortlisted roles, to keep AI use low (minutes of computer
time and slowdown on a local model; cost and data sent on an online one). **Research**: Company
and Contacts, always on; each queues a research task and fills the Organization tab. That tab
shows each task's progress and Cancel, keeps earlier results visible, and resumes unfinished
work after a restart (D-UI-162). **More**:
Deeper read (asks first, naming what is sent and the cost), Archive, Copy link. A Documents mini
card sits under the decision.

The **Documents** tab lists the resume and cover letter (Consulting: capability statement,
engagement outline). A row opens a paper-style preview that slides up (the resume is the
operator's own bullets; the cover letter marks each claim and shows its source on hover), with
Edit and Download PDF. Download uses the computer's Save dialog (D-UI-12). The engagement
outline is "an outline for a conversation, not a priced proposal". Nothing here is ever sent.

A role a Tier 0 rule set aside says why in one sentence, on the decision panel and the
Evaluation tab: the rule, the concrete detail computed from the role and the current profile, and
a link to where that rule is changed ("Set aside by your level rule: the title reads as Director,
and your lowest level is Vice President. Change it in Profile > Employment."). Destinations:
pay, excluded titles and level go to Profile > Employment; sponsorship to Profile > About you;
location to Profile > Location; the consulting rate, engagement length and sector to Profile >
Consulting; posting and event age to Settings > Which roles you see; the built-in clearance rule,
and any rule id Kestrel does not know, to Settings > Which roles you see (D-UI-73). A role set
aside only by the near-floor margin reads "Just under your pay floor" (§2.1 `/profile`). The Score
again dialog says the same for each selected role so set aside, and offers **Score this role
anyway**: Tier 0 is skipped for that role only, Tier 1 then Tier 2 run on the picked model as an
operator request (never held by the track switch), and when Tier 1 runs it the choice is stored
(`tier0_override.<id>` setting row, written in Tier 1's own commit, so a task that dies stores
nothing) so a later Tier 0 pass keeps the role, and the role says "You asked to score this despite
your rule on seniority". The operator's rules never change. Unticked, the submit is refused with
the same sentence and link (D-UI-74).

**`/calibrate` Calibrate** (D-UI-99; the active learning queue)
Ten highest-information unlabeled items, one at a time, keyboard-driven. The page asks "Good
match?" and the operator answers first: five text-only choices of equal width, **Poor, Weak,
Maybe, Good, Great** (keys 1 to 5, scores 1 to 5). **Kestrel's score is hidden until the operator
answers** (the picker starts empty, to limit anchoring); then it is revealed as "You said X.
Kestrel said Y." with Close or Different. The posting is on the left (More details shows the full
text as the employer wrote it, scrolling inside, and Open original posting), Kestrel's reading
on the right after the answer. After rating, "Tell Kestrel why" is optional and opens reason
chips and score nudges per dimension; Save reads "Save" (or "Save with reasons"). Skip is
available and has Undo. A rating stores a `label` (`source` `manual` or `active_learning`; `score`
1 to 5, `rationale`, optional `dimensions`); its verdict is derived from the score with the
operator's cutoffs (§2.6). The queue shows why a role was chosen when Kestrel is unsure (near
the Strong match cutoff, model and weighted scores disagree, unlike anything rated, recent).
Three short strips: This session; Scoring tune-up (progress toward 30 ratings, §5.2 of
`SPEC-MODULES.md`); and what Kestrel has learned about the operator's taste (progress toward 200;
after that, "In use (gets 7 of 10 right vs usual 6)" or "Learned, not in use yet; Kestrel retries
every 50 ratings"). At 30 ratings the weight suggestion appears here too, with "Use the new
weights" and "Keep current", never applied silently. Learning history (one line per rated role:
when, role, you said, Kestrel, Close, Different or Changed; 8 per page; header "You and Kestrel
were close on 78 of 212", with the count shown as n only when under 5) has a Change link that
loads the role into the main rating card in place (the page scrolls only if the card is out of
view, and returns afterward only if it moved). The end of the queue shows a summary and "Keep
going" while Kestrel is still unsure about roles, and "Kestrel is not unsure about anything right
now. It finds more after the next run." when it is not. A "How it works" button opens a flyover
with a link to the full Help article. Both tracks (the Consulting dimensions apply).

**`/profile` Profile** (D-UI-21, D-UI-119; `/documents` opens its Documents tab)
Who the operator is, what they want, and the documents that say it, as tabs in this order: About
you, Documents, Employment, Consulting (only while that track is on), Location, Stories. **Stories**
is the same story bank as `/stories` and the Story bank tab of Interview prep, shown in both
places. **Documents**:
per track and doc kind, section and slot tree, inline variant editing, tag editor, add and
remove variants and slots, add and remove roles and bullets in the operator's own words (slot and
variant keys are tooltips, never visible text; D-UI-29), a Look card, the default for every render of the document (layout, font from the fixed
list, header alignment and accent color from presets or a custom color picker, D-DOC-1; Tailor
resume can override layout, font size and margins per application; the accent
is a document color, so it is a literal color value and not a UI token), a live preview that is
one true-size page scaled with a transform and never re-flowed (so it matches the Enlarge view line
for line, D-UI-126), test render, "Assess this resume" (P13, advisory and displayed
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
Above it, **Suggested titles** (P15, D-TTL-3, #31; also under the Employment step of onboarding)
is an expandable list of titles the model proposes from the resume, each with a one-line reason
and the operator's own bullets it rests on. **Accept** adds the title to the target titles through
the same profile edit as the form (the generated searches and the Tier 0 re-check follow);
**Dismiss** keeps it from ever being suggested again; **Suggest again** queues a pass. Nothing is
added without an Accept (`GET /profile/title-suggestions`, `POST /title-suggestions/{id}/accept|
dismiss|refresh`).

**Names, photo and pay margin (D-UI-117, D-RULES-2).** About you has a required first name and a
last name in place of one full-name field; Kestrel greets by the first name and the resume header
uses both (`SPEC.md` §4.1). The photo uploader (Add, Change, Remove) feeds the avatar; the photo
stays on this computer and never reaches a resume.
Confidential mode and its "Shown as" label are set on this tab. The Employment tab has the pay
floor and target drawn as a band, a work-mode choice, a level list that suits any job seeker (not
tied to one field), and **Keep near-floor roles**: a margin chosen from Exact, 2, 5, 10 and 15
percent (default 5; the same stops in onboarding). With a margin set, a role whose posted pay tops out below the floor but within the
margin is kept and labeled "Just under your pay floor" on cards, in the pay box and on the role
page. With it off the floor is exact. A role with no posted pay is always kept. The Consulting tab
has the same control for the day-rate floor. This is the only place the margin lives; Settings
links here. Currency follows Region (§2.6) until more countries exist. Switching tabs with
unsaved changes is blocked with a nudge, not a modal; a save toast offers Undo (it restores the
`.bak`). Documents autosave each edit (each variant is its own row), so that tab has no save bar.

**`/renders/{id}` Tailor resume** (D-UI-120; the render review)
The operator's own lines, picked per role. A header with the fit ring, role, organization and
facts, "From your <master name>", **Use for this application** (attaches the render to the
pursuit; a second click detaches it) and **Download PDF** (Save dialog). A proof strip states:
"Every line is your own text, word for word. Kestrel picked which of your wordings fits this
role. It did not write or change any of them." with the model (behind Show details), the time and
Pick again (which keeps the operator's swaps). Left: one card per slot (summary, each bullet,
skills groups) showing the picked wording with a badge (Picked, Your choice, Left out, Needs
your pick) and the model's reason; "Other wordings" lists the operator's own alternatives as
radio rows, an optional slot has "Leave this line out", and a swap is instant, flashes the line in
the preview, writes a `selection_override` and offers Undo. There is no free editing here (edits
happen in Profile > Documents), no "AI suggestion" summary, no keyword chip that adds text. Center:
the live preview (one true-size page, scaled; zoom and Enlarge), a page-count guard ("Fits on 1
page" or "2 pages, your limit is 1" with every optional shown slot to leave out, in catalog
order), and a "What Kestrel chose" tab listing each choice as AI, You or Out with reasons. A
**Look** control offers per-application overrides of layout, font size and margins, with a note and
"Make this my default"; the document's own Look (layout, font, alignment, accent) is set in Profile >
Documents and is the default for every render of that document. Right: "What they ask for" (covered, partly, not in your documents; hovering
highlights the line in the preview and the slot card), "Words from the posting" (green when the
operator's text already holds them; Kestrel never adds a word), and Privacy (hide the current
employer; no company names in file details). **View posting** opens a slide-over of the posting
with requirement highlights linked to resume lines. A hallucinated pick (hard rule 2) is never
defaulted away: a red strip says one pick failed a check, the slot turns red as "Needs your
pick", the explanation says the model named a wording the operator never wrote and nothing was
invented, and the default shows until the operator chooses. While any slot is "Needs your pick",
**Download PDF** and **Use for this application** open a confirm: "1 line still needs your pick.
Kestrel used your first wording there. Download anyway?" (N lines when more), with **Choose now**
(the default) and **Download anyway**. The choice is recorded on the render, so a document with
an unresolved pick never leaves silently; the wording used is still the operator's own text (hard
rule 2). Consulting renders the capability statement with its own sections.

**`/pursuits` Applications (Leads in consulting mode)** (D-UI-100)
The route keeps its name; the page and the nav label read Applications in employment mode and
Leads in consulting mode (D-UI-46). The page has no lede line: the promise that Kestrel never
applies or contacts anyone lives in Help and About, and in the product's behavior.
Four columns per track over the fine statuses, with a **Closed** group folded inside the last
column. Employment: To apply (shortlisted, preparing), Applied (applied, screening), Interviewing
(interviewing, final), Offer (offer, accepted), and Closed (rejected, withdrawn, ghosted).
Consulting: To contact (shortlisted, researching), In conversation (outreach_sent, conversation),
Proposal (scoping, proposal_sent, negotiating), Won (won), and Closed (lost, no_response). The
four columns are fixed: custom columns are not offered, because they would make analytics
incomparable. Each card shows a lettered monogram badge, the fit, a "High priority" tag
(`pursuit.priority`), an amber "N days quiet" (it sorts first in its column), a Next step line, a
Stage label that opens a menu of the fine statuses (the keyboard alternative to dragging), one
next-step button and a menu. Every change, by menu, button or drag (a drop sets the column's first
status), writes a `status_event` and shows a toast with Undo. A card has no location or pay line
(they are in the panel).
Buttons record what the operator did and move the card: To apply has Tailor resume (a link) and
I applied (which resume was sent, an applied date defaulting to today behind a disclosure arrow,
a follow-up date defaulting to 7 days out; it sets `applied_at`, the "Follow up" next action and
`document_id`); Applied has Heard back, No reply (follow up again, +7 days) and Interview scheduled
(date and time become the next action; a named step such as Portfolio review is deferred, see
the private decision log); Interviewing has Offer received, Rejected and Withdrew ("What happened?": Offer, Not
selected, I withdrew); Offer and Closed have outcome capture (result, furthest stage, reason, days
to first reply) on the existing outcome fields. Consulting uses the equivalent verbs (Reached
out, They replied, Proposal sent, and "What happened?": Won, Lost, No reply). A follow-up is
only ever **drafted** (text the operator copies), never sent: the verb is "Draft follow-up",
never "Send" (hard rule 1). A due follow-up opens a draft dialog using P17 (`follow_up/v1`).
The operator sees the exact pursuit facts, confirms any online model send with the existing
Tier 3 disclosure, then queues a restart-safe `follow_up` task. Progress and the saved draft
appear in place; the operator edits and copies the text. Drafting leaves the reminder and stage
unchanged. "Add a role I found elsewhere" is a sheet that takes a link (fetched
once, politely, robots.txt respected; a failure is reported and pasted text is kept) and/or
pasted text, ingests it through the manual path so it is scored through the normal queue, and
puts the card in the first column at once with the arrival glow and a New tag. A Board and Table
toggle, a search field, column chips and More filters sit above the board; the Table view lists
every pursuit. "Import past applications" links to Settings > Your data (§2.6) and appears in
the empty state. Interview debrief capture stays on the role page.

*The right rail* has two cards. **Upcoming interviews** (calls, in Consulting) with Prepare,
which opens Interview prep for that role. **Follow-ups** is a checklist of what is due now
(overdue, today, tomorrow) with "N more later this week" to expand; each item can be ticked (with
Undo) and "+ Add a reminder" sets the pursuit's `next_action` and `next_action_at`. There is no
separate tasks feature: the per-pursuit next action and its date are the reminders. At narrow
widths the rail moves below the board and the board scrolls sideways inside its own area.

*The panel.* Clicking a card opens the same slide-over panel as Pipeline (§2.4) at every width: it
slides over the board, never pushes it, closes on an outside click, and holds the stage, the next
step, the record buttons with their inline forms, documents, notes, a history timeline, Remove
(with Undo) and Open full role.

*Limits for a long list (#30).* An "Updated" control hides cards whose last activity (falling back
to the applied date) is older than 7, 14, 30 or 90 days; All time is the default and a card with
no date is never hidden. The Closed group is collapsed by default behind "Closed (N)" with its
count (a search or stage chip that is looking for closed cards opens it), and opens and closes
with a fold animation. A
"Per column" control (5, 10, 25, 50 or All) shows the first N cards of each column with a
"Show all N" button. The choices live in the URL (`range`, `cap`, `open=closed`) and the last
range and cap chosen are saved in the `ui.board_range` and `ui.board_cap` setting rows, so the
next visit starts there; the URL wins. A card waiting on a reply that has been quiet for more than
`pursuits.ghosting_days` (21) shows "No reply in N days." with a Mark as ghosted (consulting: no
response) button. It is the ordinary stage move through `move_to_status`, only on the user's click;
there is no new status and no archive flag. Nothing in these attributes carries the employer's name
in confidential mode.

**`/orgs/{id}` Company page** (D-UI-123; the Organization page)
One page per company, reached from a role, the command palette or Pipeline. A header with the
name, industry, size band, headquarters and website (public data only; a blank when unknown,
never model-guessed, hard rule 7), and a **Hide this company** button. Below,
in order:

- **Your open roles from this company**: each with its fit, place and stage, and a liveness
  chip: "Kestrel checked the company's careers page today and this role is still listed" or
  "Possibly stale: posted N days ago, still listed" (listed that long often means filled or
  forgotten; Kestrel cannot tell which). Your applications here show their outcomes, with "Reply
  rate: Too few to tell (n). A rate needs at least 5 applications." under five (hard rule 6).
- **The summary** (P16, §2.1 of `SPEC-MODULES.md`): a short model-written account of what the
  company does, who it serves, where it is heading and how it hires, labeled "Written by the AI
  on this computer" (or the online model's name when that is what ran) and "can be wrong, so
  check the sources". Each sentence ends in small numbered **source markers** that open the fact
  it rests on. A **Refresh** control queues a new pass. With fewer than two public sources it
  shows "Not enough public information to write a summary yet. Kestrel found N sources." and
  makes no model call.
- **What Kestrel knows**: public facts, each with its source and the date Kestrel last saw it
  ("About 2,400 people work here · Annual report, page 12 · Mar 5"), grouped as facts and
  recent signals (a new hub, a careers page that grew, a filing). Facts are rechecked about once
  a week. Sources are the company's own website, careers page and news page, and public filings
  such as SEC EDGAR. Kestrel follows each site's robots.txt, identifies itself politely, waits
  between requests and never signs in anywhere.
- **Pay here**: built only from postings that list pay, "Based on N similar roles that list
  their pay. These are the company's posted ranges, not what people were offered." with the
  middle half and the full spread. Under five similar roles it shows "Kestrel has seen pay on
  only N similar roles here. It needs at least 5 before it shows a range." and no range.
- **People you know here**: only from the operator's own imported connections file (§2.6, Your
  data): people who work here now, and people who used to work here according to an earlier
  import. Each shows when it was imported. The section carries this exact line (hard rule 7
  exception, D-UI-131): "Because you imported these connections from LinkedIn, their details may not be public. Kestrel keeps them on this computer only and never shares them." With no import it says "Import your LinkedIn
  connections to see people you know at this company" and links to the import. Nobody is
  contacted.
- **Publicly listed business contacts**: names, titles and business contact details the company
  made public (for example a team page), each with the page it came from and the date seen. A
  note "Kestrel never sends anything. A draft is text you copy and send yourself" sits beside the
  draft-a-note action. The operator writes and sends it from their own email (hard rule 1).
- A help column with cards for public pages and filings only, how to tell what to trust, the
  summary, why some facts are missing, Hide and contacts.

**Hide this company** keeps its roles out of Pipeline and Today without deleting what Kestrel
found, with Undo and a "Show this company here" control; the company also appears in the Pipeline
Hidden chip and in the **Hidden companies** list under Settings > Which roles you see (§2.6, one
Unhide per company). There is no Follow button: watching a company's pages on a schedule is
deferred (it would need a periodic public-page check under hard rule 7; the private decision log, Deferred).
The earlier research (P5), signals accumulated on the organization, and the
drafted outreach of `SPEC-MODULES.md` §2 remain available from this page; the role page's Company
and Contacts actions queue them.

**`/stories` Story bank**
Canonical stories with theme coverage and gap analysis. Consolidation proposals awaiting review.
Usage counts. Editor. It opens the **Story bank** tab of Interview prep, and the same bank is the
Stories tab of Profile; there is one bank, shown in both places.

**`/prep/{opportunity_id}` Interview prep** (D-UI-121; the prep pack) and
**`/mock/{opportunity_id}` Mock interview**
The nav entry is Interview prep. A header with the breadcrumb (Applications, Interview prep), the
title ("Discovery call prep" in Consulting), a role picker over roles with an interview booked,
the interview date with a countdown and, when set, the step name (from the pursuit's next
action). On the tabs row, right-aligned: How it works, and Download PDF, which first opens a
preview modal of the prep pack (paper colors fixed white and ink by design) and then the Save
dialog. Tabs: **Prep pack**, **Practice**, **Story bank**.

- *Prep pack*: a readiness ring ("4 of 6 questions have a story", counted as questions with a
  mapped story) with tiles (questions needing a story, questions to ask, days left). "What they
  will likely ask": each question shows where it comes from (a requirement or a gap), its mapped
  story chip (opens the story in the operator's own words), Swap story, and Practice; a question
  with no story says "No story yet" and offers "I have a story for this", which opens the same
  story sheet as the role page and updates the ring on save. Gaps and how to handle them (the
  same items as the role page). Right column: the role in one line; Questions to ask them (each
  can be ticked to include in the PDF, and the operator can add their own); Who you may meet (a
  public listing only, unknown panelists shown dashed); Your notes (saved as the operator
  types, with an indicator).
- *Practice* (the mock interview, §3.3 of `SPEC-MODULES.md`): the mode picker (Behavioral,
  Leadership, Executive panel; Consulting: Discovery call), a line saying where it runs (this
  computer by default), and "Use an online model instead", which opens a consent dialog that
  lists exactly what would be sent, the cost and the provider; nothing is sent until the
  operator confirms, and they can switch back. Questions appear one at a time; the answer is typed
  (voice is not part of the spec); feedback shows rubric bars (structure, specific numbers,
  answered the question, length) and flags any claim that is not in the operator's own proof
  points, with "Add it as a story" and "Got it". The practice history chart says "Not enough
  sessions yet to show a trend" under five sessions.
- *Story bank*: eight theme tiles (strong, one story, no story yet), a merge proposal card
  (Review and merge opens the story in the sheet; Keep separate offers Undo for ten minutes,
  restoring the exact proposal and its links while leaving later changes alone), and story cards whose edit
  opens the sheet prefilled with the operator's words (saving keeps them unchanged). A draft the
  model wrote can carry small inline marks in that sheet (issue #93, D-UI-232): under a field,
  a stock phrase the plain-writing check found or a number no sketch or proof point
  states ("Check ...: not in your sketches or proof points"); under the fields, questions for the
  person. The note "Kestrel will not rewrite your story" stays beside them. Marks show on drafts
  only, never on the person's own words, and clear when they save. Marks are on stories only: the
  other generated-prose surfaces record their flags (cover, mock critique, negotiation,
  analytics, questions, company summary, follow-up) and showing them is a follow-up.
  Debrief capture stays on the role page. An empty state offers Build prep pack with a
  local-time estimate.

**`/offers` Offers** (D-UI-122; Negotiation)
A header ("Engagement offers" in Consulting) with "N open", a single one-line lede (Kestrel lays
out the trade-offs, never advises, never sends; stated once), and Record an offer. Tabs:
**Compare**, **Negotiate**, **History**.

- *Compare*: a card per offer with the first-year total (stacked bar of base, bonus, signing and
  equity per year) and a deadline pill (amber at seven days or fewer), the Role page pay band
  (floor, target, similar-roles bracket, marker at base), the posted-range check, a benchmark line
  from the comp corpus ("Not enough data yet" under five observations, with the bracket hidden,
  `SPEC-MODULES.md` §4.2), and a facts grid (level, reports to, where, time off, start, equity).
  The card menu has Edit details, Draft a reply, I accepted, I declined, It expired (each with
  Undo; they close the pursuit with that outcome) and Reopen. With two or more open offers a
  comparison matrix follows the rows of §4.4 (first-year total, four-year value, level and scope,
  reporting line, location and travel, company risk from research, fit), each with a live weight
  (Ignore to Most). Money rows show bars with the best highlighted; judgment rows are the
  operator's own star ratings beside the source fact. The matrix states how each money row is
  computed (first-year total is base, target bonus, signing and one fourth of the equity
  estimate; four-year value assumes no raises). The result reads "Ahead on ..." with "This
  reflects your weights and ratings, not advice." There is no winner badge.
- *Negotiate*: pick an offer, set the **walk-away** first (required; the draft panel stays
  locked until it is set, and the operator, never the model, picks it), enter the ask (refused if
  below the walk-away), pick a situation (§4.3 scripts; Consulting adds rate versus scope, payment
  terms, renewal, exclusivity). The draft is written on the operator's computer, proof-point
  phrases are highlighted with their source, and the operator can Draft again, edit inline, and
  **Copy draft** ("Paste it into your own email. Kestrel never sends it.").
- *Negotiate, before an offer arrives*: a preparation page, not a copy of Compare. It shows the
  floor and target from Profile, what to ask for beyond pay, and a card for each situation
  Kestrel can draft. Only "Deflecting the first-number question" can be drafted now; the rest say
  "Record an offer to draft this". No market chart. Nothing is sent.
- *History*: past offers, with "Too few to show a pattern" under five.
- *Before an offer arrives* (the empty state of Compare only): "What roles like yours pay" (ranges against the
  floor and target, sample sizes in words, Too few under five) and a "Before an offer arrives"
  preparation list. The Record an offer sheet groups fields (offer, pay, dates and terms;
  Consulting: engagement, money, terms) and says "Leave unknown amounts blank. Zero means zero."
  once. Currency follows Region.

**`/sources`** (D-UI-102)
CRUD, health, last run, counts, errors, run-now, cadence. JobSpy parameter-conflict validation
before save. Slug validation for ATS sources (a Lever 404 is caught here, not silently later).
The page is rows, not a table, grouped by what a source holds, in collapsible groups of uniform
rows: Job board searches; Company career pages (the hiring platform is shown per company, so one
"Greenhouse" row never hides which companies it covers); Feeds; and, in Consulting, Public bids
(SAM.gov) and Company signals (SEC filings, company news). Paused sources sit together in one
collapsed Paused group. Each row has an on or off switch, a health pill, the last check, open
and new counts, a ten-check sparkline and, when not healthy, one plain reason line. An **origin
tag** says where the source came from: shipped, built from your profile (rebuilt on every save,
with title variants), found by Kestrel (discovered company boards from search-result links,
daily, at most 40 per track, a turned-off one is never re-added), or added by you; an origin
filter and a "Where sources come from" card explain it. The toolbar row (not the scene) holds
Check all now and Add a source; three health tiles (Healthy, Slowed down, Needs you) count only
sources that are on and filter the list when clicked. A rate limit (HTTP 429) is "Slowed down"
in amber, "Site asked us to slow down. Retrying later.", never red and never a failed run
(D-SRC-5 note); the raw status is behind Show details. Switching a source off slides the switch,
folds the row out and lets it arrive in the Paused group with a glow (and back, with Undo); it
is paused, never deleted, and persists safely to config (no half-written file). A row opens an
overlay drawer (health history, settings, cadence, the robots.txt note, Show details, Check now,
Edit, Remove with Undo). **Add a source** is a sheet with a tile per type and the steps for each:
a job-board search (Indeed's one-filter rule is a radio group with a plain message), a company
career page link (checked before Add is enabled, with hiring-system detection), a company by name,
a feed, a public bid search. LinkedIn is not in the picker; it is explained under "Good to know"
(§5.1 of `SPEC.md`, D-SRC-5). **Suggested companies** (from the operator's shortlist and
applications) offer Add and Dismiss with Undo. The helper column has How sources work, What to
add, What the colors mean and Good to know (why some sites cannot be added or always come back
empty, and how to tell which case applies), each with a "More about this" flyout that adds
detail and links to Help. The sources the page may name as "Limited" (Dice, ZipRecruiter, Google
Jobs) are shown with the honest reason. New source types are listed only once an adapter has been
verified live (`REFERENCES.md`); the roadmap is under Deferred in the private decision log.

**`/settings`** (D-UI-106)
Six sections, listed on the left under two headings (§2.6 has what each holds). Changes save as
the operator makes them, and most show a message with Undo for a few seconds. Commands and
addresses sit behind Show details, so the operator sees them only if they want to. Settings are
searchable from the search field (the command palette, §2); every control has plain synonyms.
Online model keys have their full manager on Models; Settings holds a quick entry that uses the
same store. The earlier ten tabs (Filters, Weights, Core, Models, Hosted (BYOK), Applied, LAN and
backup, Learning, Appearance, System) are retired and folded in as §2.6 lists; no setting was
removed. The **Models** tab of #36 is the Models page (§2.1 `/models`).

**`/runs` and `/runs/{id}`** (D-UI-103)
Batch history, the live run, the schedule and the failed-scoring card. The header follows the
run state: while a run is going it shows a status chip and no Run now; paused shows Resume and
Stop; idle or finished shows Run now and "This run only" (a one-time choice of Local, Online or
Both for this run; online asks first and shows what is sent and the cost). Buttons cross-fade.
Stop confirms inline. When a run finishes the live card turns into Latest run with a glow.

- *Live run* (D-UI-58): the current step in words ("Searching 3 of 5 searches: Operations
  Analyst on Indeed · 42 found so far", "Quick screen 18 of 42", "Rating 3 of 12 · 1 failed",
  where done is rated plus failed), five stage tiles with bars (Searching, Your rules, Quick
  screen, Rating, Deeper read; the last always reads "Only when you ask"), the last ten events
  newest first, and "Last activity N seconds ago", which becomes "No activity for N minutes" with
  a Diagnostics link after 10 minutes while the run is active (never while paused). Live updates
  never re-mount: new events are prepended, the oldest fold out, and counts and bars update in
  place. A partial search (a 429, a server error) reads amber, "was rate-limited; it is retried
  next run", never red; a search the app stopped mid-way reads "cut off". When an online model is
  in use the card shows the provider and model, an "Online" tag on the steps that ran online,
  and the spend so far and the estimate. The status rail's worker line prints the same step
  words. The toast at the end of the search step says "Search done: 43 new, 1 source had a
  problem. Still scoring 4."; only the run panel says a run finished, when it closes (D-UI-63).
- *Latest run* (idle): status, Found, Rated, Failed and Waiting tiles, and small Duration, Model
  and Sources cards (with Online cost and credit left when online ran).
- *History*: a timeline of the last 10 runs with "Show more" (every run is in the Analytics
  export). Colored nodes with text labels: Finished (green), Partial (amber, rate-limited),
  Stopped by you (grey), Did not run (red, rare). Columns include mode, failed, duration and
  cost for online runs. A run opens a drawer: the summary, roles per minute by step, per-source
  results and a Show details log.
- *Schedule* (SPEC.md §6.5): off, daily at a time, or weekly on a weekday at a time, in local
  time, with the next firing shown and a note that it fires only while Kestrel is running; a
  "Runs use" line shows the saved run mode read-only with a link to Models (the mode is changed
  in one place), a note that online runs ask before sending data, and an amber low-credit note.
  "Keep this computer awake during runs" is the switch described under Settings > This computer.
- *Needs another try*: a card with the count and a link to the page (§2.1 `/quarantine`). Failed
  roles also stay in Pipeline under Scoring failed.
- A helper column (What a run does; Pause, Stop and picking up; When something fails), each card
  with a "More about this" flyout that adds detail.

**`/quarantine` Needs another try** (D-UI-104; the quarantine list, with D-UI-70 and D-UI-71
unchanged in behavior)
Roles that could not be scored. The page says "Nothing was dropped; they wait here." Each row shows
the role, the step that failed, the reason in plain words ("The AI's answer couldn't be read",
"No answer after 120 seconds", "Online service refused the key") and, behind Show details, the
raw response and model. Kestrel always tries once more on its own first (hard rule 3). Actions
per row: **Try again** (same model), **Try with a different model...** (a menu: on this computer
and free, labeled quicker or more careful; online, uses your credit, and asks before sending),
**Score it anyway** (skips the rules check, with a sentence that a role normally set aside may
get a score, D-UI-74), and **Remove** (takes it off this list only; the role stays in Pipeline
marked "Taken off retry list" in the group Won't be scored unless you act; Undo brings it back). Rows can be selected for "Try all again". An online
retry shows a dialog first: the provider, the posting text and the short profile summary that
would be sent, what is not sent (name, contact details, the resume file), the approximate cost
and the credit left, and waits for a yes. A refused key points to Models. A failure that did not come
from scoring a role (job title ideas, company research and the like) reads "No role attached"
with its own step name; it has no Try again and no checkbox, says where that step runs again
(job title ideas: Profile, Suggest again), and Remove takes it off this list, with Undo
(D-UI-224). A row leaves the list
when it gets a score, or on Remove; a quarantine row is resolved once a later evaluation of the
same role at the same or a higher tier exists, and stays as history (D-UI-70). Retry on a scored
tier opens the Score again dialog on the model that failed (D-UI-71). Results appear in place:
"Scored: Strong fit at 4.2 of 5" or "Still couldn't score it. Try a different model, or score it
anyway." The empty state says nothing needs another try. A helper column explains why this happens
and what Try again does. The page is reached from the Today retry note, the sidebar problem line,
the Runs card and the Pipeline Failed scoring chip; it has no nav entry.

**`/analytics`** (D-UI-101)
Is the search working, and where is the leak. One date range for the whole page (30 days, 90
days, all time) and a Table view that swaps charts for tables (accessibility). Charts are inline
server-rendered SVG or HTML with theme-token colors, hover tooltips and a table fallback; none
loads from a network. Order: **Summary** (the generated narrative of `SPEC-MODULES.md` §7, P12:
Working, Not working, Worth changing, with the track and range named, every number with its
count, "Updated today, 8:02 AM", a Rewrite summary button, and "It can be wrong, so check the
numbers"; with too little data it says nothing is reliable yet); count tiles (a tile whose base
is under five is dashed and reads "Not enough data yet (n)", hard rule 6); **Where your
applications stand** (one bar that adds up to everything applied: Offer, Interviewing, Waiting
for a reply, Turned down, No reply after 14 days, You withdrew; Consulting: Won, In conversation,
Waiting, Lost, No reply, You stepped back; each segment has a count and a label, and the
palette is validated for color-vision differences); **How Kestrel narrows the field** (the
funnel in two groups with their own scales: found, passed rules, quick check, fully read; and
shortlisted, applied, first reply, screening, interview, final, offer, each with the percent of
the step before; steps under five are hatched and say "too few"); **Replies by fit score** (a
table, not stacked bars, because stages overlap and do not add up; cells under five read "Too
few (n)" with one footnote per card; "Too early to compare" until each band holds five); **Where
roles come from** with tabs for source, work type, level, company size, ownership, remote type
(Consulting adds trigger type); **Reply timing** (median days to first reply with its count,
unanswered excluded and said so; still waiting by age; the oldest with no reply, linking to
Applications); and pace (per-week bars, shortlist to applied). A reply means any answer from a
person, including a no; automatic acknowledgments do not count. KPI tiles compare counts with
the previous period, not percentages. An **Export** menu offers the summary and charts (PDF),
all numbers (CSV), and every application with its history (CSV); files are saved locally
through the Save dialog and nothing is sent. Scoring quality and Speed and cost moved to Models;
a pointer card remains. The summary's claim floor (P12 uses 10 as "not meaningful") and the
UI's floor for showing a rate (5) both stay (hard rule 6).

**`/models`** (D-UI-105)
Which model does each step, where it runs, and whether it is working. Layout:

- A data note with an icon and color that says what leaves the computer (nothing for local; for
  online, the role text and profile lines a step needs, asked first), and a "Kestrel uses" line.
- *This computer*: detected hardware as chips and the recommendation drawn from it (a CPU-only
  machine turns amber and offers to set up an online model). *Health check* (the preflight of
  §1.2 in plain words): the AI engine, the models, room to read (the hidden-word test), thinking
  off, and structured answers, each pass or fail; Run check animates them and a failure offers
  **Fix it** (rebuild with progress, then re-test). Commands sit behind Show details. *Where the
  AI runs*: On this computer, Online model, Both (the saved choice and OpenRouter credit; the
  mode is changed in Settings, one place).
- *Scoring steps*: a color-coded table of Rules (no AI model), Quick screen, Scoring (Rating),
  Resume picking, Deep read, with plain names leading and tier codes as small captions. Each row
  shows what the step does, the model, where it runs (Default or Override), **Can read** in
  plain units (pages, about 500 words a page: "Reads a whole posting", "Reads the start of each
  posting", or in red "Too small: cuts long postings short" with Fix it; the need-versus-capacity
  numbers sit in a tooltip, per tier from SPEC.md §8.2), and how well it matches the operator's
  calls, with its count; under five ratings it reads "Too few ratings yet (n)" (hard rule 6).
  Under Both only Deep read shows online; under Online every AI step does; the embedding model
  always stays local. The hosted per-tier panel of D-UI-42 is the **Change model** drawer: an On
  this computer or Online switch, local models with a fit tag from the hardware, a Free only
  filter, a model whose context is below the tier's budget disabled with the reason, Save, Undo
  and Reset to default; choosing a model that is not downloaded starts the download. Overrides
  per tier follow D-UI-42 (stored apart from defaults). Tier 3 hosted escalation stays opt-in per
  opportunity (D-UI-44).
- *Models on this computer*: per-model status, Download with MB progress, Pause and Resume,
  "resumes if interrupted", then an automatic tuning step that builds the derived `num_ctx`
  model; the `ollama pull` lines are behind Show details. "Speed: roles scored at once" (the
  Tier 1 and Tier 2 concurrency, D-UI-105) lives here behind Show details because it is a
  model and computer performance setting. Each accepts a whole number from 1 to 16; both
  save together or neither changes. Explain the memory tradeoff, the next-pass effective
  time, tier-by-tier draining even with a shared model, and setting `OLLAMA_NUM_PARALLEL`
  to at least the largest configured concurrency (including embedding) in Ollama's own
  environment, then restarting Ollama. Show `OLLAMA_KEEP_ALIVE=2h` guidance here too.
- *Online model*: provider keys (OpenRouter, OpenModel, Anthropic, OpenAI, any compatible
  service), key fields sized so provider names never wrap, Test and Save, Get a key, "Stored only
  on this computer. Never in backups." Brand logos are not used until the brand rules are checked;
  lettered monogram badges stand in.
  Other is configured in Settings > This computer: service address, key with Show, Test
  connection and Save. Test sends only the key to the service's model-list endpoint and saves
  nothing. Both values stay in `.env`; the existing model selection and online consent apply.
- *Scoring quality* and *Speed and cost* (moved from Analytics): readable first time, repaired,
  set aside, you disagree, interview questions predicted; run time, rates, online spend, reranker
  savings ("not in use yet" until promotion).
- A helper column of at most three cards (What each step does; On this computer or online; When
  to check your models / What room to read means), each with an adding flyout.
- A suggestion banner and the **Check my models** button lead to Model check.

**`/bench` Model check** (D-UI-105, D-DRIFT-11; was Bench)
A section of Models, not a nav entry; the route keeps its name. It answers "is a different model
better for me?" against the operator's own ratings (the gold set regenerated from labels,
`SPEC-MODULES.md` §5.5). One button, **Check my models**, tests every step against alternatives
that fit the computer: Quick screen and Rating, and optionally Deeper read ("Deeper read sends
the whole posting, so a paid model uses more credit here than in other steps"). It does not
point to the command line. **The run is a queued background task** (task type `bench`, a row in
`task`, resumable, progress shown in place, "runs in the background; picks up after a restart",
Cancel), so a real comparison never blocks the web and worker loop and never needs `kestrel
bench`; the CLI command stays as a debugging entrance to the same code.

- *Result*: a summary line ("Kestrel recommends one change. Everything else stays as it is." or
  "No changes recommended."), then a card per step that has a change: the current model against
  the best alternative tested, a verdict that leads with the decision and names both models
  ("Switch to gemma3:12b. It matches your calls more often than qwen3:14b, and it is a little
  slower."), trade-off chips, the difference in plain relative points ("matches your calls on 6
  more roles in every 100, from 37 rated roles"; the number of rated roles it rests on always
  appears beside the figure, and under five it says too few), the time difference, where it runs and what it costs or Free. The
  main button is named by its action ("Switch Rating", "Make 2 changes") with per-step buttons
  when there are two or more changes, and **Undo** after. Unchanged steps are one quiet line. The
  best local option is always named so the operator can stay private; Kestrel prefers a local model
  unless an online one is clearly better (5 or more points); an online switch states what is
  sent and what it costs and asks first. Numbers are never shown side by side when they measure
  different things (a Quick screen keep-or-skip agreement and a Rating verdict agreement are not
  compared); raw numbers sit behind **See the numbers**, one table per step with its own caption.
  Under five ratings (or for a track with fewer than the gold set needs) it says there is not
  enough data and disables the check.
- *Advanced* (collapsed): choose the steps and models, and "Kestrel's instructions to the AI":
  Current (came with the latest update) or the version before it, never a bare version number.
  Promoting a hosted model saves it as that tier's hosted override, validated as on Models.
- States: recommends a change, no changes, online only (no local model fits; online candidates
  are compared on cost), and too few ratings. Earlier checks are listed with their date in
  words. A check is suggested (a notification and a line on Today) after a new model is added,
  after every 30 new ratings, and after a model update. Helper cards: Why this exists, What it
  measures, How long it takes.

**`/about`**
Version, data folder and logs folder with Copy and Open, license, an Updates row ("Check for
updates", "You have the latest version", or a link to kestrelapp.co when one exists; Kestrel
never installs on its own), release notes, a source link and a support link (GitHub, Ko-fi,
kestrelapp.co), the five public promises as tiles (Free; You are not the product; Never applies
for you; Asks before going online; Source is public), "Built with" (the AI models listed in
`CLAUDE.md`), Built on JobSpy, ideas from career-ops and parametric-resume-builder (linked to
their repositories, URLs from `REFERENCES.md`), and collapsible component versions with "Copy
versions for a bug report" and the third-party notices. The long product description is
not repeated here, and the version shown comes from the single `__version__` source (§1.4).

**`/help`** (D-UI-124)
In-app help, offline. A search field (`/` focuses it) that filters articles live, topic chips
(Getting started, How scoring works, Your data and privacy, Local and online models,
Troubleshooting), "The four steps" visual (Rules check, Quick screen, Full review, Deeper read,
each with its cost) and 21 short articles in a reader card; the longer ones run 250 to 330 words
and are written in plain language. Every "Read the full article" or "More about this" link opens
the article in the app, never an external site. Includes a Health check article that runs the
same checks as Models and a "Why is this greyed out?" article, plus "Replay the walkthrough"
(§2.1 `/onboarding`). Each page's "How it works" button is that page's help; there are no
per-page guide strips (D-UI-146).

### 2.2 Responsive and PWA

Every page is responsive. A web manifest and a service worker caching the vendored static assets
make it installable from a browser on the LAN, which gives a usable phone view for reviewing the
shortlist and updating pursuit status. **Read and light-write only on mobile**; document editing
and settings are desktop surfaces.

Today, Settings and Help also fit 400 px and 1000 px windows without horizontal page scroll
(D-UI-171). They use the existing bottom navigation at 700 px and under and icon rail at 1100 px
and under. On a phone, page headings and adjacent actions stack, Settings controls stay inside
their cards, and Help's steps stack so their labels remain readable. Wide reference tables scroll
inside their own container; they never widen the page. Desktop layouts retain their existing
1280 px and 1680 px arrangement.

### 2.3 Access

Binds `127.0.0.1` by default.

A setting allows binding `0.0.0.0`. When it is on, a shared secret is **generated automatically**
and required on every request via an `X-Kestrel-Key` header, checked by FastAPI middleware that
exempts only `/health`. Browser access uses a cookie set by a one-time
`/auth?key=...` link the settings page renders as a QR code, which is how the phone gets in
without typing a secret. The browser extension sends the header.

The settings page states plainly that there is no real authorization model, that the secret is a
single shared credential, and that this should not be exposed beyond a trusted LAN or VPN.

**Turning it on from Settings (#27, D-SEC-3, D-UI-113).** Settings > Privacy and access > Access from
other devices has a switch for `server.bind_all`, and shows the QR code of the one-time `/auth?key=`
link (dark on white, "Scan with your phone's camera", "Only works on your own network") with Copy
address and Turn off; the address and "The code includes a private key. Turn access off to make it
stop working." sit behind Show details. The old name for this place was LAN & backup; backup is
now under Your data.
The bind address and both browser guards (the Host allowlist and the Origin check) read `bind_all`
from the settings file plus environment, never from the database, so the switch writes the user's
own `settings.yml` (an atomic temp-file write with fsync that edits only that one key and keeps every
comment) and says **it takes effect after Kestrel restarts**. Rules: it enables only after a shared
secret exists and the user ticks the acknowledgement of the warning; only a request from this machine
(loopback) may change it, even one carrying the shared key; turning it off writes the file too and
needs no tick. The control shows the file's value, what the running process started with, and, when
`KESTREL_SERVER__BIND_ALL` is set, that the environment variable wins. `POST /settings/lan/bind`.

### 2.4 Shared interface rules

These rules apply to every page. They were fixed in the locked mockups (kept privately) and
each has a guard test in the build.

**Fonts, color and surfaces (D-UI-127).** The typeface is Manrope (SIL OFL), vendored as font
files so the app works offline and listed in the third-party notices. The brand is Paper and
Indigo (D-UI-53). Settings > Preferences offers three more schemes (Paper and Teal, Paper and Plum, Paper and Graphite);
each passes the same readability test in light and dark, and all colors are theme tokens. The page
background is a shared sky gradient expressed as a token pair, lighter at the top and fading to
the page background; dark mode is bluer and darker toward the bottom, and the sidebar darkens top
to bottom. Secondary text meets at least 4.5:1 against its background, verified programmatically
on every tinted surface in both themes; status color always has a text label. Cards use small
colored icon badges (tinted by meaning: good, caution, info, role) rather than large tinted
panels. Charts use a validated palette, direct labels and a table fallback.

**Overlays (D-UI-126).** Anything that opens on the right side of the window (slide-over
panels, flyouts, the scene and touch managers) opens vertically centered in the visible area on
the right, never pinned to the top; a flyout may instead align to the card that opened it when
that is lower, and an overlay taller than the view is top-aligned with internal scrolling. A
slide-over slides over the content (it never pushes or narrows it), is top-aligned with the list
only on Pipeline and Applications, is only as tall as its content, closes on Esc, the X or an
outside click, and Close is always the rightmost control. Overlays reach their position before
the first paint; they never move after appearing. Dialogs ask in plain words, list exactly what
would be sent when anything leaves the computer, and name the cost.

**Help cards (D-UI-126).** A complex page has a helper column, level with the first main card and
never inside the scene band: at most three or four cards, each with a token accent and icon and
two or three useful lines, and a "More about this" flyout that adds information (what to do, how
to tell which case applies) rather than repeating the card, with a link to the full Help article.
A "How it works" button beside the title replaces explanatory ledes. A link that opens an
article opens it in the app.

**Motion (D-UI-126).** Transform and opacity only, about 200 ms for panels. Live updates never
re-mount a list: insert, update in place, fold out. Anything that moves between lists animates
out, folds and arrives with a glow, and the destination header pulses; nothing vanishes
instantly. Bars and rings fill from their last value. Reduced motion (the OS setting or
Settings > Preferences) removes the movement and keeps the state changes. The page never
auto-scrolls to something already visible; when it must scroll it moves only as far as needed
and returns afterward only if it moved.

**Page changes (D-UI-210, D-UI-220).** A link to another page in the app changes the page in
place: the window, sidebar, top row and scene art stay, and only the content under them is
swapped, with the same cross-fade a page load has. The old page stays on screen until the new
one and its styles are ready, so the window never shows a blank or grey frame. Back and Forward
move through the pages shown this way; a page with unsaved changes holds a link, the command
palette and Back alike. *Classic page loads* (Settings > Preferences > Appearance,
`ui.classic_nav`, off by default) loads a new document for every page change instead.

**Layout (D-UI-126).** Nothing sits on the scene art; header actions go beside the title. Paired
cards are equal height; titles, buttons, tags and chips never wrap, and tables aim for one line per
cell. `scrollbar-gutter: stable` on scrolling areas so collapsing never resizes the page; scroll
bars are themed with tokens, never the native default; an animated arrow cue shows when more
content exists above or below a scrolling area. There is no dead space. The window is fixed
(§1.7) and the page scrolls inside it.

**Previews are scaled, never re-flowed (D-UI-126).** A document preview (resume, cover letter,
prep-pack PDF) is one true-size page scaled with a transform, so the small preview and the
Enlarge view show the same line breaks.

**Accessibility (D-UI-128).** Settings > Preferences > Appearance has a text size (Small,
Default, Large) that scales the whole interface; cards grow or the carousel shows fewer, taller
cards rather than clipping. The High contrast color scheme in Settings > Preferences uses
full-ink text, stronger borders, plain surfaces and thicker keyboard focus rings. Its text
meets WCAG AAA (7:1) in light and dark, including Match computer. It is stored as
`ui.scheme=contrast`, not a separate flag. Other schemes meet 4.5:1. Reduced motion follows the OS and has its own switch.
Every screen is checked in both text sizes before it ships.

**Copy (D-UI-129).** American English, no em dashes, plain words. A comparative names what it
compares ("matches your calls on 6 more roles in every 100"), a verdict leads with the decision,
and numbers that measure different things are never shown side by side. No jargon or bare
version numbers in the interface (no 16K, tokens or prompt v14; use pages, minutes, dollars).
A figure from fewer than five observations is "Too few (n)" with one footnote per card (hard rule
6). Online use states what is sent and the cost before it happens. **No operator-facing text
requires or shows a command line**: commands, addresses and file names sit behind Show details at
most. A template that names a `kestrel ` command outside a Show details block fails a guard test.
Shipped samples and screenshots use fictional people and companies. Company and job-board logos
are not used; a company is a lettered monogram.

### 2.5 Scenes and seasonal touches

**Scenes (D-UI-110).** A scene is the artwork behind the page title: large on Today, a slim band
on every other page, and the same scene everywhere. When the operator opens another page from
Today the band settles from hero size into the slim band in about half a second while the content
rises in. There are 14 scenes (Mountains, Coast, Desert, Prairie, Forest, City, Lake, Farmland,
Winter, Canyon, Island, Aurora, Rain, Savanna) and **None**, which shows no artwork. The art is
high-detail vector SVG drawn for the app (never the placeholder shapes of the mockup), built only
from scene tokens (the sky, three mountain or skyline depths, snow, tree, sun and moon, glow, mist,
water, sand, leaf and aurora tokens) so it follows light and dark (a sun in light, a moon and
stars or lit windows in dark) and the chosen scheme. Art sits in the right two thirds of a
620 by 118 band; the left third stays empty for the title. Each scene has one or two slow ambient
motions (loops of 4 to 30 seconds, nothing blinking faster than once every two seconds, at most
24 repeating particles) and is complete when still. No text, no flags, no people with faces.
**Animate the scene** is a switch separate from Reduce motion; with either off the art is still.

*Choosing.* Settings > Preferences > Appearance has a Scene row with **Manage scenes**, which opens
a slide-over manager (about 1160 px wide when the window allows, vertically centered): a list with
a checkbox per scene, a large live preview with the scene's name and a line saying what moves,
"Choose one scene" (Change daily off) or "Checkboxes include. Names preview." (on), and the line
"Previewing does not change your choices." **Change daily is on by default**: Kestrel then shows
one of the checked scenes each day, chosen by local date so the choice is stable all day and
changes at local midnight, cycling through the checked set; Today's scene is shown in the manager
and the Scene row reads "Changes daily. Today: Lake." With Change daily off, one scene shows all
the time. At least one scene must stay checked. The choices are stored as settings rows
(`ui.scene`, `ui.scene_daily`, `ui.scene_rotation`, `ui.scene_animate`).

**Seasonal touches (D-UI-111).** A touch is a small animated overlay drawn over the chosen scene
(or over the page top when the scene is None) only during its dates. There are 16, all on by
default, each showing only in its own window:

| Group | Touches (dates) |
|---|---|
| Seasons and nature | Spring (around March 20), Earth Day (around April 22), Longest day (around June 21), Shooting stars (around August 12), Autumn (around September 22), Harvest (Thanksgiving week), Longest night (around December 21) |
| Civic | New Year (December 31 to January 1: fireworks and a glowing ball drop), Fourth of July (around July 4), Halloween (around October 31: bats, a low moon, two pumpkins), Lunar New Year (late January to February, varies), Labor Day (the first Monday of September) |
| Lights and lanterns | Colors of spring (March, dates vary), Crescent evenings (a month, dates vary), Lantern night (autumn, dates vary), Winter lights (December 10 to January 2) |

The neutral rule: **no religious symbols or names, no flags and no text in any touch, and the
same rule holds for every visible string, including date labels** ("March, dates vary", not a
festival's name). A touch never covers the sun or moon area with solid shapes, never hides the
scene, and must look right over all 14 scenes and None in both themes. Dates that move each year
(the lunar and lunisolar ones) come from a table bundled in the app, one entry per year; Labor Day
follows the region (the first Monday of September for the US, the only region for now, §2.6).
Under reduced motion or with Animate the scene off, a touch is drawn still.

*Choosing.* Preferences > Appearance has a summary row, "16 of 16 enabled · Manage touches", and
a master switch. Manage touches is a slide-over like Manage scenes: category tabs with counts,
a checkbox list where the checkbox enables a touch and the name previews it, a large preview with
the name and dates, "Preview only" and Replay. A preview never changes the operator's choices.
Stored as `ui.touches_off` (the ids turned off; empty means all on) and `ui.touches_enabled`
(the master switch).

### 2.6 Settings sections

`/settings` (D-UI-106) opens a left list with two headings: SET UP (This computer, Which roles you
see, Scoring and learning) and YOU (Privacy and access, Your data, Preferences). At 1300 px and
below the list becomes a top bar so the help cards stay on the right. Each section ends with
**Restore defaults** where defaults exist. The section ids are `computer`, `roles`, `scoring`,
`privacy`, `data`, `preferences` (so `/settings#system` from older links opens This computer).

1. **This computer** (D-UI-106). *How should Kestrel run?*: three cards, On this computer (free,
   private, needs a capable computer), Online model (fast, you pay the service per use, asks
   before sending), Both (local for most roles, online for the deep read, asks before going
   online), with one line of detected hardware and "Recommended" moved by it. Choosing Online or
   Both reveals key entry inline: provider (OpenRouter, OpenModel, Anthropic, OpenAI, or any
   compatible service), key with Show, and Test key; it uses the same key store as Models
   ("Keys stay on this computer. Manage more on Models."). The earlier Local, Hosted or Both
   System tab content is here, in plain words. *Setup checklist* (not needed while using only an
   online model): four buttons in order, Install the engine, Download models (resumable, with
   progress and "Pause now and resume on a faster network"), Tune them for Kestrel (builds the
   derived `num_ctx` models, SPEC.md §8.2), Check everything works; a hardware profile choice in
   plain words (never `cpu_8gb` or raw model tags, which appear only behind Show details with the
   commands). *Everyday options*: Keep this computer awake during runs (Windows now; the control
   carries a "Windows only" tag until macOS and Linux have an equivalent), Start Kestrel when my
   computer starts (opens in the tray), Keep running when I close the window (`ui.close_to_tray`;
   on macOS it stays in the menu bar), Online model keys (opens Models), Check for updates
   automatically with Check now (§1.5).
2. **Which roles you see** (track-aware: one set per track in use). *Rules that hide roles*: the
   Tier 0 hard blockers of SPEC.md §6.1 in plain words, each with a switch, a count "would hide N
   of your last 200 roles" (the "test against the last 200" preview, updated after each run), and
   **See them**, which lists those roles with **Show it anyway** (it puts the role into
   Pipeline). Employment: pay below your minimum, needs visa sponsorship, needs a security
   clearance, excluded titles, below your level, outside your locations, and days old before a
   role is hidden (1 to 365). Consulting: rate below your minimum, weeks before a project counts
   as too short (1 to 52, the same value as Profile > Consulting's engagement length), excluded
   sectors, outside your locations, days old before a posting is hidden (1 to 365). The pay,
   titles, level and places themselves are edited in Profile, not here. Switching a rule off
   queues a `t0_recheck` so the roles it had set aside come back to Pipeline (the text says "Those
   roles will show again."). Nothing hidden is deleted, and a role with no posted pay, place or
   clearance statement is kept because the rule has nothing to check (hard rule 4). Pay a little
   under the minimum is not this rule's job: the near-floor margin is in Profile (§2.1). *Hidden companies*: the companies the operator has hidden from a Company page, each
   with Unhide; hiding deletes nothing. *Score
   cutoffs* (D-UI-107): **Strong match** (default 4.0) and **Worth a look** (default 3.0), each a
   number from 0.0 to 5.0 in steps of 0.1, with Worth a look required to be lower than Strong
   match, plain error text, and Restore defaults. They are the `apply` (`pursue`) and `consider`
   thresholds of SPEC.md §7.3 and are now editable (`thresholds.apply`, `thresholds.consider`).
   The "new strong match" notification follows the Strong match cutoff.
3. **Scoring and learning** (D-UI-108). *What matters most*: the five weights per track as bars
   with plain names (role fit, domain match, company context, comp and logistics, career
   direction; Consulting: engagement fit, buyer access, economics, delivery feasibility,
   strategic value). **The bars are read only: there are no sliders.** Kestrel can suggest a
   change (SPEC-MODULES.md §5.2): "Kestrel suggests a change. Based on 42 of your ratings, scores
   would match your calls more closely." with the proposed weights, **Accept** and **Not now**;
   it tests the change first by replaying past ratings with the new weights and suggests only if
   they match the operator's calls better. An accepted change shows Undo, and **Restore original
   weights** puts everything back; an applied change reads "Weights updated. Scores use them from
   your next run." Kestrel also learns from outcomes (replies, interviews) once there are at
   least five (SPEC-MODULES.md §5.6). *What Kestrel has learned*: three switches and a compact
   progress line each: use your ratings as examples (from `settings.learning.min_labels_for_few_shot`, default 15 ratings), tune the weights to your
   taste (from 30), build your own sorting model (from 200; "Once ready, Kestrel only uses it if
   it beats the built-in scoring on roles you've rated, and stops using it if it starts to
   slip", then "In use since <date>" with how often it is checked), and "Agrees with your calls:
   81% of the time (N ratings)" or "Too few to tell (n)" under five. All learning uses only the
   operator's ratings and stays on this computer. *Your ratings*: **Export my ratings** (a JSON
   file through the Save dialog; the employer name is included unless Confidential mode is on,
   so the export is redacted like every other output) and **Forget my ratings**, a confirm that
   lets the operator choose what to forget (their ratings, their own sorting model, suggested
   weight changes), is permanent, needs RESET typed, and never touches roles, scores,
   applications or documents.
4. **Privacy and access**. *Confidential mode*: a switch, "Hide <current employer> as <label>"
   (the employer is the one marked "This is my current employer" on Documents, D-PRIV-1; the
   label is the operator's, used consistently), a preview, and what it applies to (resumes, lists,
   exports). Backups keep the real data so a restore is complete. *Access from other devices*
   (§2.3): a switch; when on, a QR code that carries the one-time key link, "Scan with your
   phone's camera", "Only works on your own network", Copy address and Turn off, with the address
   and "The code includes a private key. Turn access off to make it stop working." behind Show
   details; it states that the change takes effect after Kestrel restarts (D-SEC-3). The QR code is
   drawn dark on white regardless of theme, the one color exception besides the window controls,
   because scanners need it. *Connect an AI assistant (MCP)* (§4): see there.
5. **Your data**. *Backups*: where the data lives (the folder path, its size, Open folder),
   **Download a backup** (a timestamped zip through the Save dialog, with progress; "Last backup:
   ..."), **Restore from a file** (a confirm sheet that says Kestrel first saves a backup of
   what is there and replaces everything now in Kestrel; afterwards "Restored from ...; earlier
   data saved as ..."), and the Included (profile, roles and scores, applications, documents,
   settings) and Never included (online model keys, logs) lists. *Import past applications*
   (SPEC.md §5.6): choose a CSV, spreadsheet or Markdown file; columns named Company, Role, Date
   and Status match by themselves and others can be matched by hand; a review table with
   untickable rows; Add, then Undo removes just those rows; a sample file; nothing is added until
   the operator presses Add. *Your contacts*: the LinkedIn connections import (below).
   *Diagnostics* (D-UI-114): **Download a diagnostics file** (a zip through the Save dialog) made
   only when asked, containing the app version, recent logs, settings (with no keys; keys are
   never in settings) and health check results, and never the profile or resumes, online model
   keys, roles or applications; Kestrel never sends it, and the text says to attach it to a
   message to support if the operator chooses. *Danger zone*: **Restart setup** (walks through
   first-run setup again, keeping the profile, documents and roles: it reopens step 1 of
   onboarding with every form prefilled) and **Clear everything** (needs RESET typed, offers "keep
   my API keys" on by default, writes a backup zip beside the data home first and never deletes
   it, then with the app running pauses the worker, empties every table except
   `alembic_version` in one transaction, removes `profile.yml` and the seeded config, clears
   renders, prep packs, the reranker and the in-process caches, re-seeds the config, shipped
   sources and layouts, resumes the worker and lands on step 1; the model setup is kept because
   the models are still in Ollama).
6. **Preferences**. *What you're looking for* (D-UI-106): switches for Employment and for
   Consulting and contract work, at least one on; turning one off hides it everywhere and deletes
   nothing; with only one on the sidebar track switcher is hidden (D-UI-57). *Appearance*: Theme
   (Light, Dark, Match computer), Color scheme (§2.4), Your picture (D-UI-112), Scene with Manage
   scenes, Animate the scene, Seasonal touches with Manage touches (§2.5), Text size, Reduce
   motion (§2.4), Classic page loads (D-UI-220). *Region* (D-UI-109): country is United States and locked
   ("Kestrel supports the US only for now. More countries are coming."), currency US dollars,
   **Date format** (MM/DD/YYYY, MM/DD/YY, DD/MM/YYYY, DD/MM/YY, YYYY-MM-DD, or "Oct 23, 2026")
   and **Time** (12-hour or 24-hour), each with a live preview (`region.date_format`,
   `region.time_format`); every date and time the interface prints uses the shared display
   helper. Default choices preserve each screen's existing wording; nondefault choices replace
   absolute date and clock labels app-wide, including HTMX fragments. Relative ages remain words.
   Stored timestamps, form values, exports, filenames, and the operator's own resume text keep
   their original formats. *Notifications*
   (§6): "Tell me about" New strong matches, Follow-ups due, Run finished, Something needs
   attention; "Where": the bell (always on), Computer notifications, Play a sound (D-UI-95) with
   the sound choice (Chime, Soft click, Bell), a volume and a Play button, and Quiet hours
   (start and end; no pop-ups and no sounds, the bell still collects them, a night such as 9:00
   PM to 7:00 AM counts as one). The sound plays from the app with each delivered pop-up, never
   from the window, and the pop-ups are silent.

**Your avatar (D-UI-112).** Preferences > Appearance > Your picture offers a lettered initial
(the first name's first letter, the default), six drawn marks (bird, leaf, star, sun, mountain,
compass) and Upload a photo. It shows in the top right of every page and links to Profile. The
photo stays on this computer, is resized when stored, and never reaches a resume or any export.

**Connecting contacts (D-UI-115).** Settings > Your data > Your contacts is where the operator
imports their own LinkedIn connections file. It takes three steps shown as a strip, Choose file,
Review and Done, and works in two phases:

- *Phase 1, import and diff.* "How to get the file" explains the four steps (sign in to LinkedIn
  in a browser, Data privacy, Get a copy of your data, Connections; LinkedIn usually takes about
  10 minutes). Kestrel never signs in there and never scrapes. The operator chooses the file (or
  a sample file). Review shows the counts ("512 people"), the line "Email addresses in the file
  are not kept." (the email column is dropped at import and never stored), and company matches:
  each company name in the file matched to a company in Kestrel with a **Sure** or **Possible
  match** pill; a possible match has a Change select (similar companies, or Not a match). The
  people at companies Kestrel does not track are kept for later, and people with no company are
  counted. Nothing is stored until **Apply changes**. The stored fields are name, company, title
  and connected date. After import the card reads "512 contacts, imported Oct 12 · 61 at
  companies in your Pipeline" with **Import a newer file** and **Remove all contacts** (a confirm
  that deletes only the rows the connections import created, marked `contact.source` =
`linkedin_import`; public research contacts, roles and applications are untouched). Everything stays on this
  computer. After an import the card shows this exact line (D-UI-131): "Because you imported these connections from LinkedIn, their details may not be public. Kestrel keeps them on this computer only and never shares them."
- *Phase 2, re-import diff and history.* Importing a newer file compares it with the last one and
  shows "23 new", "4 changed jobs" and "2 not in this file", a Changed jobs table (name, before,
  now, "Kestrel will remember Sam used to work at Larkspur"), and for the people missing from the
  file a choice, **Keep them** or **Remove them**. Applying records each earlier employer so the
  person can be shown as someone who used to work there. The first import says there is nothing
  to compare yet.
- *Where it appears.* The company page's "People you know here" lists two kinds only, **works
  here now** and **used to work here** (from earlier imports), each with the import date. The
  warm-path finder of `SPEC-MODULES.md` §2.3 is limited to these. Paths through a shared investor
  and through a former employer shared with a decision maker are **deferred** (the private decision log,
  Deferred); the public wording says Kestrel reads only a file the operator downloads.

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
`/api/stats`, `/api/notifications`, `/api/diagnostics`, `/health`. The overhaul adds no operation
that is UI-only: the contacts import and its diff, the model check task, the near-floor margin,
the scene and touch settings, and the diagnostics download are all reachable here as well
(D-UI-129).

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
The switch is on Settings > Privacy and access, in the card titled **Connect an AI assistant (MCP)**
(D-MCP-1, D-UI-113): on or off, the port, the address to paste
into an MCP client (`http://127.0.0.1:<port>/sse`, plus the LAN address under `bind_all`), and,
with `bind_all` on, the note that another device needs the shared secret. It writes the
`mcp.enabled` setting row and starts or stops the server at once in the running app, with the
same Host/Origin pinning and shared-secret rule; a busy port is refused with a reason. A server
that stopped on its own reads "On, stopped: <reason>" with Start again. A process
that cannot host it says "Restart Kestrel to apply".

**The card's wording (D-UI-113).** Title "Connect an AI assistant (MCP)", with an On pill and the
switch, and the line "Lets an AI app on this computer, such as Claude Desktop, read your roles and
applications." When on, it shows "Address to paste into the assistant" with Copy, and two short
lists. "It can": read roles, applications, stories and companies; add a role from a link; run a search
or score a role; move an application to a new stage; add a note.
"Through Kestrel, it can't": apply for you; send email; message anyone. Beneath them, always
visible and with a warning icon, exactly this text: **"The assistant is a separate app with its
own rules. What it reads from Kestrel may go to its online service, and it may be able to send
email or messages on its own. Kestrel can't control that."** Helper lines: "The assistant may use
the cloud." and "Turn it on, copy the address, and paste it into the assistant's MCP settings.
Then restart the assistant." and "It never applies, emails or messages anyone for you. Turn it
off to cut the connection." Behind Show details: Change port (1024 to 65535, "Use a number from
1024 to 65535." otherwise). The "It can" list matches the tool set below and must be kept in step with it.

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
| A new opportunity scored at or above the Strong match cutoff (`thresholds.apply`, default 4.0) | on |
| A shortlisted opportunity went `closed` | on |
| A pursuit next-action is due today | on |
| A pursuit has had no response past the ghosting threshold | on |
| A source auto-disabled after three failures | on |
| Quarantine count crossed a threshold | on |
| Reranker met its promotion criteria and is awaiting approval | on |
| Calibration is available with enough new labels | on |
| Drift detected, reranker demoted | on |
| A newer Kestrel version exists (once per version, §1.5) | on |
| Online model credit is low (the amber status, §2) | on |
| A model check is suggested (after a new model, every 30 ratings, a model update) | on |
| Every individual scored item | off, and it must stay off by default |

**Settings groups (D-UI-94).** Settings > Preferences > Notifications shows four groups, each a
set of the kinds above: *New strong matches* (a role at or above the Strong match cutoff, §2.6),
*Follow-ups due* (next action due today, no response past the ghosting threshold),
*Run finished*, and *Something needs attention* (a source auto-disabled, quarantine count, a
shortlisted role closed, the reranker awaiting approval or demoted, calibration available, low
credit, a newer version, a suggested model check). Individual kinds stay individually
toggleable: the bell's menu has "Turn off this kind" (below).

**The bell and its panel (D-UI-94).** The bell opens a panel that drops and scales in from the
bell (nothing appears without motion). The bell shows the count of **unread** items only and
wiggles when a new one arrives; the new item drops into the list. Tabs: **All**, **Needs
attention** (with a count) and **Dismissed**. Items are grouped Today, Yesterday and Earlier, each
with an unread dot, a track label whenever both tracks are enabled (every kind appears for both
tracks), a plain relative time (not a raw timestamp), a human severity (never a raw severity
word), and one context action chosen by kind (for example Go to results or View run details for a
finished scan, Go to job detail for a recorded application, Draft follow-up for a due follow-up,
opening its draft dialog on Applications).
A problem that is not yet fixed carries an **Unresolved** badge and stays in Needs attention until
it is resolved; resolving is automatic when the underlying condition clears (a source healthy
again, a quarantine row scored, credit restored). The overflow menu is context-aware per kind and
always includes Mark as read or unread, Dismiss (the item slides to Dismissed, and Restore
returns it) and Turn off this kind. **Dismissed items are deleted after 30 days.** The panel's
footer says so. Notification rows gain `dismissed_at`, `resolved_at` and `track` columns for this
(`SPEC.md` §4.6 area; the private decision log D-UI-94); a dismissal never deletes the underlying event.

**Sound (D-UI-95, D-REL-7).** The sound is Kestrel's own and never the operating system's. It
is **on by default with Chime**; the operator can turn it off, pick another sound (Chime, Soft
click, Bell), set a volume and press Play to hear it. Kestrel synthesizes the three sounds itself
(a small WAV rendered once per sound and volume and kept in its cache folder, so they work
offline) and the **app process** plays it, not the window: it plays exactly when the delivery
cycle delivers a notification, once, with its pop-up (the sound does not depend on the Computer
notifications switch), so it is never more than once a minute (the batching rule below), never
for a kind the operator turned off, and it plays just the same with the window closed to the
tray or hidden by a quiet start. Play in Settings goes through the same path, so a preview
and an alert are the same sound; it works only on the computer running Kestrel (a phone using
LAN access is told so), and a press while a sound is still playing is ignored. **The pop-ups themselves are silent** (the Windows toast carries
`audio silent`, the Linux `notify-send` call carries the freedesktop hint
`suppress-sound=true`, and the macOS notification names no sound), so on this computer the
only sound is Kestrel's, and turning Play a sound off silences an alert there. Limits: a phone
push (ntfy) uses the phone's own notification settings, and some Linux notification services
may ignore the request for silence. **Quiet hours** (a start and
an end, for example 9:00 PM to 7:00 AM, treated as one night) hold back pop-ups and sounds
together: the cycle delivers nothing inside the window, the held items stay pending, and the
first cycle after it sends them as the one usual summary with one sound. The bell still collects
every item, and scheduled runs still happen.

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
| `bench --models a,b [--prompt-version v]` | The benchmark harness, the same code the Model check page queues as a background task (§2.1 `/bench`). `--models` is required; `--prompt-version` is optional. |
| `export [--format csv\|json] [--track T]` | Dump the pipeline. |
| `backup [--out PATH]` | Timestamped zip of database, config, and documents. |
| `restore <PATH>` | Restore from a backup. |
| `serve [--headless]` | Start the API and worker without the desktop window. |
| `worker` | Start the worker alone. |
| `mcp` | Start the MCP server alone. |

Every command works against the same `%LOCALAPPDATA%\Kestrel` data directory the application
uses, so CLI and GUI are never looking at different databases.

**Nothing the operator needs may be reachable only from here (D-UI-129).** Every command above has
a button in the app or exists only for debugging and the build gates. A feature that exists only as
a CLI command is a bug in the app, never a reason to rewrite this spec to say the app does not do
it (that is how D-DRIFT-6 let the benchmark Run button stay a no-op; D-DRIFT-11 reverses it). The
interface carries no command text outside Show details, and a guard test enforces it (§2.4).
