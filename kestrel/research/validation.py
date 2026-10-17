"""The validation helpers that make the anti-hallucination rules enforceable in CODE, not merely
stated in the prompt (SPEC-MODULES 2.1/2.2). Both the research (P5) and contacts (P6) paths resolve
a returned source_index against the supplied excerpts and check any returned email against the
supplied text VERBATIM. A claim with an out-of-range index is dropped; an email absent
verbatim is nulled; a person without a valid source_index is never created.

These functions are pure so the adversarial offline gate can exercise them directly."""

from __future__ import annotations

import re

# Excerpt indices are 1-based to match the prompt block ("[1] url\n excerpt").
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def valid_source_index(index: object, n_excerpts: int) -> bool:
    """True iff `index` is an int in [1, n_excerpts] — it resolves to a supplied excerpt."""
    return isinstance(index, int) and not isinstance(index, bool) and 1 <= index <= n_excerpts


def email_verbatim(email: str | None, source_texts: list[str]) -> str | None:
    """Return the email ONLY if it appears verbatim (case-insensitively) as a WHOLE email token in
    some supplied source; otherwise None. Never construct or guess an email from a domain
    (SPEC-MODULES 2.2). Matching is against the set of complete email tokens extracted from each
    source, not a raw substring search: `roe@acme.com` must NOT pass merely because the source
    contains `jane.roe@acme.com`, or the verbatim guarantee reduces to a domain guess."""
    if not email or not isinstance(email, str):
        return None
    candidate = email.strip()
    if not _EMAIL_RE.fullmatch(candidate):
        return None
    low = candidate.lower()
    for text in source_texts:
        if isinstance(text, str) and low in {m.lower() for m in _EMAIL_RE.findall(text)}:
            return candidate
    return None


def resolve_index(index: object, excerpts: list) -> object | None:
    """Return the excerpt an index points at, or None. Excerpts are indexed 1-based."""
    if not valid_source_index(index, len(excerpts)):
        return None
    return excerpts[index - 1]
