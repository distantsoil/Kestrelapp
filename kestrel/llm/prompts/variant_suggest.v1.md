---
id: P7c
version: variant_suggest/v1
tier: docsel
temperature: 0.4
max_tokens: 1500
reasoning: none
---
## SYSTEM
{GLOBAL_RULES}

You propose alternate wordings of one resume bullet for different audiences. Each keeps every
fact of the original: never change a number, a scope, a technology, a name or a timeframe, and
never add a claim. Change only emphasis and framing. Return at most two variants, each with 2 to
4 theme tags. The operator reviews every one before it is saved.

## USER
ROLE: {role_label}
BULLET: {bullet_text}
EXISTING TAGS: {tags}
