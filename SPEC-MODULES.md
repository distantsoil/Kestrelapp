# Kestrel — Module Specification

> Functional modules built on the core engine in `SPEC.md`. Read `SPEC.md` §0-§4 first;
> everything here assumes that data model and those principles.

| § | Module |
|---|---|
| 1 | Document engine — parametric resume and capability statement |
| 2 | Research, contacts, and the networking graph |
| 3 | Interview preparation and the story bank |
| 4 | Negotiation and offer analysis |
| 5 | The learning loop |
| 6 | Intelligence — compensation, ghost jobs, liveness |
| 7 | Analytics |

---

## 1. Document engine

One source of truth per track. Every output document is a function of pre-authored content and a
selected configuration. Nothing is generated from scratch.

### 1.1 The parametric model

```
document(config) -> HTML -> PDF

config = {
  "layout": "executive",
  "summary": "transformation",
  "slots": {"r1b1": "cost", "r1b2": "security", "r1b3": "hide", "r2b1": "scale"},
  "skill_groups": {"identity": true, "security": true, "networking": false},
  "emphasis_order": ["r1", "r2", "r3"],
  "redact_orgs": false,
  "font": "Cambria",
  "header_align": "center",
  "accent": "#1F3A5F"
}
```

`font` and `header_align` are optional and presentation only (D-DOC-1): absent, the layout's own
default applies. `font` is one of a fixed list of fonts that ship with Windows and render in Edge
(Georgia, Cambria, Garamond, Calibri, Arial, Segoe UI; `kestrel.docs.styles.FONTS`); any other
value is ignored, never written into the stylesheet. `header_align` is `left` or `center`.
`accent` (D-DOC-4) is the color of headings, rules and markers: exactly `#rrggbb`, anything else
ignored. Precedence: the config's `accent`, else the master's saved `look.accent`, else the
layout's own. The Documents page offers presets (Navy, Slate, Forest, Burgundy, Charcoal, Teal,
Orange) and a custom hex value, and saves the choice on the master.

**Invariant, and the most important correctness property in the system:** the renderer emits
`doc_variant.text` byte-for-byte. No model output ever reaches a PDF. If a variant needs
rewording, the operator edits it in the UI and it becomes canonical for every future render.

### 1.2 Schema

**`doc_master`** — `id`, `track`, `name`, `doc_kind` (`resume` | `capability_statement`),
`header` json (name, contact, links), `summary_variants` json (`[{key, text}]`), `skill_groups`
json (`[{key, label, items[], default_visible}]`), `is_active`, `look` json (`{accent}`,
D-DOC-4), and four optional sections (D-DOC-3), each a json list of the operator's typed entries:
`education` (`institution`, `credential`, `field`, `dates`, `detail`), `certifications`
(`name`, `issuer`, `year`, `credential_id`), `licenses` (`name`, `issuer`, `year`), `projects`
(`name`, `role`, `dates`, `description`, `link`). The first field of each is required. These are
printed exactly as typed, by every layout, only when non-empty, and are never part of the P3
selection schema: present means included. A layout's `section_order` may name them
(`education`, `certifications`, `licenses`, `projects`); any it does not name follow the rest in
that order. `education` also prints legacy `credential` sections under the same heading.

**`doc_layout`** — `id`, `key` unique, `label`, `description` (shown to the model during
selection), `template_file` (stored but never read; unused), `section_order` json, `max_roles_shown`.

Ship eight:

| key | doc_kind | shape |
|---|---|---|
| `executive` | resume | summary, experience, skills, education. Two pages. |
| `condensed` | resume | one page, top two roles, no summary |
| `technical` | resume | skills promoted above experience |
| `classic` | resume | traditional: summary, experience, education and credentials, skills |
| `modern` | resume | summary, experience, projects, skills, education |
| `compact` | resume | dense one-page fit: summary, skills, experience, credentials |
| `capability` | capability_statement | positioning, service lines, proof engagements, differentiators, contact |
| `capability_short` | capability_statement | one page, three service lines, two proofs |

Each layout also has its own look (D-DOC-1, `kestrel.docs.styles.LAYOUT_STYLES`), so they
read as different documents and not one stylesheet with different sections. Every look draws its
headings, rules and markers from the accent (`--accent`); the column below is each layout's
default accent:

| key | look | default font | name and headline | default accent |
|---|---|---|---|---|
| `executive` | small caps headings, generous spacing, thin rules | Cambria | centered | `#222222` |
| `condensed` | smaller type, tight spacing, dates inline, one page bias | Calibri | left | `#111111` |
| `technical` | modern: sans serif, accent headings and rules | Segoe UI | left | `#D4622A` |
| `classic` | traditional: serif, uppercase name over a double rule | Garamond | centered | `#222222` |
| `modern` | clean: sans serif, name in the accent, short accent bars | Calibri | left | `#1F3A5F` |
| `compact` | dense: small type, dates inline, accent band headings | Arial | left | `#333333` |
| `capability` | classic: serif, uppercase headings, simple rules | Georgia | left | `#222222` |
| `capability_short` | condensed | Arial | left | `#111111` |

A layout the operator adds under another key reads classic. Startup seeding inserts missing
shipped layouts and brings an existing shipped row's label, description, section order and role
cap up to date; rows under other keys are never touched. There is no two-column layout: a second
column needs either a layout table or a floated column, and both break ATS reading order.

**`doc_section`** — a role on a resume, or a service line / proof engagement on a capability
statement. `id`, `doc_master_id`, `section_kind` (`role` | `service_line` | `proof_engagement` |
`credential`), `org_label`, `org_label_redacted`, `title`, `start_date`, `end_date`,
`context_line` (one line of scale and scope, always shown), `sort_order`.
`org_label_redacted` and `hide_names` are set by the operator (D-PRIV-1): the role editor's
"This is my current employer" box with its "Show it as" field and its names-to-hide chips, or the
import's opt-in for an open-ended newest role. At most one employer per `doc_master` is current;
confidential mode hides exactly the listed names.

**`doc_slot`** — `id`, `doc_section_id`, `key` (stable, e.g. `r1b2`), `sort_order`, `optional`,
`default_variant_key`.

**`doc_variant`** — `id`, `doc_slot_id`, `key`, `text` (verbatim, as it will print), `tags` json,
`derived` bool (true if it originated as a model rephrasing during import).

**`doc_render`** — `id`, `opportunity_id` nullable, `doc_master_id`, `config` json, `rationale`
json (per-slot model reason), `template`, `pdf_path`, `filename`, `page_count`,
`defaults_used` bool, `created_at`.

### 1.3 Selection flow

1. Selection runs on demand: `kestrel render <master> --opportunity <id>` or the Tailor page on
   the role. Nothing queues a `doc_select` task today, so an `apply`/`pursue` verdict does not
   trigger it by itself.
2. The prompt receives the key requirements from evaluation Block B (employment) or the
   engagement thesis from Block B (consulting), plus, for each slot, the available variants as
   `{key, tags, text}`.
3. The model returns `{layout, summary, slots, skill_groups, rationale, coherence_check}`.
4. **The schema is generated dynamically per `doc_master`, and this is not optional.**

   A generic schema using `"slots": {"type": "object", "additionalProperties": {"type": "string"}}`
   is an open-ended map. Constrained decoding gives the model no guidance on the valid key set,
   `{"slots": {}}` is grammar-valid but validation-invalid so it burns a repair on every call,
   and a hallucinated variant key is only caught after the fact. This is the schema protecting
   the single most important invariant in the system, so it must make the failure structurally
   impossible rather than merely detectable.

   Build it at call time from the live catalog:

   ```json
   "slots": {
     "type": "object",
     "required": ["r1b1", "r1b2", "r2b1"],
     "properties": {
       "r1b1": { "type": "string", "enum": ["cost", "scale", "security"] },
       "r1b2": { "type": "string", "enum": ["security", "compliance", "hide"] },
       "r2b1": { "type": "string", "enum": ["migration", "vendor"] }
     }
   }
   ```

   `"hide"` appears in the enum only where `slot.optional` is true. `skill_groups` and
   `rationale` are generated the same way, with explicit `properties` keyed by the real group and
   slot keys. `layout` and `summary` become enums of the real available keys.

5. **Validation remains strict as a second line of defense**, because a backend running
   `json_object` mode rather than `json_schema` has no grammar to enforce anything. Every
   returned slot key must exist; every value must be a known variant key for that slot, or
   `"hide"` where optional. A hallucinated key is a validation failure surfaced in the UI,
   **never a silent fallback**. On repair failure, render with defaults and set `defaults_used`,
   flagged prominently.
6. Render, produce PDF, store the render with its rationale.
7. The operator reviews: chosen variant beside each alternative, with the model's reason, one
   click to swap. **Every swap writes a `selection_override` row** (§5.1).

### 1.4 Rendering

- Jinja2 template producing semantic HTML with a print stylesheet
  (`@page { size: Letter; margin: 0.5in }`).
- Playwright `page.pdf(print_background=True, prefer_css_page_size=True)`, with **explicitly
  empty `header_template` and `footer_template`**.
- **ATS safety rules the template must obey:** single column body, no text inside images, no
  tables used for layout, real Unicode bullets, selectable text, standard section headings
  (`Experience`, `Education`, `Skills`).
- **Privacy:** no generator string in the HTML, no organization name in PDF metadata (see
  `SPEC.md` §9). `redact_orgs: true` swaps the marked current employer's `org_label` for its
  `org_label_redacted` everywhere it appears in the document; other employers print as written
  (D-PRIV-1).
- A `?preview` route renders the identical HTML in the browser. Not an approximation.
- **Page-count guard:** count pages after generation; over `settings.docs.max_pages` warns and
  suggests which optional slots to hide. The suggestions are every optional slot that is currently
  shown, in catalog order. They are not ranked by confidence; the selection prompt returns no
  per-slot confidence to rank by.
- Filename: `{LastName}_{Org}_{RoleSlug}.pdf`.

### 1.5 Import

The operator uploads (.docx preferred, .pdf, .md, .txt, .doc) or pastes an existing resume or
capability statement. It is converted to markdown, shown to the operator, then mapped by
`PROMPTS.md` P7 **by line reference** (D-IMPORT-3): an outline call finds the summary, roles and
skills, then one call per role maps its bullets, and the app copies the referenced lines.

- Every imported variant is the operator's own text, copied byte for byte; the model never
  writes resume text. The verbatim check still runs and flags anything non-verbatim in red.
- An invented or out-of-role line reference is an error shown to the operator, never defaulted.
- A bullet line that no slot took is listed in the review ("not placed in any slot").
- Import creates one variant per slot. Alternate wordings (at most two `derived: true` per slot,
  never changing a number, scope, technology, name or timeframe) are suggested per slot on
  demand and added one at a time by the operator.
- Proof points are extracted at the same time and written to the profile; a metric is kept only
  when it appears in the bullet.
- PDF conversion drops repeated page headers, footers and page numbers and rejoins wrapped
  bullets; the review screen says .docx gives the cleanest result.
- The import reports its stages (Uploading, Reading, Processing) and a failed proposal can be
  retried on the text already read.
- **Nothing writes to the database until the operator accepts the proposal.**

**Editing after import (D-UI-29).** The Documents tab adds a role (company, title, dates,
context line, and its first bullet, which is required) at the end of the master, and adds a
bullet at the end of any role. Both store the operator's text as the slot's one `original`
variant, `derived: false`, and generate a slot key from the text's first words, unique across the
master (`r2_led_the_migration_of`). Removing a bullet or a role asks for confirmation and deletes
its variants with it; an empty master is allowed. Slot and variant keys are internal identifiers:
the tree shows "Bullet N" with a readable label and keeps the keys as tooltips and data
attributes.

**Assessment (D-DOC-2, `PROMPTS.md` P13).** "Assess this resume" sends the master and the
operator's own target titles and must-haves to the tier 2 model and shows overall fit, strengths
(citing bullets), gaps a hiring manager for those titles will look for, and bullets to
strengthen with the reason. It is advisory and displayed only: no replacement text exists in its
schema and nothing it returns is stored or rendered. The page names the model that ran it.

### 1.6 Cover letters and engagement outlines

Generated prose is permitted here, under constraint.

**Cover letter** (employment): every factual claim must trace to a proof point or a doc variant;
the prompt supplies only those and forbids asserting anything else. 250-350 words, markdown, in
an editor, operator edits before rendering. A `claims_used` array is returned and every entry
must resolve to a real key; any that does not flags the draft "unverified claims present". A
banned-phrase check runs on the body.

**Engagement outline** (consulting): situation, proposed scope, phases with durations, named
deliverables, commercial model (day rate × days, or fixed fee, or retainer), assumptions, and
explicit exclusions. Same claim-tracing constraint. **It is an outline for a conversation, not a
priced proposal**, and the UI says so.

**The system never sends either.**

---

## 2. Research, contacts, and the networking graph

### 2.1 Company research

**`org_research`** — `id`, `org_id`, `summary_md`, `signals` json, `unknowns` json,
`angle` text, `sources` json, `researched_at`, `ttl_days`, `insufficient_sources` bool.

Source acquisition, in order, passing whatever is obtained to the model with its URL:

1. **Direct fetch of company URLs.** From `org.url` plus conventional paths: `/about`,
   `/careers`, `/team`, `/leadership`, `/news`, `/blog`, `/investors`. httpx first, Playwright
   (`channel="msedge"`) on a JavaScript shell.
2. **The ATS board itself**, which usually carries a company blurb and reveals every open role.
   A company posting five IT roles at once is a signal regardless of what they are.
3. **SEC EDGAR** for public companies: recent 8-K and 10-K risk factors. Free, verified, and the
   most factually reliable source available.
4. **Optional SearXNG** at `settings.research.searx_url`, which the operator may already run.
   Skipped when unset.
5. **Operator paste.** A text area on the research page. **This is the guaranteed path** and the
   reason the feature is never network-blocked.

The model **synthesizes supplied excerpts and does not browse.** Every substantive claim carries
a `source_index` resolving to a supplied excerpt. Fewer than two sources returns
`insufficient_sources: true` rather than a synthesized answer.

Signals are categorized (`ownership`, `funding`, `growth`, `layoffs`, `leadership_change`,
`tech_direction`, `culture`, `compliance`, `m_and_a`, `risk`) and **written back onto the `org`
record**, so they accumulate across every opportunity at that organization and feed the
consulting track's multi-signal promotion rule.

**Company summary (P16, D-UI-123, D-LLM-9).** The company page (`SPEC-INTERFACES.md` §2.1
`/orgs/{id}`) opens with a short summary written from the same public sources, so the operator
reads three or four sentences instead of the research memo. It is a separate, cheaper pass than
P5: P5 is the deep synthesis behind a role or a Research click; P16 is a labeled overview that
exists for every company the operator opens or follows.

**`org_summary`** — `id`, `org_id`, `sentences` json (an ordered array of `{text, source_indexes}`),
`tags` json (an array of `{key, text, source_indexes}` with `key` one of `does`, `serves`,
`direction`, `hiring`; a tag is left out when the sources do not say), `sources` json
(the excerpts used: `{index, url, title, kind, fetched_at}`), `model`, `prompt_version`,
`generated_at`, `insufficient_sources` bool, `source_count` int, `status` (`ok` | `insufficient` |
`quarantined`).

- Sources are acquired exactly as in the list above (direct fetch of company URLs, the ATS board,
  SEC EDGAR, optional SearXNG, operator paste), politely and with `robots.txt` respected, and
  refreshed about once a week; **no new fetch behavior** is added by the summary. The company page
  can show a source list only from this set.
- Fewer than two sources: `insufficient_sources` is true, **no model call is made**, and the page
  says "Not enough public information to write a summary yet. Kestrel found N sources."
- **The model summarizes supplied excerpts only; it does not browse and never invents.** Every
  sentence carries at least one `source_index` that resolves to a supplied excerpt, and the page
  shows it as a numbered marker that opens the source and the date it was seen. Any figure, name or
  date in a sentence must appear in one of the excerpts it cites. A failure of these checks is a
  validation failure: one repair call, then a quarantine row (`tier` 16, raw response kept) and the
  page keeps the previous summary or shows the not-enough state, never an unsourced sentence.
- Contacts and people names come only from the supplied excerpts and are not part of the summary.
- A **Refresh** control queues a new `org_summary` task (resumable, a `task` row). A stale summary
  (older than `ttl_days`) is labeled with its date.
- It runs on the tier-2 model. When Tier 2 is a hosted model, nothing is sent until the operator
  presses Refresh (no automatic pass sends data online without a user action, as P15).
- The page labels it "Written by the AI on this computer" (or names the online model), says it
  "can be wrong, so check the sources", and never presents it as a fact table: the public facts
  (headcount, headquarters, filings) are shown separately with their own sources.
- Public pay on the company page is arithmetic over `comp_observation` rows (§6.1) scoped to the
  organization, with the usual under-five rule, not model output.

### 2.2 Contacts

**Background work (D-UI-167).** Role-page Company research and Find contacts enqueue
`org_research` and `org_contacts` task rows, respectively. The worker drains them together on
Tier 3 after Tier 2 work, never beside another model tier. Progress and Cancel appear in the
Role page's Organization tab. A restart reclaims unfinished leases; results and completion
commit together so a restart cannot duplicate contacts or lose completed research. Cancel
stops in-flight work and discards uncommitted results. Existing results remain visible.
Online approval is bound to the configured backend, URL and model. The worker reloads settings
and checks Hosted mode and confidential handling again before sending. A changed destination
or missing approval holds the task for a new explicit action, never silently sends it.

**`contact`** — `id`, `org_id`, `name`, `title`, `contact_type` (`hiring_manager` | `recruiter` |
`peer` | `exec` | `referral` | `board` | `pe_operator`), `linkedin_url`, `email`, `phone`,
`source`, `source_url`, `confidence`, `notes`, `relationship` (`none` | `weak` | `known` |
`strong`), `last_contacted_at`, `created_at`.

Discovery rules, enforced in validation, not just in the prompt:

- **Never invent a person.** Every contact must carry a `source_index` resolving to supplied
  text.
- **Never construct or guess an email.** If it is not present verbatim in a source, it is null.
  No pattern-guessing from a domain. The LinkedIn connections import (§2.3) **never stores an
  email address**: the column is dropped when the file is read.
- Rank by usefulness for this opportunity: hiring manager for the function beats generalist
  recruiter beats peer beats unrelated executive. For consulting: economic buyer beats
  operating partner beats functional lead.
- Confidence is honest. Inferring "probably the hiring manager" from a title alone is medium at
  best.

**`outreach_draft`** — `id`, `opportunity_id`, `contact_id`, `channel` (`linkedin` | `email` |
`other`), `subject`, `body`, `status` (`draft` | `copied` | `sent_manually` | `discarded`),
`created_at`. Drafts only. LinkedIn messages capped at 300 characters. **The system never
sends, and no automation touches any social platform.**

### 2.3 Networking graph

**`connection`** — the operator's own network. `id`, `contact_id`, `how_known`, `strength`,
`last_interaction_at`, `notes`.

**`interaction`** — append-only. `id`, `contact_id`, `opportunity_id` nullable, `kind`
(`message` | `call` | `meeting` | `email` | `event`), `direction`, `occurred_at`, `summary`,
`outcome`.

**Seeding: the connections import (D-UI-115).** The operator exports their LinkedIn connections as
CSV (a legitimate first-party data export offered by the platform; **no scraping, no automation,
Kestrel never signs in to LinkedIn**) and imports it from Settings > Your data > Your contacts
(`SPEC-INTERFACES.md` §2.6). Fields read are name, company, position and connected-on date. The
file's email column is **dropped at read time and never stored**, so a contact created by this
import has `email` null. The preamble lines a real export prepends (the "Notes:" lines and a blank
line) are skipped. Nothing is stored until the operator presses Apply. All of it stays on the
operator's computer; the import is a single first-party file the operator chose, not "public data"
in the sense of hard rule 7, and holds only the fields above.

Two tables record it:

- **`contact_import`** — `id`, `imported_at`, `file_name`, `row_count`, `n_new`, `n_changed`,
  `n_missing`, `kept_missing` bool. One row per applied import. The file itself is not kept.
- **`connection_position`** — `id`, `contact_id`, `company_name` (as in the file), `org_id`
  nullable, `match` (`sure` | `possible` | `manual` | `none`), `title`, `connected_on`,
  `operator_decided` bool (the operator ruled on this company name, so a later import reuses the
  answer; this tells a remembered Not a match apart from a name that simply had no match yet),
  `first_import_id`, `last_import_id`, `is_current` bool. A person has one current row and zero or
  more earlier rows. The profile URL column is not read either; only name, company, position and
  connected-on are kept.

*Company matching.* Each distinct company name in the file is matched to an `org`. A match is
**Sure** when the normalized names are equal (case, punctuation and common suffixes such as Inc,
LLC, Co and Corp ignored; "&" equals "and") and **Possible** when one name contains the other or
they share their leading words ("Cedar Ridge" and "Cedar Ridge Foods"). A Possible match is
shown for the operator to confirm, change to a similar company, or mark Not a match; the choice
is remembered (`match` becomes `manual` or `none`) and reused by later imports. Contacts the import creates
carry `contact.source` = `linkedin_import`; Remove all contacts deletes only those and their
`connection_position` rows. An unmatched
company is kept for later, and a person with no company is counted but not matched. No fuzzy match
is ever applied silently.

*Phase 1: import and diff.* The first import creates contacts and current positions. A newer file
is compared with the stored state, by name plus connected-on date, and reports **new** people,
people whose company or position **changed**, and people **not in this file**. For people not in
the file the operator chooses to keep them or remove them. Applying writes the changes in one
transaction and records a `contact_import` row.

*Phase 2: history.* When a person's company changes, the earlier position is kept with
`is_current` false and the import that last saw it, so Kestrel can say a person used to work at an
organization. The only history is what earlier imports contained; Kestrel does not look anyone up.

**Warm path finding.** Given a target org, surface in ranked order:

1. **Works here now:** direct connections whose current position matches the org.
2. **Used to work here:** direct connections whose earlier position, from an earlier import,
   matches the org.

Each line shows the person's name, title, how long ago the file was imported and the match
confidence (a Possible match that the operator has not confirmed is labeled as such). This is a
graph query over `connection_position`, no ML needed. **Two further paths are deferred** (see
the private decision log, Deferred): connections at a company that shares an investor with the org (PE
portfolio overlap), and contacts who share a former employer with a known decision maker at the
org. Both need data the import does not carry and raise public-wording questions (Kestrel reads
only a file the operator downloads), so neither is added without a spec decision first. No path is
second-degree.

Nothing here contacts anyone. A drafted note is text the operator copies and sends themselves
(hard rule 1).

---

## 3. Interview preparation and the story bank

### 3.1 The story bank

Every Tier 2 employment evaluation emits STAR+Reflection sketches in Block F. Individually they
are disposable. Consolidated they become the operator's actual interview preparation.

**`story`** — `id`, `title`, `situation`, `task`, `action`, `result`, `reflection`,
`proof_point_key` nullable, `themes` json, `use_count`, `is_canonical`, `operator_edited`,
`embedding_id`, `created_at`, `updated_at`.

**`story_usage`** — `id`, `story_id`, `opportunity_id`, `question`, `created_at`.

**Consolidation.** A `consolidate_stories` task runs on demand and after every N evaluations:

1. Embed every Block F sketch produced since the last consolidation.
2. Cluster by cosine similarity above `settings.stories.merge_threshold` (default 0.88).
3. For each cluster, propose a single merged canonical story to the operator, showing the
   sketches it came from.
4. The operator edits it into their own words. `operator_edited` becomes true and **an
   operator-edited story is never overwritten by a later merge**; new sketches attach to it as
   usage instead.
5. Target 8-12 canonical stories. The UI shows coverage: which common behavioral themes
   (conflict, failure, influence without authority, turnaround, prioritization under
   constraint, difficult stakeholder, technical judgment, team building) have a strong story and
   which have a gap.

**Gap analysis is the point.** Knowing you have no good failure story is more useful than
having forty mediocre ones.

**Stories the operator writes (D-UI-97, D-UI-121).** The operator can add a story from a gap or
evidence line on a role, from Interview prep, or from Profile > Stories. The sheet has three
labeled boxes (what was the situation, what did you do, what changed), stored as `situation`,
`action` and `result` exactly as typed; `task` and `reflection` stay empty unless the operator
fills them. The story is saved as `operator_edited` and canonical, so no later merge overwrites it,
and Kestrel never rewrites it. "Save and re-score this role" queues a single-role re-score task
that includes the new story. The bank is shown as the Story bank tab of Interview prep and the
Stories tab of Profile; it is one bank.

### 3.2 Prep packs

**`prep_pack`** — `id`, `opportunity_id`, `content_md`, `stories` json (mapped question →
story_id), `questions_to_ask` json, `panel` json, `pdf_path`, `created_at`.

**`mock_session`** — `id`, `opportunity_id`, `mode` (`behavioral` | `technical_leadership` |
`executive_panel` | `consulting_discovery`), `started_at`, `ended_at`, `question_count`,
`mean_scores` json, `transcript` json (ordered `[{question, answer, critique, scores,
unsupported_claims}]`), `notes`.

Generated on demand for a shortlisted opportunity. Assembles: the role summary, the research
signals, the Block B gaps and their mitigations, the mapped canonical stories for the likely
questions, questions the operator should ask (drawn from research unknowns, which is what makes
them specific rather than generic), and any known panel members from `contact`.

Renders to PDF via the same pipeline. Practically, this is what the operator reads on the
morning of the interview.

**Interview questions on Applied (P14, D-INTERVIEW-1).** When the operator marks a role Applied,
a queued `interview_questions` task asks the Tier 2 model for a focused list of likely questions
for that role (`PROMPTS.md` P14), each optionally linked to one of the operator's real stories
and proof points by id. **`interview_question_set`** — `id`, `opportunity_id`, `pursuit_id`,
`prompt_version`, `questions` json, `created_at`, `superseded_at` nullable; one active set (null
`superseded_at`) per pursuit. The trigger is the applied branch of `move_to_status` only, so
every way of marking Applied (the verb button, the stage select, a drag, an offer) fires it once;
a bulk import of past applications does not. Regenerating writes a new set and supersedes the old
one only after the new one validates.

Once a set exists it **supersedes Block F for that role**: the prep pack, the mock interview's
first question and the debrief's predicted-vs-asked hit rate use the P14 set. Block F stays the
source for roles that are not Applied and for any Applied role with no set yet (the task has not
run, or its reply was quarantined).

### 3.3 Mock interview

A conversational loop, not a document.

1. The operator picks an opportunity and a mode: behavioral, technical-leadership,
   executive-panel, or consulting-discovery.
2. The model asks one question at a time, drawn from the prep pack.
3. The operator answers in text.
4. The model critiques against an explicit rubric: structure (did it follow STAR), specificity
   (were there concrete numbers), relevance (did it answer the question asked), length, and
   whether the claimed content matches a known proof point. **It flags any claim the operator
   made that is not in their own proof points**, which catches drift before it reaches a real
   interview.
5. Session stored as `mock_session` with a transcript and per-answer scores, so improvement is
   visible over time.

### 3.4 Debrief

After a real interview the operator captures a debrief: what was asked, what went well, what did
not, who was in the room, next steps, and read on the role. This writes `interaction` rows,
updates `pursuit`, adds any newly discovered contacts, and **feeds the learning loop as outcome
data** (§5.6). Questions actually asked are compared against questions predicted, and the hit
rate is reported in analytics — a direct measure of whether the predicted questions (the P14
set when present, else Block F) are any good.

---

## 4. Negotiation and offer analysis

### 4.1 Schema

**`offer`** — `id`, `pursuit_id`, `base`, `bonus_target_pct`, `equity_description`,
`equity_value_estimate`, `signing`, `benefits_notes`, `pto_days`, `start_date`, `deadline`,
`level_title`, `reports_to`, `location_policy`, `received_at`, `status`
(`verbal` | `written` | `accepted` | `declined` | `expired`), `notes_md`.

For consulting: **`engagement_offer`** — `id`, `pursuit_id`, `day_rate`, `days_committed`,
`duration_weeks`, `payment_terms`, `expenses`, `renewal_terms`, `exclusivity`, `ip_terms`,
`notice_period`, `received_at`, `status` (`verbal` | `written` | `accepted` | `declined` |
`expired`), `notes_md`.

### 4.2 Benchmarking

Grounded in the operator's own accumulated data, not model priors:

- The comp corpus (§6.1) for the normalized title, geography bucket, and organization size band.
- The stated band from the original posting, and whether the offer landed inside it.
- Any competing offers currently open.
- Historical: what the operator has been offered before, and what they accepted.

Output is a range with an explicit sample size. **When the sample is under five, say so and
lower the confidence rather than presenting a precise-looking number from three data points.**
The interface words it "Not enough data yet" with the count and hides the bracket, and the same
rule applies before any offer exists: the Offers page's "What roles like yours pay" view shows
ranges against the operator's floor and target with the sample size in words, and "Too few" under
five (D-UI-122).

### 4.3 Scripts

Generated for the standard situations, in the operator's voice, always as a draft to edit:

- Deflecting the first-number question
- Countering below-target with a specific justified number
- Pushing back on a geographic discount
- Using a competing offer without bluffing
- Equity versus cash trade
- Start date and notice period
- Title and level, which for a senior-track candidate is frequently worth more than the base
- Consulting: rate versus scope, payment terms, renewal option, exclusivity

Each script includes the ask, the justification drawn from real proof points, and a stated
walk-away position the operator sets. **The model does not decide the walk-away number; the
operator does, and the model works within it.**

### 4.4 Offer comparison

When more than one offer is open, a comparison matrix across total first-year compensation,
four-year value, level and scope, reporting line, location and travel, organizational risk from
research signals, and trajectory fit (shown to the operator as career direction). Weights are the
operator's, adjustable live. The matrix states how each money row is computed: first-year total is
base, target bonus, signing bonus and one fourth of the equity estimate; four-year value assumes no
raises (D-UI-122). Judgment rows are the operator's own star ratings shown beside the source
fact; the summary reads "Ahead on ..." and says it reflects the operator's weights and ratings.

Presented as analysis, never as a recommendation to accept. This is a life decision and the
tool's job is to make the trade-offs legible.

---

## 5. The learning loop

The mechanism that makes the system worth more in month six than in week one. All machinery is
built up front; the models it trains come online as data accumulates.

### 5.1 What is captured

**`verdict_override`** — the operator disagrees with a score or verdict.
`id`, `opportunity_id`, `evaluation_id`, `system_verdict`, `system_score`, `operator_verdict`,
`operator_score`, `reason` text, `dimension_corrections` json (optional per-dimension), `created_at`.

**`selection_override`** — a resume or capability slot changed after model selection.
`id`, `doc_render_id` (nullable, FK ON DELETE SET NULL), `opportunity_id`, `slot_key`,
`model_choice`, `operator_choice`, `model_choice_text`, `operator_choice_text` (the wording each
choice key stood for, copied when the row is written), `created_at`. A row outlives its render
(D-DOC-7): deleting a resume detaches the rows (null `doc_render_id`) and keeps them unless the
person ticks "Also forget the N choices Kestrel learned from this resume". Readers skip the
render-dependent parts of a detached row and keep the learning signal.

**`label`** — an explicit gold judgment, usually from the active-learning queue.
`id`, `opportunity_id`, `track`, `score`, `verdict`, `dimensions` json nullable, `rationale`,
`source` (`active_learning` | `manual` | `derived_from_outcome`), `created_at`.

**`outcome`** — what actually happened. `id`, `pursuit_id`, `opportunity_id`, `final_status`,
`furthest_stage`, `days_to_first_response` nullable, `rejection_reason`, `operator_retrospective`,
`recorded_at`.

Every one of these is cheap to capture at the moment the operator is already acting. **The UI
must never present labeling as a separate chore**; it is a one-click side effect of reviewing.

**How the interface asks (D-UI-99).** Calibrate asks "Good match?" and takes a rating of Poor,
Weak, Maybe, Good or Great, stored as `label.score` 1 to 5, with the system's score hidden until
the rating is given. The optional "Tell Kestrel why" stores reason chips and per-dimension nudges
as `label.rationale` and `label.dimensions`, and a verdict derived from the score with the
operator's cutoffs (SPEC.md §7.3). The role page's "Your view" card (Yes or Adjust) and the four
"Not for me because..." reasons in Pipeline write the same records; shortlisting a low score and
hiding a high score for a fit reason are read implicitly as "too low" and "too high". Skipping a
queue item writes nothing and can be undone. Ratings can be changed later, and a changed rating
replaces the label and is shown as Changed in the history.

### 5.2 Weight recalibration

When labeled examples reach `settings.learning.min_labels_for_calibration` (default 30):

1. Fit dimension weights to minimize error between `Σ(dimension × weight)` and the operator's
   own score, using ridge regression with non-negativity and a sum-to-one constraint.
2. Report current versus proposed weights, and cross-validated mean absolute error for each.
3. **The operator accepts or rejects.** Never applied silently. The interface shows the weights
   as read-only bars with no manual sliders: the only way a weight changes is an offered
   suggestion, where the operator presses Accept or Not now (D-UI-108). Before offering one, the
   system replays the operator's past ratings with the proposed weights and offers it only if they
   match the operator's calls better. An accepted change can be undone, and "Restore original
   weights" returns the shipped defaults.
4. Store as **`calibration`** — `id`, `track`, `created_at`, `n_samples`, `previous_weights`
   json, `proposed_weights` json, `cv_mae_previous`, `cv_mae_proposed`, `status`
   (`proposed` | `accepted` | `rejected`), `decided_at` — so any regression is traceable and
   revertible.

Runs per track independently. The consulting weights will diverge from the employment weights,
and that divergence is itself informative.

### 5.3 Few-shot example selection

The cheapest large quality win available, and it should be built early.

Few-shot retrieval starts once `settings.learning.min_labels_for_few_shot` (default 15, deliberately
lower than the calibration bar) labeled examples exist.

For every Tier 2 call, retrieve `settings.learning.few_shot_k` (default 3) past evaluations
where the operator either confirmed the score or corrected it, selected by embedding similarity
between the current description and the past one, with two constraints:

- Same track.
- **Diversity across verdicts.** Do not inject three `apply` examples; a model shown only
  positives inflates. Prefer a spread across the score range near the current item.

Injected as compact `{summary, dimensions, holistic, verdict, one-line rationale}`, not full
evaluations, to control token cost. The IDs used are recorded in `evaluation.few_shot_ids` so
any result is reproducible.

Below the minimum label count, run zero-shot and record that. In the interface this stage reads
"use your ratings as examples" and has an on and off switch (D-UI-108).

### 5.4 The learned reranker

Replaces the Tier 1 LLM call once it earns the right.

**Features:** the opportunity embedding (reduced to 64 dimensions by PCA fitted on the corpus),
plus structured features — `seniority_rank`, comp min/max/known-flag, `pre_score`, each soft
signal as a binary, source kind one-hot, `remote_type`, description token count, org size band,
org ownership, `seen_count`, days since posted, and for consulting the trigger event type and
confidence.

**Target:** the operator's score where labeled, otherwise the Tier 2 holistic where the operator
did not override it.

**Model:** `sklearn.ensemble.HistGradientBoostingRegressor`. No torch, no GPU, trains in seconds,
bundles cleanly into a PyInstaller build, and is the correct tool for tabular plus reduced-embedding
features at this data scale.

**Promotion criteria, all of which must hold on a held-out split:**

1. At least `settings.learning.min_labels_for_reranker` labeled examples (default 200).
2. Held-out mean absolute error below `settings.learning.reranker_max_mae` (default 0.6).
3. **Its keep/kill agreement with the operator at the Tier 1 threshold beats the Tier 1 LLM's
   agreement on the same held-out set.** Beating the LLM is the bar, not beating chance.
4. Recall on `apply`-verdict items above 0.95. **Missing a good opportunity is the one error
   that matters**, so the threshold is set for recall and the resulting false positives are
   accepted; they simply cost a Tier 2 call.

Once promoted, Tier 1 becomes a millisecond operation. Items where the reranker's prediction
falls within `settings.learning.reranker_uncertain_band` of the threshold still escalate to the
LLM. The LLM path is never deleted; it remains the cold-start behavior and the fallback.

**`ml_model`** — `id`, `kind`, `track`, `version`, `trained_at`, `n_samples`, `metrics` json,
`artifact_path`, **`pca_artifact_path`**, **`sklearn_version`**, **`feature_names`** json,
`is_active`, `promoted_at`, `promoted_by` (`auto` | `operator`).

Three details that will otherwise bite:
- **The fitted PCA must be persisted alongside the model.** Without it the 64-dimension features
  are not reproducible and the model silently scores garbage after a restart.
- **Pickles are sklearn-version-locked.** Record the version and refuse to load a model trained
  under a different one, falling back to LLM Tier 1 and raising a notification rather than
  loading a mismatched artifact.
- **Set `n_jobs=1`.** joblib's loky backend re-executes `sys.executable`, which inside a frozen
  PyInstaller application relaunches the whole app rather than spawning a worker.

In the interface the reranker is "your own sorting model": it says that Kestrel tests it on the
operator's ratings first, uses it only if it beats the built-in scoring (criterion 3), and stops
using it if it slips, shows since when it has been in use and how often it is checked, and has an
on and off switch. After a failed promotion Kestrel retries every 50 ratings (D-UI-99).

**Drift detection.** Rolling MAE over the last 50 labeled items. Rising past
`settings.learning.drift_threshold` demotes the reranker back to LLM Tier 1 automatically and
raises a notification. Job markets change; a model trained in July should not be trusted
unexamined in December.

### 5.5 Active learning

Each label the operator gives should be the most informative one available.

Rank unlabeled evaluated opportunities by an uncertainty score combining:

- Proximity to the 4.0 decision threshold (a 3.9 is worth far more than a 1.2)
- `abs(holistic - weighted)` divergence
- Reranker prediction variance, once a reranker exists
- Distance from existing labeled examples in embedding space, to cover unrepresented regions
- Recency weighting, so the labeled set tracks the current market

Surface the top ten as a **"Calibrate" queue**: the description on the left and a one-click
rating ("Good match?", Poor to Great) first. The system's evaluation is revealed on the right
only after the operator has rated, so the rating is not anchored on the model's score (D-UI-99);
an optional "Tell Kestrel why" captures reasons and per-dimension nudges, and Skip is available.
Target under 30 seconds per item. Ten minutes a week meaningfully improves everything downstream.

The gold set used by `bench` is **regenerated from these labels** once there are enough, so
model comparison is measured against the operator's real judgment rather than synthetic
placeholders.

### 5.6 Outcome-driven calibration

The honest feedback nothing else provides. Scores measure predicted fit; outcomes measure
whether that prediction meant anything.

Computed and surfaced:

- Response rate by score band. **If 4.5+ converts at 12% and 4.0-4.5 at 11%, the score is not
  discriminating** and the prompt or the weights need work. This is stated plainly rather than
  buried.
- Response rate by source, organization size, ownership type, remote type, and track.
- Furthest stage reached by score band.
- Response rate by document configuration: which summary variant and which slot choices
  correlate with a reply. Correlational, small-sample, and **labeled as such** so it is not
  over-read.
- Time-to-first-response distribution, which sets the ghosting threshold empirically instead of
  by guess.

When response data contradicts scoring, the system says so on Analytics (its Summary) and on Today. That is the whole
point of collecting it.

### 5.7 Prompt versioning

Every prompt is a versioned file. `evaluation.prompt_version` records what produced each result.
`bench` accepts a prompt version so two versions can be compared on the same gold set with the
same model, **changing exactly one variable**. Results from a superseded version carry a subtle
marker in the UI so stale scores are visible.

---

## 6. Intelligence

### 6.1 Compensation corpus

Every observed compensation figure is retained and aggregated. Over a few hundred opportunities
this becomes a private dataset more relevant than any public salary source, because it is scoped
to exactly the roles the operator is actually looking at.

**Capture starts at ingestion, not here.** Both the comp corpus and the ghost score are pure
arithmetic over data the ingestion pipeline already has, with no model involved. They are written
during normalization (`SPEC.md` §5.4) from the very first scrape. This section describes how they
are *used*; the writing happens far earlier, because a corpus is worthless without history and
waiting until a late phase to start collecting would leave every evaluation before that point
ungrounded.

**`comp_observation`** — `id`, `opportunity_id`, `normalized_title`, `seniority_rank`,
`geo_bucket`, `org_size_band`, `org_ownership`, `sector`, `min`, `max`, `currency`, `interval`,
`source` (`structured` | `parsed` | `offer` | `operator_reported`), `observed_at`.

Queried by (normalized title, geo bucket, size band) returning median, p25, p75, and **sample
size**. Sample size is always shown. Below five observations the UI says "insufficient data"
rather than rendering a chart from three points.

Feeds Block D of the evaluation prompt, so market estimates are grounded in observed local data
rather than model priors, and feeds the negotiation module (§4.2).

### 6.2 Ghost job detection

A posting that has been live for months, reposted repeatedly, or never closes is frequently
pipeline-building rather than a real opening. Applying costs real effort for zero probability.

Signals, accumulated from `seen_count`, `first_seen_at`, `last_seen_at`, `version` history, and
liveness checks:

| Signal | Weight |
|---|---|
| `seen_count >= 5` across distinct scrape runs | strong |
| Live longer than 90 days with no description change | strong |
| Republished with a fresh `date_posted` but an identical `content_hash` | very strong |
| Same requisition id reappearing after being marked closed | very strong |
| No compensation, no location, and no team detail | moderate |
| Organization has more than 20 simultaneous postings with generic descriptions | moderate |

Combined into a `ghost_score` stored on the opportunity, shown as a badge, and passed into Block
G so the model reasons with the history rather than guessing from one snapshot. **This requires
accumulated history, so it is weak in week one and genuinely good by month three.** Capture
starts on day one regardless.

### 6.3 Liveness checking

A `liveness` task re-fetches the URL of every shortlisted and applied opportunity on a cadence
(default every 3 days).

An *expired read* is any of:

- HTTP 404 or 410 (`http_404`, `http_410`)
- Page text matching "no longer accepting", "position filled", "this job is closed" (`page_text`)
- ATS API no longer returning the id (`ats_missing`; reserved, not implemented)

**A posting closes only on the second consecutive expired read** (D-INTEL-1). The first expired
read sets `liveness_strikes` to 1, leaves `liveness` unchanged and stamps `liveness_checked_at`;
the row is read again after `liveness.recheck_hours` (default 24) instead of the cadence. The
second consecutive expired read sets `closed`, stores the `closed_reason`, and raises the
notification, which states the reason in plain words. A live read resets `liveness_strikes` to 0
and clears `closed_reason`. A transient failure (5xx, 429, 403, a network error) changes neither
the state nor the strike count. Otherwise → `live`, timestamp updated.

A shortlisted opportunity going closed raises a notification. Prevents preparing for something
that no longer exists, and the close date is useful data for the ghost model.

---

## 7. Analytics

One page answering: is any of this working, and where is the leak.

**Funnel**, per track, with conversion at each step:

```
ingested -> tier0 pass -> tier1 pass -> tier2 evaluated -> shortlisted
  -> pursued -> first response -> screening -> interview -> final -> offer
```

**Breakdowns:** by source, score band, organization size, ownership, remote type, seniority, and
for consulting by trigger event type. Each shows count, conversion, and sample size.

**Cost and throughput:** tokens consumed per tier, wall-clock per run, items per minute,
projected time for a run of N, and how much the reranker has saved since promotion. These and the
quality figures below are shown on the Models page, not on Analytics (D-UI-101); Analytics keeps
a pointer card.

**Quality:** JSON validity rate, quarantine rate, repair rate, override rate (how often the
operator disagrees), score-vs-outcome correlation, and Block F question-prediction hit rate from
interview debriefs.

**Time:** applications per week, time from shortlist to submission, time to first response by
source, and current pipeline age with stale items flagged.

**Exports (D-UI-101).** An Export menu offers the summary and charts as a PDF, all numbers as a
CSV, and every application with its history as a CSV. Files are written locally through the Save
dialog; nothing is sent. This is new scope recorded in the private decision log.

**The honest summary.** Titled "Summary" on the page, with a Rewrite summary button and an
"Updated" time. A short generated narrative at the top: what is working, what is not,
and the one thing most worth changing. Grounded strictly in the numbers on the page, with
explicit sample sizes, and **it must be willing to say the system is not discriminating well
when that is what the data shows.** An analytics page that always reports good news is worse
than no analytics page.
