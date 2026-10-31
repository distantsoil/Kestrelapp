# Region dates and times

Branch: `deferred/region-dates`, based on redesign final `01c989d`.

Built: `kestrel.date_display.format_display` reads the saved Region preferences and formats
absolute dates and times across templates and view helpers, including Today, Pipeline, Role,
Applications, Interview prep, Offers, Runs, Model check, Settings, notifications and analytics.
Each web request validates and snapshots preferences once. Full pages and HTMX fragments see
the next saved change immediately. Settings refreshes quiet-hour labels and scene previews too.
The interview rail shows a calendar icon with the selected full date beside the role when a
custom format is chosen, keeping the original month/day tile for default preferences.

Default preferences preserve the existing wording on each surface. Every nondefault date
choice and the 24-hour clock override the corresponding display style. Relative ages stay
relative; date-only values and appointment wall times stay on their written calendar day.
Existing instant displays retain their local timezone conversion.

Decision: D-UI-166 on this independent branch, completing D-UI-158. No migration or AI prompt
change. No reviewer was started, as requested.

Deliberately unchanged: machine-readable storage, form values, exports, filenames, durable
event payloads and the operator's own resume text. Those are data, not date display labels.

Focused verification: lint clean; 126 tests passed in the date-helper, route and Offers run;
23 live Edge tests passed across the new Region browser coverage and existing Settings/prep
coverage (light/dark, 1280/1680, numeric and long dates, layout and interaction checks).
The final HTMX date regression passed both full-page and fragment cases. An earlier affected
screen run passed 273 tests and found an Offers import compatibility issue, corrected and
verified in the 126-test run. No test was skipped, weakened or marked xfail for this work.

Full check: `$env:KESTREL_LIVE_BROWSER="1"; uv run --extra dev python scripts/check.py`
completed with **4 failed, 4040 passed, 8 skipped in 6373.12s (1:46:13)**. Lint passed.
The four failures are all `tests/test_onboarding_browser.py:201`, test
`test_no_overlap_no_wrapping_no_sideways_scroll`, with these parameters:

- `welcome-1280-dark`
- `searches-1280-light`
- `searches-1280-dark`
- `searches-1680-dark`

The owner identified these as a known Windows overflow issue in the base branch, outside this
item, and explicitly authorized publishing this branch despite this gate result. The owner's
integrator will fix the base. Onboarding code and tests were not changed. The eight existing
conditional skips cover platform-specific paths, an absent bundled embedding model, and an
idle-only Runs menu. No tests were skipped or weakened for this item.

Last five lines:

```text
    1  bundled model not installed; run scripts/fetch_embed_model.py
    1  TZ switching is POSIX only
    1  SO_REUSEADDR is left alone on Windows
    1  the menu is only there while idle
Not ready to push: fix the failure above first.
```
