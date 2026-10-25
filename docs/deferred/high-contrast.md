# Deferred item: high-contrast

Added High contrast to Preferences through ui.scheme=contrast, covering saved server defaults, browser storage, first paint, page navigation and Undo. Light uses black ink on white with a deep blue accent; dark uses white ink on black with a yellow accent. Plain surfaces, full-strength secondary/status text, strong borders, 3px keyboard focus rings and legacy token mappings apply app-wide. Text and focus tokens have a 7:1 gate, including pinned light/dark and Match computer.

Decision: D-UI-162 (numbered from this independent branch base). No migration.

Validation includes route and template tests plus opt-in Playwright tests in light/dark at 1280/1680: no overlap, no wrapped titles or buttons, no horizontal scroll, effective token contrast, save, Undo, reload, navigation and a fresh browser context. Existing Settings and shell behavior assertions are preserved.

Left out: no assigned functionality. External review happens elsewhere, per the build-only instruction.

Focused validation: ruff check passed; 115 passed in 112.04s, with KESTREL_LIVE_BROWSER=1.

Full check command: KESTREL_LIVE_BROWSER=1; uv run --extra dev python scripts/check.py
Result: 4 failed, 3996 passed, 8 skipped in 6584.87s (1:49:44); exit code 1.

Full gate exception: the full gate did not end "Ready to push." Failures are in the unmodified tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll at line 201, asserting .ob-body has no horizontal overflow:
- tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[about-1280-light]
- tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[about-1680-light]
- tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[resume-1680-dark]
- tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[wants-1680-light]

These failures are outside this item. The owner confirmed they are a known Windows layout issue in the base branch, not caused by this item; the owner's integrator will fix them on the base. No onboarding code or tests were changed. No tests were weakened or newly skipped. Existing skips cover POSIX-only fixtures and locks, an unavailable bundled model, POSIX TZ switching, Windows socket behavior and a Runs menu state. Both complete check runs were allowed to finish.

The owner explicitly authorized committing and pushing deferred/high-contrast despite these known base-branch failures. This exception does not change the recorded check result. Build-only instructions were followed: no reviewer or other agent was started.

Last five check lines:

~~~text
    1  bundled model not installed; run scripts/fetch_embed_model.py
    1  TZ switching is POSIX only
    1  SO_REUSEADDR is left alone on Windows
    1  the menu is only there while idle
Not ready to push: fix the failure above first.
~~~
