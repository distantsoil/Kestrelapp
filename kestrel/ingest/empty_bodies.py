"""Roles that arrive without a description, counted per source (issue #182, D-SRC-640 to 649).

A search board sometimes answers with a challenge page, and the library hands back an empty
description. Kestrel cannot tell that from a posting that truly has no text, and it never tries
to get past a bot check (hard rule 7). What it can do is measure, and say so plainly:

  * Typed evidence only: whether a body is empty is measured structurally (no visible text after
    markup is removed). No page text is ever matched for words like "captcha" (AGENT-RULES 8a.2).
  * "Likely" is general wording plus a "worth checking" note, never a claim (8a.5). The role is
    kept and shown either way (hard rule 4).
  * A share is shown with its sample size, and below `MIN_SAMPLE` roles it is "too few to tell"
    (hard rule 6).
  * The state and the sentence that explains it come from one decision (`Reading`, 8a.3). The
    run-level sentence is `base.body_empty`, a sibling of E's one-sentence-per-kind family.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from kestrel.ingest.base import Notice, body_empty

MIN_SAMPLE = 5  # hard rule 6: fewer than this many roles is too few to tell
JUMP_POINTS = 0.25  # a share this far above the usual share is a jump
UNKNOWN_USUAL_FLOOR = 0.5  # with no usual share to compare, half or more empty is a jump
HISTORY_RUNS = 10  # earlier runs that make up a source's "usual" share
COUNTS_KEY = "bodies"  # run.counts[COUNTS_KEY] = {source key: {"new": n, "empty": m, ...}}
WHOLE_SOURCE = "_all"  # the key for a source that is not a multi-board search

# The role-level note (issue 182 scope A): general wording, chosen by typed evidence (the role
# came from a search board and its body is empty), never a claim that a block happened.
ROLE_NOTE = ("The site may have blocked Kestrel's reader, for example with a bot check. Worth "
             "checking: open the posting and paste the description.")

TOO_FEW = "too few to tell"


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


_MARKDOWN_ONLY = re.compile(r"[\s#*_>`~\-=|\[\]()!.]+")


def visible_text(html_or_md: str | None) -> str:
    """The text a reader would see: markup removed, markdown punctuation and space dropped."""
    if not html_or_md:
        return ""
    parser = _Text()
    parser.feed(html_or_md)
    parser.close()
    return _MARKDOWN_ONLY.sub(" ", "".join(parser.parts)).strip()


def is_empty_body(raw: Any) -> bool:
    """True when a posting carries no visible description text. Structural: length of the
    visible text, not its words."""
    return not (visible_text(getattr(raw, "description_md", None))
                or visible_text(getattr(raw, "description_html", None)))


def role_note(source_kind: str | None) -> str | None:
    """The scope A sentence for a role with an empty body: only a search board (JobSpy) gets it.
    Company boards, feeds and pasted roles keep their current wording."""
    return ROLE_NOTE if source_kind == "jobspy" else None


@dataclass(frozen=True)
class Reading:
    """One source's share of new roles without a description in one run, and what it means."""

    new: int
    empty: int
    state: str  # "too_few" | "ok" | "jump"
    usual: float | None  # the usual share over earlier runs, None when they are too few

    @property
    def share_text(self) -> str:
        """"3 of 20 new roles had no description" or "too few to tell (2 new roles)"."""
        if self.state == "too_few":
            return f"{TOO_FEW} ({self.new} new {'role' if self.new == 1 else 'roles'})"
        return f"{self.empty} of {self.new} new roles had no description"

    def warning(self, board: str) -> Notice | None:
        """The plain warning for a jump, from the same decision as `state`."""
        return body_empty(board, self.empty, self.new) if self.state == "jump" else None

    def as_counts(self) -> dict[str, Any]:
        return {"new": self.new, "empty": self.empty, "state": self.state}


STATES = ("too_few", "ok", "jump")  # the only states an entry can carry (D-SEC-20)
_MAX_ROLES = 1_000_000  # a sanity bound on a stored count; no real run is near it


def _count(value: Any) -> int | None:
    """A stored count as a plain non-negative int, or None for anything else (a string, a list,
    a bool, a float, a negative or absurd number). Never raises."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _MAX_ROLES else None


def clean_entry(raw: Any) -> dict[str, Any] | None:
    """Rebuild one stored entry from our own schema (D-SEC-20): `new` and `empty` are
    non-negative ints with `empty <= new`, and the state is re-derived where it can be: under
    `MIN_SAMPLE` roles is always `too_few` (hard rule 6), a clean run with no empty role is
    never a `jump`. An entry that cannot be rebuilt is None and is ignored."""
    if not isinstance(raw, dict):
        return None
    new, empty = _count(raw.get("new")), _count(raw.get("empty"))
    if new is None or empty is None or empty > new:
        return None
    if new < MIN_SAMPLE:
        state = "too_few"
    else:
        stored = raw.get("state")
        state = "jump" if stored == "jump" and empty > 0 else "ok"
    return {"new": new, "empty": empty, "state": state}


def usual_share(history: list[Any]) -> float | None:
    """The pooled share over earlier runs, or None when they hold under MIN_SAMPLE roles
    together (not enough to call anything usual). Left out: runs that already jumped, runs under
    MIN_SAMPLE roles (too few to tell), and runs at or above `UNKNOWN_USUAL_FLOOR`. A board that
    stays blocked must not become its own usual and go quiet. Because no run at or above half is
    ever in the pool, the usual share is under 0.5 and the bar under 0.75, so a run of 100
    percent empty roles always warns (D-SRC-646, D-SRC-649). Entries
    that cannot be rebuilt are ignored."""
    clean = [
        c for c in map(clean_entry, history)
        if c is not None and c["state"] == "ok" and c["empty"] < UNKNOWN_USUAL_FLOOR * c["new"]
    ]
    new = sum(c["new"] for c in clean)
    if new < MIN_SAMPLE:
        return None
    return sum(c["empty"] for c in clean) / new


def read_share(new: int, empty: int, history: list[Any]) -> Reading:
    """Decide the state of this run's share against the source's own history."""
    usual = usual_share(history)
    if new < MIN_SAMPLE:
        return Reading(new, empty, "too_few", usual)
    share = empty / new
    bar = UNKNOWN_USUAL_FLOOR if usual is None else usual + JUMP_POINTS
    jumped = share >= bar and empty > 0
    return Reading(new, empty, "jump" if jumped else "ok", usual)


def stored_entry(counts: Any, key: str) -> dict[str, Any] | None:
    """One source's entry from a stored run's counts, rebuilt through `clean_entry`, or None if
    the run recorded none or it cannot be trusted."""
    bodies = counts.get(COUNTS_KEY) if isinstance(counts, dict) else None
    return clean_entry(bodies.get(key)) if isinstance(bodies, dict) else None
