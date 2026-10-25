# Model concurrency (deferred item 4)

Branch: `deferred/concurrency`, based on finalized redesign commit `01c989d`.
Decision: D-UI-165. No migration or AI prompt changes.

## Built

- Quick screen and Rating accept 1 through 16 requests at once behind Models on this computer >
  Show details, for local and online configurations. Both values validate before either saves.
- Configuration loading enforces integer bounds for tier1, tier2, and embed across YAML,
  environment, and database overrides. The legacy Settings route also refuses invalid values.
- Download-card refreshes retain the saved controls. Changes apply on the next scoring pass.
- Guidance covers memory use, tier-by-tier draining with shared models, OLLAMA_NUM_PARALLEL using
  the maximum including embedding, setting it in Ollama's own environment and restarting Ollama,
  OLLAMA_KEEP_ALIVE=2h, and online providers' separate limits.
- Page CSS keeps the control labels on one line. Route, configuration, template, and Edge tests
  cover persistence, atomic refusal, bounds, and light/dark at 1280 and 1680 with details open.
- SPEC 6.6 now agrees with the existing worker and standing rule: shared models do not interleave
  tiers. Worker scheduling stays as implemented.

## Left out

- Automatic Ollama environment changes: the application cannot verify a separate process's
  environment. The operator changes Ollama's settings and restarts it.
- Hardware-specific maximums: existing model profiles remain the recommendations; the 16-request
  ceiling is a bound, not a promise of sufficient memory.
- Resume picking and deeper-read concurrency controls: these one-off actions do not use the
  pipeline's tier1/tier2 concurrency settings. Embedding remains validated configuration, as before.

## Verification

- `uv run --extra dev ruff check .`: All checks passed.
- First focused route/configuration run: 113 passed and one new test failed because it used a
  nonexistent setup route. Corrected the test to use `/setup/mode`.
- `uv run --extra dev pytest -q tests/test_concurrency.py`: 37 passed.
- First Models browser run: 15 passed; four new cases caught wrapping control labels. Added
  page-scoped nowrap and fixed input sizing, preserving the assertions.
- `KESTREL_LIVE_BROWSER=1 uv run --extra dev pytest -q tests/test_models_browser.py -k
  speed_controls`: 4 passed, 15 deselected.
- Full check, run once with `KESTREL_LIVE_BROWSER=1 uv run --extra dev python scripts/check.py`:
  **4 failed, 4025 passed, 8 skipped in 6549.09s (1:49:09)**. Lint passed. All concurrency
  and Models tests passed in this run. The full gate failed. The owner subsequently authorized
  publishing `deferred/concurrency` despite that result.

Unrelated failures, left unchanged per assignment:

- `tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll`:
  `about-1280-dark`, `resume-1680-light`, and `searches-1280-light`. Each fails the `.ob-body`
  scroll-width assertion at line 201. These three failures are outside this item. The owner
  confirmed they are a known Windows layout issue in the base branch; the owner's integrator
  will fix them on the base. The tests remain unchanged.
- `tests/test_sources_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[1680-light]`:
  document scroll width exceeds the viewport at line 132.

The eight existing skips were platform-specific tests (six), the absent bundled embedding model
(one), and the idle-only Runs menu case (one). No tests were skipped, marked xfail, or weakened
for this item. There were no browser-unavailable skips.

Full output is in `%TEMP%/kestrel-deferred-concurrency-check.log`. Last five lines:

```text
    1  bundled model not installed; run scripts/fetch_embed_model.py
    1  TZ switching is POSIX only
    1  SO_REUSEADDR is left alone on Windows
    1  the menu is only there while idle
Not ready to push: fix the failure above first.
```
