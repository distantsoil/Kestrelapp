"""Text normalization shared by the role page's source matcher and requirement corrections.

`norm` is the source matcher's comparison form: lowercase, every run of non-alphanumerics to one
space, trimmed. `requirement_norm` is the requirement's identity form: the same, except that it
keeps every letter and digit of any script and the `+` and `#` that tell C++ and C# from C, so
"Proficiency in C++" and a requirement written in Cyrillic or Chinese keep their own identity
(D-LEARN-11). `requirement_hash` is the sha1 of that form, so a correction made on one read still
finds its requirement after a rescore reorders the rows or changes punctuation and case
(SPEC-MODULES 5.1 `requirement_correction`, D-LEARN-3)."""

from __future__ import annotations

import hashlib
import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Anything but a letter or digit of any script, `+` or `#`. `_` is a word character in `\w`, so it
# is named here to be dropped like other punctuation.
_NON_REQ = re.compile(r"[^\w+#]+|_+")


def norm(text: str | None) -> str:
    return _NON_ALNUM.sub(" ", (text or "").lower()).strip()


def requirement_norm(text: str | None) -> str:
    """Lowercase, every run of characters that are not a letter or digit (any script), `+` or `#`
    to one space, trimmed. Finer than `norm`: two texts that share this form share `norm` too, so
    re-deriving stored hashes never merges two rows (migration 0034)."""
    return " ".join(_NON_REQ.sub(" ", (text or "").lower()).split())


def requirement_hash(text: str | None) -> str:
    return hashlib.sha1(requirement_norm(text).encode("utf-8"), usedforsecurity=False).hexdigest()
