# Deferred 11: parallel tests

Branch: `deferred/parallel-tests`. Base: `25fbe9f` from the finalized redesign branch.

## Built

- Added pytest-xdist 3.8.0 and its execnet dependency to development dependencies and the lockfile.
- The local gate runs the same suite with work-stealing scheduling. Its default is
  `max(1, min(8, cpu_count // 2))`; `KESTREL_TEST_WORKERS` overrides the default, and
  `--workers N` overrides the environment. Pytest receives an explicit numeric count.
- `--workers 0` retains serial execution; `--junitxml PATH` records individual outcomes for
  comparison. Worker crashes fail the gate without automatic restarts. Skip reporting remains.
- All SQLite app, sync, and migration connections use WAL with `synchronous=NORMAL`.
  Fresh sync and async connections, including reopened connections, must report `synchronous=1`.
- Added runner regression tests and developer instructions. Existing per-test data homes,
  process isolation, and OS-assigned server ports already isolate test resources; no existing
  assertions or onboarding tests were changed.

Decisions: D-UI-172 introduces xdist; D-UI-173 supersedes its initial worker cap;
D-UI-174 records NORMAL synchronization and its durability tradeoff. A power loss can lose
recent commits while preserving database consistency; tasks resume from durable state.
No schema migration is required.

## Verification

- The new requirements first produced 9 failures and 13 passes before implementation.
- `uv run --extra dev ruff check .`: passed after the final implementation.
- Final authorized check:
  `uv run --extra dev pytest tests/test_check_runner.py tests/test_db_schema.py -q`:
  **22 passed in 4.80s**.
- An earlier complete gate, before the requested cap and NORMAL synchronization, used
  16 workers: **14 failed, 4091 passed, 8 skipped in 699.89s (11:39)**. Every failure was
  the known base onboarding horizontal-overflow assertion in `tests/test_onboarding_browser.py:192`,
  outside this item. The owner's initial estimate was 3 to 4; this run observed these 14:
  - `test_no_overlap_no_wrapping_no_sideways_scroll[welcome-1280-dark]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[welcome-1680-light]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[welcome-1680-dark]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[about-1280-dark]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[about-1680-dark]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[resume-1280-light]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[resume-1680-dark]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[wants-1280-dark]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[wants-1680-light]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[wants-1680-dark]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[searches-1280-light]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[searches-1280-dark]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[searches-1680-light]`
  - `test_no_overlap_no_wrapping_no_sideways_scroll[searches-1680-dark]`

Last five nonempty lines of that completed gate, which does not validate the final changes:

```text
    1  SO_REUSEADDR is left alone on Windows
    1  bundled model not installed; run scripts/fetch_embed_model.py
    1  TZ switching is POSIX only
    1  the menu is only there while idle
Not ready to push: fix the failure above first.
```

## Deferred verification

The owner explicitly stopped the capped full gate and instructed that only the changed test
files run before committing and pushing; the full suite runs once after merge. The restarted
gate was also stopped when that instruction arrived. Neither capped attempt has a final
summary or a `Ready to push.` result. The obsolete serial and 32-worker runs were stopped
after the cap requirement arrived.

Final full-suite results, per-test serial/parallel parity including skip reasons, and the
requested few-minute runtime are therefore unverified and left for integration. No test was
skipped, xfailed, or weakened to obtain a pass. The onboarding failures remain for the owner's
base-branch fix. Publication follows the owner's explicit instruction to push after the
targeted checks instead of requiring another full gate.
