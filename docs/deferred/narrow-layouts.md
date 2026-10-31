# Narrow layouts

Branch: `deferred/narrow-layouts`, based on redesign commit `25fbe9f`.
Decision: D-UI-171.

## Built

- Today stacks section headings and carousel controls at narrow content widths. The found-count
  subtitle occupies its own line; carousel navigation and role actions remain available.
- Settings wraps the page title actions and constrains the computer-profile selector to its card.
- Help stacks the four step cards on phones, allows long card headings to reflow, and keeps native
  table layout inside the existing reference-table scroller. This overrides the shell's phone
  table display rule only on Help.
- All changes use page CSS and existing theme tokens. The shared shell, bottom bar, icon rail,
  navigation breakpoints, routes and data behavior stay as defined in D-UI-91 and D-UI-161.
- The shell browser suite now asserts no horizontal page scroll for all five pages at 400 px,
  including Today, Settings and Help. Additional light/dark cases at 400, 1000, 1280 and 1680 px
  cover populated Today and carousel navigation, every Settings section, and Help's expanded
  reference tables, including scrolling to the last source column on a phone.
- The edited browser file waits for fonts and completed animations instead of its old fixed
  500 ms sleep.

No migration, AI prompt, online send or dependency changes.

## Validation

- Baseline regression: 6 failed, 4 passed, 11 deselected. The six failures reproduce Today,
  Settings and Help overflowing at 400 px, in both themes.
- Initial fixes: the new 400 px light-theme cases pass (3 passed, 42 deselected).
- `uv run --extra dev ruff check .`: All checks passed.
- `KESTREL_LIVE_BROWSER=1 uv run --extra dev pytest -q tests/test_shell_responsive_browser.py
  --tb=short`: **45 passed in 823.36s (0:13:43)**. This is the only changed test file; the run
  covers both themes at 400, 1000, 1280 and 1680 px, plus the existing 1200 px sidebar check.
- Started the required full check with `KESTREL_LIVE_BROWSER=1`. Its lint passed, then the owner
  deliberately stopped it at 1 percent of pytest. It did not produce a full-suite result or
  `Ready to push.` The command session returned exit code -1.
- The owner then explicitly instructed: do not rerun the full suite; use only the changed test
  files, commit and push. The changed test file had just completed successfully. No code changed
  after that run. The full suite is deferred to the owner's check after merge.

## Outside this item

The owner identified these base-branch Windows onboarding layout failures at
`tests/test_onboarding_browser.py:201`, all in
`test_no_overlap_no_wrapping_no_sideways_scroll`, as outside this item:

- `welcome-1280-dark`
- `searches-1280-light`
- `searches-1280-dark`
- `searches-1680-dark`

These four failures were identified by the owner, not observed in this item's interrupted full
check. The owner's integrator will fix them on the base. The owner's later instruction explicitly
defers the full suite until after merge and authorizes publishing after the changed tests pass.
Onboarding code and tests are unchanged. Settings dialogs and the import mapping workflow are
desktop surfaces and are outside this page-layout fix.

## Full check last five lines

The full check was deliberately stopped by the owner; these are its final available lines:

```text
==> ruff: C:\Users\mcrow\Github\kestrel-deferred-narrow-layouts\.venv\Scripts\python.exe -m ruff check .
All checks passed!
    ok (0s)
==> pytest: C:\Users\mcrow\Github\kestrel-deferred-narrow-layouts\.venv\Scripts\python.exe -m pytest -q -rs -p no:cacheprovider
........................................................................ [  1%]
```
