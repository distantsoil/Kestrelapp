"""The posting text a quick round card shows (D-UI-331): an excerpt and the full plain text, both
from the employer's own description and nothing else. Deterministic string rules only: no model
call, no network, and Kestrel's score is never read. The excerpt may skip a leading company
paragraph or an equal opportunity line; the full text keeps every word."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from kestrel.web.posting_format import clean_markup

EXCERPT_CHARS = 280
EXCERPT_SENTENCES = 3
FULL_CHARS = 40000
_CLAMPED_CHARS = 120  # a longer excerpt can be cut by the card, so More stays available
_MIN_KEPT = 120  # when skipping a leading block would leave less than this, keep everything

# Bounded spans that stop at the next bracket: a run of "[" costs linear time (D-UI-721).
_IMAGE = re.compile(r"!\[[^\[\]\n]{0,1000}\]\([^()\n]{0,2048}\)")
_LINK = re.compile(r"\[([^\[\]\n]{1,1000})\]\([^()\n]{0,2048}\)")
_HEAD = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET = re.compile(r"^\s*[-*+•]\s+")
_BOLD_LINE = re.compile(r"^\s*(\*\*|__)(.+?)\1\s*:?\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(“]?[A-Z0-9])")
_EEO = re.compile(r"equal (opportunity|employment)|\beeo\b|do(es)? not discriminate", re.I)
_ABOUT_US = re.compile(
    r"^(about (us|the company|our company|the organization|our organization)"
    r"|who we are|company overview|our (story|mission|company)|overview of the company)\W*$",
    re.I,
)


@dataclass
class Block:
    text: str
    heading: bool


@dataclass
class PostingText:
    excerpt: str = ""
    paragraphs: list[str] = field(default_factory=list)
    has_more: bool = False


def _plain_line(line: str) -> str:
    line = _IMAGE.sub("", line)
    line = _LINK.sub(r"\1", line)
    line = line.replace("**", "").replace("`", "")
    line = re.sub(r"\\([\\`*_{}\[\]()#+\-.!>|~])", r"\1", line)
    return " ".join(line.split())


def blocks_of(raw: str | None) -> list[Block]:
    """Paragraphs of plain text. Markup is removed; no word is. A bullet keeps a bullet mark."""
    if not raw:
        return []
    text = clean_markup(raw)  # bounded patterns shared with the Posting tab (issue #210)
    out: list[Block] = []
    for chunk in re.split(r"\n\s*\n", text):
        lines: list[str] = []
        heading = False
        for line in chunk.split("\n"):
            is_head = bool(_HEAD.match(line)) or bool(_BOLD_LINE.match(line))
            bullet = bool(_BULLET.match(line))
            plain = _plain_line(_BULLET.sub("", _HEAD.sub("", line)))
            if not plain:
                continue
            lines.append(("• " + plain) if bullet else plain)
            heading = heading or is_head
        if lines:
            heading = heading and len(lines) == 1
            if not heading and len(lines) == 1 and len(lines[0]) <= 50 \
                    and not re.search(r"[.!?,;]$", lines[0]) and not lines[0].startswith("•"):
                heading = True  # a short line with no end mark reads as a heading
            out.append(Block("\n".join(lines), heading))
    return out


def _skip_leading(blocks: list[Block]) -> list[Block]:
    """Drop, at the START only, an equal opportunity paragraph or a company introduction (its
    heading and the paragraphs up to the next heading). Unsure: keep. Never leaves less than
    _MIN_KEPT characters, so a short posting is shown whole."""
    cur = list(blocks)
    while cur:
        first = cur[0]
        nxt = 1
        if len(first.text) < 700 and not first.heading and _EEO.search(first.text):
            pass
        elif first.heading and _ABOUT_US.match(first.text.strip()):
            while nxt < len(cur) and not cur[nxt].heading:
                nxt += 1
        else:
            break
        rest = cur[nxt:]
        if sum(len(b.text) for b in rest) < _MIN_KEPT:
            break
        cur = rest
    return cur


def _ended(text: str) -> str:
    """`text` with an ellipsis, unless it already ends a sentence (. ! or ?, with or without a
    closing quote or bracket): "…" after a full stop reads as a cut that is not one."""
    return text if text.rstrip("\"')”’]").endswith((".", "!", "?")) else text + "…"


def _excerpt(blocks: list[Block]) -> str:
    blocks = [b for b in blocks if not b.heading] or blocks  # a heading is not a sentence
    text = " ".join(" ".join(b.text.replace("• ", "").split()) for b in blocks).strip()
    if not text:
        return ""
    sentences = _SENTENCE_END.split(text)
    picked: list[str] = []
    for sent in sentences:
        joined = " ".join(picked + [sent])
        if picked and (len(picked) >= EXCERPT_SENTENCES or len(joined) > EXCERPT_CHARS):
            break
        picked.append(sent)
    out = " ".join(picked)
    if len(out) > EXCERPT_CHARS:
        cut = out[:EXCERPT_CHARS]
        if " " in cut and out[EXCERPT_CHARS] != " ":
            cut = cut[: cut.rfind(" ")]
        return _ended(cut.rstrip(" ,;:-"))
    return _ended(out) if len(out) < len(text) else out


def posting_text(raw: str | None, redact: Callable[[str], str] | None = None) -> PostingText:
    """The card's text for one posting description. `redact` (Confidential mode) runs on the
    plain text AFTER markup is removed, so a name split by a tag or an entity is still found, and
    on the stored text first, where a tag next to a name still ends the word (issue #210)."""
    blocks = blocks_of(redact(raw) if redact is not None and raw else raw)
    if redact is not None:
        blocks = [Block(redact(b.text), b.heading) for b in blocks]
    if not blocks:
        return PostingText()
    excerpt = _excerpt(_skip_leading(blocks))
    paragraphs = [b.text for b in blocks]
    total = 0
    kept: list[str] = []
    for para in paragraphs:
        if total + len(para) > FULL_CHARS:
            room = max(0, FULL_CHARS - total)
            if room > 200:
                kept.append(para[:room].rsplit(" ", 1)[0] + "…")
            break
        kept.append(para)
        total += len(para)
    whole = " ".join(" ".join(paragraphs).replace("• ", "").split())
    has_more = excerpt.rstrip("…") != whole or len(excerpt) > _CLAMPED_CHARS
    return PostingText(excerpt=excerpt, paragraphs=kept, has_more=has_more)
