# Condition-based browser waits (item 10)

Branch: `deferred/test-waits`. Base: finalized redesign commit `25fbe9f`.
Decision: D-UI-177. No production changes, migrations, or AI prompt changes.

## Built

Replaced all 59 fixed sleeps found in 22 `tests/*_browser.py` files. Layout synchronization
waits for fonts, HTMX swaps, and finite animations, including embedded document frames. Infinite
activity indicators remain running. Action synchronization waits for observable UI state or the
specific request, not elapsed time. Existing assertions remain unchanged.

The shared helper has a real-browser test proving a finite animation finishes while an infinite
animation continues. No browser motion preference is overridden and no timeouts are increased.

## Verification

- `uv run --extra dev ruff check .`: All checks passed.
- Fixed-sleep inventory: no `time.sleep` or `wait_for_timeout` calls remain in `tests/*_browser.py`.
- Initial focused browser batch (UI helpers, Undo toast, Help/About, Today): 49 passed in 504.20s.
- The second exploratory batch was stopped after passing tests to avoid duplicating the
  five-run matrix; it is not counted as a completed validation.
- Every changed browser file completed five invocations with `KESTREL_LIVE_BROWSER=1`:
  `uv run --extra dev pytest -q -rs -p no:cacheprovider --tb=short <file>`.
  Twenty-one files have five passing runs. Onboarding's five invocations finished with the
  base failures described below. Across these 110 invocations, using Pipeline's fresh corrected
  series: 1,483 passed, 5 existing skips, 2 known base overflow failures. Per-file/per-round
  logs are in `%TEMP%/kestrel-test-waits-stability/`.
  Pipeline round three exposed an Undo race: the test waited for the list to refresh before
  locating the expiring toast. It now waits for the hide action's specific Undo and hovers it
  while awaiting the list change, using the toast's normal pause-on-hover behavior. The original
  removal and restoration assertions remain. Pipeline completed five fresh passing runs after
  this fix (10 tests/run).
- Full check: stopped intentionally by the owner on 2026-10-25. It did not complete, so there is
  no full-suite pass claim. The owner instructed that it must not be rerun here: the full suite
  runs once after merge.
- Publication: authorized after the focused tests, without a completed full check.

| Changed browser file (`tests/test_..._browser.py`) | Tests per run | Five-run result |
| --- | ---: | --- |
| analytics | 15 | Five passes |
| applications | 16 | Five passes |
| calibrate | 5 | Five passes |
| company | 6 | Five passes |
| help_about | 12 | Five passes |
| models | 15 | Five passes |
| needs_try | 10 | Five passes |
| offers | 15 | Five passes |
| onboarding | 32 | Three passes; two runs with base overflow failures |
| pipeline | 10 | Five fresh passes after Undo synchronization fix |
| prep | 6 | Five passes |
| profile | 27 | Five passes |
| role | 16 | Five passes |
| runs | 17, plus one existing skip | Five passes |
| settings | 9 | Five passes |
| shell_responsive | 21 | Five passes |
| sources | 7 | Five passes |
| surfaces | 12 | Five passes |
| tailor | 9 | Five passes |
| toast_undo | 4 | Five passes |
| today | 15 | Five passes |
| ui | 18 | Five passes |

Last five lines from the final targeted runner output:

```text
tests/test_today_browser.py | round 1 | exit 0 | 15 passed in 101.62s (0:01:41)
tests/test_today_browser.py | round 2 | exit 0 | 15 passed in 101.33s (0:01:41)
tests/test_today_browser.py | round 3 | exit 0 | 15 passed in 99.19s (0:01:39)
tests/test_today_browser.py | round 4 | exit 0 | 15 passed in 97.51s (0:01:37)
tests/test_today_browser.py | round 5 | exit 0 | 15 passed in 93.74s (0:01:33)
```

Last five lines of the intentionally stopped full check, retained as partial output only:

```text
==> ruff: C:\Users\mcrow\Github\kestrel-deferred-test-waits\.venv\Scripts\python.exe -m ruff check .
All checks passed!
    ok (1s)
==> pytest: C:\Users\mcrow\Github\kestrel-deferred-test-waits\.venv\Scripts\python.exe -m pytest -q -rs -p no:cacheprovider
........................................................................ [  1%]
```

There is no "Ready to push." result. The owner's later instruction explicitly replaces that
gate for this item with changed-file tests, then commit and push. The full suite runs after merge.

## Outside this item

Production layout fixes, parallel test execution, and dependency upgrades belong to separate
items. Known base Onboarding overflow assertions remain unchanged. All five Onboarding
invocations finished: rounds one, three, and four passed all 32 tests; rounds two and five each
had 31 passes and one failure. Both failures are the unchanged `.ob-body` horizontal-overflow
assertion at `tests/test_onboarding_browser.py:192`:

- Round two: `test_no_overlap_no_wrapping_no_sideways_scroll[searches-1680-dark]`.
- Round five: `test_no_overlap_no_wrapping_no_sideways_scroll[welcome-1280-light]`.

The owner identifies these Windows overflow failures as outside this item and assigned to the
base integrator. The owner reported three to four known variants; these are the two actually
observed here. Onboarding does not have five passing runs, and this report does not claim it does.

Runs retains its existing skip at `tests/test_runs_browser.py:324`: "the menu is only there
while idle." No skip was added by this item.
