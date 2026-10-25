# Changelog

All notable changes to Kestrel are listed here, newest first. The format follows Keep a Changelog.

## [Unreleased]

### Changed

- The About page and README credit MiniMax M3 (formerly listed under its stealth name, Space Bunny) and add Kimi K2.7 and MiMo: Claude, ChatGPT, Qwen, DeepSeek, NVIDIA Nemotron, MiniMax M3, LongCat, Kimi K2.7 and MiMo.

## [0.6.5] - 2026-10-23

### New

- Likely interview questions. When you mark a role Applied, Kestrel writes up to 12 questions you are likely to be asked, each with why it is asked, linked to your own stories where one fits. Your prep pack uses them.
- Suggested titles. Kestrel suggests job titles that fit your resume and the roles you apply to. Nothing is added to your profile until you click Accept, and a title you dismiss does not come back.
- The Applications board can show only recent activity (7, 14, 30 or 90 days), cap how many cards each column shows, and fold the Closed column. Your choices are remembered. A role with no reply for three weeks offers "Mark as ghosted?".
- Remove from shortlist on each Applications board card, with the same confirmation as the role page.
- Applied roles get their own Applied group on the Pipeline, with a status pill, and no longer count toward Review on Today.
- Every role still waiting for a score says why: being scored, queued, waiting in the background, held, or not queued.
- Four more company job boards: Rippling, ADP Workforce Now, Paylocity and Workable. Kestrel follows them from your searches like the others.
- RemoteRocketship, for people with their own paid key: Kestrel uses it only to find employer links, then reads each posting from the employer's own page.
- Turn LAN access on or off from Settings, after creating a shared secret. It takes effect after a restart.
- Change which model each tier uses from Settings > Models.
- With an OpenRouter key, the status rail shows what you have spent this month.

### Changed

- A posting is marked closed only after two checks in a row find it gone, and the role page says why it was closed.
- Open full page now sits at the top of the role side panel.
- When an online key runs out of credit, Kestrel says so once, names where to top up, and stops sending to that service. It never switches to another online service on its own.

### Fixed

- The LAN QR code scans in dark mode, and the secret and join link can be copied with one click.
- Calibrate no longer shows roles with no description, and its count matches the page.
- A reply from an online model that ran out of room is retried once with more room, instead of failing.
- The tray and menu-bar icons are indigo again.
- Error messages no longer show technical details anywhere in the app.
- Saving a board preference twice at once no longer loses the choice.
- On macOS, quitting while Kestrel was starting in the browser could leave the menu-bar icon running. It now quits cleanly.
- Windows notifications now come from Kestrel by name with its current icon, instead of from "Kestrel.exe" with an old logo.

## [0.6.2] - 2026-10-19

### Fixed

- Kestrel no longer closes the moment it opens on macOS 27. The menu-bar icon was set up off the main thread, which macOS 27 does not allow.

## [0.6.1] - 2026-10-19

0.6.1 is the first release of these changes. The 0.6.0 tag was made, but its release was never published with installers.

### New

- Company boards Kestrel finds score the roles that look like your field and level first. The rest wait and are scored when Kestrel is otherwise idle, up to 200 a day, so nothing is thrown away. Importing your past applications teaches it your field.
- A Score everything now button on the Sources page scores every waiting role at once, after a clear warning about the time and, on an online model, the cost.
- Search and filter your Applications as you type: find a role by company, title, place or notes, narrow by stage, hide closed ones, sort the table, and press "/" to jump to the search box.
- The Sources page shows how many postings each job board gave in the last run, and says when a board found nothing.
- Kestrel now finds company job boards for you. When a job from your searches links to a company's own Greenhouse, Lever, Ashby, Workday or SmartRecruiters page, Kestrel starts reading that company's board directly, once a day, so you see all of its openings first-hand. You can turn any company off on the Sources page, and it stays off.
- The About page has a Check for updates button, with when it last checked, and a Release notes link beside the version number.
- A Mock interview link on the prep pack and on the role page, so you can reach a mock interview without typing its address.
- `kestrel-cli research "<company>"` now runs the same company research as the app, for a company Kestrel already knows. Results built from fewer than two sources are labeled insufficient instead of summarized.

### Changed

- Job searches now ask Indeed and Glassdoor. Google Jobs returns nothing through the search library, so it is no longer asked by default.
- Changing your profile rules (seniority floor, excluded titles, pay floor, locations) now gives the roles they set aside a second look, and the save says how many.
- Clearer wording throughout. Kestrel says "you" instead of "the operator", "1 page" instead of "1 pages", and shows local dates and times. Pages and buttons use plain names: "My stories", "Offers", "Use for deeper reads", "Couldn't score", and a plain hardware description. The sidebar names the model that is running in plain words.
- The confirmation before a deeper read names the online service your text goes to, says what is included, and still shows the full text that will be sent.
- The Offers page shows money in whole dollars, ranges as "$178,500 to $210,000", competing offers by company and role, and "not enough data yet (0 of the 5 needed)" when there is too little to compare. A walk-away amount you type is formatted, but never changed.
- The About page and README credit all seven models Kestrel was built with: Claude, ChatGPT, Qwen, DeepSeek, NVIDIA Nemotron, Space Bunny and LongCat.

### Security

- Updated libraries to fix known security issues.
- Error messages no longer show file paths or technical details; those go to the log instead. Text typed into forms is escaped wherever it is shown back.

### Fixed

- A role added to your shortlist by mistake can be taken off again: Remove from my shortlist, on the role page and in the list's side panel, undoes the add completely: the role leaves your board and your stats, and Kestrel forgets the "good fit" it learned from the add (for adds made from this version on). It works at any stage, so a role tracked by mistake can be removed too.
- Backups no longer contain the LAN shared key, and restoring a backup keeps the key this computer already has. On a different computer, set up LAN pairing again.
- In confidential mode, your current employer is now hidden everywhere on the Offers page too: in the competing offers, the pursuit picker, the offer terms you typed, and the negotiation scripts.
- `kestrel-cli bench` works in the installed app, and uses your own labels after `kestrel-cli goldset regen`.
- Messages about online models no longer show internal names such as "frontier" or "tier2".
- The Warm paths section no longer suggests importing LinkedIn connections, which Kestrel cannot do.

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
