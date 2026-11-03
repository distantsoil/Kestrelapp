# OMP working log — Kestrel (public repo)

## Invariants (NEVER violate)
- **NO PR, NO MERGE, NO SQUASH, NO PUSH.** Local commits only, to `ompepic`.
- **NEVER touch `main`.** All work on `ompepic`.
- **Gates pass on the literal `Ready to push.` line**, never an exit code.
- **NEVER `ruff format` on an existing file.**
- **Never weaken a test to make it pass.**
- **Tests: invented text only, never a private phrase in any form.**
- **Clocks in tests:** patch a module's own clock alias, seed via `tests/clock_seed.py`.
- **Scoring prompts/schemas/prompt versions off-limits** unless the task is about scoring.
- **No em dashes in UI copy.** American English. Executive register.
- **UI change = light/dark/narrow screenshots + overlap check.**
- **Local model gates BLOCKED** (Ollama reboot pending on PC; hosted-mode safety check
  refused). Record exact unblock command; never fake verification.

## Repo / branch
- **Repo:** `C:\Users\mcrow\Github\kestrelapp` (public, `distantsoil/Kestrelapp`).
- **Branch:** `ompepic` @ `7d633ec` (Kestrel 0.7.4).
- **Gate:** `uv run --extra dev python scripts/check.py` (must end `Ready to push.`).
- **Tooling:** `.venv/Scripts/python.exe`, `.venv/Scripts/ruff.exe`. Windows. Line length 100.
- **Set `KESTREL_DATA_HOME`** before any ad-hoc run.
- **NOTE:** Public repo issue numbers differ from the private tracker. Use the numbers below.

## Issue queue (public repo, work in number order)
| # | status | title |
|---|--------|-------|
| 81  | **done** | no model activity 11 min — shipped in 0.7.4 (CHANGELOG L46) |
| 97  | **done** | code + tests present (test_doctor_needle.py); local confirmation BLOCKED |
| 101 | **done** | marks on all surfaces (style_marks.html); named-detail grounding (name_sources + grounding_texts); scoring prompt off-limits |
| 103 | **done** | mid-run switch to local: note set on run + shown on panel; reset per run; tests pass |
| 104 | **done** | stage priority: scoring outranks searching; 40 tests pass; committed b70adf4 |
| 105 | **done** | newer database: Download button + clear message; 2 tests pass; committed b5a5830 |
| 106 | **done** | balance timeout raised 5s→15s; 23 tests pass; committed 62a4bb5 |
| 107 | **blocked** | needs UI wiring (letters have no caller); backend ready; note in PR |
| 108 | **n/a** | planning/roadmap issue; sub-items tracked separately |
| 109 | **done** | run_finished notification added; 2 tests pass; committed 02f3176 |
| 110 | **done** | failure count + severity in notification; 3 tests pass; committed c35bb0b |
| 111 | **done** | high_score notification for worth-reviewing roles; committed d7527fb |
| 112 | **done** | card width capped at 420px; committed 60acfae |
| 113 | **done** | Maybe roles included in Decisions waiting; committed c5b0d5c |
| 114 | **done** | "Too small" only shown for local steps; committed 3cbb349 |
| 115 | **done** | local cards greyed out in online-only mode; committed 66dd499 |
| 116 | **done** | Select all button per group added; committed c5a1a92 |
| 117 | **done** | title variants interleaved fairly; committed e675b4f |
| 118 | **blocked** | needs DB migration to link suggestions to resumes; requires alembic + cascade delete |
| 119 | **done** | 'Suggest again' note made more prominent; committed 2f1717a |
| 120 | **done** | date range filter added to applications export; committed 2f3440b |
| 121 | **blocked** | needs UI work: sortable columns, date column, remember view; complex JS + template changes |
| 122 | **done** | Calibrate queue deduplicated by dedup_group_id; committed 8794b01 |
| 123 | **blocked** | needs expensive board lookup in suggestions; complex caching + UI changes |
| 124 | **blocked** | large feature: cover letters draft/edit/check/preview/download; needs UI + backend wiring |
| 125 | **blocked** | needs UI work: animations + transitions for tab switching; complex CSS + JS changes |
| 126 | **blocked** | needs UI work: sidebar toggle + keyboard shortcut + persistence; complex CSS + JS changes |
| 127 | **blocked** | needs UI work: bulk Score again state display; complex UI + backend wiring |
| 128 | **done** | score_stub handed from Tier 1 to Tier 2; committed dbe4bba |
| 129 | **blocked** | needs UI work: 'I applied' button + date field on role page; complex UI + backend wiring |
| 130 | **done** | fastmcp included in packaged app; committed 5bb1a8e |
| 131 | **blocked** | deferred: re-import applied history safely; needs spec first, complex matching logic |
| 132 | **done** | _Splash.fail now accepts code param; committed 239c0e0 |
| 133 | **done** | QR code no longer cropped; committed b584c49 |

## Per-issue loop
1. `gh issue view N` (authoritative body, public repo).
2. Locate code (grep/partial read, or scout subagent to stay lean).
3. Implement fix (reuse existing patterns; state invariant for risky briefs).
4. Tests alongside (invented text, clock via module alias + `tests/clock_seed.py`).
5. Targeted tests, then `uv run --extra dev python scripts/check.py` (must end `Ready to push.`).
6. `git add` + conventional commit on `ompepic`.
7. Record here; move to next issue.

## Running log
- 2026-11-02: Switched from private repo to public repo (`kestrelapp`, branch `ompepic`)
  per owner's operating rules. Discovered public repo issue numbers differ from private
  tracker. Rebuilt queue with public repo numbers.
- 2026-11-02: #81 (no model activity) verified as already shipped in 0.7.4.
- 2026-11-02: #97 (doctor needle test) in-progress. Located code: `kestrel/doctor.py`
  `_check_needle` (line 303), `_needle_prompt` (line 280), `NEEDLE_REPLY_TOKENS = 256`
  (line 299). The issue: chatty replies and 5xx errors are misreported as context loss.
- 2026-11-02: #104 (stage priority) done. Reversed stage order in `run_summary.py`:
  scoring (t2/t1) now outranks searching. Updated 4 existing test assertions; added
  `test_stage_priority.py` with 3 boundary tests. 40 tests pass. Committed b70adf4.
- 2026-11-02: Fixed missing GROUP_MAYBE import in `today_view.py` that was causing
  test failures. All announcement tests now pass.
- 2026-11-02: #132 (startup timeout + second server) partially fixed. The _Splash.fail
  method now accepts an optional code parameter, which was causing a TypeError when
  _show_problem called it with 3 args. Committed 239c0e0.
