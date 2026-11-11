"""What the role page's match rows say about a requirement and how it can be corrected (issue 170,
D-UI-290). Pure functions over the rows `kestrel.learning.corrections.corrected_requirements`
builds and the sources `kestrel.web.pages.role` matches; nothing here reads the database or the
clock. The wording is the exact copy of the private overhaul notes 3.7.

A row shows a source only when a stored line matched (invariant 4); otherwise it is Inferred, and
that is the only thing `inferred` ever means. A correction changes how the row is read, never the
stored Evaluation and never a resume bullet (invariants 2 and 6)."""

from __future__ import annotations

from typing import Any

from kestrel.web import role_view as rv

MET, PART, GAP = rv.MET, rv.PART, rv.GAP
WORDS = rv.STATE_WORDS

# Chip labels (handoff 3.7). `rl-mchip` text and the reason each one saves.
LABELS = {
    "overstated": "It's overstated",
    "not_me": "Not true of me",
    "misread": "They mean something else",
    "all": "I have all of it",
    "some": "Some of it",
    "none": "None of it",
    "agree": "It's a fair gap",
}
STORY_REASONS = ("all", "some")
NOTE_NEEDED = "What do they mean? (needed)"
NOTE_OPTIONAL = "Anything to add? (optional)"
STORY_PROMPT = (
    "Kestrel can only credit what you've written down. Pick the story that shows it, or write a "
    "new one."
)
NEW_STORY = "+ Write a new story"
NOTE_MAX = 500


def states_to(reason: str, kestrel_state: str) -> str:
    """Where an answer puts the row: the rule `corrections.operator_state_for` applies."""
    from kestrel.learning.corrections import operator_state_for

    return operator_state_for(reason, kestrel_state)


def route_kind(row: dict[str, Any]) -> str:
    """Where a "Not true of me" answer should send the person (D-LEARN-3): `story_ai` for a story
    Kestrel drafted that they never edited, `story_own` for one they wrote or edited, `resume` for
    a resume line, `inferred` when the model's evidence matched nothing, `none` when it gave
    none."""
    src = row.get("src")
    if src:
        if src["kind"] == "resume":
            return "resume"
        return "story_own" if src.get("edited") else "story_ai"
    return "inferred" if row.get("evidence") else "none"


def effect(reason: str, shown: str, kestrel_state: str, kind: str) -> tuple[str, str]:
    """The one effect line before Save, as (bold head, tail). `shown` is the state the row has
    now; the target comes from the answer."""
    if reason == "misread":
        return "Stays where it is, with your note.", "Nothing else changes."
    to = states_to(reason, kestrel_state)
    if reason == "agree":
        return f"Stays {WORDS[GAP]}.", "Kestrel learns its read was right."
    tail: list[str] = []
    if to == shown:
        head = f"Stays {WORDS[to]}."
    else:
        head = f"Moves to {WORDS[to]} on this role."
        tail.append("The score changes only when you score again.")
    if reason in ("overstated", "not_me"):
        if kind in ("inferred", "none"):
            tail.append("Kestrel learns not to read your experience this way.")
        elif kind == "resume":
            tail.append("Your resume line stays as it is.")
        else:
            tail.append("Your story stays as it is.")
    elif reason in STORY_REASONS:
        tail.append("The story you pick counts for every role from now on.")
    elif reason == "none":
        tail.append("Kestrel learns this about you.")
    return head, " ".join(tail)


def chips_for(shown: str, kestrel_state: str) -> list[str]:
    """The answers a row offers, by the state it shows now and Kestrel's own read."""
    if shown == MET:
        return ["overstated", "not_me", "misread"]
    out = ["all", "some"]
    if shown == PART:
        out.append("none")
    elif kestrel_state == GAP:
        out.append("agree")
    out.append("misread")
    return out


def decorate(
    rows: list[dict[str, Any]], sources: dict[int, dict[str, Any]], stories: dict[int, Any]
) -> list[dict[str, Any]]:
    """Add what the template prints to each row, in place: `src` (the matched stored line or
    None), `inferred` (the tag), `kind` (the Not true of me route), `pill` (the corrected-row
    words), `chips` (each with its effect line) and `quick` (an unverified row's one-click
    answers). A row an applying correction credits with a story is sourced to that story."""
    for r in rows:
        c = r.get("correction")
        backing = stories.get(c["story_id"]) if c and c["applies"] and c.get("story_id") else None
        src = sources.get(r["k"])
        if backing is not None:
            st = backing
            src = {
                "kind": "story",
                "label": f"Story · {st['title']}",
                "href": f"/stories#story-{st['id']}",
                "story_id": st["id"],
                "edited": True,
                "line": st["title"],
                "mine": True,
            }
        r["src"] = src
        r["inferred"] = bool(r.get("evidence")) and src is None and not r.get("unverified")
        r["kind"] = route_kind(r)
        shown = r["state"]
        raw = c["kestrel_state"] if c else shown
        r["raw_state"] = raw
        chips = []
        for reason in chips_for(shown, raw):
            head, tail = effect(reason, shown, raw, r["kind"])
            chips.append(
                {
                    "reason": reason,
                    "label": LABELS[reason],
                    "head": head,
                    "tail": tail,
                    "story": reason in STORY_REASONS,
                    "note": "needed" if reason == "misread" else "optional",
                }
            )
        r["chips"] = chips
        r["quick"] = bool(r.get("unverified")) and not c
        r["pill"] = pill(r)
    return rows


def pill(row: dict[str, Any]) -> dict[str, Any] | None:
    """The words on a corrected row: "You: <state>" with Kestrel's state struck through, "You
    agreed" when the answer kept Kestrel's state, "Your note" for a They mean something else."""
    c = row.get("correction")
    if not c or not c["applies"] and c["reason"] != "misread":
        return None
    if c["reason"] == "misread":
        return {"text": "Your note", "was": None}
    if c["operator_state"] == c["kestrel_state"]:
        if c["reason"] == "agree":
            return {"text": "You agreed", "was": None}
        return {"text": f"You: {WORDS[c['operator_state']]}", "was": None}
    return {
        "text": f"You: {WORDS[c['operator_state']]}",
        "was": f"Kestrel: {WORDS[c['kestrel_state']]}",
    }


def strip_text(n: int, score: float | None) -> str:
    """Your view's strip: `n` is `score_age.changed_things`, how many things in the role's
    corrections block changed since its score (D-SCORE-4)."""
    score_txt = f"{score:.1f}" if score is not None else "score"
    return (
        f"You changed {n} thing{'' if n == 1 else 's'} Kestrel concluded. Its {score_txt} is "
        f"from before {'that' if n == 1 else 'those changes'}."
    )


def toast_text(row_state_changed: bool, new_state: str) -> str:
    if row_state_changed:
        return f"Saved. Now {WORDS[new_state].lower()} on this role."
    return "Saved. Kestrel will learn from it."


REMOVED = "Correction removed. Back to Kestrel's read."
