# Deferred item: schemes

Exposed Paper and Plum and Paper and Graphite in Preferences and validated configuration. Existing palettes apply through the saved scheme setting, first paint, browser storage, page navigation and Undo. Swatches use tokens, without inline colors. Extended contrast coverage includes status labels and avatars; light status text was darkened and avatar gradient endpoints follow each scheme to meet 4.5:1.

Decision: D-UI-163 (numbered from this independent branch base). No migration.

Validation includes route and template tests plus opt-in Playwright tests in light/dark at 1280/1680: no overlap, no wrapped titles or buttons, no horizontal scroll, effective token contrast, save, Undo, reload, navigation and a fresh browser context. Existing Settings and shell behavior assertions are preserved.

Left out: no assigned functionality. External review happens elsewhere, per the build-only instruction.

Focused validation: ruff check passed; 119 passed in 140.42s, with KESTREL_LIVE_BROWSER=1.

Full check command: KESTREL_LIVE_BROWSER=1; uv run --extra dev python scripts/check.py
Result: 3 failed, 4001 passed, 8 skipped in 6609.88s (1:50:09); exit code 1.

Full gate exception: the full gate did not end "Ready to push." Failures are in the unmodified tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll at line 201, asserting .ob-body has no horizontal overflow:
- tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[models-1680-light]
- tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[about-1280-dark]
- tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[resume-1280-dark]

These failures are outside this item. The owner confirmed they are a known Windows layout issue in the base branch, not caused by this item; the owner's integrator will fix them on the base. No onboarding code or tests were changed. No tests were weakened or newly skipped. Existing skips cover POSIX-only fixtures and locks, an unavailable bundled model, POSIX TZ switching, Windows socket behavior and a Runs menu state. Both complete check runs were allowed to finish.

The owner explicitly authorized committing and pushing deferred/schemes despite these known base-branch failures. This exception does not change the recorded check result. Build-only instructions were followed: no reviewer or other agent was started.

Last five check lines:

~~~text
    1  bundled model not installed; run scripts/fetch_embed_model.py
    1  TZ switching is POSIX only
    1  SO_REUSEADDR is left alone on Windows
    1  the menu is only there while idle
Not ready to push: fix the failure above first.
~~~
