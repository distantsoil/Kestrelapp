# Changelog

All notable changes to Kestrel are listed here, newest first. The format follows Keep a Changelog.

## [Unreleased]

### Fixed

- **Import past applications** works again with a large tracker. A file over about 1 MB made Next do nothing; it now goes through every step, and the same fix covers importing your connections.
- **A press that fails always says so.** When Kestrel cannot complete something you pressed, a message now says nothing changed, instead of the page staying as it was.
- **Roles you already applied for no longer say "Being scored".** Roles brought in by Import past applications, and re-posts of roles you applied to, are hidden and never scored. The role page, the Pipeline panel and Applications now say "Not scored" and why.
- **Where roles come from on Analytics** no longer runs past its card when the window is narrowed to just above the size where the cards stack. Long source names and the reply rate wrap instead.
- **Needs another try** no longer keeps a failure you cannot clear. A failure that did not come from scoring a role, such as job title ideas, names its step, says where to run it again, and can be removed.

### Changed

- **Tap or click a card on Today** to see the role summary, the same as pressing Review role. Swiping and dragging the cards work as before.

### Security

- **PDF reading library updated** to pypdf 6.21.4, which closes seven reported problems where a malformed PDF could make reading it very slow or use a lot of memory.

## [0.7.0] - 2026-10-30

The Release Candidate for 1.0: every screen redesigned, smoother page changes, more places to find roles, and security fixes.

### What's new

- **A new look.** Every screen is redesigned, with a frameless window, a command palette (Ctrl K or Cmd K) and gentle motion that your system's reduce-motion setting turns off.
- **Smoother.** Pages change in place without reloading the window, and the setup wizard moves smoothly between steps.
- **More places to find roles.** USAJobs and Adzuna (each with your own free key), Himalayas and We Work Remotely for remote roles, Recruitee, Teamtailor, Personio and Breezy HR, and any careers page that publishes its jobs for search engines.
- **Fewer irrelevant roles.** Roles outside your area or unrelated to your target titles are dropped before they cost time or money.
- **Alerts on your terms.** An alert sound of Kestrel's own, and Quiet hours that hold pop-ups and send one summary afterward.
- **Safer.** Bundled libraries updated past their known vulnerabilities, and error messages that never show file paths or other internal details.

### Added

- A frameless window with minimize, maximize and close in the sidebar (macOS keeps its own buttons), a thin themed frame you can resize from, double-click to maximize, and a window that reopens at the size and place you left it.
- A loading screen at start, "Updating your data" after an update, a clear "Kestrel couldn't start" card with Try again, and a "Closing Kestrel" screen when you quit.
- A command palette (Ctrl K, or Cmd K on a Mac) for roles, companies, settings and actions.
- Gentle motion on page changes, tabs, folds, saves and presses; your system's reduce-motion setting turns it off.
- USAJobs, a free source of US government jobs: tick it when adding a job board search, paste your own free USAJobs key and email, and Kestrel searches the official listings. It sends USAJobs only your search words, place, key and email.
- Adzuna, a job search board with your own free keys, and two remote job boards that need none: Himalayas and We Work Remotely. Tick them when adding a job board search. Adzuna sends only the start of each description, so those roles say so. Ticking two boards at once and pressing Undo now removes both.
- Any careers page, not only the named systems. Paste a link and Kestrel reads the job data the page publishes for search engines (schema.org JobPosting), including pages that only link to their job pages (up to 25 a run). A page that builds its list with JavaScript is told plainly instead of showing zero. A link to a hiring system Kestrel already reads still uses that reader.
- More company job boards: Recruitee, Teamtailor, Personio and Breezy HR. Kestrel follows it from your searches like the others, and a careers page on the company's own domain is recognized too. A Teamtailor site that has switched its job feed off is reported plainly instead of failing. Personio roles with no description in its feed get theirs from the job page, within a per-run cap, as Breezy HR roles do from their posting pages.
- An alert sound: with **Play a sound** on in Settings, Kestrel plays the sound and volume you picked with each pop-up it delivers (its own sound, never the system's, and it works with the window hidden), at most once a minute and never during Quiet hours.
- Quiet hours work: pop-ups (on this computer, by phone, or by webhook) wait until the quiet hours end, then arrive as one summary. The bell in Kestrel still updates.
- **Classic page loads** in Settings > Preferences: a fallback that loads each page fresh, for anyone who prefers it or hits a problem.
- The interface font (Manrope) ships with the app, so text looks as designed offline.
- A manual workflow that exports the open security alerts, for maintainers.

### Changed

- The sidebar, the top row (which now scrolls with the page), the scene art and the background follow the mockups in light and dark, and layouts fit the window instead of stacking early.
- A requirement your profile does not show is "Partly met" and listed under Gaps, never counted in your favor; reasons are plain sentences.
- Pipeline lists scored roles first; one role found on several boards shows once.
- Messages point at the current Settings sections (Privacy and access, Your data).
- The About page and README credit MiniMax M3 (formerly listed under its stealth name, Space Bunny) and add Kimi K2.7 and MiMo: Claude, ChatGPT, Qwen, DeepSeek, NVIDIA Nemotron, MiniMax M3, LongCat, Kimi K2.7 and MiMo.
- Moving between pages no longer reloads the window: the sidebar, top row and header art stay put and only the page changes. Back and Forward work as before, and unsaved Profile changes still hold you on the page.
- Roles outside your search area, and roles whose titles are unrelated to the ones you target, are dropped before they are stored or scored. "New York - Remote" style listings still pass.
- Top matches shows fully reviewed roles only; roles still being reviewed are a count under the heading.
- Interview prep keeps its three tabs (Story bank, Practice, Prep pack) on every tab.
- **Run now** on Today respects your online-model choice and approval the first time.
- Work mode and Location agree on remote and hybrid in Profile.
- Kestrel's requests identify it with a link to kestrelapp.co, and About links the public source repository.
- The browser extension no longer runs on LinkedIn.
- The release stage is named Release Candidate.

### Fixed

- The **Computer notifications** switch and the per-alert switches in Settings now take effect for pop-ups (they were saved but not read).
- Windows pop-ups no longer play the system sound.
- The setup wizard's steps slide over each other, so the screen is never blank between steps.
- Sheets and dialogs open in view on a scrolled page, and when a page is still arriving.
- The command palette keeps keys typed before it opens; pages load faster after an update; the sidebar menu scrolls above the status line on a short window; no grey window during seasonal art; Role tabs fade in without a flash.

### Security

- Updated the image, PDF and sign-in libraries Kestrel bundles to versions without known vulnerabilities.
- Error messages on screen never include file paths, addresses or other internal details; the log keeps them.

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
