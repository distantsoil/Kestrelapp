# Keep separate Undo

Branch: `deferred/keep-separate-undo`, based on redesign final `01c989d`.

Built: a durable, single-use, ten-minute Undo snapshot restores every original proposal field
and usage link. Snapshot creation and dismissal commit together. Restoration refuses expiry,
reused story or usage ids, deleted roles, and later changes to dismissal fingerprints it wrote.
Unrelated dismissals remain intact. The existing toast refreshes the current tab in place.
Expired snapshots are cleaned on the next dismissal or attempted Undo. No migration or AI call.

Decision: D-UI-164. Nothing left out. No reviewer was started, as requested.

Focused verification: 49 passed across `test_prep_screen.py`, `test_prep_browser.py`, and
`test_interview_stories.py`, including light/dark at 1280/1680. Additional dismissal-state
coverage runs in the full gate.

Full check: `$env:KESTREL_LIVE_BROWSER="1"; uv run --extra dev python scripts/check.py`
completed successfully: **3999 passed, 8 skipped in 6390.78s (1:46:30)**. The eight existing
conditional skips cover POSIX-only paths, an absent bundled embedding model, and an idle-only
Runs menu. No tests were skipped or weakened for this change.

Last five lines:

```text
    1  bundled model not installed; run scripts/fetch_embed_model.py
    1  TZ switching is POSIX only
    1  SO_REUSEADDR is left alone on Windows
    1  the menu is only there while idle
Ready to push.
```

Known issue outside this item: the Region branch's full check encountered four base Windows
onboarding overflow failures at `tests/test_onboarding_browser.py:201`, test
`test_no_overlap_no_wrapping_no_sideways_scroll`: `welcome-1280-dark`, `searches-1280-light`,
`searches-1280-dark`, and `searches-1680-dark`. None failed in this item's passing full check.
The owner confirmed they are outside both items and that the integrator will fix the base.
Onboarding code and tests were not changed.
