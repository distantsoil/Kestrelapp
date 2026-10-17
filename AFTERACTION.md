# AFTERACTION.md — post-remediation state of Kestrel

**Rewritten 2026-08-01 at commit `25df452`+ (Wave 5B).** This replaces the pre-remediation audit.
The original findings are preserved in §4 as resolved history, because a list that only shows what
is left is a list you cannot check.

Written for an agent picking this repo up cold. the private build log and the private decision log are treated as
**claims**, and the claims in them were re-checked rather than believed.

---

## 0. What is green, and what that is worth

Verified by running, not by reading:

| Gate | Result |
|---|---|
| `.venv/Scripts/ruff.exe check .` | All checks passed |
| `.venv/Scripts/python.exe -m pytest -q` | **462 passed, 0 failed** (follow-on work; was 428 at Wave 5B) |
| `.venv/Scripts/python.exe -m kestrel.cli doctor` | **0 failing**, 3 warning, 21 ok |
| `git status` | clean, all work committed and pushed |

The suite is now genuinely deterministic. It previously varied between 416 and 417 depending on
whether an Ollama model happened to be warm, because one Playwright test rendered `/analytics`,
which makes a real generation. That call is stubbed; the page render is what that test is for.

**What "428 passing" is worth.** A four-way audit deliberately tried to make these tests pass while
the features were broken. Several succeeded, and those tests were rewritten (§2). The ones that
survived mutation are noted per hard rule in §3.

---

## 1. Formerly open, now resolved (follow-on work, 2026-08-02)

1.1, 1.2 and 1.3 below are FIXED and verified; each carries its commit. 1.4 and 1.5 remain open.
Full detail is in the private build log and the private decision log under "Follow-on 1" through "Follow-on 4".

- **1.1 the embed consumer** now drains in all three worker paths, so the reranker, the Tier 1
  gate, few-shot injection and active-learning novelty are live. Review caught that a changed
  description kept a stale vector; migration 0007 keys the guard on a hash of the embedded text.
- **1.2 the frozen build** was run for the first time. The Wave 5B packaging fix turned out to
  break the build outright, and is corrected. The exe now launches, serves every page route, and
  runs preflight and the tray.
- **1.3 confidential mode** now redacts in the JSON API, all MCP read tools, `kestrel render` and
  `kestrel export`, and Settings warns when the mode is on but the redaction map is empty.

### 1.1 RESOLVED (ad33aac) — the embed consumer exists
`embed` tasks were enqueued from Phase 3 and never leased, so `opportunity_embedding` had no
production writer. `kestrel/pipeline/embed.py::handle_embed` now drains in all three worker paths,
before t1, because the Tier 1 reranker gate and Tier 2 few-shot both consult the vector. Migration
0007 adds `text_hash` so a re-seen posting whose description changed re-embeds instead of keeping a
stale vector while its task completes `done`. Semantic search now has a corpus.
STILL DEFERRED: **dedup pass 3 does not exist** and this did not deliver it. Passes 1 and 2 are all
that `normalize.py::_assign_dedup_group` implements, and dedup runs inside `normalize_and_store`
while the embed task is enqueued by its caller afterward, so the vector is not there yet. It needs
its own post-embed hook.

### 1.2 RESOLVED (d4a0171) — the frozen app builds and runs
PyInstaller was run for the first time. The Wave 5B fix (`collect_submodules("kestrel")`) turned out
to BREAK the build: PyInstaller imports every collected package in one isolated subprocess during
binary-dependency analysis, and collecting all of kestrel dragged jobspy, pandas, the sklearn test
tree, scipy, fastmcp and beartype in together and crashed it. Narrowed to the two subpackages
Kestrel actually string-imports (`kestrel.web.pages`, `kestrel.ingest.signal_sources`).
The bundle now carries 137 kestrel modules (was 86) including all 18 page modules (was 0), counted
in `build/kestrel/PYZ-00.toc`. Note for the next reader: modules live in the PYZ, not in
`dist/Kestrel/_internal/kestrel/`, which holds only data files. The earlier audit's conclusion was
right but it read the wrong location.
VERIFIED BY RUNNING THE EXE with an isolated data home: it launches, writes runtime.json on port
8765, runs preflight against Ollama, starts the tray, and serves /health, /opportunities, /pursuits,
/documents, /analytics, /calibrate, /quarantine and /settings, all 200 with real content. stderr
shows a clean uvicorn startup and no traceback. The exe carries the Kestrel icon.

### 1.3 RESOLVED (05cafb3) — confidential mode reaches the API and MCP
`redact_name`/`redact_text` are threaded through `routes_api.py` and every MCP read tool, including
`search_opportunities`, which returned `SearchHit.__dict__` raw and was the one tool that leaked
outright. Matching now also uses `ingest/orgs.py::normalized_name`, so "Northwind Freight Inc." and
", LLC" no longer slip through an exact-match lookup. Free text is redacted too: the org field was
being blanked while the title and description in the same response named the employer throughout.
`kestrel render` and `kestrel export` honor the mode, and Settings warns when the mode is ON but the
redaction map is EMPTY, which silently redacted nothing on a fresh install.
STILL OPEN, with homes in the decision log: the posting URL still carries the employer slug,
`Org.domain` renders on `/orgs/{id}`, and `/prep/{id}`, `/renders/{id}`, `/mock/{id}` and the offers
list have no redaction path.

### 1.4 RESOLVED 2026-09-30 — the MCP port now carries a Host/Origin guard and the LAN key
Resolved in the BYOK/review round (decision log D-REV-3): fastmcp's SSE app had silently dropped the
`allowed_hosts`/`allowed_origins` kwargs, so the port was unguarded in BOTH bind modes; the guard is
now an explicit middleware pinned to `local_addresses()` and non-loopback peers must present
`X-Kestrel-Key`. Tests in `tests/test_mcp_lan_key.py`. Original text kept below for the record.

With `mcp.enabled` and `bind_all` both on, `mcp/server.py` binds `0.0.0.0` with
`allowed_hosts=["*"], allowed_origins=["*"]` and no shared key, exposing the action tools
(`trigger_scan`, `ingest_opportunity`, `set_pursuit_status`, `add_note`) unauthenticated on the LAN.
MCP is **off by default**, and the Settings disclosure now names this explicitly. The comment in
`web/app.py` claiming the web app applies "the same control the MCP server already applies" is
wrong about its sibling and should be corrected when this is.

### 1.5 SHOULD-FIX — spec drift
`SPEC-INTERFACES.md` still describes: saved views and bulk actions as shipped (§2.1, deferred with
triggers in the decision log), a `/runs/{id}` live log tail (not built), a preflight "setup window" with
one-click fixes (`run_preflight` only logs), and a LAN secret that is "generated automatically" and
whose middleware "exempts only `/health`" (it is operator-generated and fails closed, and `/auth` is
exempt too). CLAUDE.md says fix the spec first; that was not done.

### 1.6 NIT-level, recorded so they are not lost
`kestrel/logging_setup.py` is dead code whose docstring describes logging the app does not use.
(`packaging/kestrel.ico` and the README's missing LAN section were fixed in 16eb74e.) `HttpClient` is rebuilt per Research click, so the
per-host limiter and robots cache are per-request. `_EDGAR_UA` carries a placeholder contact
address, and SEC requires a real one.

---

## 2. Tests that were passing for the wrong reason

The test-quality audit mutated production code to see which tests noticed. These did not, and were
rewritten. This section exists because "the suite passes" is worth exactly as much as the suite's
ability to fail.

| Test | Mutation it survived | Now |
|---|---|---|
| all 24 in `test_lan_auth.py` | middleware rewritten to refuse a **valid** key | the file migrates a DB; allow-path assertions demand **200**, not `!= 401`. Re-mutated: 3 tests fail as they should |
| `test_rotation_wins_over_an_in_flight_load` | generation guard deleted | drives the real race, injecting a rotation during the awaited DB read |
| `test_ladder_steps_over_a_taken_rung` | ladder disabled entirely | asserts the exact next rung |
| `test_mcp_actions.py` outbound guard | (found in Wave 3) sync-only socket patch let every async call out | patches the asyncio layer; explicit trip tests for async, httpx, and DNS |
| hard rule 7 | rate limiter **and** User-Agent deleted from `HttpClient` → 20 tests still passed | two new guards; re-mutated, the limiter guard fails as it should |

Remaining known weakness: several `!= 403` assertions in `test_serve_discovery.py` observe a 500
from an unseeded DB. They are negative controls beside one load-bearing refusal test, so they are
honest, but they would not notice a broken route.

---

## 3. Hard rules: which have a regression guard

| Rule | Guard | Verdict |
|---|---|---|
| 1 never acts externally | `test_mcp_actions.py` socket/asyncio guard | **Strong** — mutation-tested. MCP surface only; the pipeline has no equivalent |
| 2 selects, never invents | `test_docs.py`, `test_docselect.py`, `test_guard.py` | **Strong**, byte-for-byte bullet check |
| 3 structured output or failure | `test_guard.py` repair-once-then-quarantine | Adequate |
| 4 uncertainty escalates | `test_pipeline_integration.py`, `test_web_runs_quarantine.py` | **Strong** — mutation-tested |
| 5 resumability | `test_queue.py` lease/expiry/reclaim | Partial: no test kills and restarts a run |
| 6 sample sizes stated | `test_calibrate_progress.py`, `test_analytics.py`, `test_intelligence_comp.py` | **Strong** — mutation-tested |
| 7 public data, politely | `test_http_client.py` robots + **UA + rate limit** | Adequate as of Wave 5B |

---

## 4. RESOLVED — the original audit's findings

Preserved verbatim in substance. Each carries the commit that closed it and how it was verified.

**BLOCKING-1 — reranker promoted on n=1.** RESOLVED `7e6b14f`. `recall_ok` requires
`n_apply >= _MIN_APPLY_FOR_RECALL` and keys off the operator's own apply/pursue verdict.
*Caveat now known:* the gate still promotes on 3 apply observations (recall must be 3/3), and a
degenerate held-out split can satisfy every criterion. The panel labels 3 as insufficient; the gate
does not. Recorded, not fixed.

**BLOCKING-2 — Tier 3 unimplemented while documented.** RESOLVED `fbe33c6`. Preview-then-confirm,
hash-bound, confidential redaction before send, idempotent, audited. Re-verified in Wave 5B: exactly
one `allow_external=True` call site, two callers, no worker/MCP/scheduler path, and the confirmation
token is re-derived server-side and compared before anything leaves.

**BLOCKING-3 — suite red / non-deterministic.** RESOLVED `e5ebdf3`, and finished in Wave 5B. The
per-test data-home isolation fixed the original failure; the residual 416/417 variance was a live
LLM call inside a Playwright test, now stubbed. Three consecutive runs at 428.

**3.1 — no MCP action-tool tests.** RESOLVED `1f83a7d`. All five tools, each asserting the local
effect and that nothing left the machine. The guard was itself defective on first write and is now
mutation-tested.

**3.2 — 70% structured-output misread as a tier-1 quality signal.** RESOLVED `25df452`.
Re-benchmarked live: `kestrel-tier2` is **100% first-attempt valid on both tracks**. Added a fitness
guard that refuses to present an off-tier or under-provisioned model's numbers as comparable, and a
caveat printed whenever agreement is measured against the placeholder gold set. The guard
corroborates the original diagnosis: `kestrel-tier1` has `num_ctx` 4096 against tier 2's required
15200. It also caught that `gemma4` has no explicit `num_ctx`, so its row in the re-benchmark is
itself not a sound comparison.

**3.3 — LAN/mobile access advertised but non-functional.** RESOLVED `45d3c5d`. Real shared-key
middleware, `/auth` cookie join, QR, `bind_all` honored, fails closed. Security review found and
fixed three blocking defects in the first cut, including that widening the Host/Origin guards for
`bind_all` had disabled all three controls at once.

**3.4 — missing web surfaces.** RESOLVED `1f83a7d`. Six opportunity-detail actions, `/runs/{id}`,
`/quarantine` with retry, `/calibrate` promotion progress. Retry was rewritten after review: it can
no longer un-promote an item hard rule 4 had already saved.

**3.5 — SPEC paths stale.** RESOLVED `3684a05`. Corrected to `kestrel/resources/{config,models}`.

**3.6 — missing `python_multipart` hiddenimport.** RESOLVED `3684a05`.

**3.7 — no `AppMutex`.** RESOLVED `3684a05`. Byte-matches `MUTEX_NAME`.

**3.8 — `OMP_NUM_THREADS` unpinned.** RESOLVED `3684a05`, relocated after review to
`kestrel/__init__.py`, because BLAS reads its variables at the first numpy import and numpy is
imported outside the learning package.

**3.9 — extension port mismatch.** RESOLVED `3684a05`. The real defect was upstream: the app took a
random port. It now claims a deterministic ladder and the extension probes it, requiring a
`service: "kestrel"` marker before sending page text anywhere.

**3.10 — line-ending churn risk.** RESOLVED `3684a05`. Deviation: `*.min.js -diff` rather than
`binary`, because `binary` implies `-text` and froze CRLF into the index, corrupting the vendored
bundle it was meant to protect.

---

## 5. Introduced by the remediation, and fixed within it

Recorded because a remediation that only reports what it fixed is not an audit.

- Making the port deterministic (3.9) made localhost **CSRF and DNS rebinding** practical against an
  app with no auth. Both controls added in the same wave.
- Making those controls "`bind_all`-aware" by widening them to `["*"]`/any-origin **disabled all
  three controls** whenever LAN mode was on. Found in Wave 4 review, reproduced, pinned to
  `local_addresses()`.
- The Wave 3 outbound guard **did not guard**: sync-socket-only patching on a Windows Proactor loop.
- `Archive` wrote a column nothing read; `Research` could never succeed with `http=None`;
  the `/calibrate` panel could claim the heuristic was in charge while a reranker decided Tier 1,
  and rendered a bare MAE from one observation.
- A huge path id 500'd on 37 routes, and `GET /renders/new` (which spawns Edge and writes a PDF)
  was outside the origin guard. Both fixed in Wave 5B.
- **Operator data was touched.** Ad-hoc verification scripts early in the session ran against the
  real database, inserting 5 synthetic opportunities and migrating it 0004→0006. Backed up to
  `kestrel.db.pre-cleanup-backup`, synthetic rows deleted, every table verified back to 0.

---

## 6. UNVERIFIED — OPERATOR ACTION REQUIRED

Nothing below was run. Do not treat any of it as working.

1. `pyinstaller packaging/kestrel.spec` and launching `dist\Kestrel\Kestrel.exe`. **This is the one
   that matters most** (see §1.2). Confirm the window opens and `/opportunities` renders.
2. `iscc packaging/kestrel.iss`, then install on a clean machine and on a path with spaces.
3. `AppMutex` actually blocking an install while Kestrel runs.
4. Close-to-tray, reopen from the tray, and quit releasing leases.
5. The LAN path end to end: set `KESTREL_SERVER__BIND_ALL=true`, restart, generate the secret, scan
   the QR from a phone on the same network. All LAN tests use a simulated peer; no second physical
   device was ever involved.
6. The frozen-app OMP pin (`sys.frozen` is false in every test run).
