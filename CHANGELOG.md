# Changelog

All notable changes to Kestrel are listed here, newest first. The format follows Keep a Changelog.

## [Unreleased]

## [0.5.0] - 2026-10-18

### New

- Record a job offer or a consulting engagement against the pursuit it came from, then rate each one on level and scope, reporting line, location and travel, risk, and fit with where you are headed. The comparison weighs those ratings the way you set them. Kestrel never guesses them for you.
- A consulting engagement appears in the offer comparison as an annualized figure, clearly labeled as not guaranteed, so it can sit beside a salary.

### Fixed

- Restoring a backup from Settings now works on Windows while Kestrel is running. It used to fail every time with a "file in use" error. Kestrel pauses its own work for the moment it takes and uses the restored data right away, with no restart.
- A backup made by a newer version of Kestrel is turned away with a clear message instead of being half restored. So is a file that is not a Kestrel backup, is damaged, or is far larger than a backup can be.
- Every restore keeps a dated copy of the data it replaced. The three most recent are kept, and Clear everything removes them along with the rest.
- When you send a role to an online model, the same request can no longer go out twice, even if you click twice or the connection drops. If the request never reached the provider, you can try again. If it might have, Kestrel says so and tells you what to change for a fresh request.
- Score again on a role no longer scores it twice when Kestrel is interrupted at the wrong moment. A scoring problem in one stage no longer stops the later stages, and it is counted rather than reported as a clean run.
- Recording an offer moves the pursuit to its offer stage from any open stage, and the move appears in its history. Ratings always land on the offer you meant, and an offer is filed under the pursuit's own track.
- Today's due and overdue lists, and "days quiet", no longer shift by a day in the evening.
- Uploaded files (backups, profiles, resumes, application trackers) are closed as soon as they are read, so Windows no longer reports them as in use afterward.
- The app's log now records what Kestrel itself is doing, so a saved diagnostics file can show what went wrong. Confidential employer names and saved keys are still removed from it, including names with accented letters.

## [0.4.0] - 2026-10-17

### New

- First public release.
- Checks kestrelapp.co once a day and tells you when a new version is available, with what changed. Nothing about you is sent, and you can turn it off in Settings. Kestrel never downloads or installs updates on its own; you choose if and when to update.
- Finds openings from public job boards, company hiring pages, a browser extension, and a paste box, and merges duplicates.
- Scores each opening for fit with an AI model that runs on your own computer, and always keeps a role when it is unsure.
- Tailors your resume by choosing from text you wrote, so every line in a generated PDF is your own words.
- Tracks applications to the outcome, with interview prep, mock interviews, and offer comparison. Kestrel never applies or contacts anyone for you.
