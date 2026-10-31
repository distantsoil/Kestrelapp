# Background Research and Contacts

Item 6, branch `deferred/background-research`, based on redesign commit `01c989d`.

## Built

- Company research and Find contacts create durable `org_research` and `org_contacts` tasks.
  The legacy POST routes queue the same work; no web request runs a research model.
- The existing worker drains these tasks serially on Tier 3 after Tier 2 work. Leases renew
  during long calls, expired leases resume, and a graceful stop returns unfinished work.
- The Organization tab shows progress and Cancel, polls while work is live, and retains
  earlier research and contacts. Finished, held, cancelled and failed states stop polling.
- Result writes, outreach drafts, quarantine rows and task completion commit together.
  Cancelled or superseded leases cannot publish results, and restart cannot duplicate a
  task's contacts. Cancelling a running call cancels its coroutine and discards pending writes.
- Existing P5/P6 validation, repair, source attribution, public-fetch policy and quarantine
  behavior remain in use. Outreach is draft text only; nothing contacts anyone.
- Existing online disclosure and confirmation are preserved. The task binds approval to the
  backend, URL and model. Hosted-mode approval and confidential handling are checked by the
  worker and again after fetching, before inference. Changed settings hold work for a new
  explicit action.

## Decisions

`D-UI-167` amends the deferral in `D-UI-150` item 12. No migration or prompt change.

## Left Out

- Percentage and time estimates: fetch and model calls have no reliable measured denominator.
  The UI shows the current step instead.
- New research providers, automatic follow-up, or sending outreach: outside item 6.
- Live provider/model verification: tests use fictional sources and mocked model replies.

## Validation

Focused coverage includes simultaneous enqueue, expired leases, stale-worker writes, cancellation
during inference and at the fetch boundary, restart, retry exhaustion, raw-reply quarantine,
source-backed research, contact/draft storage, changed consent, and confidential-mode holds.
Browser coverage runs in light and dark at 1280 and 1680, checks layout and wrapping, queues
both jobs, reloads, cancels in place, and confirms earlier contacts remain visible.

- `uv run --extra dev ruff check .`: passed.
- Focused research, route, browser, progress and CLI tests: 118 passed in 128.54s.
- Additional consent and cancellation-boundary tests with the Role page: 47 passed in 74.59s.

Full check: `$env:KESTREL_LIVE_BROWSER='1'; uv run --extra dev python scripts/check.py`
finished with **4012 passed, 8 skipped in 6636.98s (1:50:36)** and `Ready to push.`
The eight existing environment/state skips were two POSIX stand-in scripts, two POSIX flock
tests, the absent bundled embedding model, POSIX timezone switching, Windows SO_REUSEADDR,
and the Runs idle-only menu. No tests were weakened, skipped or marked xfail for this item.
The run took longer than the estimate; it completed normally.

### Known Base-Branch Onboarding Issue

The owner reports three to four Windows onboarding overflow failures in
`tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll`, at the
`.ob-body` overflow assertion on line 201. The affected parameter IDs were not supplied.
These failures are outside item 6; the owner's integrator will fix them on the base branch.
This item's full check did not reproduce them: all 4012 tests passed. Onboarding code and
tests were not changed, skipped or weakened.

Last five lines:

```text
    1  bundled model not installed; run scripts/fetch_embed_model.py
    1  TZ switching is POSIX only
    1  SO_REUSEADDR is left alone on Windows
    1  the menu is only there while idle
Ready to push.
```
