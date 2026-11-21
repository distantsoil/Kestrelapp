"""Posting text as a small, safe set of our own nodes (issue #210, D-UI-720).

A description is hostile input (D-SEC-20): it is read, never adopted. `parse` rebuilds it from an
explicit allowlist of node types defined here, and the template macro
(`partials/posting_macros.html`) prints them with autoescaping. Nothing in the text can become
markup: no tag, attribute, link, image or style from the input ever reaches the page. A link is
shown as its words followed by the address in brackets, as plain text.

Allowlist: headings (`#` to `######`, shown at three sizes), paragraphs with line breaks,
`**bold**`, `*italic*` and `***both***` (paired, non-empty, not touching a space on the inside),
bullet lists (`*`, `-`, `+`, a bullet sign) and numbered lists (the number is kept as text, so
the words and numbers are exactly the posting's). Anything else stays literal.

Bounded: every pattern is limited to a fixed span and cannot scan past the next marker, and the
tree is at most three levels deep (block, bold, italic), so time grows linearly with the text.
The stored description is never changed; scoring reads it as stored.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

# Raw HTML that a source stored instead of Markdown: line-ending tags become line breaks and
# every other tag is dropped, never passed through. Bounded classes ([^<>]) keep this linear.
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^\s<>]{1,2048})>", re.I)
_BR = re.compile(r"<\s*(?:br\s*/?|/p|/li|/div|/h[1-6]|/tr)\s*>", re.I)
_TAGS = (
    "a|abbr|article|aside|b|big|blockquote|body|br|button|center|code|col|colgroup|dd|del|details"
    "|div|dl|dt|em|embed|figcaption|figure|font|footer|form|h[1-6]|head|header|hr|html|i|iframe"
    "|img|input|ins|label|li|link|main|mark|meta|nav|noscript|object|ol|option|p|picture|pre|s"
    "|script|section|select|small|source|span|strike|strong|style|sub|summary|sup|svg|table"
    "|tbody|td|textarea|tfoot|th|thead|title|tr|u|ul|video"
)
# Dropped: a known tag name with anything after it, any bare tag ("<wbr>", "</cite>"), and any
# tag that carries a name=value attribute ("<path d='M0'/>"). Words in angle brackets with no
# "=" ("<USD 100k>", "<5 years>") are kept. Every span is bounded and stops at the next "<".
_TAG = re.compile(
    rf"</?(?:{_TAGS})(?![A-Za-z0-9-])[^<>]{{0,2000}}>"
    r"|</?[A-Za-z][A-Za-z0-9-]{0,40}\s*/?>"
    r"|</?[A-Za-z][A-Za-z0-9-]{0,40}\s[^<>=]{0,2000}=[^<>]{0,2000}>"
    r"|<!--[^<>]{0,5000}-->",
    re.I,
)

_HEAD = re.compile(r"^ {0,3}(#{1,6})[ \t]+(\S.*)$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_BULLET = re.compile(r"^([ \t]*)[-*+•][ \t]+(\S.*)$")
_NUMBER = re.compile(r"^([ \t]*)(\d{1,9}[.)])[ \t]+(\S.*)$")

# Backslash escapes Markdown adds. A few become private sentinels so an escaped mark can never
# pair with a real one; the rest just lose the backslash.
_ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>|~])")
_SENTINEL = {"*": "", "[": "", "]": "", "(": "", ")": ""}
_RESTORE = str.maketrans({v: k for k, v in _SENTINEL.items()})
_DROP_SENTINELS = str.maketrans({v: None for v in _SENTINEL.values()})

_LINK = re.compile(
    r"!?\[([^\[\]\n]{0,1000})\]\([ \t]*([^\s()<>]{1,2048})(?:[ \t]+\"[^\"\n]{0,500}\")?[ \t]*\)"
)
_STRONG = re.compile(
    r"\*\*\*(?![\s*])([^*]{1,2000}?)(?<![\s*])\*\*\*(?!\*)"
    r"|(?<!\*)\*\*(?![\s*])((?:[^*]|\*(?!\*)){1,2000}?)(?<![\s*])\*\*(?!\*)"
)
# A star between two digits is arithmetic ("2*3*4"), never an italic mark.
_EM = re.compile(r"(?<![*\d])\*(?![\s*])([^*]{1,2000}?)(?<![\s*])\*(?![*\d])")


@dataclass(frozen=True)
class Text:
    kind: ClassVar[str] = "text"
    text: str


@dataclass(frozen=True)
class Em:
    kind: ClassVar[str] = "em"
    children: tuple[Text, ...]


@dataclass(frozen=True)
class Strong:
    kind: ClassVar[str] = "strong"
    children: tuple[Text | Em, ...]


Inline = Text | Em | Strong
Line = tuple[Inline, ...]


@dataclass(frozen=True)
class Heading:
    kind: ClassVar[str] = "heading"
    level: int  # 1, 2 or 3: chosen here from the mark count, never read from the input as is
    line: Line


@dataclass(frozen=True)
class Paragraph:
    kind: ClassVar[str] = "paragraph"
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class Item:
    number: str  # "" for a bullet, else the posting's own "3." or "3)"
    sub: bool  # indented under the item before it (one level only)
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class ListBlock:
    kind: ClassVar[str] = "list"
    ordered: bool
    items: tuple[Item, ...]


@dataclass(frozen=True)
class Rest:
    """Text past the node budget, as one plain run with its line breaks kept (D-UI-721)."""

    kind: ClassVar[str] = "rest"
    text: str


Block = Heading | Paragraph | ListBlock | Rest

# A real posting needs a few hundred nodes. Past this many, the rest of the text is shown as one
# plain run, so a hostile or runaway description cannot cost more than a bounded tree to render.
NODE_BUDGET = 4000
# A line longer than this is shown as plain text: no posting has a paragraph this long, and it
# keeps a single runaway line inside the same bound.
LINE_FORMAT_CHARS = 10000


def clean_markup(raw: str) -> str:
    """Line breaks unified, autolinks unwrapped, known HTML tags dropped, entities read once."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _AUTOLINK.sub(r"\1", text)
    text = _BR.sub("\n", text)
    text = _TAG.sub("", text)
    # Invisible characters inside a name are handled by the redaction matcher itself
    # (kestrel/privacy.py), on every surface, not dropped here.
    return html.unescape(text).replace("\u00a0", " ")


def _text(value: str) -> Text:
    return Text(value.translate(_RESTORE))


def _italic(s: str) -> list[Text | Em]:
    out: list[Text | Em] = []
    pos = 0
    for m in _EM.finditer(s):
        if m.start() > pos:
            out.append(_text(s[pos : m.start()]))
        out.append(Em((_text(m.group(1)),)))
        pos = m.end()
    if pos < len(s):
        out.append(_text(s[pos:]))
    return out


def _emphasis(s: str) -> list[Inline]:
    out: list[Inline] = []
    pos = 0
    for m in _STRONG.finditer(s):
        if m.start() > pos:
            out.extend(_italic(s[pos : m.start()]))
        if m.group(1) is not None:
            out.append(Strong((Em((_text(m.group(1)),)),)))
        else:
            out.append(Strong(tuple(_italic(m.group(2)))))
        pos = m.end()
    if pos < len(s):
        out.extend(_italic(s[pos:]))
    return out


def _inline(s: str) -> Line:
    """One line of text as inline nodes. A link or image becomes its words and its address as
    plain text, so nothing in the posting can place a link on the page."""
    s = s.translate(_DROP_SENTINELS)
    s = _ESC.sub(lambda m: _SENTINEL.get(m.group(1), m.group(1)), s)
    s = s.rstrip()
    if s.endswith("\\"):  # a Markdown hard line break mark
        s = s[:-1].rstrip()
    if len(s) > LINE_FORMAT_CHARS:
        return (_text(s),) if s else ()
    out: list[Inline] = []
    pos = 0
    for m in _LINK.finditer(s):
        out.extend(_emphasis(s[pos : m.start()]))
        label, url = m.group(1).strip(), m.group(2)
        if label and label != url:
            out.extend(_emphasis(label))
            out.append(_text(f" ({url})"))
        else:
            out.append(_text(url))
        pos = m.end()
    out.extend(_emphasis(s[pos:]))
    return tuple(n for n in out if not (isinstance(n, Text) and not n.text))


def _flat(line: Line) -> str:
    parts: list[str] = []
    for n in line:
        if isinstance(n, Text):
            parts.append(n.text)
        else:
            parts.append(_flat(n.children))  # type: ignore[arg-type]
    return "".join(parts)


def parse(raw: str | None, redact: Callable[[str], str] | None = None) -> list[Block]:
    """The description as blocks. `redact` (Confidential mode) runs twice: on the stored text, where
    a tag next to a name still marks the end of a word ("Acme<sup>2</sup>"), and again after
    markup is removed, so a name split by a tag or an entity ("<b>Acme</b> Freight") is found."""
    if not raw:
        return []
    if redact is not None:
        text = redact(clean_markup(redact(raw)))
    else:
        text = clean_markup(raw)

    def line_nodes(s: str) -> Line:
        """One line's nodes. In Confidential mode the line's words are checked once more after
        the marks are gone: "Ac*me*" or "Ac[me](x)" reads as the name, so a line whose plain
        words change under redaction is shown as one plain, redacted run (issue #210 review)."""
        nodes = _inline(s)
        if redact is None or not nodes:
            return nodes
        flat = _flat(nodes)
        hidden = redact(flat)
        return nodes if hidden == flat else (Text(hidden),)
    blocks: list[Block] = []
    para: list[Line] = []
    items: list[Item] = []
    ordered = False
    used = 0

    def flush_para() -> None:
        if para:
            blocks.append(Paragraph(tuple(para)))
            para.clear()

    def flush_list() -> None:
        if items:
            blocks.append(ListBlock(ordered, tuple(items)))
            items.clear()

    lines = text.split("\n")
    for index, line in enumerate(lines):
        if used > NODE_BUDGET:
            flush_para()
            flush_list()
            rest = "\n".join(ln.strip() for ln in lines[index:] if ln.strip())
            if rest:
                blocks.append(Rest(rest.translate(_DROP_SENTINELS)))
            break
        used += 1
        if not line.strip():
            flush_para()
            flush_list()
            continue
        if _HR.match(line):
            flush_para()
            flush_list()
            continue
        head = _HEAD.match(line)
        if head:
            flush_para()
            flush_list()
            body = head.group(2).rstrip()
            closing = re.search(r"[ \t]#+$", body)  # an optional closing run of marks
            if closing:
                body = body[: closing.start()].rstrip()
            inl = line_nodes(body)
            used += len(inl)
            if inl:
                blocks.append(Heading(min(len(head.group(1)), 3), inl))
            continue
        bullet = _BULLET.match(line)
        number = None if bullet else _NUMBER.match(line)
        if bullet or number:
            flush_para()
            indent = (bullet or number).group(1).expandtabs(4)  # type: ignore[union-attr]
            sub = bool(items) and len(indent) >= 2
            if items and not sub and ordered != (number is not None):
                flush_list()  # a bullet list and a numbered list side by side stay two lists
            if not items:
                ordered = number is not None
            mark = number.group(2) if number else ""
            body = number.group(3) if number else bullet.group(2)  # type: ignore[union-attr]
            inl = line_nodes(body)
            used += len(inl)
            items.append(Item(mark, sub, (inl,)))
            continue
        if items and line[:1] in (" ", "\t"):
            last = items[-1]  # an indented line continues the item above it
            inl = line_nodes(line.strip())
            used += len(inl)
            items[-1] = Item(last.number, last.sub, last.lines + (inl,))
            continue
        flush_list()
        inl = line_nodes(line.strip())
        used += len(inl)
        if inl:
            para.append(inl)
    flush_para()
    flush_list()
    return blocks


def limit_blocks(blocks: list[Block], max_chars: int) -> tuple[list[Block], bool]:
    """The first blocks that fit in about max_chars of text; True when some were left out."""
    kept: list[Block] = []
    total = 0
    for b in blocks:
        size = len(block_text(b))
        if not kept and size > max_chars:  # one huge first block: its start, as plain text
            return [Rest(block_text(b)[:max_chars].rsplit(" ", 1)[0])], True
        if kept and total + size > max_chars:
            return kept, True
        kept.append(b)
        total += size
    return kept, False


def block_text(block: Block) -> str:
    if isinstance(block, Rest):
        return block.text
    if isinstance(block, Heading):
        return _flat(block.line)
    if isinstance(block, Paragraph):
        return "\n".join(_flat(ln) for ln in block.lines)
    return "\n".join(
        " ".join(filter(None, [it.number, " ".join(_flat(ln) for ln in it.lines)]))
        for it in block.items
    )


def plain_text(raw: str | None, redact: Callable[[str], str] | None = None) -> str:
    """The description's words with every mark removed, on one line (for short excerpts)."""
    return " ".join(" ".join(block_text(b) for b in parse(raw, redact)).split())
