"""Interview preparation and the story bank (SPEC-MODULES.md 3).

Block F STAR+Reflection sketches accumulate across every Tier 2 employment evaluation; individually
disposable, consolidated they are the operator's actual interview preparation. This package holds:

  - stories.py  — P8 consolidation: cluster new sketches, merge each cluster into one canonical
    story, and (absolutely) NEVER overwrite an operator-edited story; new sketches attach as usage.
  - coverage.py — theme coverage and, more importantly, GAPS: knowing you have no failure story is
    more useful than forty mediocre ones.
  - prep.py     — prep packs: assemble the role summary, research signals, Block B gaps, mapped
    canonical stories, and questions-to-ask drawn from research UNKNOWNS; render to PDF.
  - mock.py     — P9 mock interview: one question at a time, critique against the rubric, and flag
    any claim not present in the operator's own proof points or canonical stories.
  - debrief.py  — capture the debrief into interactions/pursuit/contacts and report the
    predicted-vs-asked question hit rate, a direct measure of whether Block F is any good.

The model SELECTS and synthesizes the operator's own material; it never invents facts, and a merged
story introduces no number or outcome absent from its source sketches or the proof points."""

from __future__ import annotations
