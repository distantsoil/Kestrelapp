# Prompt token budgets

Measured token usage of every pipeline prompt against its tier's `num_ctx`, so the operator can
run tiers on hosted metered models (OpenRouter, including free models with small windows) and on
weaker local machines without silent truncation (CLAUDE.md platform trap: the context window
truncates with no error, and "runs fine, results are bad" follows).

Counts are `cl100k_base` via `kestrel.ingest.tokens.count_tokens`, which is also what the
per-tier `max_jd_tokens` and `TIER1_HEAD_TOKENS` slices are cut with. A Qwen or Llama tokenizer
lands within roughly 10 percent of these numbers.

Regenerate the tables with:

```
.venv/bin/python -m pytest -q -s -p no:cacheprovider tests/test_prompt_budgets.py -k table
```

The same file asserts that every rendered prompt fits its `num_ctx` with at least 25 percent of
the window left free for output, on both fixtures.

## Fixture

- Profile: the shipped fictional `kestrel/resources/config/profile.example.yml`, both tracks.
- Job description: a 900-word VP of IT posting in the shape job boards serve (duties,
  requirements, nice-to-haves, location, comp and benefits, EEO boilerplate), about 1,030 tokens.
  The worst case repeats it eight times (about 8,300 tokens) to force every truncation path.
- Org: three accumulated signals (acquisition, CIO departure, situation).
- Comp intel: a corpus line with n=7 (hard rule 6: n is always stated).
- Few-shot: k=3 labels, diversified across apply, consider and pass, built the way
  `tier2._gather_few_shot` builds them (summary and rationale both from `label.rationale`).
- Doc selection: a resume master of 4 roles x 4 slots x 3 variants, 3 summary variants, 3 skill
  groups, 2 layouts.

Column meanings: `system` is the system message including GLOBAL_RULES; `template` is the user
message minus the JD slice and the few-shot block (profile, headers, metadata, catalog);
`headroom` is `num_ctx - total`, which must cover the tier's `max_tokens` (tier1 300/350,
tier2 4000, docsel 1500, tier3 6000) and then some.

## Per-tier windows

| tier | num_ctx | max_tokens | fixed input (system + template + few-shot) | JD slice |
|---|---|---|---|---|
| tier1 | 4096 | 300 / 350 | 483 (P1) / 720 (P1C) | head, at most 1200 incl. marker |
| tier2 | 16384 | 4000 | 1374 (P2) / 1395 (P2C) | middle-elided, at most `max_jd_tokens` (6000) |
| docsel | 8192 | 1500 | 2890 for a 48-variant master | none |
| tier3 | 20480 | 6000 | same prompt as tier2 (P2/P2C) | same as tier2 |

## Before (as found)

Realistic 900-word JD:

| prompt | tier | system | template | JD | few-shot | total | num_ctx | headroom |
|---|---|---|---|---|---|---|---|---|
| P1 | tier1 | 244 | 236 | 1040 | 0 | 1520 | 4096 | 2576 (63%) |
| P1C | tier1 | 360 | 327 | 1040 | 0 | 1727 | 4096 | 2369 (58%) |
| P2 | tier2 | 546 | 686 | 1040 | 231 | 2503 | 16384 | 13881 (85%) |
| P2C | tier2 | 584 | 625 | 1040 | 231 | 2480 | 16384 | 13904 (85%) |
| P3 | docsel | 274 | 2616 | 0 | 0 | 2890 | 8192 | 5302 (65%) |
| P2 | tier3 | 546 | 686 | 1040 | 231 | 2503 | 20480 | 17977 (88%) |
| P2C | tier3 | 584 | 625 | 1040 | 231 | 2480 | 20480 | 18000 (88%) |

Worst case (JD over `max_jd_tokens`):

| prompt | tier | system | template | JD | few-shot | total | num_ctx | headroom |
|---|---|---|---|---|---|---|---|---|
| P1 | tier1 | 244 | 236 | 1200 | 0 | 1680 | 4096 | 2416 (59%) |
| P1C | tier1 | 360 | 327 | 1200 | 0 | 1887 | 4096 | 2209 (54%) |
| P2 | tier2 | 546 | 686 | 6000 | 231 | 7463 | 16384 | 8921 (54%) |
| P2C | tier2 | 584 | 625 | 6000 | 231 | 7440 | 16384 | 8944 (55%) |
| P3 | docsel | 274 | 2616 | 0 | 0 | 2890 | 8192 | 5302 (65%) |
| P2 | tier3 | 546 | 686 | 6000 | 231 | 7463 | 20480 | 13017 (64%) |
| P2C | tier3 | 584 | 625 | 6000 | 231 | 7440 | 20480 | 13040 (64%) |

Every tier already fit with more than half the window free. The system prompts are 244 to 584
tokens and contain no restated schema, no examples and no markdown decoration; the cost lives in
the context blocks assembled by `kestrel/pipeline/context.py`, which is where the trims were made.

## After

Realistic 900-word JD:

| prompt | tier | system | template | JD | few-shot | total | num_ctx | headroom |
|---|---|---|---|---|---|---|---|---|
| P1 | tier1 | 244 | 239 | 1027 | 0 | 1510 | 4096 | 2586 (63%) |
| P1C | tier1 | 360 | 360 | 1027 | 0 | 1747 | 4096 | 2349 (57%) |
| P2 | tier2 | 546 | 675 | 1027 | 153 | 2401 | 16384 | 13983 (85%) |
| P2C | tier2 | 584 | 658 | 1027 | 153 | 2422 | 16384 | 13962 (85%) |
| P3 | docsel | 274 | 2616 | 0 | 0 | 2890 | 8192 | 5302 (65%) |
| P2 | tier3 | 546 | 675 | 1027 | 153 | 2401 | 20480 | 18079 (88%) |
| P2C | tier3 | 584 | 658 | 1027 | 153 | 2422 | 20480 | 18058 (88%) |

Worst case (JD over `max_jd_tokens`):

| prompt | tier | system | template | JD | few-shot | total | num_ctx | headroom |
|---|---|---|---|---|---|---|---|---|
| P1 | tier1 | 244 | 239 | 1199 | 0 | 1682 | 4096 | 2414 (59%) |
| P1C | tier1 | 360 | 360 | 1199 | 0 | 1919 | 4096 | 2177 (53%) |
| P2 | tier2 | 546 | 675 | 5998 | 153 | 7372 | 16384 | 9012 (55%) |
| P2C | tier2 | 584 | 658 | 5998 | 153 | 7393 | 16384 | 8991 (55%) |
| P3 | docsel | 274 | 2616 | 0 | 0 | 2890 | 8192 | 5302 (65%) |
| P2 | tier3 | 546 | 675 | 5998 | 153 | 7372 | 20480 | 13108 (64%) |
| P2C | tier3 | 584 | 658 | 5998 | 153 | 7393 | 20480 | 13087 (64%) |

## Trims made (all in `kestrel/pipeline/context.py`; PROMPTS.md shared blocks updated to match)

| change | before | after | note |
|---|---|---|---|
| Few-shot block: compact form, `Why:` dropped when it repeats the summary, `---` separators dropped | 231 (k=3) | 153 (k=3) | `tier2._gather_few_shot` fills both `summary` and `rationale` from `label.rationale`, so every example was sent twice. The PROMPTS.md block also listed a `Dimensions:` line the code never rendered; removed from the doc rather than added to the prompt. |
| JD whitespace normalization before slicing (nbsp and space runs to one space, trailing whitespace dropped, blank-line runs to one) | 1040 | 1027 | Words are never removed or reordered. Larger savings on real board HTML-to-markdown output, which is heavy on blank lines. |
| `First seen` / `Last seen` rendered as dates, not full timestamps | 686 (P2 template) | 675 | The ghost and repost read needs days, not seconds. |
| Tier 1 slice: explicit truncation marker, reserved inside the 1200 budget | 1200 | 1199 | Marker appears only when the description was actually cut. The slice never exceeds `TIER1_HEAD_TOKENS`. |

Net per Tier 2 call on the fixture: 2503 to 2401 tokens (about 4 percent). The absolute numbers
are small because the prompts were already lean; the few-shot fix is the only one that scales
(it grows with k and with rationale length, up to 400 characters each).

### One addition (not a trim)

| change | before | after | note |
|---|---|---|---|
| Consulting PROFILE_COMPACT carries `Target orgs: size ...; sectors ...`, `Excluded sectors`, and labeled day rates | 327 (P1C template) | 360 | The P1C and P2C system prompts tell the model to kill when "the organization is far outside the target size range", but the block never sent the target size or sectors, and it rendered `Level: 20 and above` (the minimum engagement days in the seniority slot) and unit-less comp. PROMPTS.md already said the consulting build uses these fields. |

### PROMPTS.md reconciliations

- PROFILE_COMPACT now shows both track builds as the code renders them.
- The `Core domains: {top 8 skills}` line and the PROFILE_FULL `EXPERIENCE SHAPE` block were
  removed from the doc: the profile schema (SPEC §4.1) has no skills list and no resume-role
  list, so they were never buildable and the code never sent them.
- A "Description slices" subsection documents normalization, the Tier 1 head slice and marker,
  and the Tier 2 middle elision.

## Decided not to trim, and why

- **Prompt file text (`kestrel/llm/prompts/*.v1.md`).** Nothing in them is duplicated between
  system and user, none restate the JSON schema's types or required lists, and none carry
  examples the few-shot mechanism supplies. The dimension lists in P2/P2C are rubric meaning, not
  schema restatement: constrained decoding enforces the shape but never shows the model the
  field descriptions. PROMPTS.md also requires a version bump for any prompt text change, and the
  version map lives in `kestrel/llm/schemas.py`, outside this change's ownership.
- **GLOBAL_RULES.** Partly redundant under `json_schema` constrained decoding, but it is the only
  instruction the model gets on backends that fall back to `json_object`, and it carries the
  "never invent a field" hard-rule language.
- **EEO and benefits paragraphs.** A conservative regex detects them, but on the fixture the EEO
  paragraph carries the work-authorization statement ("we do not sponsor employment visas") and
  the benefits paragraph carries the only comp band. Both feed `comp_logistics`. A wrongly
  removed sentence is invisible, so these stay (hard rule 4 in spirit).
- **`DOC_SLOT_CATALOG` line format.** 90 percent of the P3 prompt is the catalog itself and the
  rendering lives in `kestrel/docs/select.py`, outside ownership. It is already dense.
- **Tier 1's 1200-token head.** SPEC §6.2 fixes it; P1 worst case is 1682 input + 300 output in
  a 4096 window. `TIER1_HEAD_TOKENS` is a constant in `context.py` (and the default for
  `build_tier1_values`), not a setting; `tier1.py` passes it through `_shared.jd_budget`, which
  may hand a smaller slice to a hosted metered tier. The marker logic applies to any `n_tokens`.

## Running on small windows

Required window for a tier is roughly `system + template + few-shot + JD + max_tokens`. With the
default Tier 2 `max_tokens` of 4000 (PROMPTS.md: the floor, not a ceiling), choose
`settings.pipeline.max_jd_tokens` so the JD slice fits:

| model window | max_jd_tokens to set | Tier 2 worst-case input + output |
|---|---|---|
| 8192 | 2500 | 1395 + 2500 + 4000 = 7895 |
| 12288 | 4500 | 1395 + 4500 + 4000 = 9895 |
| 16384 (shipped) | 6000 | 1395 + 6000 + 4000 = 11395 |
| 32768 and up | 6000 | no benefit beyond the default; middle elision is rarely triggered |

Tier 1 fits any window of 4096 or more as shipped. Doc selection needs about `2900 + 1500` for a
48-variant master and grows roughly 35 tokens per variant; a 4096-window model is not enough for
P3. `kestrel doctor` checks the effective `num_ctx` of local derived models against these budgets
(`kestrel/doctor.py: input_budget`); hosted backends report no `num_ctx`, so the table above is
the operator's check.
