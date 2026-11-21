"""The person's own LinkedIn profile address (D-PHONE-6). Shape only, offline, never rejects.

Nothing here opens a socket: the address is the person's own text, like their email (D-SRC-5
forbids automated LinkedIn access; this is not that)."""

from __future__ import annotations

import re
from dataclasses import dataclass

UNREADABLE_NOTE = (
    "Kestrel could not read this as a LinkedIn profile address; it is kept as you typed it."
)
_SHAPE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/([\w%.\-]{1,100})/?", re.IGNORECASE
)
_MAX_LEN = 300


@dataclass(frozen=True)
class LinkedInResult:
    entered: str
    display: str       # the stored text: the normalized address, or exactly what was typed
    readable: bool

    @property
    def note(self) -> str:
        return "" if self.readable or not self.entered.strip() else UNREADABLE_NOTE


def normalize(text: object) -> LinkedInResult:
    entered = str(text or "")
    stripped = entered.strip()
    if not stripped or len(stripped) > _MAX_LEN:
        return LinkedInResult(entered, stripped, False)
    bare = re.split(r"[?#]", stripped, maxsplit=1)[0]
    m = _SHAPE.fullmatch(bare)
    if not m:
        return LinkedInResult(entered, stripped, False)
    return LinkedInResult(entered, f"https://www.linkedin.com/in/{m.group(1)}", True)


def href(stored: object) -> str:
    """The address to link to: only a normalized address ever becomes a link."""
    res = normalize(stored)
    return res.display if res.readable and res.display == str(stored or "").strip() else ""


def short(stored: object) -> str:
    """How the contact line prints a normalized address: no scheme, no www."""
    h = href(stored)
    return re.sub(r"^https://(?:www[.])?", "", h) if h else str(stored or "").strip()


def handle_of(text: object) -> str:
    """The lowercase profile handle in a link written any way, else "" (whole handle, no scheme,
    query, fragment or trailing slash)."""
    m = re.search(r"linkedin\.com/in/([\w%.\-]+)", str(text or ""), re.IGNORECASE)
    return m.group(1).lower() if m else ""
