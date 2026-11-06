# Changelog

All notable changes to Kestrel are listed here, newest first. The format follows Keep a Changelog.

## [Unreleased]

### Changed

- **Diagnostics no longer carry network addresses.** The Local AI connection check names the addresses Ollama connected to; a saved diagnostics file now shows "a public address: <redacted>" instead of the address and port, in every mode. The Local AI notes list each shipped model with its size and checked digest, and point to the Ollama library for who made it and its license.
- **Models page counts roles, not copies.** The labeled count and agreement for each model count a role posted on several boards once, and fewer than five shows as not enough yet.
- **A setting changed while a run works says so.** A change to where the models run, the model, reasoning or parallel requests applies to the next run, because a run keeps what you confirmed. The save now answers "This run is still using the model you confirmed, as confirmed. Your change applies to the next run" with a Stop the run button, and the sidebar and Today's run card keep showing what the run is really using until it ends.
- **Run notifications say what happened.** A run that ends with roles needing another try raises one warning under Something needs attention that opens Needs another try, and a role that fails again on a later run is not announced again. New roles worth a look names the role when there is one ("1 new role worth a look: Title at Company"), shows how many are strong, and opens the role or the new roles in Pipeline. The switch is now called New roles worth a look; your saved choice still applies.
- **Runs that finish inside the app now notify.** Only the command line's run raised these notifications before.
- **Models page, online mode.** When Kestrel runs online, the Health check and Models on this computer cards are greyed and say "Not needed while Kestrel runs online." Rows from an earlier local check are hidden, not shown as current. There is no Run check, Fix it or Download button, and the server refuses to start local work. Switching back to this computer or Both runs one new check.
- **Can read for an online step** says "Set by the online model" instead of a local verdict. Fix it on a Scoring steps row now answers beside the click.
- **Local AI check on the Health check card.** Backend address, Ollama's bind address, cloud model tags, Ollama's cloud features, connections and model digests show there with their real states, and a check that could not run says Unverified. Connections are now sampled while the model tests run. A firewall tip and a Local AI notes guide are added.
- **Select all works on a whole Pipeline group.** After Select all ticks the rows on screen, the bar offers "Select all 40 in Maybe". The server resolves the group when you act, so a role that left it is untouched, and a group that changed since you selected it changes nothing. The bar's actions carry the count ("Not for me, 40 roles"), one Undo reverses the whole action and names the number, and Deeper read stays per role. One action changes at most 200 roles; the bar and the toast say so, and nothing is cut silently.
- **Bulk decisions no longer teach Kestrel.** Shortlist, Not for me and Mark reviewed on a selection still apply to every selected role, but only decisions made one role at a time feed calibration, examples and the reranker.
- **Local AI privacy is stated with its limits.** The Models card now reads "Private when Ollama is set up as shipped", with Show details saying what Kestrel does, what only your Ollama controls (updates, its cloud models, other machines, how a model was trained), and how to keep everything on this computer. Setup, Settings and the Help article carry the same qualified line, and the outbound-calls document has a "Local AI and Ollama" section.
- **Suggested companies check themselves.** On Sources, each suggested company is checked in the background for a public job board, a few at a time and no more than once in 30 days. Its row says Checking, then Found on Greenhouse (Add), No board Kestrel can read (Add by hand, Check again) or Could not check (Try again). Kestrel reads the board from your role's link or its duplicate copies first, without any request. Only public pages are read, and nothing is added until you press Add.
- **I applied on any role.** The role page can mark a role applied even if you never shortlisted it, with quick picks for Today and Yesterday, a Follow up on date (a week later by default), then Undo and Edit date. A date in the future is refused. A follow-up that has already passed reads "Follow-up overdue".
- **Undo after Edit date on the role page.** Undo and Save dates no longer race: each waits for the other, so a quick Undo after changing the dates is no longer lost while the page still showed the role as applied. Undo also stays available after Kestrel has written the likely interview questions, and it takes back a question list that is still waiting to be written.
- **Resume used, the Pipeline panel and re-posts for I applied.** The role page's I applied form asks which resume you sent (the documents made for that role) and shows it afterward; Applications lists it too. The Pipeline side panel has the same I applied control, with Undo and Edit date. A saved application says that re-posts of the role are hidden, with the Show new listings switch, which now confirms each change.
- **Suggested titles from an older deleted resume.** Suggestions saved before Kestrel linked them to a resume are now matched to a deleted resume by the bullet they quote, so that resume's words do not stay on Profile while another resume remains.
- **A cover letter shows the words it was written from.** Each claim's source in the check view is the text that was sent when the letter was drafted, with a note when that proof point, resume line or story has changed or been deleted since. Two sources sharing one key are refused before anything is sent, so one can never show as the other.
- **The sidebar opens over the page.** Opening the icon rail draws the full sidebar above the page without moving anything, then closes by itself after a few idle seconds, on Esc, on a click outside or after you pick a page. Works at every window width, the smallest included.
- **A pushpin keeps the sidebar open.** Pin it and it takes its place beside the page; Kestrel remembers it, and every page opens that way with no flash.
- **The sidebar rail keeps the track switch.** Collapsed, it shows one letter per track, with the full name on hover and for screen readers.
- **Ctrl B (Cmd B on a Mac) collapses or opens the sidebar.** It is listed in the toggle's tooltip and in Help.
- **Toasts and the Profile save bar follow the sidebar** as it changes width.
- **Cover letters can draw on your stories.** Stories you wrote or edited in the story bank can now back a cover letter, next to your proof points and resume lines. Kestrel sends only the one or two most relevant, and only one action line and the result of each. A story Kestrel proposed but you have not saved is never used. Select a story-backed sentence to see the story behind it.
- **Cover letters keep to their length.** Kestrel sends at most three of your lines and two stories per letter. A draft over 350 words or four paragraphs is asked once to shorten (the same one extra request the ask-first panel already names); one still too long is kept and marked "Over length". The editor warns above 350 words and will not save above 600. Preview checks that the PDF fits on one page before you save it.
- **Draft again keeps your draft.** A new draft now waits beside the one you have, which stays in Documents and on the application until you choose Keep new draft or Keep previous draft. After keeping the new one, Undo (it asks first, since it deletes the newer draft and any edits to it) brings the previous draft back exactly as it was.
- Letters now have their own place in the database. Letters saved by earlier versions are moved over when you update and open as before.

### Fixed

- **A fast press on a Settings form sent unticked boxes.** The entry motion draws ticked boxes unticked for two frames after a card opens; a press in that window, such as Add on the import review, sent nothing ticked and added 0 applications. Any press, key or submit now puts the real values back first (D-UI-262).
- **The safe-mode offer only appears when it can help.** A start that fails because your data was saved by a newer Kestrel, or could not be opened, now shows its own message every time instead of offering to start with optional features off.
- **Deleting a resume keeps title suggestions another resume still backs.** A suggested title that quotes both the deleted resume and one you kept stays as it was, and deleting a capability statement no longer removes any suggested titles.
- **A paused run keeps the settings it was started with.** After a pause, the run in progress no longer picks up a model or parallel-requests change made in the meantime, as the notice says, and a run that starts from a scheduled search now shows the notice too.
- **A cover letter's source is never called deleted when it was not.** If another of your sources takes the same key after a draft, the check view now says so instead of "Deleted since this draft", and still shows the text the draft was written from.
- **A failed I applied leaves nothing behind.** If saving I applied on a role you had not shortlisted fails, the role is no longer left on your shortlist with its learning labels while the page says nothing was recorded.
- **Undo of I applied on a shortlisted role takes back the interview questions too.** The question list the application queued is canceled while it waits, and a list already written for it is removed, so the role is exactly as it was.
- **Check my models asks before it lists online models.** With Include online models ticked, Kestrel no longer requests the provider's model list until your confirmation for that provider is in place, and the tick only confirms the provider the card named.
- **Diagnostics hide every form of an observed address.** An IPv6 address written with its port and no brackets, an address joined to a word (`ip_8.8.8.8`), and an address written with leading zeros are now masked too, and the Local AI connection check writes IPv6 addresses in brackets.
- **A Remotive job with no description reads as having none.** It was stored as an empty description instead.

- **A failed assistant connection switches itself off.** If the AI assistant (MCP) server cannot start when Kestrel opens, Kestrel turns the switch off and Settings says why: "MCP was turned off because it could not start: ... Turn it on again to retry."
- **Safe mode.** After two starts in a row that did not finish, the loading window offers "Start with optional features off". It opens Kestrel with your data and roles as they are and leaves the assistant connection, update check, notifications, schedules, model setup and keep-awake off for that run.
- **Access from other devices.** "Takes effect after Kestrel restarts." shows once, the title no longer runs under its switch on a narrow window, and if the QR code cannot be drawn the card says to copy the address.
- **Kestrel always starts, whatever happens to the AI assistant connection (MCP).** If the MCP server cannot load, Kestrel still opens, Settings says in plain words that it is not running, and the technical detail sits under Show details. Before, a saved MCP switch plus a failing server could stop Kestrel from starting at all.
- **The installed app now ships the package information the MCP server needs to start.**
- **The AI assistant card shows the right address.** The local address is labeled "this computer only". With LAN access on, the card also shows the address on your network, skipping VPN and virtual adapters, with its own Copy button; with it off, the card links to LAN access.
- **The shared-key note reads cleanly.** The key name no longer collapses into a narrow column over the next line.
- **The phone pairing code is no longer cropped.** The QR code now scales whole, with the standard white margin on every side.
- **Remotive roles stay on this computer.** Exports (applications file, ratings, command-line export, diagnostics) and the read API and MCP tools leave out roles that came from Remotive, as its terms ask. A role also found on another board is shared from that board's copy.
- **Source credit on every Remotive, Arbeitnow and Jobicy role.** The Posting tab reads "Source: <board>" with a link to the board.
- **A Remotive reply Kestrel cannot read** now shows as a failed check with a plain sentence, like the other boards, instead of a quiet empty run.
- **The online balance never slows the sidebar.** A slow or failed balance read no longer delays the status polls, and it never reads as zero.
- **An older Kestrel on newer data names the version.** The window says which release saved the data when Kestrel can tell, and "a newer Kestrel" when it cannot.
- **Calibrate asks about a role once.** A role posted on several boards is one role: once you rate or override any copy, Calibrate never asks about another copy, and the sidebar count is the number of roles it can still ask about. Calibration and your sorting model count each role once (your most recent rating of it), and every count of ratings is a count of distinct roles. Nothing stored is merged or deleted.
- **Applications table.** A third press on a column header returns to the default order, the Date applied column follows your date format and hides on a narrow window before anything overlaps, and Stage, Fit and Applied sorts are covered by tests.
- **Interview prep tabs.** Changing tabs keeps the tab row in place: the underline slides, the pane eases in and the help cards arrive one after another. The Story bank checks for changes while a build runs and leaves the page alone when nothing is new. Reduced motion turns the motion off.
- **Profile title versions are searched fairly.** Abbreviations and spelled-out forms (such as VP and Vice President) are searched first, one title at a time, so every title gets its turn before the cap of 9. Each chip names the title it came from, a note lists titles with no searched version, and the help text states the real rule.
- **Names in parentheses are searched properly.** "Company (Qualifier)" now looks up the plain name first, then the names inside the parentheses, instead of a joined name that never exists.
- **A company covered under a variant name is not suggested.** Parentheses and endings such as Inc, LLC, Ltd, Corp, Co and GmbH are ignored when Kestrel compares names with your sources.
- **Add updates the row at once.** When a lookup finds no board or could not answer, the row changes on the first press instead of the second. A lookup that could not answer is never remembered as "not found".
- **One count for decisions.** Today's Decisions waiting and the Pipeline Your call chip now use the same rule, so a Maybe role you have not decided counts on both. The chip also includes shortlisted roles you have not applied to, and a role first seen today waits in Top matches until tomorrow.
- **Suggested titles after a resume is deleted.** With no resume, the card says to import one and ignores leftover rows. Deleting a resume removes its waiting suggestions and the resume wording stored with the rest, and keeps dismissed titles as bare titles so they do not come back.
- **Window controls are whole in the collapsed rail.** Minimize, maximize and close were cut off by the frame edge at some widths.
- **The sidebar toggle always matches the sidebar.** When the window made it collapse by itself, the arrow pointed the wrong way and the first click did nothing visible.

## [0.7.5] - 2026-11-04

Cover letters are rebuilt and written in your own voice, every online send waits for your tick, and setup checks report honestly.

### Changed

- **Cover letters are rebuilt.** Every tab of the letter page has an action bar with Draft again and Discard, and Save on the Edit text tab. The preview fits the page, and Download PDF is one click. A letter is written in the first person, opens "Dear Hiring Team," and ends "Sincerely," with your name. Anything in a draft your notes do not support is marked for you to check. Engagement outlines use the same design.
- **Drafting a letter survives a restart.** A letter being drafted is saved as work in progress, so closing Kestrel mid-draft picks up where it left off.
- **Every online send waits for your tick.** On every panel that asks before sending to an online model, Send is at full strength; pressing it without ticking the box shows a hint and sends nothing.
- **I applied is on the role page.** Mark a role applied without leaving it.
- **The setup checklist refreshes** as you finish each step.
- **Score again tells you what happened.** Pipeline names each role's outcome, keeps your picks ticked, and adds your request to a role already waiting. A role that needs your OK to go online reads "Waiting for your OK" with Confirm, on Pipeline and Today.
- **A posting with no description says so** instead of staying silent, and a thin copy of a role links to the copy Kestrel already scored from another board.

### Fixed

- **Local AI checks report honestly.** The checks ask Ollama only when it answered, never call Kestrel's own derived models "unknown", and compare model digests correctly.
- **Error pages show plain sentences.** No page or doctor report shows an internal error name in its main text; technical detail stays under Show details.
- **Run-finished notification, re-import and duplicates.** The run-finished notice is reliable, re-imported possible duplicates are flagged, and calibration skips duplicate decisions.
- **Remotive** is fetched within its published limits and credited as its terms ask.
- **Polish.** The sidebar, Applications sort, Select all, Today and narrow Documents rows behave as expected.
- **Suggested companies keep their layout** while Add looks the company up.

## [0.7.4] - 2026-11-02

Today counts quiet applications by your own calendar day, the online ask says when Kestrel may send a second request, and more drafts mark wording to check.

### Changed

- **More drafts mark stock phrasing.** Mock interview feedback, likely questions, the company summary, the analytics summary, negotiation drafts and follow-up drafts now show the same small mark stories have when a phrase reads as stock wording. Kestrel never rewrites the text.
- **Names in stories are checked against your resume.** A company, person or title in a story draft is marked for you to check when it is in none of your notes, proof points or resume. Kestrel does not ask the model to change it, and your resume is only compared on this computer, never sent.

- **The online ask names the second request.** Before a story build, likely questions, practice, a negotiation or follow-up draft, the analytics summary or a company summary goes to an online model, the dialog now says that Kestrel may send one more request to the same model, with the same material plus its first reply, if the reply needs plain-writing fixes. Scoring and import never send this second request.

### Fixed

- **Quiet applications are counted by your local day.** An application last touched in the evening could read as one day less quiet than it was, because the count used the UTC date. It now uses your own calendar day.
- **A refused second request leaves nothing behind.** If the second plain-writing request is refused before it is sent, the first reply stands and no refusal or failure entry is recorded.

## [0.7.3] - 2026-11-02

Pipeline explains every role without a score, drafts read plainly, the bell shows a real count, and Negotiate helps before an offer arrives.

### Changed

- **Pipeline says why a role has no score.** Each one carries a chip: Waiting, Paused, Scoring, No description, Hidden: applied, or Taken off retry list. The three that nothing will score on their own now sit in a second group, "Won't be scored unless you act" (it was "Not scored", which read as "not scored yet"). A role with no description has a **Paste the description** button: Kestrel checks the text against your rules and scores it.
- **Drafts read plainly.** Cover letters, story drafts, mock interview feedback, negotiation notes, the analytics summary, interview questions, company summaries and follow-ups follow plain-writing rules. A draft that breaks them is asked again once, and a story draft marks any detail your notes and proof points do not support so you can check it. Your own words are never changed, and existing scores are not affected.
- **The notification count on the bell is now a real badge.** An 18 px indigo pill with a ring that separates it from the bell, readable in light and dark and in every color scheme. It shows 1 to 9, then "9+", and disappears at zero. A screen reader hears "Notifications, N unread" with the true count. The bell itself is a darker amber in light mode so it clears the contrast minimum. The count ticks gently when it changes, and not at all if you reduce motion.

### Fixed

- **Negotiate works before an offer arrives.** The Negotiate tab no longer repeats the Offers empty screen. It shows your floor and target from Profile, what to ask for beyond pay, and the drafts Kestrel can write for you.

## [0.7.2] - 2026-11-02

Local AI models stop thinking out loud, resume import keeps every line, and several screens hold steady under repeat use.

### Changed

- **Local AI models no longer think out loud.** Kestrel now uses corrected models. An existing install downloads one corrected model of about 2.5 GB the first time it is needed, and nothing already installed is removed.
- **Diagnostics keep the events that matter.** A diagnostics file now holds the whole recent log, plus every warning and error from earlier, so a problem from an earlier run is still in it.
- **Stop reacts at once.** Pressing Stop on a run shows "Stopping" straight away, and a second press does nothing extra.
- **Long model calls show progress.** While a slow model call is in flight, Runs shows what it is doing, so a long wait no longer looks like a stall.

### Fixed

- **Resume import keeps every line and finds more roles.** Import uses your scoring model, no line of your resume is dropped, a role header the first pass misses is checked again, and a wrapped line stays as one line. A small local model can still miss a role in a long resume, so check the roles before you save.
- **Import shows what it is doing.** A progress panel appears when an import starts, and a failure says what happened and offers Try again, without raw error text or addresses. Try again on an online model asks for your consent again.
- **Confidential mode hides every current employer.** If you hold more than one open-ended role, each of those employers is hidden, not only the newest.
- **Setup warns about a bad API key.** A key that does not work now shows a message instead of appearing to succeed.
- **Switching to a local model asks for your computer's setup.** Settings no longer says "0 of 0 ready" and marks downloads done when no models are chosen yet.
- **Local model loads recover.** If a model fails to load while another is in memory, Kestrel unloads the others and tries once more before it reports a plain message.
- **The local model check judges the reply.** A chatty answer or a server error is no longer reported as lost context, and the check no longer passes a reply that is really reasoning.
- **Open folder works on Windows.** After saving a diagnostics file, Open folder (Windows) opens the folder with the file selected, or says where the file is if it cannot.
- **Runs holds still.** The page no longer jumps every five seconds, and the top loading bar moves only while Kestrel is working.
- **An empty Work authorization field looks empty.** It no longer looks filled in with a sample answer.
- **One save message on Profile.** A save shows a single confirmation, and toasts clear on their own instead of covering the save prompt.
- **Calibrate saves do not fail twice.** A save made while Kestrel is busy shows at most one plain message, and "Not enough data yet" no longer carries the "(n=2)" note.
- **The story sheet works every time.** Long stories scroll inside the sheet so Cancel and Save story stay in reach, reopening after Cancel works, and saving keeps your place on the page.
- **Send and assess opens clickable.** The consent dialog responds to the mouse, and the assessment shows that it is running.

## [0.7.1] - 2026-11-01

Scoring now uses the proof points from your resume, setup tells you the next step, and you can see when a role is being scored.

### Fixed

- **Import past applications** works again with a large tracker. A file over about 1 MB made Next do nothing; it now goes through every step, and the same fix covers importing your connections.
- **A press that fails always says so.** When Kestrel cannot complete something you pressed, a message now says nothing changed, instead of the page staying as it was.
- **Roles you already applied for no longer say "Being scored".** Roles brought in by Import past applications, and re-posts of roles you applied to, are hidden and never scored. The role page, the Pipeline panel and Applications now say "Not scored" and why.
- **Where roles come from on Analytics** no longer runs past its card when the window is narrowed to just above the size where the cards stack. Long source names and the reply rate wrap instead.
- **The "On my shortlist" button on a role** no longer cuts off the column name mid-letter. A long one such as "Interviewing" now ends in "…" when the column is narrow, and hovering shows the full label.
- **Needs another try** no longer keeps a failure you cannot clear. A failure that did not come from scoring a role, such as job title ideas, names its step, says where to run it again, and can be removed.
- **Proof points from your resume now reach scoring.** Accepting a resume import used to drop its proof points without a word, so every role was scored as if you had none. The review now lists each proof point to tick, and the ones you accept are saved to your profile.
- **"Why it is a weak match" says why.** A role that is not a fit now leads with what held it back, in the model's words, instead of the reasons in its favor.
- **Build stories no longer fills the list with empty drafts.** Write-ups with nothing in them are skipped, a story the model could not build is not saved, and **Clear all drafts** (with Undo) removes the drafts you have. Stories you saved are never touched.
- **A posting with no description is not scored.** It shows as "Not scored" with the reason, and **Score anyway** scores it if you judge it enough. A re-scrape that brings the description scores it as usual.
- **A job board's pay estimate is labeled as one.** Glassdoor's estimated pay shows as "est.", never rules a role out on pay, and is kept out of market pay figures.
- **Score again on Today and Profile scores.** With an online model it asks for your go-ahead right there and names every online service and what it receives, instead of sending you to Pipeline.
- **No saved profile means an empty profile.** Kestrel no longer scores roles against the built-in example person when you have not set up your own. A profile file that cannot be read says why, is never saved over, and can be started fresh with the old file kept.
- **Today counts every match** in its heading again.
- **The Suggest titles box** no longer drops a click made while it is still opening.

### Changed

- **See which titles a scan skipped.** On Runs, a scan that skipped unrelated titles now has Show details under its skipped line, listing up to 25 of them with their companies so you can check the filter did not leave out a good role. Diagnostics bundles never include them.
- **Tap or click a card on Today** to see the role summary, the same as pressing Review role. Swiping and dragging the cards work as before.
- **Kestrel tells you the next step to set up your profile.** Today and Profile name one step at a time: add your resume, review its proof points, add the titles you want, then score again the roles scored before your proof points. When your profile has no proof points, the rows Kestrel cannot check say how to fix that.
- **See when a role is being scored.** The role page, Pipeline and Today show "Being scored" or "Being scored again", with its place in line, and the new score replaces the earlier one in place.
- **Delete a resume**, even the last one, from Profile > Documents. The choices Kestrel learned from it are kept unless you tick "Also forget". A resume you sent with an application stays, so the record of what you sent is kept.
- **New story drafts send a notification** when Build stories makes them, following your Quiet hours.
- **Documents keeps the editor and preview side by side** down to smaller windows, and the import panel sits under Optional sections.

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
