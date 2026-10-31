# Other OpenAI-compatible service

Built in `deferred/other-service`, based on redesign commit `25fbe9f`.

Settings > This computer now offers Other with a service address, password field, Show,
Test connection and Save. Both address and key stay only in the data home's `.env`.
Test requests only the model list with the supplied or saved key and changes no settings.
Save registers the generic chat completions backend without granting online approval.
The Models drawer connects it through the existing hosted disclosure and default model picks;
normal model overrides, per-action consent, confidential employer handling and structured
reply repair/quarantine remain in force. A changed destination clears standing approval.
Saved and unsaved test credentials are redacted from app, HTTP and exception logs.

Decision: D-UI-176. SPEC.md and SPEC-INTERFACES.md describe the behavior. No new AI prompt was
needed: the item adds configuration and model-list metadata requests, not a new inference task.

Left out: Responses and Anthropic Messages endpoints, which are separate protocols from the
requested OpenAI chat completions format. Existing named providers retain their current paths.
HTTP addresses outside loopback are refused so credentials cannot be sent in cleartext.

Validation: route, template, storage/reload, backup/diagnostics, log privacy, approval, generic
schema repair and quarantine tests; opt-in Edge browser coverage for light and dark at 1280 and
1680, including Test, Save, reload, Show, disclosure and connection. Existing Settings, hosted
connect and BYOK tests also run.

Completed focused validation:
- `uv run --extra dev ruff check .`: All checks passed.
- Settings, BYOK and hosted connection suites: 113 passed in 165.57s.
- Other browser matrix: 4 passed in 91.51s.
- Existing Models suites: 45 passed in 115.34s.
- Expanded Other unit suite: 33 passed and one fixture failure caused by an unmocked local
  Ollama probe. The new fixture was corrected; the focused rerun passed in 7.81s.

Full live-browser check, first completed run: 5 failed, 4128 passed, 8 skipped in 4856.86s.
Two item failures were corrected: the shared disclosure retains its literal `warnbox` class,
and the provider label now has a CSS rule. Those guards and the browser matrix then passed:
6 passed in 37.92s. Lint passed again.

Outside this item, that full run reported:
- `tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[wants-1680-dark]`
- `tests/test_onboarding_browser.py::test_no_overlap_no_wrapping_no_sideways_scroll[searches-1680-light]`
- `tests/test_role_browser.py::test_the_other_tabs_fit_and_have_their_own_urls[/organization]`

The onboarding assertions fail at line 201 in the base Windows layout, as identified by the
owner. This run had two of the previously reported three to four onboarding failures. They
are outside this item; no onboarding code or tests were changed. The Role Organization
overflow test passed unchanged on rerun: 1 passed in 15.96s. No Role code or tests were changed.

Final full live-browser check after the item corrections:
`$env:KESTREL_LIVE_BROWSER="1"; uv run --extra dev python scripts/check.py`.
Result: 4133 passed, 8 skipped in 2944.70s (0:49:04), exit 0, ending `Ready to push.`
All three layout failures above passed unchanged in this final run. The eight skips are the
existing platform, unavailable bundled model, and idle-only menu cases listed by the check.
An earlier interrupted attempt exited -1 without a pytest result and is not counted as
completed validation.

Last five lines of the final check:
```text
?? kestrel/private_logging.py
?? kestrel/services/other_service.py
?? tests/test_other_service.py
?? tests/test_other_service_browser.py
Ready to push.
```
