# CONTINUE.md — state after the BYOK and review round (2026-09-30)

The remediation described by the previous version of this file is **COMPLETE**. Waves 1A/1B/1C, 2,
3, 4, 5A and 5B are all done, committed, and pushed. This file no longer contains a work plan; it
tells you where things stand and what to do next.

**Read `AFTERACTION.md` first.** It is current as of this commit and is the real handoff: what is
green, what is still open, which tests were passing for the wrong reason, and what needs an operator.

## Verified state at handoff

- Branch `main`, clean tree, pushed to `origin/main`.
- `.venv/Scripts/ruff.exe check .` → All checks passed
- `.venv/Scripts/python.exe -m pytest -q` → **462 passed, 0 failed** (was 428 at Wave 5B)
- `.venv/Scripts/python.exe -m kestrel.cli doctor` → **0 failing**, 3 warning, 21 ok
- Windows, Git Bash (`cd /d/dev/kestrel`). Use `.venv/Scripts/python.exe` and
  `.venv/Scripts/ruff.exe` directly. Line length 100. `from __future__ import annotations` at the
  top of every module.

## State after the follow-on work (2026-08-02)

The three items above are DONE: the embed consumer (ad33aac), confidential mode on the API and MCP
(05cafb3), visual assets and the README (16eb74e), and the frozen build (d4a0171). Suite 462 passing,
ruff clean, doctor 0 failing.

## State after the UI phase (2026-10-12, later, cloud session)

Branch `claude/festive-archimedes-qtsg8d`, pushed. Suite **912 passed, 5 skipped**, ruff clean.
The UI proposal is implemented: sidebar shell with two schemes, Setup, Today, Pipeline triage
with Hide, Opportunity detail with tabs, Settings > Learning (view/export/reset what the
operator's decisions taught Kestrel) and About (version, GitHub, Ko-fi, diagnostics zip). The
The build log entry "UI implementation phase" lists what is CI-only or operator-verified. Next:
walk the UI on Windows from the installer build, then tag v0.2.0.

## State after round two (2026-10-12, cloud session)

Branch `claude/festive-archimedes-qtsg8d`, pushed. Suite **808 passed, 5 skipped**, ruff clean.
Round two added macOS and Linux support with installers for all three platforms (CI-built),
`kestrel setup` with hardware profiles and a hosted-only path, Hosted mode for any tier with an
acknowledged disclosure and a Local/Hosted switch plus per-run mode, openmodel.ai Messages and
Responses, PDF/DOCX/DOC import, the Models, Help and Backup pages, a prompt token audit, and the
UI proposal (docs/UI_PROPOSAL.html; Paper and Indigo default, Graphite and Teal alternate). The
The build log entry for 2026-10-12 lists what is CI-only or operator-verified. Next phase: implement
the UI proposal, setup page first.

## State after the BYOK and review round (2026-09-30, cloud session)

Branch `claude/festive-archimedes-qtsg8d`, pushed. Suite **567 passed, 4 skipped**, ruff clean.
The round added bring-your-own-key hosted backends for Tier 3 (OpenRouter and openmodel.ai; SPEC
§6.4, README "Bringing your own hosted model"), fixed 41 fresh-context reviewer findings across
every area (the private build log entry for the list), added `server.public_hostnames` for the Cloudflare
Tunnel path (`docs/WEBAPP.md`). AFTERACTION
§1.4 (MCP unguarded) is closed and was worse than recorded: see decision log D-REV-3.

Operator-required, in order: (1) `kestrel provider test` against a real OpenRouter key, then one
real Tier 3 send and a look at its `frontier_transmission` row for cost and token counts; (2) the
same for openmodel.ai if wanted (Responses protocol is documented, not verified live); (3) the installer build and
GUI checks below, unchanged; (4) if you publish through a tunnel, set `server.public_hostnames` and
test a POST from the phone before trusting it.

## The next things, in order (pre-2026-09-30 list; item 2 is now done)

1. **Run the installer build and the GUI checks.** Only an operator can do these; the exact commands
   are at the bottom of this file. The frozen app is verified to launch and serve every route, but
   nobody has seen the window, the tray menu, or an install on a clean machine.
2. **`AFTERACTION.md` §1.4**: the MCP port carries none of the three request-path controls when
   `bind_all` and `mcp.enabled` are both on. Off by default and disclosed, but it is the last
   unguarded surface.
3. **`AFTERACTION.md` §1.5**: spec drift. `SPEC-INTERFACES.md` still describes saved views, bulk
   actions and a `/runs/{id}` log tail as shipped. CLAUDE.md says fix the spec first.

## Things you must not undo

- **Three request-path controls** live in `kestrel/web/app.py` and `kestrel/web/auth.py`: a
  cross-origin guard, `TrustedHostMiddleware`, and the LAN shared key. They are not redundant, and
  none subsumes another. Under `bind_all` the first two narrow to `local_addresses()`. Widening
  either back to `["*"]`/any-origin turns all three off at once, because a DNS-rebound request also
  arrives on loopback where the key is exempt. There are tests pinning this.
- **`serve.py` sets `proxy_headers=False` and `access_log=False`.** Both are security settings with
  the reasoning inline: the first stops an ambient env var letting `X-Forwarded-For` forge loopback,
  the second keeps the shared secret out of `logs/stderr.log`.
- **`tests/conftest.py` makes every TestClient present a loopback base_url and peer.** Without it
  the Host guard 400s and the key middleware 401s the entire suite.
- **Tier 3 is the only path that transmits operator data**, and only on explicit per-item
  confirmation against a server-side hash. Do not add a second one, and do not give it an automatic
  invocation path.
- **Quarantine retry never moves an item backward.** A tier-1 quarantine is already promoted by hard
  rule 4; rolling it back would let Tier 1 re-run and reject the item the rule rescued.

## Standing rules (unchanged, restated because compaction drops them)

1. Never act externally on the operator's behalf. 2. The model selects, never invents; every PDF
bullet is `doc_variant.text` byte-for-byte. 3. Structured output or failure: schema-validate, one
repair, then quarantine with the raw response. 4. Uncertainty escalates, never rejects.
5. Resumability: every unit of work is a task row. 6. Sample sizes always stated; under five is
labeled insufficient. 7. Only public data, politely: robots.txt, descriptive User-Agent, rate limits.

Platform facts (do not rediscover): simpleeval cannot evaluate generator expressions and the AST ban
in `rules/helpers.py` stays; Ollama `/v1` cannot set `num_ctx`, so per-tier Modelfiles are required;
`reasoning_effort:"none"` for Ollama and `chat_template_kwargs` for oMLX/vLLM; `response_format`, not
the native `format`; JSON Schema subset only; never interleave tier models; all paths via `paths.py`;
all package data via `importlib.resources`; PyInstaller cannot see `importlib.import_module` on a
string, which is why the spec collects `kestrel.web.pages` and
`kestrel.ingest.signal_sources` (and NOT all of `kestrel`, which crashes the build).

## Working practice that earned its keep

Every wave ran a fresh-context reviewer at the parent's model class, asked only "what is wrong with
this", and the reviewer was never asked to fix anything. It found a blocking defect in **every**
wave, including three that the author had already declared verified. The final audit fanned out four
reviewers and found two more blocking defects plus five tests that passed while their feature was
broken. Keep this. A wave is not done because its tests pass; it is done after something with fresh
eyes has tried to break it.

If you run ad-hoc verification scripts, **set `KESTREL_DATA_HOME` first.** Only the pytest fixture
isolates the data directory; a bare `python -c` writes to the operator's real database.


## Operator commands for the remaining unverified work

```powershell
# 1. Build the installer (Inno Setup 6 must be on PATH)
.venv\Scripts\python.exe packaging\gen_version_iss.py
iscc packaging\kestrel.iss          # writes dist\Kestrel-Setup-<version>.exe

# 2. Rebuild the app itself if needed (about 70 seconds)
.venv\Scripts\python.exe -m PyInstaller packaging\kestrel.spec --noconfirm

# 3. Launch and check by hand: the window opens on the dashboard, close-to-tray
#    keeps it running, reopening from the tray works, and Quit releases the task
#    leases (check logs\kestrel.log for "released_leases_on_shutdown").
dist\Kestrel\Kestrel.exe

# 4. Install on a clean machine AND on a path containing a space, then confirm
#    AppMutex blocks an install while Kestrel is running.
```
