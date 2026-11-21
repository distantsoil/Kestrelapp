"""The typed kind of a quarantined model reply (D-UI-550).

The guard knows WHY a reply failed at the line that decides it: a decline, a reply cut at its cap,
a generation loop, or an answer that did not fit the schema. That choice is made once, there, and
travels with the reason as a `KindedReason` (a `str` that carries `.kind`), so a hook that wants it
reads `kind_of(error)` and a hook that does not sees an ordinary string. The quarantine row stores
it on its own last line (`KIND_MARK`), written by `tag` and read back by `split_stored` against the
`KINDS` allowlist. Nothing here ever reads the wording of an error to pick a kind (SPEC-INTERFACES
section 8a.2); a row written before this existed has no kind and is never grouped."""

from __future__ import annotations

KIND_MARK = "\n[kind] "

UNREADABLE = "unreadable"  # the answer did not fit the schema, even after the repair
REPEATED = "looping"  # a generation loop
OUT_OF_ROOM = "out_of_room"  # a reply cut at its cap, or all of it spent thinking

KINDS: frozenset[str] = frozenset({UNREADABLE, REPEATED, OUT_OF_ROOM})


class KindedReason(str):
    """A failure reason that carries its typed kind. Equal to, and printed as, the plain text."""

    kind: str | None

    def __new__(cls, text: str, kind: str | None = None) -> KindedReason:
        obj = super().__new__(cls, text)
        obj.kind = kind if kind in KINDS else None
        return obj


def kind_of(reason: object) -> str | None:
    """The kind the guard attached to `reason`, or None (a plain string has none)."""
    kind = getattr(reason, "kind", None)
    return kind if isinstance(kind, str) and kind in KINDS else None


def tag(error: str, kind: str | None) -> str:
    """`error` with the kind on its own last line; `error` unchanged when there is no kind."""
    if kind not in KINDS:
        return error
    return f"{error}{KIND_MARK}{kind}"


def split_stored(stored: str | None) -> tuple[str | None, str]:
    """(kind or None, the error without the kind line). Only the last line is read, and only a
    value on the allowlist counts, so a reply that happens to contain "[kind]" can never pose as
    one: the mark must be the very end of the stored text."""
    text = stored or ""
    head, sep, last = text.rpartition(KIND_MARK)
    if sep and last in KINDS:
        return last, head
    return None, text


def declined_kind(exc: object) -> str | None:
    """The kind of a ModelDeclined, from the flags the client set where it raised it. A decline with
    none of them (a safety refusal, a provider's HTTP 400) has no kind: the cause is not proven, so
    it is never grouped (section 8a.5)."""
    if getattr(exc, "degenerate", False):
        return REPEATED
    if getattr(exc, "truncated", False) or getattr(exc, "reasoning_exhausted", False):
        return OUT_OF_ROOM
    return None
