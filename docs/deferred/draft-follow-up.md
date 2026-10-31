# Draft follow-up

Branch: `deferred/draft-follow-up`, based on redesign READY commit `01c989d`.

## Built

- Added P17, `follow_up/v1`, to PROMPTS.md, the packaged prompt library and Pydantic registry.
  It drafts a subject, short body and review checks from recorded pursuit facts only.
- Applications and Leads offer Draft follow-up on open follow-ups due by tomorrow, matching
  the existing due-now rail. Other reminders retain their existing completion action.
- The dialog shows the facts, uses the existing Tier 3 disclosure, and requires confirmation
  for an online model. The server verifies the model, backend, facts, Hosted mode and employer
  protection again when the task runs. Changed facts or routing require a fresh confirmation.
- A `follow_up` task stores the input snapshot, consent and prompt version. The worker drains
  it after Tier 2 work, renews its lease, retries transport failures within the existing bound,
  and saves the result atomically with completion. Expired leases resume after restart.
- Invalid output receives one repair, then quarantine with the raw reply retained. The dialog
  displays a failure instead of fallback text. Reopening shows the saved draft.
- The operator can edit and copy the subject and body. Drafting and copying do not send,
  change a stage, complete a reminder or alter resume text.
- Due-follow-up notifications open the matching draft on Applications. Generic reminders
  continue to open Applications normally. No database migration or new dependency.

## Decisions

D-UI-168 supersedes D-UI-148's deferral. SPEC.md and SPEC-INTERFACES.md now describe the task
and action, including the notification entry point.

## Scope limits

- No delivery or scheduling of messages, per hard rule 1.
- No profile, resume, contact information, notes or job description in this prompt. Recorded
  pursuit facts are sufficient for a neutral request for an update.
- Edits in the copy dialog belong to the operator's current draft and are copied directly.
  Reopening restores the generated draft; it does not save an alternate document or message.
- Real-model quality is not measured by these offline tests. Model calls in verification are
  schema-validating fakes; the UI runs in the installed Edge browser.

## Validation

`uv run --extra dev ruff check .`: All checks passed.

With `KESTREL_LIVE_BROWSER=1`, targeted verification:

```text
uv run --extra dev pytest -q -x tests/test_follow_up.py tests/test_follow_up_browser.py tests/test_prompts.py tests/test_applications_screen.py tests/test_applications_browser.py tests/test_surfaces.py
112 passed in 243.86s (0:04:03)
```

The new browser cases cover local and online drafts in light and dark at 1280 and 1680 pixels,
including layout, initial consent, polling, editing, clipboard contents and reopening a saved
draft. The Applications browser regression covers both employment and consulting. Service and
route cases cover restart recovery, bounded transport retry, repair and quarantine, stale
facts/routing/consent, confidential employer protection, escaped output and notification links.

Full check, run once with `KESTREL_LIVE_BROWSER=1`:

```text
1 failed, 4020 passed, 8 skipped in 6684.66s (1:51:24)
```

The unrelated failure is
`tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[wants-1280-dark]`
at line 201. On `/onboarding`, `.ob-body` failed
`e.scrollWidth <= e.clientWidth + 1`. No Onboarding file or test was changed. The failure was
not weakened, skipped or fixed in this item. The new follow-up tests all passed in the full run.
Existing platform/model/idle-only tests account for the eight skips; no skip was added to get
past a failure. The full check did not end Ready to push. Publication proceeds under the owner's
explicit exception for the known base-branch Onboarding failures, recorded below.

### Owner-accepted base-branch exception

The owner identifies 3-4 Windows Onboarding overflow failures in
`tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll`
at line 201 as known issues in the redesign base branch, outside this item. The owner's
integrator will fix them on the base. This item's full run observed the named
`[wants-1280-dark]` case above, not 3-4 failures; the other failing parameter names were not
provided or observed in this run. No Onboarding code or tests are changed by this item.
The owner explicitly authorizes committing and pushing `deferred/draft-follow-up` despite
these failures. This exception does not change the recorded check result or claim a passing gate.

Last five lines of the full check:

```text
    1  bundled model not installed; run scripts/fetch_embed_model.py
    1  TZ switching is POSIX only
    1  SO_REUSEADDR is left alone on Windows
    1  the menu is only there while idle
Not ready to push: fix the failure above first.
```

Full raw check log on this machine:
`C:\Users\mcrow\AppData\Local\Temp\kestrel-draft-follow-up-full-check.txt`.
Publication is authorized by the owner's accepted base-branch exception above.
