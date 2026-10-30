"""Pure view helpers for the Tailor page (D-UI-120): which printed line answers which thing the
posting asks for, which words of the posting the operator's own text already holds, and the
posting itself with its requirement phrases marked.

Nothing here writes text for the operator. Every function reads the lines the render prints (the
operator's own wordings, byte for byte) and the posting, and reports. The matching is plain word
overlap, so it is deterministic and testable; it never calls a model (hard rules 2 and 3)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

from kestrel.web.pipeline_view import UNVERIFIED_HOW

_WORD = re.compile(r"[a-z0-9][a-z0-9+#&.\-]*[a-z0-9+#]|[a-z0-9]", re.IGNORECASE)
_STOP = frozenset(
    """a about across all also an and any are as at be been being but by can for from has have in
    into is it its more most of on or our over per than that the their them then there these they
    this to up us was we were what when where which while who will with within without you your
    experience ability strong knowledge skills skill work working years year plus using use used
    including etc preferred required requirements role team teams""".split()
)


def tokens(text: str | None) -> set[str]:
    """The meaningful lowercase words of a phrase: no stop words, no one-letter words."""
    out: set[str] = set()
    for raw in _WORD.findall(text or ""):
        w = raw.lower().strip(".-")
        if len(w) < 2 or w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return out


@dataclass
class Line:
    """One printed line the page can point at: its slot, what it prints, the operator's tags."""

    slot: str
    text: str
    tags: tuple[str, ...] = ()


def link(requirement: str, lines: list[Line]) -> str | None:
    """The slot whose printed line best answers a requirement, or None. Two shared meaningful
    words are needed (one when the requirement itself has only one or two), so a stray common
    word never links a line to a requirement. Ties go to the first line in resume order."""
    want = tokens(requirement)
    if not want:
        return None
    need = 2 if len(want) > 2 else 1
    best: tuple[int, str] | None = None
    for ln in lines:
        have = tokens(ln.text) | tokens(" ".join(ln.tags))
        score = len(want & have)
        if score >= need and (best is None or score > best[0]):
            best = (score, ln.slot)
    return best[1] if best else None


def requirements(
    blocks: dict | None, lines: list[Line], *, limit: int = 12
) -> list[dict[str, Any]]:
    """"What they ask for": each requirement the role's evaluation named, as covered ("m"),
    partly covered ("p") or not in the documents ("n"), with the slot of the line that answers it.
    A match whose line is left out of this version reads "n" with its own note: Kestrel never
    claims coverage the printed page does not show."""
    fit = (blocks or {}).get("b_fit") or {}
    out: list[dict[str, Any]] = []
    for m in fit.get("matches") or []:
        req = str(m.get("requirement") or "").strip()
        if not req:
            continue
        strength = str(m.get("strength") or "")
        if strength == "unverified":
            # Never linked or read as covered: the profile does not show it (PROMPTS.md P2
            # post-processing rule 8, O20).
            out.append({"text": req, "status": "n", "slot": "", "note": UNVERIFIED_HOW})
            continue
        slot = link(req + " " + str(m.get("evidence") or ""), lines) or link(req, lines)
        if slot is None:
            out.append({"text": req, "status": "n", "slot": "", "note": "Left out of this version"})
        else:
            partly = strength in ("partial", "adjacent")
            out.append({"text": req, "status": "p" if partly else "m", "slot": slot, "note": ""})
    for g in fit.get("gaps") or []:
        req = str(g.get("requirement") or "").strip()
        if req:
            out.append({"text": req, "status": "n", "slot": "", "note": ""})
    return out[:limit]


_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9&+]{1,5}\b")
_NOT_TERMS = frozenset(
    """US USA UK EU AND THE FOR OR ID AM PM PTO EEO EOE FAQ CEO CFO COO CTO VP HR OK NO YES
    LLC INC LTD CO ON OF TO IN AT AS IT BY""".split()
)


def _find(haystack: str, term: str) -> str | None:
    """The term as the text spells it, or None when it is not there as a whole word."""
    m = re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", haystack,
                  re.IGNORECASE)
    return m.group(0) if m else None


def _contains(haystack: str, term: str) -> bool:
    return _find(haystack, term) is not None


def posting_words(
    description: str | None,
    *,
    vocabulary: list[str],
    shown_text: str,
    gap_phrases: list[str] | None = None,
    limit: int = 14,
) -> list[dict[str, Any]]:
    """"Words from the posting": the terms that appear in the posting, each marked by whether
    the operator's printed text already holds it. The terms come from three places only: the
    operator's own vocabulary (skills and variant tags), short phrases from the requirements the
    evaluation found missing, and all-capital acronyms the posting uses. Kestrel never adds a
    word to a document; a grey term is a prompt to write it in Profile, Documents."""
    post = description or ""
    if not post.strip():
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def add(term: str) -> None:
        term = term.strip()
        key = term.lower()
        found = _find(post, term) if term and len(term) <= 28 else None
        if not found or key in seen:
            return
        seen.add(key)
        # Shown as the posting spells it ("SQL", not the operator's tag "sql").
        out.append({"term": found, "held": _contains(shown_text, term)})

    for v in vocabulary:
        add(v)
    for phrase in gap_phrases or []:
        words = re.sub(r"\([^)]*\)", " ", phrase).split()
        # The whole short phrase if the posting uses it, else its first three or two words.
        for n in (len(words), 3, 2):
            if 1 <= n <= len(words):
                before = len(out)
                add(" ".join(words[:n]).strip(" .,;:"))
                if len(out) > before:
                    break
    for a in _ACRONYM.findall(post):
        if a not in _NOT_TERMS:
            add(a)
    out.sort(key=lambda w: (not w["held"],))  # held words first, each group in found order
    return out[:limit]


_STATUS_NAME = {"m": "Covered", "p": "Partly covered", "n": "Not in your documents"}


def posting_blocks(description: str | None, reqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The posting as paragraphs and list items, with the requirement it answers marked.
    Each requirement marks the one line of the posting that shares the most words with it (at
    least two, or one when it is short), so a hover can link it to the resume line. The text is
    the employer's, shown as posted and escaped by the template."""
    rows: list[dict[str, Any]] = []
    for raw in (description or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        bullet = bool(re.match(r"^([-*•]|\d+[.)])\s+", line))
        head = bool(re.match(r"^#{1,6}\s", line))
        text = re.sub(r"^([-*•]|\d+[.)]|#{1,6})\s+", "", line)
        text = re.sub(r"[*_`]{1,3}", "", text)
        rows.append({"text": text, "kind": "h" if head else "li" if bullet else "p",
                     "req": None, "status": "", "name": ""})
    taken: set[int] = set()
    for r in reqs:
        want = tokens(r["text"])
        if not want:
            continue
        need = 2 if len(want) > 2 else 1
        best: tuple[int, int] | None = None
        for i, row in enumerate(rows):
            if i in taken or row["kind"] == "h":
                continue
            score = len(want & tokens(row["text"]))
            if score >= need and (best is None or score > best[0]):
                best = (score, i)
        if best is not None:
            taken.add(best[1])
            rows[best[1]].update(
                req=r["slot"] or "none", status=r["status"], name=_STATUS_NAME[r["status"]]
            )
    return rows


def esc(text: str) -> str:
    return html.escape(text, quote=True)
