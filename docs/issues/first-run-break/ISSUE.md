# First run: build interview stories automatically, and show it in the "grab a drink" notice

**Filed as distantsoil/kestrel#176 (2026-11-11).**

**Labels:** feature, onboarding, interview-prep · **Decision refs:** D-UI-249, D-UI-249 follow-up (the private decision log)

## In short

- **What:** the first run drafts interview stories automatically, so a new user has some ready when their first search finishes. Today stories are built only when the user presses Build on Interview prep.
- **How:** pressing "Start searching" the first time marks stories as due. Once the first roles have had their deep read, Kestrel queues one story build (the same one the Build button runs). The first-run notice gains a fourth line, "Drafting interview stories from your best matches."
- **The catch:** if the deep read runs on an online model, building stories would send data online, and Kestrel never does that without the user's OK. In that case nothing is queued and the notice keeps three lines. This is the one behavior that needs a test.
- **Guardrails:** once per install, never re-armed by restarting onboarding, survives the app closing mid-run. The illustration and the scoring prompts are not touched.

Everything below is the implementation brief for whoever builds it.

---

# Implementation brief

## Summary

The first time a user finishes onboarding, Kestrel shows a one-time notice on Today ("Good time to grab a drink") that explains the first search takes a while. That notice is **already built** on branch `claude/relaxed-fermi-1qm54k`: an animated illustration (a person in an armchair sipping coffee), a heading, three steps with step 1 marked "Now", and a note.

This issue adds a fourth step to the first run: **automatically queue one interview story build once the first run's deep reads have finished**, and add the matching fourth line to the notice. Today, stories are built only when the user presses Build on the My stories page, so a new user finishes the first run with an empty story bank.

**Decision already made by the owner:** queue the story build *after* scoring. Do not build stories before searching, and do not add a new resume-to-stories input. See "Rejected alternatives".

## Target design

The shipped notice stays as it is. This issue adds **one step and one clause**, nothing else.

The notice as shipped now (light 1280, dark 1280, light 420):

| Light, 1280 | Dark, 1280 | Light, 420 |
|---|---|---|
| ![Shipped, light](https://github.com/distantsoil/kestrel/blob/841b1d4d479dc9a43fb1a89afb60bb00f184d9e0/docs/issues/first-run-break/app-light-1280.png?raw=true) | ![Shipped, dark](https://github.com/distantsoil/kestrel/blob/841b1d4d479dc9a43fb1a89afb60bb00f184d9e0/docs/issues/first-run-break/app-dark-1280.png?raw=true) | <img src="https://github.com/distantsoil/kestrel/blob/841b1d4d479dc9a43fb1a89afb60bb00f184d9e0/docs/issues/first-run-break/app-light-420.png?raw=true" alt="Shipped, narrow" width="220"> |

How step 4 should look in the list (the four-step mockup, [`the locked design mockup (kept privately)`](https://github.com/distantsoil/kestrel/blob/841b1d4d479dc9a43fb1a89afb60bb00f184d9e0/the locked design mockup (kept privately)); it predates the illustration, so use it for the step list only):

<img src="https://github.com/distantsoil/kestrel/blob/841b1d4d479dc9a43fb1a89afb60bb00f184d9e0/docs/issues/first-run-break/mockup-light-1280.png?raw=true" alt="Four-step mockup, light" width="520">

### What changes from the shipped notice

| | Shipped now | Target (this issue) |
|---|---|---|
| Steps | 1 Finding (Now), 2 Reading and scoring, 3 Closer look | adds **4 Drafting interview stories from your best matches**, joined by the existing connector line |
| Note | "Leave Kestrel open. Today fills in as roles are scored, so there is no need to watch it." | "Leave Kestrel open. Today fills in as roles are scored, and your stories appear in Interview prep when they are ready." |
| Illustration, heading, subtitle, "Now" pill, connector, motion | as shipped | unchanged |

### Exact copy (checked for AI-writing patterns; American English; no em dashes)

- Title: `Good time to grab a drink`
- Subtitle: `Your first search has started. It is the biggest job Kestrel does, so it takes a while, longer when the model runs on this computer.`
- Step 1: `Finding openings from your searches` (with pill `Now`)
- Step 2: `Reading the openings and scoring them for fit`
- Step 3: `Taking a closer look at the strongest matches`
- Step 4: `Drafting interview stories from your best matches`
- Note: `Leave Kestrel open. Today fills in as roles are scored, and your stories appear in Interview prep when they are ready.`
- Button: `Got it, show me around` (or `Got it` when Today was opened without `?tour=1`)

**Copy rule:** show step 4 and the "your stories appear" clause **only when the story build will really be queued** (see Invariants). Otherwise render the three-step version that ships now. The dialog must never claim work that will not happen.

## Background a new agent needs

Kestrel is a local-first desktop job-search app: Python 3.12, FastAPI, Jinja templates, htmx and vanilla JS, SQLite through SQLAlchemy async. Read `CLAUDE.md` first, then `INSTRUCTIONS.md` (private repo). They contain the hard rules, the gate, and the screenshot requirements.

**The pipeline.** Ingestion finds postings. Tier 0 applies rule filters. Tier 1 gives a quick score. Tier 2 does a deep read and writes `Evaluation` rows with `tier == 2`. The worker drains every task for one tier before starting the next, to avoid model-swap thrash. `kestrel/pipeline/worker.py` runs a batch, then calls `runlog.finish_run(...)` and `self._notify_run_finished(stats, run_id)` at the end of a run that was not stopped (around lines 1006-1016).

**Stories.**
- `kestrel/interview/stories.py:consolidate_stories` builds stories from two sources:
  - sketches stored in `Evaluation.blocks['f_interview']` on **tier 2, employment-track** evaluations (`collect_sketches`, around line 395);
  - the user's own interview notes (`collect_note_sketches`).
- A fresh install has neither before its first run, which is why stories cannot be built before searching.
- `kestrel/interview/story_task.py`:
  - `request_build(session, *, consent=None) -> bool` queues one `build_stories` task, unless one is already waiting or running. It commits.
  - **When Tier 2 runs on an online model and no consent is passed, it queues nothing.** That is deliberate: a build never goes online without a click (`BUILD_SENDS` says what is sent).
  - `drain_story_builds(...)` runs queued builds on the serve loop (`kestrel/web/app.py`, around line 450). No new runner is needed.

**The notice (already built).**
- `kestrel/services/onboarding.py`: the setting key `BREAK_KEY = "onboarding.first_run_break"`, with `mark_first_run_break`, `first_run_break_pending`, `dismiss_first_run_break`. `restart()` writes the row as seen.
- `kestrel/web/pages/onboarding.py`:
  - `onboarding_start_searching` (POST `/onboarding/searches/start`) marks the notice on the first completion;
  - POST `/onboarding/first-run-break/dismiss`.
- `kestrel/web/pages/today.py:page_context` passes `first_break`.
- `kestrel/web/templates/dashboard.html` includes `partials/first_run_break.html` when it is set.
- `kestrel/web/static/js/today.js:boot` starts the walkthrough on the dialog's `close` event.
- `kestrel/web/static/js/first-run-art.js` draws the illustration (token colors `--il-*`, classes `sf-fra-*`). Leave it alone.
- Styles: `.sf-break*` and `.sf-fra-*` in `kestrel/web/static/css/pages/surfaces.css` (every class there must start `sf-`, enforced by `tests/test_surfaces.py`).
- Tests: `tests/test_onboarding.py -k first_run_break` and `tests/test_today_browser.py -k first_run_break` (the second needs `KESTREL_LIVE_BROWSER=1`).

## What to build

1. **A first-run story flag.** In the same step that marks the notice (`onboarding_start_searching`, first completion only), record that the first run should build stories:
   - Either add a field to the `BREAK_KEY` value, e.g. `{"pending": true, "stories": "due"}`, or use a separate key.
   - Keep it in the `setting` table, not in memory (hard rule 5: resumable).
   - `restart()` must leave it absent or `"done"`.
2. **Queue the build when the first deep reads are done.** At the end of a finished, not stopped, run in `worker.py`:
   - when the flag is `"due"` **and** the run produced at least one tier 2 employment evaluation, call `story_task.request_build(session)` with no consent argument;
   - then set the flag to `"done"` whether or not it queued;
   - when the run produced no tier 2 evaluations, leave the flag `"due"` so the next run tries again.
   - Never call it more than once per install.
3. **Decide whether step 4 shows.** Add a small helper, e.g. `first_run_stories_expected(session) -> bool`. It is true only when the flag is `"due"` and Tier 2 is local (when it is online, `request_build` will refuse without consent). Pass it to `first_run_break.html` as `stories`, and render step 4 and the note's second clause only when it is true.
4. **Add step 4 to the notice.** In `first_run_break.html`, add the fourth `<li>` and the note's second clause, both under the condition above. The existing styles already cover a fourth step (number circle, connector line, staggered entrance: add a `nth-child(4)` delay in `surfaces.css` to continue the stagger). Keep the existing motion and the 400 ms fallback timer. Use tokens only, no hardcoded colors.
5. **Update the paperwork:**
   - `SPEC-INTERFACES.md` ("First-run notice" paragraph, above "Walkthrough") and the story bank section of `SPEC-MODULES.md`: say the first run queues one build after its first deep reads;
   - `CHANGELOG.md` `[Unreleased]`;
   - a the private decision log entry;
   - a `working.log` entry.

## Invariants

- **Nothing goes online without the confirmed dialog (test required, CLAUDE.md "state the invariant in every risky brief").** With Tier 2 online, the first run queues **no** story build and the notice shows the three-step version. Test: Tier 2 configured as hosted, finish a run with tier 2 evaluations, assert no `build_stories` task row and no step 4 in Today's HTML.
- **At most one automatic build per install.** Restarting onboarding never re-arms it.
- **Resumable.** The flag lives in the DB, so a killed process still queues the build after the next finished run.
- **A run with no deep reads does not use it up.** The flag stays "due".
- **The copy is true.** Step 4 shows only when the build will really be queued.

## Off-limits

- Scoring prompts, schemas and prompt versions (`PROMPTS.md`, `kestrel/llm/schemas.py`). They don't change.
- `consolidate_stories`, `request_build`'s consent logic and `BUILD_SENDS`. Call them; don't change them.
- The walkthrough steps in `today.js`.
- The illustration (`first-run-art.js`, the `--il-*` tokens, the `.sf-fra-*` styles).
- The hard rule that Kestrel never acts externally (no emails, no applications).

## Acceptance

- [ ] On a fresh install with a local Tier 2: finish onboarding. The notice shows four steps with step 1 "Now". After the first run's deep reads, one `build_stories` task is queued, it runs, and stories appear on Interview prep > My stories.
- [ ] With Tier 2 online: the notice shows three steps, and no build is queued.
- [ ] The notice still shows once per install, and dismissal (button, Escape, or the app's Reduce motion setting) still works, as the existing browser tests prove.
- [ ] Screenshots of the four-step notice in light 1280, dark 1280 and light 420 are attached to the PR, and the layout overlap check reports nothing.
- [ ] Motion is checked frame by frame in a recording (rise in, the four-step stagger, sink out), and both reduced-motion settings turn it off.
- [ ] `uv run --extra dev python scripts/check.py` ends with the literal line `Ready to push.`

## Rejected alternatives

- **Build stories before searching.** On a fresh install there are no sketches and no notes, so the build would produce nothing.
- **Draft stories from resume proof points.** That needs a new input, a new prompt and a spec change, and it carries a hard-rule-2 risk (the model selects, it never invents). The owner did not choose it. File it separately if wanted.
- **Promise a duration ("about an hour").** It depends on the model, the hardware and the number of searches, and Kestrel can't know it in advance.
