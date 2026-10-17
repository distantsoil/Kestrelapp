# Phase -1 — Process Model Spike, FINDINGS

Built and verified on the operator's machine, 2026-07-30. The spike code is disposable; this file
plus the spec corrections are the deliverable. Phase 14 should transcribe this, not rediscover it.

## Environment that worked
- Toolchain: `uv` 0.12.0, CPython **3.12.13** (uv-managed), venv at repo `.venv`.
- Windows 11, Edge present at `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`.
- PyInstaller **6.21.0**, one-folder, `--windowed` (bootloader `runw.exe`, `console=False`).
- Bundle: **287.9 MB, 1003 files**. Matches SPEC-INTERFACES.md §1.4's "sklearn adds 150-250MB".

## Exact working dependency versions
```
fastapi 0.141.1        uvicorn 0.52.0        starlette 1.3.1
pywebview 6.2.1        pythonnet 3.1.0       clr-loader 0.3.1
pystray 0.19.5         pillow 12.3.0         httpx 0.28.1
playwright 1.61.0      scikit-learn 1.9.0    scipy 1.18.0      numpy 2.5.1
sqlalchemy 2.0.51      alembic 1.18.5        tiktoken 0.13.0
pyinstaller 6.21.0     pyinstaller-hooks-contrib 2026.6
```

## The eight gate verifications — all PASS on the BUILT app
1. Launches to a window — `/health` 200, window created, `webview.start()` reached.
2. Close → tray, worker keeps running — the real cancel branch is proven programmatically:
   `POST /api/_test/closing` invokes `on_closing()`, which returns `False` (cancels the OS close)
   and hides; the worker counter keeps incrementing while hidden. (`/api/_test/hide` additionally
   drives the hide helper directly.) A human X-button click is the only path exercised solely by
   eyeball; the return-value decision behind it is covered by the endpoint above.
3. Reopen from tray → window shown.
4. Second launch focuses, no second worker — exit code 0, `runtime.json` pid unchanged, exactly
   one `shell_spike.exe` process.
5. Quit exits cleanly — pid gone, zero remaining processes (see finding #1 below — this failed the
   first time and drove a real spec correction).
6. Playwright `channel="msedge"` PDF from the frozen app — 35,890 bytes, `selftest.json.frozen=true`.
7. tiktoken offline — 15 tokens from the vendored `cl100k_base` cache, no network.
8. Nothing written beside the exe — dist manifest byte-identical before/after a full run; every
   write landed in `%LOCALAPPDATA%\Kestrel\{data,logs}` + `runtime.json`.

## Findings that correct or extend the spec

### F1 (CORRECTION, §1.1 trap 1) — `window.destroy()` re-fires the `closing` event
SPEC-INTERFACES.md §1.1 says "Never call `webview.destroy()` unless quitting" and to implement
close-to-tray by returning `False` from the `closing` handler. What it omits: on quit you DO call
`window.destroy()`, and pywebview 6.2.1 **re-fires the same `closing` event** for that destroy. If
the handler unconditionally returns `False`, it cancels its own quit and the process can never
exit (observed: the app hung alive after a quit request, log showed
`closing event -> hide to tray` immediately after `quit requested`). Fix, now in the spike and
required in Phase 14: a module-level `_quitting` flag; the handler returns `True` (allow close)
when `_quitting`, else hides and returns `False`. `do_quit()` sets `_quitting = True` before
`window.destroy()`. Spec updated.

### F2 (ADDITION, §1.4 tiktoken row) — hidden import AND the cache filename is load-bearing
Two things beyond "vendor the encoding file and set `TIKTOKEN_CACHE_DIR`":
1. **Hidden imports `tiktoken_ext` + `tiktoken_ext.openai_public`.** `get_encoding("cl100k_base")`
   resolves its constructor through the `tiktoken_ext` namespace-package plugin, invisible to
   PyInstaller's static graph. This is a known tiktoken+PyInstaller requirement; the spike included
   it from the start (`shell_spike.spec`), so it was confirmed working rather than negative-tested
   by removal. The frozen app tokenized offline (15 tokens). Pure-Python modules land in the PYZ
   archive, so a disk search for `openai_public.py` finds nothing even when it is present.
2. **The vendored cache file must keep its exact hash name.** The file is named
   `9b5ad71b2ce5302211f9c61530b329a4922fc6a4` — the SHA-1 of the cl100k_base blob URL. tiktoken
   locates the cache by hashing that URL, so renaming it (e.g. to `cl100k_base.tiktoken`) makes
   tiktoken silently fall back to a network fetch. Vendor it by running tiktoken once online with
   `TIKTOKEN_CACHE_DIR` set and copying the resulting cache directory verbatim.

The `blobfile` missing-module warning is benign: it is tiktoken's *remote* loader, unused offline.
Spec updated.

### F3 (CONFIRMATION, §1.1 trap 4) — `--windowed` really does give `sys.stdout/stderr is None`
Proven, not assumed: the frozen app created `logs\stdout.log` and `logs\stderr.log` only because
the launcher replaced the `None` streams before configuring logging. Any `print`/logging before
that replacement would have raised `AttributeError`. Spec is correct; keep the replacement first.

### F4 (CONFIRMATION, §1.4) — the fragile packaging hooks all worked with this set
- pywebview/pythonnet (the "most fragile hook"): `collect_all("webview")` +
  `collect_all("clr_loader")` + `hiddenimports=["clr"]` bundled `Python.Runtime.dll` and the
  webview JS assets; the EdgeChromium backend loaded and rendered. No manual DLL wrangling needed
  with hooks-contrib 2026.6.
- Playwright: `collect_all("playwright")` bundled the driver (`node.exe` + `package/`); the
  `channel="msedge"` PDF proves the driver is present. No Chromium bundled.
- sklearn: `collect_submodules("sklearn")` + explicit `sklearn.utils._typedefs/_heap/_sorting/
  _vector_sentinel` + `scipy.special.cython_special` (exactly §1.4's list) trained an HGBR in the
  frozen app.
- Alembic: `versions/` shipped as `datas` (not analyzed), `script_location` resolved via
  `sys._MEIPASS`, `command.upgrade()` on a **`sqlite://`** (sync) URL created the table. Matches
  §1.4 exactly.
- Included `collect_submodules` for `uvicorn`, `sqlalchemy`, `alembic` as insurance for their
  dynamic autoloaders. The app ran; trimming these is a Phase-14 size optimization, not a
  correctness need.

### F5 (CONFIRMATION, §1.1) — free port, off-main-thread uvicorn, worker-on-loop, mutex
- Free port: bind a socket to `("127.0.0.1", 0)` in-process, read the assigned port, then
  `uvicorn.Server.run(sockets=[sock])`. This yields the port *before* serving, which is cleaner
  than reading it back from `server.servers` after startup. Recorded as the recommended pattern.
- Off-main-thread uvicorn: **the assignment is not why it works.** Verified against the installed
  source: uvicorn 0.52.0 has no `install_signal_handlers` attribute at all;
  `Server.capture_signals()` skips signal installation when
  `threading.current_thread() is not main_thread()`, so off-thread serving works regardless. On
  OLDER uvicorn `install_signal_handlers` is a *method* that raises off-thread, so override it with
  a no-op *callable* (`= lambda: None`, now in the spike); `= False` is wrong there because the bool
  gets called. §1.1 trap 2 corrected.
- Worker started from the FastAPI `lifespan` on uvicorn's loop kept ticking with the window hidden.
- Single instance: ctypes `kernel32.CreateMutexW(None, False, "Local\\KestrelSpike")` +
  `GetLastError()==183`; the second instance read `runtime.json` and `POST /api/focus`ed the first,
  which showed/restored the window. The handle is never closed. No pywin32 needed.

### F6 (DEV-ONLY NOTE, not a spec issue) — uv venv python is a trampoline
Running the spike from `.venv\Scripts\python.exe` spawns two processes (the venv trampoline plus
the base interpreter that does the work; `runtime.json` records the base pid). This is a
development-run artifact of uv's venv launcher and does **not** occur in the frozen one-folder exe,
which is a single process. Verifications 4 and 5 were therefore run against the built exe, where
process accounting is clean (one process; zero after quit).

### F7 (NOTE) — 592 benign missing-module warnings
All are optional/test/alternative-backend imports guarded by try/except in numpy, scipy, sklearn,
pydantic, anyio, requests, uvicorn (`torch`, `jax`, `dask`, `cupy`, `pytest`, `cryptography`,
`yaml`, `psutil`, `Cython`, `blobfile`). None affected the end-to-end frozen run. Do not chase them
in Phase 14 unless a specific runtime `ImportError` appears.

### F8 (GUIDANCE for Phase 14) — the spike hard-kills; the real quit must drain first
`do_quit()` sets `server.should_exit = True` but does not wait for it, then `window.destroy()`
unblocks `webview.start()` and `main()` calls `os._exit(0)`. That guarantees "no orphan" (the
spike's only goal) but **hard-terminates**, truncating uvicorn's lifespan shutdown. SPEC.md §1.3
requires the real Quit to stop the scheduler, signal the worker, wait up to 30s for in-flight
tasks, release leases, *then* exit. Phase 14 must insert that drain/lease-release before
`destroy()` and must not transcribe `os._exit(0)` as the quit path.

### F9 (GUIDANCE for Phase 14) — keep the entry module's top level minimal
`freeze_support()` is correctly the first statement in `__main__`, but this spike resolves paths
and calls `mkdir` at *import* (module top level), which in a `spawn`-based multiprocessing child
would run before `freeze_support()` short-circuits. Harmless here (nothing spawns a Python mp
child), but the real entry module should keep the top level to imports only and do path/dir setup
inside `main()` after `freeze_support()`. The `LOCALAPPDATA` lookup is now `os.environ.get` with a
fallback so it cannot `KeyError` at import.

## Verbatim packaging recipe for Phase 14
See `spike/shell_spike.spec` and `spike/shell_spike.py`, both kept in-tree as the reference. The
non-obvious, must-carry items: the `_quitting` guard on the closing handler (F1); the
`tiktoken_ext.openai_public` hidden import (F2); the stdout/stderr replacement before logging (F3);
`collect_all` for playwright + webview + clr_loader and the explicit sklearn/scipy hidden imports
(F4); the vendored tiktoken cache under its exact SHA-1 filename (F2); the free-port-then-
`run(sockets=[...])` pattern (F5). Do NOT transcribe `install_signal_handlers` as protection: it is
a no-op on modern uvicorn and only needs a no-op *callable* on old uvicorn (F5). The quit path is a
deliberate hard-kill and must be replaced by the graceful §1.3 drain (F8).
