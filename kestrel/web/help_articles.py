"""The Help articles (SPEC-INTERFACES 2.1 /help, D-UI-124): 21 short articles in five topics.

The text is the locked mockup's (the locked design mockup (kept privately)), kept here as
data so the page renders it server side, offline, and a test can check the count, the length and
the wording rules. Bodies are fixed HTML written in this repository (p, h6, ul, ol, li only), never
user input, and are marked safe by the template. Hard-wrapped for the linter; HTML ignores the
line breaks.
"""

from __future__ import annotations

from typing import NamedTuple


class Article(NamedTuple):
    key: str
    title: str
    body: str


class Topic(NamedTuple):
    key: str
    title: str
    icon: str
    tone: str
    articles: tuple[Article, ...]


TOPICS: tuple[Topic, ...] = (
    Topic(
        "start",
        "Getting started",
        "compass",
        "i",
        (
            Article(
                "what",
                "What Kestrel does",
                """\
<p>Kestrel is a free job search app that runs on your computer. It finds openings, checks
each one against what you want, and shows you the ones that look like a fit, with the
reasons.</p>
<h6>What it does for you</h6><ul><li>Searches public job boards, company career pages and
public company filings on the schedule you set. You can also paste a posting in by
hand.</li><li>Scores every opening for fit, from 1 to 5, and explains the
score.</li><li>Tailors your own resume lines to a role you shortlist, and drafts a cover
letter and an interview prep pack.</li><li>Tracks each application from shortlist to
outcome.</li><li>Learns from your decisions, so later scores fit you better.</li></ul>
<h6>What it never does</h6><p>Kestrel never applies to a job, sends an email or messages
anyone. It drafts and recommends. You press Apply on the employer's site, and you send every
message yourself. It does not search LinkedIn, because LinkedIn forbids automated access. A
LinkedIn posting can only be pasted in by hand.</p>
<h6>Where your information goes</h6><p>Everything stays in one folder on this computer, and
the AI runs here by default. If you choose an online model with your own key, Kestrel shows
what it will send and waits for your OK each time.</p>
<h6>What you'll see</h6><p>Today shows new roles that passed your rules. Pipeline lists
every role found. Applications tracks the ones you pursue. Calibrate is where you rate roles
so scoring learns your taste. The consulting track is optional and off by default.</p>
<h6>If something's off</h6><p>If a page looks empty, nothing may have run yet. Open Runs and
choose Run now. If scores look wrong, read Something isn't working.</p>""",
            ),
            Article(
                "first",
                "Your first search",
                """\
<p>After setup, Kestrel searches on its own schedule. You can also start a search whenever
you like.</p>
<h6>Run a search now</h6><ol><li>Open Runs from the sidebar.</li><li>Choose Run
now.</li><li>Watch the live card. It describes each step in words, such as "Rating 3 of 12",
with a bar for Searching, Your rules, Quick screen, Rating and Deeper read.</li><li>When the
run ends, the card folds into "Last run finished" and the run joins the history below
it.</li></ol>
<h6>Where the results go</h6><p>New roles that pass your rules appear on Today and in
Pipeline. Roles Kestrel sets aside stay in Pipeline under Not a fit, so you can check them.
Nothing is deleted.</p>
<h6>Set a schedule</h6><p>The Schedule card on Runs offers Off, Daily or Weekly, with a
time. Scheduled runs happen only while Kestrel is open. If you close it, the next run waits
until you open it again.</p>
<h6>What you'll see</h6><p>A run ends as Finished, Partial or Stopped by you. Partial means
a job board limited how many requests Kestrel could make, so it used what it got. That is
normal. Pause, Resume and Stop sit on the live card. After a stop or a closed app, Pick up
where it stopped continues from the same place.</p>
<h6>If something's off</h6><ul><li>No results: check Sources to see which are on. Your rules
in Settings > Which roles you see may also be too narrow.</li><li>A run says Did not run:
open Diagnostics from that run, or read Something isn't working.</li><li>The first run is
slower because the model has to load. Later runs start faster.</li></ul>""",
            ),
            Article(
                "short",
                "Shortlist and Not for me",
                """\
<p>These two buttons are how you tell Kestrel what you think of a role. Both also teach it
your taste.</p>
<h6>Shortlist</h6><p>Shortlisting says "I want to pursue this." It switches on the slower
work: a tailored resume, a cover letter draft and an interview prep pack. Kestrel builds
these only for shortlisted roles, because each one takes minutes of your computer's AI time
or some online credit.</p>
<h6>Not for me</h6><ol><li>Choose Not for me on the role.</li><li>Pick a quick reason, such
as the pay, the location or the company.</li><li>The role moves out of your way. It stays in
Pipeline, so you can bring it back.</li></ol><p>The reason matters. Kestrel uses it to learn
what to skip.</p>
<h6>Changing your mind</h6><p>Both choices show Undo right after you click. Later, open the
role and choose a different action.</p>
<h6>If something's off</h6><p>If a role you did not touch moved out of view, one of your
rules in Settings > Which roles you see probably set it aside. The role page names the
rule.</p>""",
            ),
            Article(
                "con",
                "The consulting track",
                """\
<p>The consulting track is for contract and consulting work. It is optional and off by
default.</p>
<h6>Turn it on</h6><ol><li>Open Settings > Preferences.</li><li>In What you're looking for,
switch on Consulting and contract work.</li><li>A Consulting switch now appears beside
Employment at the top of the sidebar.</li></ol><p>Either track can be on, or both. When only
one is on, the switch is hidden.</p>
<h6>What changes</h6><p>Consulting scores a role on engagement fit, buyer access, economics,
delivery and strategic value, instead of the employment parts. Applications becomes Leads.
Signals appear, such as a new executive or an acquisition, that suggest a company may need
help. Postings count as fresh for longer, up to 120 days instead of 45, because engagements
fill slowly.</p>
<h6>What you'll see</h6><p>On the Consulting side, Today and Pipeline look the same, with
consulting roles and signals in place of job postings. Your profile, documents and ratings
are shared where they apply, and the two tracks never mix their scores.</p><h6>If
something's off</h6><p>If the Consulting switch is missing, check that it is on in
Preferences. Turning a track off never deletes your roles or ratings.</p>""",
            ),
        ),
    ),
    Topic(
        "score",
        "How scoring works",
        "analytics",
        "v",
        (
            Article(
                "steps",
                "The four steps",
                """\
<p>Every role goes through up to four steps. Each step is slower and more careful than the
one before, so the quick steps clear the obvious misses and the slow steps only see roles
worth the time.</p>
<h6>Step 0: Rules check</h6><p>Your own filters, such as blocked titles or places. No AI is
involved, so it takes an instant. A role is set aside only for a hard blocker, and Kestrel
records which rule did it. A confusing title passes.</p>
<h6>Step 1: Quick screen</h6><p>A small AI model reads the key facts and the start of the
description, then keeps the role or sets it aside. When it is unsure, the role is kept.
After enough of your ratings, Kestrel can replace this step with a ranking learned from your
choices, but only once it shows that it agrees with you.</p>
<h6>Step 2: Full review</h6><p>The main model reads the whole posting next to your profile.
It scores each part of the fit, lists blockers and gives a verdict. Expect roughly 15
seconds per role on a typical setup.</p>
<h6>Step 3: Deeper read</h6><p>Optional, and only when you ask, on one role at a time. It
gives a second opinion, a cover letter or research on the company. It can use an online
model. If it does, Kestrel shows exactly what will be sent and the cost, then waits for
you.</p>
<h6>Why a role is never lost</h6><p>Doubt keeps a role, it never rejects one. A role hidden
by mistake is gone for good, and a role kept by mistake costs you a few seconds. Set-aside
roles remain in Pipeline under Not a fit.</p>
<h6>What you'll see</h6><p>Runs shows each step as it works, with a bar per step. Models
shows which model handles each step and whether it runs on this computer or online.</p>
<h6>If something's off</h6><p>If scores look wrong, choose Check my models on the Models
page. The most common cause is a model that reads only part of each posting.</p>""",
            ),
            Article(
                "fit",
                "What the fit score means",
                """\
<p>The fit score is a number from 1 to 5 that says how well a role matches what you want. It
comes from the full review step, and the reasons sit next to it so you can check the
thinking.</p>
<h6>What goes into it</h6><p>Five parts, each scored on its own:</p><ul><li>Role fit: the
work itself. It counts most.</li><li>Industry: whether the field suits you.</li><li>Company:
size, stage and reputation.</li><li>Pay and logistics: pay, place and
schedule.</li><li>Trajectory: where the role leads your career.</li></ul>
<h6>Reading the number</h6><ul><li>4 and above: a strong fit, worth applying.</li><li>3 to
4: worth considering.</li><li>Below 3: a weak fit.</li></ul><p>You can move these cutoffs in
Settings > Which roles you see, and restore the defaults at any time.</p>
<h6>Blockers</h6><p>A high score can still carry a blocker, such as a license the posting
requires that you do not list. Blockers are written out in plain words next to the
score.</p>
<h6>Changing how scores work</h6><p>After you rate enough roles, Kestrel may suggest
different weights for the five parts. Settings > Scoring and learning shows the suggestion
with Accept and Not now. You do not drag sliders, and nothing changes until you accept.</p>
<h6>What you'll see</h6><p>The role page shows the score, a bar for each part and the
reasons. Consulting roles use five different parts: engagement fit, buyer access, economics,
delivery and strategic value.</p>
<h6>If something's off</h6><ul><li>If one score disagrees with you, rate that role in
Calibrate. It is the fastest way to teach Kestrel.</li><li>If many scores look wrong, run
Check my models.</li><li>A number built from fewer than five examples shows as Too few, not
as a result.</li></ul>""",
            ),
            Article(
                "maybe",
                "Why some roles say Maybe",
                """\
<p>Maybe means Kestrel was not sure, so it kept the role instead of hiding it.</p>
<h6>Why it works this way</h6><p>A role hidden by mistake is gone for good, and you would
never know. A role kept by mistake costs you a few seconds to dismiss. So when a step is
unsure, the role moves forward. The same goes for a confusing title in the rules check, or
an answer Kestrel could not read at the quick screen: the role passes on.</p>
<h6>What you can do</h6><ol><li>Open the role and read the reasons.</li><li>If you can tell,
choose Shortlist or Not for me.</li><li>If you are unsure too, leave it. It will not expire
quickly.</li></ol>
<h6>What you'll see</h6><p>Roles Kestrel is unsure about also appear in Calibrate. Rating
them there helps the most, because they are the ones Kestrel learns from fastest.</p>
<h6>If something's off</h6><p>If almost everything says Maybe, run Check my models on the
Models page. A model that cannot read the whole posting leaves Kestrel unsure of most
roles.</p>""",
            ),
            Article(
                "learn",
                "How Kestrel learns from you",
                """\
<p>Kestrel gets better at your taste from the choices you make. Everything it learns stays
on this computer.</p>
<h6>What counts as a lesson</h6><ul><li>Shortlist and Not for me, with the reason you
give.</li><li>Your answers to Good match? in Calibrate.</li><li>Your changes to an earlier
rating.</li></ul>
<h6>Rate in Calibrate</h6><ol><li>Open Calibrate.</li><li>Read the role card. Your own score
comes first, so Kestrel does not sway you.</li><li>Answer Good match?. Use Tell Kestrel why
if you want to add a reason.</li><li>Keep going until the queue ends. Choose Keep going for
more, or stop any time.</li></ol>
<h6>What changes as you rate</h6><ul><li>From about 15 ratings, Kestrel shows examples of
your past decisions to the model when it scores.</li><li>From about 30, it checks its scores
against yours and corrects its habits.</li><li>From about 200, it can build a ranking from
your choices. It is used only after it proves that it agrees with you, and you can see the
result first.</li></ul>
<h6>What you'll see</h6><p>Calibrate lists your Learning history: when, the role, what you
said and what Kestrel said. Choose Change an earlier rating to correct one. Settings >
Scoring and learning shows your progress toward each stage.</p>
<h6>If something's off</h6><ul><li>To wipe what Kestrel learned, use Reset learning in
Settings > Scoring and learning. It asks you to confirm.</li><li>Numbers based on fewer than
five ratings show as Too few.</li><li>If you use an online model, the examples used for a
role are part of what Kestrel lists before it sends anything.</li></ul>""",
            ),
            Article(
                "story",
                "Stories and re-scoring",
                """\
<p>Kestrel only knows what you write down. A story is a short note about something you did
that a role might ask for.</p>
<h6>Add a story</h6><ol><li>Open a role where Kestrel lists a gap you can cover.</li><li>Add
a story about it in your own words.</li><li>Choose Save and re-score this
role.</li></ol><p>Kestrel scores the role again with the story included, so you can see
whether the fit changes.</p>
<h6>What to expect</h6><p>Kestrel keeps your words as you wrote them. It never rewrites a
story, and it never invents one. Stories also feed your interview prep pack, where each is
matched to the questions it answers.</p>
<h6>Where stories live</h6><p>Stories belong to your profile, not to one role. A story you
add for one posting is available for every later role and for interview prep. Open Profile
to read, edit or delete them. Keeping three to five strong ones is better than keeping
dozens.</p><h6>If something's off</h6><p>If the score does not move, the gap may not matter
to that role, or the story does not speak to it. Read the reasons, then rework the
story.</p>""",
            ),
        ),
    ),
    Topic(
        "privacy",
        "Your data and privacy",
        "shield",
        "g",
        (
            Article(
                "where",
                "Your data and privacy",
                """\
<p>Kestrel is built so your job search stays yours. It has no accounts and no analytics, and
nothing about you is sold or shared.</p>
<h6>Where your data lives</h6><p>Your profile, roles, ratings, documents and notes live in
one folder on this computer. Settings > Your data shows where it is and how large it is,
with Open folder to look inside.</p>
<h6>What Kestrel reaches out to</h6><ul><li>Public job boards and company career pages, to
find openings.</li><li>Public company filings, for hiring signals.</li><li>Its own site, to
learn whether a new version exists.</li><li>An online model, only if you chose
one.</li></ul>
<h6>Keep it safe</h6><ol><li>Open Settings > Your data.</li><li>Make a backup. Kestrel saves
it as one file you can keep anywhere.</li><li>To go back, choose a backup and
Restore.</li></ol>
<h6>Who else can see it</h6><p>Kestrel answers only on this computer. Settings > Privacy and
access lets you open it from your phone on your home network, using a QR code, and it is off
until you turn it on. Confidential mode hides your current employer everywhere on screen and
in documents.</p>
<h6>What you'll see</h6><p>Before anything goes online, Kestrel lists what it will send. If
you see no such list, nothing is leaving.</p><h6>What Kestrel keeps</h6><p>Kestrel also
keeps a plain log of what it did, in a logs folder you can open from About. Logs never hold
your keys.</p>
<h6>If something's off</h6><ul><li>To start fresh, use Restart setup or Clear everything in
the danger zone of Settings > Your data. It asks you to confirm first.</li><li>To share a
bug report, download diagnostics in Settings > Your data. You can open the zip first to
check what it holds.</li></ul>""",
            ),
            Article(
                "online",
                "What goes online",
                """\
<p>By default, nothing. Kestrel runs its AI on this computer.</p>
<h6>When something does go online</h6><p>Only when you pick an online model, with your own
key from OpenRouter, OpenModel, Anthropic, OpenAI or any OpenAI-compatible service. Kestrel
never uses an online model on its own.</p>
<h6>What you are asked</h6><ol><li>Kestrel lists what it will send: the posting text and the
parts of your profile that step needs, such as target titles, skills and resume
lines.</li><li>It shows the cost, and your remaining credit for OpenRouter.</li><li>You
choose to continue or cancel. Cancel changes nothing.</li></ol>
<h6>What is never sent</h6><p>Your contacts, notes and other applications stay on this
computer.</p>
<h6>What you'll see</h6><p>The disclosure appears before the first online use and again for
each deeper read. It names the model, lists the items to be sent and shows the cost. Models
also shows your spend this month, which reads $0.00 when everything runs on this
computer.</p><h6>If something's off</h6><p>To keep everything local, set each step to This
computer in Settings > This computer. You can change it at any time, and each step can use a
different choice.</p>""",
            ),
            Article(
                "conf",
                "Confidential mode",
                """\
<p>Confidential mode protects a search your employer does not know about.</p>
<h6>What it does</h6><p>It hides your current employer in everything Kestrel shows or makes:
lists, role pages, resumes, cover letters and exports. The employer name is replaced, not
just covered.</p>
<h6>Turn it on</h6><ol><li>Open Settings > Privacy and access.</li><li>Switch on
Confidential mode.</li><li>Check the preview, which shows how your employer appears
afterward.</li></ol>
<h6>What you'll see</h6><p>With the mode on, your current employer does not appear in lists,
role pages or anything Kestrel generates. PDFs carry no organization name in their
properties, headers or footers, whether the mode is on or off. Turn the mode off and the
name returns.</p><p>Use it whenever someone else might see your screen or your files, such
as a shared computer or a screen share.</p><h6>If something's off</h6><p>If your employer's
name still shows somewhere, tell us on GitHub with a screenshot that hides the rest of the
page. Kestrel also leaves company names out of the properties of every PDF it makes.</p>""",
            ),
            Article(
                "words",
                "Your resume stays in your words",
                """\
<p>Every line of a resume Kestrel makes is a line you wrote. Kestrel chooses among your
lines. It never writes new ones.</p>
<h6>How tailoring works</h6><ol><li>You keep several versions of each line in your
documents.</li><li>For a shortlisted role, Kestrel picks the version that fits
best.</li><li>You review the choices and can change any of them before you download the
PDF.</li></ol>
<h6>Cover letters</h6><p>A cover letter is a draft for you to edit. Each claim in it links
back to a line in your resume or to a story, and Kestrel flags any claim it cannot
trace.</p>
<h6>Why it works this way</h6><p>A model that writes resume lines can invent a job, a number
or a skill. Kestrel avoids the risk by only selecting. Every bullet in a PDF is your text,
character for character. If a choice cannot be matched to one of your lines, Kestrel stops
and tells you instead of filling the gap.</p><h6>If something's off</h6><p>If a choice looks
wrong, change it yourself. Your choice always wins, and Kestrel learns from it.</p>""",
            ),
        ),
    ),
    Topic(
        "models",
        "Local and online models",
        "cpu",
        "i",
        (
            Article(
                "local",
                "Local and online models",
                """\
<p>Kestrel needs an AI model to read postings. The model can run on this computer or on an
online service. You choose, and you can change your mind at any time.</p>
<h6>On this computer</h6><p>Local models run through Ollama, a free app that runs AI models.
They cost nothing and your data never leaves. Speed depends on your computer. A recent
graphics card or an Apple chip makes a large difference. A computer without one still works,
only slower.</p>
<h6>Online</h6><p>You add your own key from OpenRouter, OpenModel, Anthropic, OpenAI or any
OpenAI-compatible service. Online models are usually faster, and you pay the provider
directly, often cents per run. Before each use, Kestrel lists what it will send and what it
will cost.</p>
<h6>Both</h6><p>Everyday scoring stays on this computer, and the deeper read can go online
when you ask. Kestrel asks each time.</p>
<h6>Choose and check</h6><ol><li>Open Settings > This computer and pick This computer,
Online or Both.</li><li>For online, paste your key when asked. It is stored on this computer
only.</li><li>Open Models to see which model handles each of the four steps.</li><li>Choose
Check my models to test them.</li></ol>
<h6>What you'll see</h6><p>The Models page shows the steps in colors, with where each runs.
A shield note marks anything that leaves this computer.</p>
<h6>If something's off</h6><ul><li>If scoring is slow, try a smaller local model or an
online one.</li><li>If a key fails the test, copy it again from the provider's site and
check that your account has credit.</li><li>For more fixes, read Something isn't
working.</li></ul>""",
            ),
            Article(
                "pick",
                "Which should I pick?",
                """\
<p>Start where your computer is comfortable, then adjust.</p>
<h6>Pick This computer if</h6><ul><li>Your computer has a recent graphics card or an Apple
chip.</li><li>You want everything to stay on your machine.</li><li>You do not want to manage
a key or pay anyone.</li></ul>
<h6>Pick Online if</h6><ul><li>Scoring feels slow on your computer.</li><li>You are happy to
pay a few cents per run.</li><li>You accept that the text of postings and parts of your
profile are sent each time, after you approve.</li></ul>
<h6>Pick Both if</h6><p>You want daily runs to stay private and free, with an online model
for the deeper read on roles you care about.</p>
<h6>What you'll see</h6><p>Models shows the choice as three cards: This computer, Online and
Both. A shield note on the online card lists what leaves the computer. Your choice applies
to every step unless you override a step.</p><h6>If something's off</h6><p>Switching never
loses anything. Change the choice in Settings > This computer, then run Check my models.</p>""",
            ),
            Article(
                "grey",
                "Why is this greyed out?",
                """\
<p>A button is greyed out when Kestrel would have to do real work first, and it waits for
you to say the role matters.</p>
<h6>The usual reason</h6><p>Tailored resumes, cover letters and prep packs each take minutes
of your computer's AI time, or some online credit. Kestrel builds them only for roles you
have shortlisted.</p>
<h6>Turn it on</h6><ol><li>Open the role.</li><li>Choose Shortlist.</li><li>The greyed
buttons switch on.</li></ol>
<h6>Why Kestrel waits</h6><p>Building documents for every role found would keep your
computer busy for hours and, with an online model, cost money for roles you will never open.
Waiting for your Shortlist keeps both under your control.</p><h6>Other
reasons</h6><ul><li>No model is ready yet: open Models and finish setup.</li><li>A run is in
progress and using the model. The button returns when it ends.</li></ul>
<h6>What you'll see</h6><p>A greyed button shows a short note saying what it is waiting for.
Nothing is hidden. The same action switches on as soon as the condition is met.</p><h6>If
something's off</h6><p>If it stays grey after you shortlist, run Check my models.</p>""",
            ),
        ),
    ),
    Topic(
        "fix",
        "Troubleshooting",
        "tune",
        "w",
        (
            Article(
                "problem",
                "Something isn't working",
                """\
<p>Work through these in order. Most problems end at step 2.</p>
<h6>Try this</h6><ol><li>Look at the sidebar. The line near the bottom says whether local AI
is ready. If it is not, open Models.</li><li>On Models, choose Check my models. It tests
that each model answers, reads a whole posting without cutting it short, and gives answers
Kestrel can read. Fix whatever it flags.</li><li>On the role, choose Score again. A run that
failed shows Needs another try on Runs, with Try again.</li><li>Open Runs and read the last
run. Partial is normal. Did not run needs a closer look.</li><li>Restart Kestrel.</li><li>If
it still fails, download diagnostics in Settings > Your data and attach the zip to a GitHub
issue.</li></ol>
<h6>What you'll see</h6><ul><li>Scoring is slow: see Kestrel seems slow.</li><li>A role says
Scoring failed: see A role says Scoring failed.</li><li>Scores look wrong across the board:
the model is probably reading only part of each posting. Check my models tests for
this.</li><li>No new roles: look at Sources, and at your rules in Settings > Which roles you
see.</li></ul>
<h6>Before you start</h6><p>Nothing here deletes your data. Retrying a role, rerunning a
check and restarting Kestrel are all safe. Your ratings, documents and notes stay where they
are.</p><h6>What a check never does</h6><p>It does not change your ratings or your
documents. It only tests the models.</p>
<h6>If something's off</h6><p>Kestrel never sends your information anywhere to fix a
problem. Diagnostics stay on your computer until you choose to share them.</p>""",
            ),
            Article(
                "check",
                "Check that your models work",
                """\
<p>The model check tells you whether the AI behind Kestrel is doing its job.</p>
<h6>Run it</h6><ol><li>Open Models.</li><li>Choose Check my models.</li><li>Wait a minute or
two. Nothing leaves your computer for a local model.</li></ol>
<h6>What it tests</h6><ul><li>The model answers.</li><li>It reads a long posting to the end,
using a hidden word placed at the very end.</li><li>It gives answers in a form Kestrel can
read.</li></ul>
<h6>What you'll see</h6><p>Each test shows pass or fix. When a model reads only part of a
posting, Fix it rebuilds it with enough room and tests it again.</p>
<h6>When to run it</h6><ul><li>After you download or switch a model.</li><li>After a Kestrel
update.</li><li>When scores suddenly look off.</li></ul><p>Kestrel also suggests a check
when a new model arrives or after a block of new ratings.</p><h6>If something's
off</h6><p>If Fix it does not help, download diagnostics from Settings > Your data. If a
model keeps failing, pick another model for that step in Settings > This computer.</p>""",
            ),
            Article(
                "slow",
                "Kestrel seems slow",
                """\
<p>Slow scoring has three common causes.</p>
<h6>What to check</h6><ol><li>Open Models and see which model each step uses. Large local
models are slow on computers without a graphics card.</li><li>Choose a smaller local model,
or switch the slow step to an online model in Settings > This computer.</li><li>On Models,
behind Show details, Speed: roles scored at once sets how many roles are scored together.
Raise it only if your computer has room.</li></ol>
<h6>What you'll see</h6><p>The live card on Runs shows words such as "Rating 3 of 12". If it
says "No activity for 10 minutes", the run may be stuck. Choose Diagnostics.</p>
<h6>A few more things that help</h6><ul><li>Close other heavy programs while a run is going.
They compete with the model for your computer's memory.</li><li>Run during a break. Runs
continue while you work elsewhere in Kestrel.</li><li>Check that your computer is plugged
in. Laptops slow down on battery.</li></ul><h6>If something's off</h6><p>Kestrel scores one
kind of step at a time, so the model does not reload constantly. Mixed activity on a small
graphics card is slower.</p>""",
            ),
            Article(
                "failed",
                "A role says Scoring failed",
                """\
<p>This means the AI gave an answer Kestrel could not read, twice in a row. Kestrel keeps
the role and sets the raw answer aside. Nothing is lost.</p>
<h6>Fix it</h6><ol><li>Open the role.</li><li>Choose Score again.</li><li>If it keeps
failing, open Runs and find the card Needs another try. Choose Try again, or choose another
model.</li></ol>
<h6>After you retry</h6><p>The role moves out of Needs another try and into your
history.</p>
<h6>Why it happens</h6><p>Kestrel asks the model for a strict, structured answer so it can
check every field. Small or overloaded models sometimes ramble. Kestrel asks once more and,
if the answer is still unreadable, stops and keeps the role so you can retry. It also keeps
the raw answer, so a developer can look at it if you send diagnostics.</p><h6>What you'll
see</h6><p>The role shows Scoring failed with the reason in plain words, and Runs lists it
under Needs another try. Nothing is hidden from your Pipeline.</p><h6>If something's
off</h6><p>If many roles fail, run Check my models. A model that does not follow
instructions needs to be swapped.</p>""",
            ),
            Article(
                "diag",
                "Send diagnostics with a bug report",
                """\
<p>Diagnostics are one zip file of recent logs, made on your computer.</p>
<h6>Make the file</h6><ol><li>Open Settings > Your data.</li><li>Choose Download
diagnostics.</li><li>Save the zip where you can find it.</li></ol>
<h6>Send it</h6><p>Open an issue on GitHub and attach the zip. Add what you did, what you
expected and what happened. Your version is on the About page.</p>
<h6>What it is for</h6><p>The zip helps someone see what Kestrel did, in order, around the
time of the problem. Logs list steps, timings and errors. They are plain text.</p><h6>What
you'll see</h6><p>A zip file with a name that includes the date. It is saved where you
choose. A copy of your versions is also on the About page under Component versions, with a
Copy versions for a bug report button.</p><h6>If something's off</h6><p>You can open the zip
first to see what is inside. Kestrel never sends it for you.</p>""",
            ),
        ),
    ),
)
