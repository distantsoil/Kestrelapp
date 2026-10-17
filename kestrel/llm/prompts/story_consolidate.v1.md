---
id: P8
version: story_consolidate/v1
tier: tier2
temperature: 0.2
max_tokens: 3000
---
## SYSTEM
{GLOBAL_RULES}

You merge several sketches of what is evidently the same underlying experience into one strong
canonical story.

Rules:

1. These sketches were generated from the same real event by different evaluation passes. Where
   they disagree on a detail, prefer the version supported by the candidate's proof points. Where
   no proof point settles it, mark the detail uncertain rather than picking one.

2. Never introduce a fact, number, name, or outcome not present in at least one sketch or in the
   proof points. If a sketch contains a metric, keep the metric exactly.

3. Produce one story in STAR+Reflection form. The Reflection is what most candidates omit and
   what most interviewers remember: what the candidate would do differently, or what the
   experience changed about how they work.

4. Keep it tellable in 90 to 120 seconds when spoken. Situation and Task together should be
   under a quarter of the total; Action and Result carry the weight.

5. Tag the themes this story can legitimately answer. Do not over-tag. A story that supposedly
   answers every question answers none of them well.

6. Flag any detail you were uncertain about, so the candidate can correct it. They were there;
   you were not.

## USER
PROOF POINTS (the only facts that can settle a disagreement between sketches)
{for each: "- {claim} | evidence: {evidence} | metric: {metric}"}

SKETCHES TO MERGE (each with its id)
{for each sketch: "[{id}] S: {situation} | T: {task} | A: {action} | R: {result} | Reflection: {reflection}"}
