# Changelog

All notable changes to Kestrel are listed here, newest first. The format follows Keep a Changelog.

## [Unreleased]

### Fixed

- **Every reason a role could not be scored now reads in plain words.** A provider's safety refusal, an online send that was turned off before it ran, and an unreadable privacy setting no longer show the generic "did not finish" or a wrong "choose your current employer"; the Pipeline row and Needs another try say the same thing. A new test fails when a reason Kestrel writes has no words.
- **Refused online sends say "Not sent" everywhere.** Needs another try, Documents suggestions and the resume assessment no longer tell you to choose your current employer when the real reason was that online sending was turned off or your privacy setting could not be read.
- **Startup waits out a clock change.** Opening Kestrel waits up to 30 seconds for its server; that wait is now measured on a clock that cannot jump, so a time sync or waking from sleep during startup no longer cuts it short.

### Changed

- **Security lint on the whole tree.** `ruff check` now includes the security rules (flake8-bandit). Every accepted finding says why, either in `pyproject.toml` for tests, scripts and migrations or on the line itself in app code; the stdlib XML parsing of fetched feeds is marked for a decision.
- **CodeQL runs before the public push.** The maintainers' full ship check now runs CodeQL (Python, JavaScript and workflow files, the same queries as GitHub code scanning) on the exported tree and stops on any alert not in a reasoned allowlist.
- **A stress run in the full check.** The tests a change affects run five times in parallel in random order, with the app's usual port already taken, with a fast clock, and just after and just before midnight UTC, so a test that only passes alone fails before a push. Every check tier also lints the public copy of the code.

## [0.7.6] - 2026-11-12

### Added

- **The role page shows where each match comes from and lets you correct it.** A match with no line of your resume or stories behind it carries a small "Inferred" tag that opens Kestrel's reasoning. A pencil on each match, and "I've done this" on each gap, opens a short panel in the row: it's overstated, not true of me, they mean something else, or all or some of it with the story that shows it (or a new one written in the same sheet). One line says what will change before you save; saving slides the row to its new list and shows Undo. Your view then says how many things you changed and offers "Score again with my changes", which opens the usual dialog: an online model still asks first, and the dialog lists the corrections it would send. After a correction about you, Kestrel lists the other roles that ask for the same thing and offers to rescore them. Evidence is collapsed with a count, shows every line, and now compares against every story and resume line. Nothing you type here is added to a resume.
- **Kestrel keeps your corrections to what it concluded about a role (groundwork).** A new store holds each correction you make to one thing a posting asks for, applies one about you to every role on the track that asks the same thing, and keeps one about a single posting on that role. The role page's match wheel and counts already use your corrected states; the role page now has the buttons to make and undo them (below). The stored read is never changed, and a correction never hides, rejects or rescores a role.
- **A quick round of yes or no cards.** After a finished run, Today can offer 10 roles to sort in about a minute: Yes, Not for me, Top pick or Not sure, by buttons, the arrow keys or by dragging the card. Kestrel's score stays hidden, each answer is saved on this computer as one of your own examples and counts toward the 15, 30 and 200 steps, and Undo removes the last one. Not now on the strip snoozes it for 3 runs or stops asking, each with an Undo, and Calibrate has the switch and how often. Calibrate now has Quick and Detailed tabs; Detailed is the page as it was, and its history marks Quick answers.
- **A security model document** describes who Kestrel defends against and how.
- **4 Day Week job source.** Sources > Add a source > Job board search now offers 4 Day Week, a board of employers with four-day or reduced-hour schedules. It reads the board's public API, brings each role's full description and pay when given, links to the role's 4 Day Week page where you apply, and credits the board.
- **Hacker News "Who is hiring?" job source.** Sources > Add a source > Job board search now offers Hacker News, which reads this month's thread through the official API and turns each company's comment into a role, with the comment as its description and its link as the place you apply.
- **Jobvite company job boards.** Paste a company's Jobvite careers link (jobs.jobvite.com/company) or its job feed link and Kestrel watches its open roles, reading each role's full description from the company's public Jobvite job feed.

- **Export a work search log from Applications.** An "Export work search log" button opens a sheet with a date range (This week, Last week, This month, or your own dates) and the count the file will hold, then saves a CSV of role, company and applied date for every application in that range, including rejected, withdrawn, no-reply and closed-posting ones. Roles you never applied for are left out, applications with no date are listed at the end, and Confidential mode hides your current employer without blocking the file. The file is saved on this computer only and nothing is sent.
- **Drafts and story builds run while the online work goes on.** When the model that writes your cover letter or builds your story is an online model, that writing runs in its own loop at the same time as the board searches and scoring, instead of waiting behind them. When those models run on this computer, nothing changes. When neither online model is in use the lane rests between checks (about 15 s) instead of every 2 s, and returns to 2 s the moment one of them is.

### Changed

- **With no corrections, scoring sends exactly what it sent before.** The new scoring instructions add your corrections only when there are some; otherwise the request is identical to the previous version, so scores are not shifted by formatting.
- **Scores are marked older only when your corrections would change them.** After this update a score from before it reads "Scored with older scoring instructions" only on roles where one of your corrections applies, so with no corrections nothing needs scoring again. A score is marked older exactly when the corrections its role would send changed since: adding or changing one marks exactly the roles it applies to, removing one the score was given marks that role, and removing one added after the score clears the mark. The role page's "You changed N things" strip opens on the same roles. Pipeline, the role panel, the role page's score card and the rescore offers show and count the same roles.
- **Scoring a role again now takes your corrections into account.** The full review reads the facts you have stated about yourself on this track, and what you said a posting means, as a short list beside your profile: only the ones about what this posting asks for, and the notes you added to this posting (at most 20, newest first). A correction about something this posting does not ask for is never sent with it, so it cannot lower an unrelated role's verdict. A role's first score has no read to match against yet, so it carries that posting's notes and the corrections whose wording appears, as whole words, in the posting's own description (a very short one such as "C" or "Go" waits for the first read); after that, the read decides, and if the read lists a correction the first score left out, the role shows as scored with older instructions until you score it again. Nothing else about how roles are scored changes. Because this is a new version of the scoring instructions, every score made before it shows "Scored with older scoring instructions. Score again to refresh it." until it is scored again; your ratings, examples and calibration are kept. When scoring runs on an online model, these facts are part of what is sent; the preview of an online second opinion already shows them in the exact text sent.
- **An online send of a score asks again when your corrections changed.** Confirming Send and score on an online model now covers the corrections the dialog listed, not only the model. If you add, change or remove one before the score is sent, including on Try again, nothing is sent: the role waits for your confirmation and Score again shows the new list. A change made while the dialog is open is refused with a plain note. The deeper read dialog lists your corrections under What will be sent.
- **Hosted mode asks you to accept its disclosure again.** The full review's line now says that your corrections and their notes are sent, so online scoring waits until you accept the updated disclosure on Settings > Hosted.
- **A Corrections tab on Profile.** Profile > Corrections lists every correction you made to what Kestrel concluded about you, newest first, with a search box. Each card shows Kestrel's read struck through, your answer, why Kestrel thought so, your note, and the role it was added from. The pencil opens What's true now? to change the state or note, or to remove the correction; Save and Remove both have Undo, and after a change Kestrel offers to rescore the roles that use it through the usual dialog. Nothing is sent online and your resume is never touched. Confidential mode hides your current employer here too.
- **Kestrel keeps your corrections to what it concluded about a role (groundwork).** A new store holds each correction you make to one thing a posting asks for, applies one about you to every role on the track that asks the same thing, and keeps one about a single posting on that role. The role page's match wheel and counts already use your corrected states; the buttons to make and undo corrections come next. The stored read is never changed, and a correction never hides, rejects or rescores a role.
- **The Local AI check watches Ollama itself, not its tray app, and the new-roles alert says where its line is set.** The tray app checks for its own updates, which the check used to report as a model connection; it is now left out. The "New roles worth a look" switch now says it follows your Score cutoffs in Which roles you see.
- **Tier 2 runs faster on an online model.** When the full-review step uses an online model, it now asks OpenRouter models for no reasoning (the full review only: story builds, mock interview critiques and the analytics summary keep the model's own reasoning) and runs four requests at once, unless you set either yourself (Settings and the Models page still win). Models on this computer keep their settings.
- **One place asks for ratings.** The old "Teach Kestrel your taste" and "N more ratings" prompts were never shown anywhere and are gone; the quick-round strip on Today is the only ask, and it can be snoozed or switched off.
- **The Applications table sorts every application, not one page, and remembers the sort.** Pressing Role, Stage, Due, Date applied or Fit now orders all of your applications and keeps the order on every page of the table (the sort used to reorder only the 100 rows on screen). The order you chose opens already in place when you come back, together with Board or Table, and pressing a heading again with the keyboard keeps working after the rows move.
- **A read-only applications list for scripts.** A new address lists your applications with their Kestrel ID, role, company, source, applied date, status and last change, with a since filter and pages, so a script can update a tracker kept elsewhere. It needs the shared key off this computer, hides your current employer in Confidential mode, and only reads.
- **Every application has a Kestrel ID, and importing trusts it.** The Applications export now ends with a Kestrel ID column (a short, opaque ID that says nothing about the role or company), and a file with that column matches by ID even after you edit a title or company. An ID Kestrel does not know is added as new with a warning, a repeated ID is held until you fix the file, and a blank ID falls back to matching by role and date. After an import you can download a copy of your own file with the IDs filled in to keep as your tracker; your original file is never changed.
- **Importing your applied history again is safe.** A re-import now matches by role and date (an approximate date matches by month and never replaces an exact one), pairs each application with one row of the file, keeps an application that is further along or closed instead of moving it backwards (a switch lets the file win), counts what already matches instead of listing it, and groups the rest into what differs, what needs your call, what looks like an application made again, and what is new. Undo puts every change back in one step.
- **Pipeline and Analytics load only what they show, and the Applications board holds back old closed cards.** The Pipeline list no longer reads the posting text or the model's raw reply of each row and works out each role's group once instead of once per group; Analytics reads a few columns and counts the funnel in the database. On a very large database Pipeline went from 2.2 s to 0.7 s and Analytics from 1.9 s to 0.4 s. The Applications board draws every open card but only the first 100 closed ones in each column, with "Show more closed" for the rest, and the table shows 100 rows a page (D-PERF-3).
- **Applications search and filters cover every application.** On a long list a search or filter now finds a closed card that is not drawn yet or a row on another table page, "Show more closed" and the table pages are of the matches, and a change to a card on table page 3 stays on page 3.
- **Clearing out old work keeps what it must.** A role that came through the background lane stays local-only after its old tasks are gone, a role whose vector never got stored is still found and re-queued, and a removed scrape run also removes its raw file when that file is inside Kestrel's data folder.
- **Old finished work is cleared out once a day.** Finished background tasks are kept 30 days (a failed one stays while its role still needs another try), and run history and notifications are kept a year. Nothing open and no role, score, pursuit or note is ever removed, and quitting mid-clean-up is safe.
- **Faster role lookups.** New indexes on the task's role, a scrape run's roles and a role's failures (migration 0030).
- **Model servers on your home or office network.** A server on another computer on your network (an Ollama, oMLX, vLLM or llama.cpp server) now runs without Hosted mode and without the ask-first dialog, because it never leaves your network. Where a server runs is decided by its literal IP address, with no name lookup, so a host name or an address on the internet is still online whatever a settings file says; add a server by its IP address. Models has a new On your network card to add a server, test the connection, and pick its models for Quick screen and Fit rating, and the Models table, status rail and Today say "on another computer on your network".
- **Restore can bring your AI and connection settings along.** When you restore a backup, a box (off by default) restores the backup's model servers, online service, assistant connection and notice, search and update destinations too, and the sheet first lists the values it would set, including any address on the internet. Hosted mode and its accepted disclosure, which key each server uses, and your online confirmations, are never restored. Showing the list sends nothing and shows only the host of a notice or update address.
- **Faster status rail, role lists and startup.** Live-task checks use the task index (rail 10 s to 1.4 s, list 27 s to 2.4 s on a very large database), SQLite memory-maps the file, and the learning libraries load only when needed.
- **Faster sidebar badges, status rail and role pages.** The badge, rail and tray counts now come from one grouped query instead of loading every role in Worth a look, Previous and Next on a role page no longer loads the whole pipeline, and the parsed settings are reused until the settings file, environment or a saved setting changes (D-PERF-2).
- **The "needs another try" count says item(s), not role(s).** The open-failed total counts every open failure, including story-merge failures that have no role, so the sidebar readout, the Today retry note, the scoring progress panel and the run panel now read "1 item needs another try" / "N items need another try", and the readout title says "Something the model could not finish". The run segment and the run notification, which count only this run's failed roles, are unchanged (D-UI-264).

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

- **Errors from the applications API no longer include internal error text.** A refused query shows one of Kestrel's own sentences; the details go to the log (D-SEC-40).
- **Errors from Profile > Corrections no longer include internal error text.** A change that cannot be saved says so in Kestrel's own words; the details go to the log (D-SEC-41).
- **Saved learning model files are always named for one of the two tracks** (D-SEC-42).
- **A role whose posting cannot fit your local model now says so.** Needs another try reads "Too long for your local model" and the Pipeline row "too long for your local model", instead of the generic "Scoring did not finish" (D-SCORE-21).
- **Score again clears the older-score mark on a role that had a deeper read.** The deeper read stays the role's score, but the mark now measures against the newest score, so a Score again that sends your corrections clears "Scored with older scoring instructions" on the role page, its strip, the Pipeline row and the panel alike.
- **The role page's "You changed N things" strip opens whenever a score from older instructions has corrections to send,** so it agrees with the older-score line.
- **A story written from a correction's panel never queues a score on its own.** The correction is saved with the story first, and the page then offers Score again with your changes. Save and re-score never says Kestrel will score the role when that score is waiting for your OK; it says nothing was sent.
- **A long local full review keeps its safety margin with a short description.** When the prompt is close to the local model's limit, the description is shortened by what it actually holds, so the whole prompt fits.
- **A backup from an older Kestrel that cannot be brought up to date is refused in plain words.** Any failure while upgrading the backup's copy of the database now gives the "could not be brought up to this version of Kestrel" message with nothing changed, never an error page; a stored requirement that is not text is read as text instead of failing the upgrade (D-SEC-23).
- **Going back to an earlier version keeps every requirement correction.** When two corrections the earlier version could not tell apart (such as C++ and C#) exist on one track, the downgrade keeps both: one gets the earlier form and the other keeps today's, so nothing is merged or deleted (D-DB-3).
- **A restore or a reset no longer makes a model download or build run twice.** Only the work they stop hands back its place in the queue; a download or build in progress keeps going and finishes once. A backup that would be refused is now refused before anything is stopped (D-QUEUE-10).
- **Restore leaves the old computer's queue bookkeeping behind.** The record of roles already re-queued once, a Stop press, a Cancel scan hold and a company summary waiting for an online confirmation no longer come back from a backup (D-QUEUE-10).
- **A run's confirmed settings end with the run when the worker is stopped mid-cycle.** A restore, a reset or a quit that cut a cycle short used to leave them in use by the online lane until the next cycle (D-QUEUE-10).
- **A restore that replaced the database says so.** If copying the config files or documents fails afterwards, the message says the database was restored and names the parts that were not, instead of "Restore failed", and Kestrel refreshes what it had cached from the old data (D-SEC-23).
- **A long message no longer runs off the side of the window.** The message strip at the bottom of the page now fits the page column beside the sidebar. `kestrel restore` also says "Restore partly complete." and ends with an error code when a part after the database could not be restored.
- **An online model chosen for one task is used only with your confirmation.** A queued score that names its own online model and carries no confirmation now waits for one instead of being sent, and a scan's one-run online model carries the scan's confirmation through to each role's full review.
- **The Score again dialog lists every correction it sends.** With many roles selected the dialog used to show 20 lines and "and N more"; it now lists them all (the list scrolls).
- **Models on this computer or your home network never go through a proxy.** Every page that calls a local or home-network model now uses a connection that ignores system proxy settings, as the background worker already did.
- **Another device with the access key no longer clears the out-of-credit pause.** Score again, Try again, Score anyway, Try all again, Score everything and Run now release it only from this computer.
- **A sorting model that cannot be used is reported once a day, not once per role.** After an upgrade, a sorting model saved before model files were signed used to add one notice for every role scored; Settings > Scoring and learning now also agrees with Calibrate that it is not in use.
- **Quick answers teach the full review what they were about.** Each example the full review sees names the role it rated instead of "summary unavailable", a role answered twice counts once with its newest answer, and the "15 answers" step on the meter and the done screen follows the answers the full review can actually use (and is left out when examples are switched off).
- **The quick round follows your choices.** Changing How often applies to the wait already running, the round honors Settings > Reduce motion, and an answer that fails to save after you answered the next cards no longer makes Undo remove a different card's answer.
- **Calibrate offers the right roles after you hide one and put back another.** The cached order now notices offsetting changes (and a reused answer number or a moved duplicate).
- **Corrections tell C++, C# and C apart, and roles written in any script.** One answer about C++ used to answer C# and C too, and every requirement written wholly in Cyrillic, Greek or Chinese shared one correction. Existing corrections are re-keyed on update.
- **Saving a correction in Confidential mode keeps your note.** A note naming your current employer is shown redacted and read only in Profile > Corrections, and Save keeps the note as you wrote it.
- **Undo on the role page stays on the right role.** A row's Undo that can no longer be applied shows a warning instead of blanking the match area, and a toast's Undo pressed after Next no longer loads the previous role's rows.
- **Old training records needed for automatic retraining are kept.** Clearing out old work no longer removes the last training task of each track or the record of a refused model's retrain, so neither repeats every 30 days.
- **A crash cannot make a failing task run forever.** Quitting normally gives back only the work this run of Kestrel was doing; work a crash left behind uses up its attempt, so a task that keeps failing still stops after three tries.
- **A long posting scored on this computer keeps room in the model's window.** When a prompt comes within 3% of the window, the posting text is shortened a little more, so the local model never cuts the prompt silently.
- **A restore now imports your data into a fresh database instead of copying the backup's file.** Kestrel builds a new database from its own setup and copies in only the rows of its own tables, so nothing else a backup's database holds (extra tables, views or triggers) can come along and quietly change your settings later. A backup from an older Kestrel still restores; the restore message says how many items were left out (D-SEC-20).
- **Queued work never comes back from a backup.** A restore brings back no waiting background jobs (only your finished follow-up drafts, as read-only history), so a backup cannot start a model download or rebuild, score a role on a model it chose, or release something waiting for your confirmation. One-run model choices and old Undo links are dropped too.
- **A restore or Clear everything first stops background work, including online story builds and cover letters.** Work already running gets the same short grace as Quit, then is stopped and handed back, so nothing made from the old data is written into the restored or emptied data. Both start again afterwards.
- **A settings file in a backup is always checked, whatever its spelling.** `Settings.yml` (or `settings.yml.`) now goes through the same this-computer-only merge as `settings.yml`, and the restore preview shows the same file. Config files Kestrel does not use are skipped and counted.
- **A job source can only send its own service's key.** A source's settings could name any saved key, including an online model key, and Kestrel sent it to that job board. The name must now be that service's own (for example `USAJOBS_...`), checked again every time the source runs, and only this computer can set it (D-SEC-21).
- **Keep the computer awake is this computer's setting.** A restored backup no longer turns it on, the restore preview lists it, and its Undo works.
- **The startup problem card hides a quoted or bracketed folder path with a space in it**, and reads "canceled" in American English.
- **Restore messages read as plain sentences** ("1 setting was kept", never "setting(s)") and name only the file name of the earlier data, never its folder.
- **Undo on a removed correction lasts a day, as it says.** After that the link restores nothing, and the saved copy of the correction and its note is deleted by the daily cleanup.
- **Choose models works for a model server set in settings.yml**, and the "Use for" pickers appear only after a new server is saved.
- **Quit always hands back unfinished work, even with the quit grace at its 30 second maximum.**
- **Saving Models > Speed without changing the fit rating count keeps it automatic**, so going back to local models uses the local default again instead of the online 4.
- **The quick round no longer shows your current employer in Confidential mode.** The card's company line, title and place now use your "Show it as" text, like the rest of the app.
- **A long job title or place no longer spills over the quick round card.** It ends in an ellipsis and the full text shows on hover.
- **Reduce motion in Settings now also calms the Applications sort and the Calibrate tabs.** They followed only the computer's own setting.
- **Keyboard focus stays where you were after Save and Undo on a role's matches, Save on a correction, the Applications pager and the work search log dates.**
- **The work search log and applied-file downloads are shown as unavailable on a phone or other device that cannot save files from this computer.**
- **The Not now menu on Today no longer closes by itself when the strip refreshes.**
- **A new Applications search or filter opens on the first page of its matches, and a trailing space in Company no longer hides the real totals.**
- **A backwards date range in the work search log keeps the dates you typed.**
- **Undo on a correction can no longer undo someone else's later answer.** Undo from the role page now only works while the answer is as the save left it; if you answered the same thing on another role, in Profile or in another tab since, Undo says so and changes nothing. Profile's Undo is checked by Kestrel itself instead of trusting the page.
- **Confidential mode now hides your employer in a correction's note on the role page.**
- **The copy of your file with Kestrel IDs now puts every ID on its own row.** A row that is blank in every column you matched (a memo or subtotal line) no longer shifts the IDs after it onto the wrong applications. Reading the copy back no longer shows notes that begin with a dash or plus as changed, a blank line inside a note is kept, and the copy is removed after the day it is kept. Undoing an import is no longer refused because Kestrel checked a role's links or indexed it in the meantime.
- **Hacker News postings whose header starts with the city or a work style are kept.** "Remote Senior Backend Engineer", "City | Role | Onsite" and similar headers were read as a place and then dropped as unrelated. One odd value in one Hacker News or 4 Day Week entry no longer loses the whole fetch, a later page or item that fails keeps what was already read, and a pay period 4 Day Week sends that Kestrel does not know no longer shows as a yearly figure.
- **A Jobvite feed that declares a DOCTYPE or ENTITY is refused whatever characters hide the keyword, and a job description that merely mentions one is read.** A Jobvite source given only a company ID is named apart from other companies.
- **Links on internationalized domain names (such as bücher.example) can be fetched.**
- **The applications list for scripts hides your employer in the source name** in Confidential mode.
- **Writing a new story from a correction no longer queues a re-score that is then held.** The sheet saves the story and the correction; Score again with my changes does the re-score. A re-score that finishes while you are writing a correction no longer wipes the note you typed. A model check that waits for you to confirm a changed destination no longer churns the queue.
- **A row only shows a story or resume line as its source when the evidence really quotes it** (a longer quote, whole words), so "Java" no longer matches "JavaScript".
- **Profile no longer edits or removes a "They mean something else" note** meant for one role.
- **Undoing a dismissed story draft gives the answers that rested on it their story back.**
- **The role page respects Reduce motion in Settings** for the row moves and the score card.
- **Leaving Calibrate while it switches tabs can no longer change the next page's address.** The Quick or Detailed choice is written to the address only while Calibrate is on screen.
- **Hacker News postings read their header line properly.** "Company | Role | Location | ONSITE | Full-time" now gives a one-part title and its own location, a link in parentheses no longer sticks to the company name, and "no remote" is no longer read as remote.
- **4 Day Week weekly pay is no longer shown as a yearly figure.** A weekly rate keeps its week interval and is converted like any other.
- **A dropped connection keeps what was already found.** On Hacker News (later items) and 4 Day Week (later pages) it is now a partial run, like a slow-down answer, not a failed one that discards the postings collected.
- **One bad Jobvite link no longer fails the whole board.** The job is kept with a built link.
- **Importing a file with a non-English name no longer breaks the copy download.**
- **The copy with Kestrel IDs is safe to open in a spreadsheet.** Cells that start like a formula are defused, as in every other export.
- **An import never quietly updates another application.** A row shown as new or a re-application now creates its own application, so one Undo removes exactly what the import added.
- **Importing Kestrel's own applications file with blank dates matches by Kestrel ID.** Those rows no longer land in Ask.
- **The work search log says how many applications it left out.** Applications whose posting source does not allow passing it on are still left out, and the sheet now tells you how many and why.
- **Quit always hands work back, and online drafting gets the same grace.** A settings.yml that does not parse no longer stops Quit from cancelling the worker and handing its leases back. Story builds and cover letters running on an online model now get the same few seconds to finish as the worker. A letter cut off anyway shows why with Try again, which asks before sending again; a story build cut off keeps the list of groups that already failed, so the resumed build does not send them again.
- **Asking again starts the work now.** Pressing Download, Draft or Build stories again while that work waits to retry after a failure starts it at the next pass instead of up to ten minutes later.
- **The background lane waits for scoring that is about to retry,** instead of running ahead of it, and the online lane releases held work by the settings the current run holds, so held work is not released and held again every pass.
- **Show more closed on Applications draws 100 cards a press,** with a button for the next 100, instead of every remaining closed card at once.
- **A drafted follow-up is no longer removed by the 30-day cleanup** while its Follow up dialog still shows it.
- **The startup problem card hides every kind of file path.** A folder at the top of a drive with a space in its name, or a relative path, could still show part of a name under Show details. The full text stays in the log.
- **Quit gives a running model call a few seconds, then hands its task back.** Quit used to cut a model call off after 1 second, which wasted a paid online call. It now waits up to 5 seconds (set `server.quit_grace_s`, 0 to 30) only when a call is running, stops at once when nothing is, and always finishes within that grace plus about five seconds whatever the model does. A cut-off task goes back to the queue with its attempt refunded, not failed.
- **A start that crashes now says why.** When the local server stops while starting, the error screen says so in plain words and, under Show details, shows the last error read from the server's error log (paths and keys removed), instead of "did not answer in time. Trying again usually fixes it".
- **A tunnel host always has its shared key.** When `server.public_hostnames` is set, Kestrel makes the shared key itself at start (and after a restore that holds none), so a published tunnel is never reachable without one. docs/WEBAPP.md no longer asks you to generate it by hand.
- **A restored backup can no longer change a this-computer-only setting by naming a whole section.** A backup row for a whole section (for example all of `llm` or `notify`) used to slip past the check and could add an online model server reading your saved key, accept Hosted mode for you or move your notice webhook. A restore now checks every setting inside such a row one by one, the preview lists them, and nothing changes unless you tick the box; Hosted mode acceptance and which key a server reads never come from a backup.
- **Your saved key stays with the address you set.** When you restore a backup's AI and connection settings, a model server that uses a key saved on this computer keeps this computer's address, and the restore message says how many did.
- **A backup can no longer turn off hiding your current employer in online sends.** That choice always stays as it is on this computer, whether or not you restore the backup's AI and connection settings.
- **A restore cannot turn Confidential mode off, or loosen an online service's privacy settings.** Confidential mode set in settings.yml now counts, and whether an online service may keep your prompts (and zero data retention) always stays as it is on this computer. A model server added by a newer Kestrel than your settings file also keeps its address when it uses your key.
- **A restore never lowers a protection when this computer's settings cannot be read.** If your settings file is damaged or a setting is invalid, the restore keeps all of this computer's AI and connection settings and treats Confidential mode as on, and it no longer stops on a damaged settings file. An invalid setting that does not affect privacy or your AI connections does not block restoring them. A model server in a backup with a name Kestrel does not use is left out, and a model server added by a newer Kestrel keeps working with your saved key after a restore.
- **Someone using the shared key cannot turn Confidential mode off from Settings.** The check now reads the mode wherever it was turned on, including settings.yml.
- **The startup error no longer shows part of a folder name with spaces.** A path such as one under a user folder with a space in its name is now hidden whole: everything from the start of a path to the end of the line.
- **The doctor says when a model answered at length.** A reply that talks instead of repeating the test code is now reported as "answered at length; context not verified" with a fitting remedy, not "model not ready yet".
- **A padded tracker over 2 MB can be imported.** The applied-history import refused text or files over 2 MB, although its own limit is 25 MB and a notes-app table pads every row; it now accepts up to 25 MB.
- **Quick round fixes from review (issue 171).** A quick answer and the strip's bookkeeping now save together, so "That answer was not saved" is always true and a retry never saves twice. Undoing every answer of a round puts the Today offer back as it was. A save that fails while the card flies off brings the same card back. Old links to Calibrate (Keep going, See your rating history, the sorting-model notices) open Detailed, and the chosen tab stays on reload. Turning the offer back on waits for the next run on every track, and only runs that finished normally count. Today's strip and Calibrate no longer re-rank every candidate on each poll (about 1 s to under 20 ms with 3000 roles); the order itself is unchanged. On narrow Today the Not now menu opens under its button, and the next role shows behind a dragged card.
- **A refused sorting model retrains early only once.** An ordinary retrain no longer re-arms the early retrain of a model that failed its signature check.
- **Title versions and Maybe roles are pinned by tests.** The fair, abbreviation-first cut of "Other ways your titles are searched" (each chip names its title, titles with none are named) now has a test for an 11-title profile, and a Maybe role first seen before today is tested to appear in Today's Decisions waiting. No behavior change.
- **Learned reranker files are signed and checked before they load.** Each saved model file is signed with a key that stays on this computer (never in a backup), and a changed, copied or older unsigned file is refused with a notice while scoring uses the AI model step instead. Rerankers saved before this change retrain once.
- **The Top matches cards no longer shrink when the window gets larger.** Cards that do not all fit keep one steady width and a wider window shows more of them; once every card fits they share the extra width up to a cap, so a card is never narrower in a bigger window.
- **Suggest again now says what happened.** Under Suggested titles the card always shows how the last pass ended (for example "2 new, 1 already seen", "Nothing new", or that the resume could not be read, the model's reply could not be used, or it is waiting for your OK to send to the online model), "Reading your resume." shows while a pass is queued even when titles are listed, and waiting suggestions from a resume that is no longer active, or that the model has not proposed for 60 days, are retired.
- **Windows and macOS code checks are clean again.** The memory size read on Linux and macOS no longer trips the Windows type check, the macOS window buttons are read by name, a story build that fails part-way remembers which groups failed, and two tests that failed only under load on Windows no longer do.
- **A model's reply can no longer point Kestrel at an address.** A contact's profile link is kept only when it is on linkedin.com as well as written in the source page, and the browser that prints a PDF now loads nothing from the web, so an address that reached a page is never fetched. New tests check that no request Kestrel makes to a model carries a tool, that no file that reads a model's reply can make a web request, and that a reply naming an address or a path makes no request and opens no file (D-SEC-12).

- **More of what only this computer may change is now kept that way.** Where notifications, searches and update checks go, the User-Agent and the robots.txt switch can no longer be changed by a key holder or a restored backup; a restored follow-up draft asks for a fresh confirmation before anything is sent; the fetch of a link you paste tries each checked address and follows your proxy settings the way the rest of the app does; the MCP port and the web app read the same public host names; and an id with a trailing newline no longer passes the older job-board checks.

- **A restored backup can no longer change where your data goes, and a few doors a key holder could still open are closed.** Restore keeps this computer's own model route, keys and server, MCP and update switches (the restore page says how many it kept), writes only generated PDFs into the document folders, and strips online confirmations from restored tasks; an online backend at any address that is not this computer now always asks first. Run now with another mode, turning confidential mode off from About you and the keep-awake switch are this computer only; Workday sources refuse a tenant or site that could point at another host; a pasted link is fetched from the address that was checked; spreadsheet exports no longer carry live formulas; the MCP port uses the web app's loopback rule; and the generated API pages are no longer served (D-SEC-10).

- **A restored backup can no longer make Kestrel load a model file it named.** A learned reranker that a restored backup pointed at a model file outside Kestrel's data folder is now deactivated on the restore, and the restore page says how many rerankers were turned off and never which; scoring then uses the LLM tier until a reranker is retrained on this machine. Separately, any learned model whose file lives outside the data folder is refused the moment Kestrel would load it, with a note, because loading a model file can run code (D-SEC-9).

- **The key holder's limits have no side doors.** Undo and the settings form can no longer write a key whose own control is this-computer-only, turning confidential mode off, choosing another model or mode for one run, Check my models apply and promote, Quit, setup and download actions, and opening folders are this computer only, and a key holder's page views show no balance, online model list, folder path or terminal command. A request from this computer now counts only with a Host of localhost, 127.0.0.1 or ::1 and no proxy header.
- **Only this computer can restore, wipe, download your data or change keys.** The shared key now opens browsing and working only: restoring a backup, Clear everything, every download of your data (backup, profile, diagnostics, ratings, Analytics exports) and changing, testing or checking an online model key, where the models run, the LAN switch and MCP are refused with "Only this computer can change this." or "Only this computer can download this.", and those controls show disabled on a phone.
- **A failure with no role is no longer hidden on one track.** A story-merge failure has no role and therefore no track, but the track filter on the Today surfaces treated "no track" as "not this track" and hid it on both Employment and Consulting. It now shows on whichever track you are on, so the item-needs-another-try note and the count are never silently missing (D-UI-264).
- **Quitting no longer costs an attempt, and a failing task waits before trying again.** Closing Kestrel while scoring, a letter, or any other task is in progress returns that attempt, so the task resumes at the next launch as if it had not started: an intentional quit is not a failure. A task that keeps failing waits before its next attempt, 30 seconds after the first failure, 2 minutes after the second, and 10 minutes after the third and later ones. Asking again (Score again, Draft again) runs it at once, without the wait.

- **A sorting model refused for its signature now retrains on its own.** A model saved before files were signed stayed marked active, so the automatic retrain never ran and scoring stayed on the AI model until you pressed Retrain. Kestrel now queues one retrain for it; if that attempt does not pass the checks, the next waits for 50 more ratings as usual.
- **A restored backup can no longer point a download link at a file outside Kestrel's data folder.** A backup file that carried a stored document path outside the data folder would, after a restore, have been servable from its download link. A restore now clears every stored path it cannot confine to the documents or prep packs folder before the data is installed, and the page says how many paths were cleared and never which. The file a cleared path pointed at is left exactly where it was, and its download link now answers "not found" (D-SEC-8).
- **The Restore button restores.** In Settings > Your data, choosing a backup and pressing Restore closed the dialog and did nothing: the upload never went out, no answer appeared and the data was not changed. It now sends, answers in the Backups row (Restored from ..., with the earlier data saved first), and the dialog closes after the answer, whether it succeeded or was refused (D-UI-269).
- **A slow start is waited for, and Try again never starts a second server.** When Kestrel's local server is still coming up after 30 seconds, the loading screen now keeps waiting (up to 90 seconds), says "Still starting (45 s)", and offers a Cancel. If it really never answers, the failure window points at a file in the logs folder with the thread stacks and the waiting tasks, which you can send back. Try again first stops the server from the last attempt; if it is still closing it says to wait a moment instead of starting a second one beside it. Background work (searches, scoring and the like) now starts only after the window opens, so the first answer is never delayed by resumed work (D-DESK-2).
- **Failure messages no longer name an internal error type.** A failed save, import, document read or key check that could read as "an unexpected problem (RuntimeError)" now reads as Kestrel's own sentence; the type and its text stay in the log and under Show details, so a person never sees a class name (D-UI-268).
- **"Make a new key" is where you look for it.** In Settings > Privacy > Access from other devices the button was hidden inside Show details, so people could not find it. It now sits next to the key whenever access is on (still hidden when access is off, and Show details keeps the raw values), and because a new key signs out every device that used the old one, it asks you to confirm what that means first (D-UI-267).
- **The Today retry note no longer says failed items are kept in Pipeline.** A failed story merge has no role and nothing is kept in Pipeline, so the sentence after the count now reads "See them on Needs another try." with the Review link unchanged (D-UI-266).
- **A missing built-in embedding model now says what to do** instead of "an unexpected problem (EmbedModelMissing)". Matching stories and other text always runs on this computer, even with an online model, so a source checkout must fetch the model once; the developer guide now lists that step.
- **The ask-first panel for building stories now says which step stays on this computer.** Your notes and saved interview sketches are matched on this computer first; only the writing step uses the online model.
- **Building stories no longer holds everything else up.** Each story is saved as soon as it is written, so drafts appear while the build runs and quitting keeps them. Every two minutes or so the build lets a waiting search or cover letter run first.
- **Stories and cover letters you ask for now run before background work**, and the status says "Building your stories, then search 3 of 5" instead of "Searching" while a search waits.
- **Roles that missed their embedding** because the built-in model was missing are queued again once it is there.
- **The story panel's list of what is sent** now names exactly what a story build sends: your notes, your interview sketches and the proof points in your profile.
- **A cover letter no longer marks the employer's own name as unverified** unless it says you worked there, and a number or length of time from your own headline or narrative counts as yours. A company you never named is still marked, and the send dialog now says the headline and narrative are sent.
- **A failed step on Settings > Your computer now says what happened in plain words** instead of naming an internal error type.
- **A cover letter could claim things you never said.** A gap the review had found you already meet was passed to the letter as one to acknowledge, so a letter wrote "three years" for a person with eight. That gap is no longer passed on, a length of time now has to match your own text exactly (a "3" elsewhere no longer covers "three years"), and promises such as starting immediately or relocating are marked unless you wrote them (D-LETTER-9).
- **The run notification counted a role twice.** A role rated by the quick screen and then the full review was counted once per step, so one role read as "2 roles rated". Each role now counts once (D-RUN-2).
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
- **The single-instance lock is marked to the operating systems that use it.** The two lock-file helpers now say they are for macOS and Linux; the type checker no longer reports them on Windows, where the named-mutex path is used instead.
- **Releasing the single-instance lock on a failed start now works on Windows.** The platform check sat on the whole release function, which also hands back the Windows mutex; it now sits only on the Unix-only lock-file step.
- **A story that keeps naming the same theme is kept, not lost.** When a story wrote the same theme over and over, it was set aside as a failure and the build sat on it until a retry. A story's themes are now the two or three it can legitimately answer (from the eight themes the coverage and prep tools track), so a looping reply is trimmed to a real set instead of being quarantined; and the story is written to avoid the dashes and stock words the writing check looks for, so it is less often sent back for a re-mark (D-LLM-13).
- **A request through a published tunnel now needs the shared key.** A tunnel such as cloudflared connects to Kestrel from loopback, and the key check exempted every loopback request, so with Kestrel published on a public hostname and Cloudflare Access missing or misconfigured, anyone on the internet had the app's full control without the key. A request that carries your public hostname now always needs the LAN shared key, even when it arrives through the tunnel, and local requests from this computer are unchanged (D-SEC-8).
- **A story named for a theme that is not tracked is kept, not quarantined.** When a story used a theme outside the eight the coverage and prep tools track (such as "scaling"), the whole story was set aside as a failure until a retry. The extra tag is now simply dropped and the story is kept with the tags it does use, so a good story is never lost over a tag it cannot use (D-LLM-14).
- **Work waiting on you no longer spins in the background.** A step held for your approval, for the online credit limit, or because its models run online was picked up and put down again while it waited, which could spend its retries before you got to it. A held step now stays put for as long as the hold lasts, is shown as waiting rather than as a failure, and runs exactly once when the hold clears (a safety sweep releases it if the clearing event is missed) (D-QUEUE-3).
- **A backup with a damaged file inside is turned down before anything is stopped.** The check that runs before a restore read only the database, so a backup whose database was fine but which held a damaged or password-protected document passed the check and was then refused by the restore itself, after the work in progress had already been stopped. The check now reads every file the restore would read, to the end, and refuses the backup while everything is still running (D-SEC-30). Entries compressed with a method Kestrel never writes (bzip2, LZMA) are refused without being read, and a damaged compressed file is a plain refusal rather than an error page (D-SEC-31).
- **A role is no longer scored from a prompt too long for the local model.** When your profile and notes left no room for a posting in the local model's context window even after the posting was shortened as far as it goes, the prompt was sent anyway and the model server could cut it without saying so, which gives a quietly wrong score. Such a role is now set aside under Needs attention with the reason, nothing is sent, and a role that already had a score keeps it (D-SCORE-20).

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
