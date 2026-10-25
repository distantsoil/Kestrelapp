"""What the operator decides on the Interview prep page, kept apart from the generated pack.

A rebuild of the pack writes a new `prep_pack` row, so anything the operator chose must live
outside it. Each piece is one `setting` row per role (JSON), like the notes in `notes.py`:

- `prep_story.<id>`: the story the operator chose for a question (`{question: story_id}`). A swap
  or a new story writes here; Kestrel's own suggestion is never rewritten.
- `prep_ask.<id>`: the questions to ask them (`added` by the operator, `ticked` per question).
- `prep_people.<id>`: people the operator heard will be in the room (`[{name, title}]`). These
  are the operator's own notes. Nothing here contacts anyone (hard rule 1).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting

STORY_PREFIX = "prep_story."
ASK_PREFIX = "prep_ask."
PEOPLE_PREFIX = "prep_people."
TEXT_MAX = 400
ADDED_MAX = 12
PEOPLE_MAX = 8
DEFAULT_TICKED = 3  # the first three generated questions start ticked, as in the mockup


async def _get(session: AsyncSession, key: str) -> object:
    row = await session.get(Setting, key)
    return row.value if row is not None else None


async def _put(session: AsyncSession, key: str, value: object) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


# ------------------------------------------------------------------ the chosen story per question
async def story_choices(session: AsyncSession, opportunity_id: int) -> dict[str, int]:
    raw = await _get(session, f"{STORY_PREFIX}{opportunity_id}")
    if not isinstance(raw, dict):
        return {}
    return {
        str(q): int(s) for q, s in raw.items() if isinstance(s, int) and not isinstance(s, bool)
    }


async def set_story_choice(
    session: AsyncSession, opportunity_id: int, question: str, story_id: int | None
) -> None:
    """Point `question` at `story_id`; None removes the choice (Kestrel's own pick shows again).
    Does not commit."""
    choices = await story_choices(session, opportunity_id)
    if story_id is None:
        choices.pop(question, None)
    else:
        choices[question] = story_id
    await _put(session, f"{STORY_PREFIX}{opportunity_id}", choices)


# ------------------------------------------------------------------------- questions to ask them
async def ask_state(
    session: AsyncSession, opportunity_id: int
) -> tuple[list[str], dict[str, bool]]:
    raw = await _get(session, f"{ASK_PREFIX}{opportunity_id}")
    if not isinstance(raw, dict):
        return [], {}
    added = [str(a) for a in raw.get("added") or [] if isinstance(a, str) and a.strip()]
    ticked = {str(k): bool(v) for k, v in (raw.get("ticked") or {}).items()}
    return added, ticked


async def _save_ask(
    session: AsyncSession, opportunity_id: int, added: list[str], ticked: dict[str, bool]
) -> None:
    await _put(session, f"{ASK_PREFIX}{opportunity_id}", {"added": added, "ticked": ticked})


async def tick_question(
    session: AsyncSession, opportunity_id: int, text: str, on: bool
) -> None:
    added, ticked = await ask_state(session, opportunity_id)
    ticked[text] = on
    await _save_ask(session, opportunity_id, added, ticked)


async def add_question(session: AsyncSession, opportunity_id: int, text: str) -> str | None:
    """Add the operator's own question (ticked). The cleaned text, or None when blank, already
    there or over the limit. Does not commit."""
    text = " ".join((text or "").split())[:TEXT_MAX]
    added, ticked = await ask_state(session, opportunity_id)
    if not text or text in added or len(added) >= ADDED_MAX:
        return None
    added.append(text)
    ticked[text] = True
    await _save_ask(session, opportunity_id, added, ticked)
    return text


def ask_rows(generated: list[str], added: list[str], ticked: dict[str, bool]) -> list[dict]:
    """Every question to ask, in order, with its source and whether it goes in the PDF."""
    rows: list[dict] = []
    for i, q in enumerate(generated):
        rows.append({
            "text": q, "mine": False, "on": ticked.get(q, i < DEFAULT_TICKED),
            "src": "Research could not find this",
        })
    for q in added:
        rows.append({"text": q, "mine": True, "on": ticked.get(q, True), "src": "Your question"})
    return rows


# ------------------------------------------------------------------------------ who they may meet
async def people(session: AsyncSession, opportunity_id: int) -> list[dict]:
    raw = await _get(session, f"{PEOPLE_PREFIX}{opportunity_id}")
    if not isinstance(raw, list):
        return []
    return [
        {"name": str(p.get("name") or ""), "title": str(p.get("title") or "")}
        for p in raw if isinstance(p, dict) and str(p.get("name") or "").strip()
    ]


async def add_person(
    session: AsyncSession, opportunity_id: int, name: str, title: str
) -> bool:
    name = " ".join((name or "").split())[:120]
    title = " ".join((title or "").split())[:160]
    current = await people(session, opportunity_id)
    if not name or len(current) >= PEOPLE_MAX:
        return False
    current.append({"name": name, "title": title})
    await _put(session, f"{PEOPLE_PREFIX}{opportunity_id}", current)
    return True
