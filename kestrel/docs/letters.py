"""Cover letters (P4) and engagement outlines (P4C) — SPEC-MODULES 1.6. Generated prose is permitted
here, under constraint: every factual claim must trace to a real proof point or doc-variant key
(`claims_used`); a banned-phrase check runs on the body; and an engagement outline needs at least
three explicit exclusions (it is an outline for a conversation, never a priced proposal). The system
never sends either — it drafts and hands over (hard rule 1). The verification helpers are pure."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kestrel.llm.guard import run_prompt
from kestrel.llm.plain_repair import enforce
from kestrel.llm.prompts import load_prompt

_MIN_EXCLUSIONS = 3
_BANNED = [
    "hit the ground running",
    "think outside the box",
    "team player",
    "results-oriented",
    "results-driven",
    "go-getter",
    "synergy",
    "synergies",
    "proven track record",
    "detail-oriented",
    "self-starter",
    "value-add",
    "move the needle",
    "best of breed",
    "win-win",
    "circle back",
    "low-hanging fruit",
    "boil the ocean",
]
# Word-bounded so "value-add" does not fire inside "value-added" etc. (fewer false positives).
_BANNED_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _BANNED) + r")\b", re.IGNORECASE
)


@dataclass
class LetterDraft:
    value: object | None
    unverified_claims: list[str] = field(default_factory=list)
    banned_hits: list[str] = field(default_factory=list)
    too_few_exclusions: bool = False
    # Plain-writing marks left after the one re-ask (issue #93); shown, never rewritten.
    style_flags: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def clean(self) -> bool:
        return (
            self.value is not None
            and not self.unverified_claims
            and not self.banned_hits
            and not self.too_few_exclusions
        )


def verify_claims(claims_used, valid_keys: set[str]) -> list[str]:
    """Every claims_used entry must resolve to a real proof-point or variant key; return the ones
    that do not (the draft is then flagged 'unverified claims present')."""
    return [c for c in (claims_used or []) if c not in valid_keys]


def same_claims(first, fresh, valid_keys: set[str]) -> bool:
    """A re-asked letter body is taken only when it cites the same claims as the first reply and
    resolves them the same way: new wording must not bring new claims."""
    return sorted(first.claims_used or []) == sorted(fresh.claims_used or []) and sorted(
        verify_claims(first.claims_used, valid_keys)
    ) == sorted(verify_claims(fresh.claims_used, valid_keys))


def banned_hits(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _BANNED_RE.finditer(text or "")})


async def _run(client, spec, messages, *, session, on_quarantine, reask=False):
    """The tier3 prompts go through the Hosted mode plan (redaction or refusal) when the caller
    has a session; without one the call is local-only as before."""
    if reask:  # a failed re-ask leaves no quarantine row
        on_quarantine = None
    if session is None:
        return await run_prompt(client, spec, messages, on_quarantine=on_quarantine)
    from kestrel.pipeline._shared import run_prompt_hosted

    return await run_prompt_hosted(
        session, client, spec, messages, quarantine_tier=4, on_quarantine=on_quarantine
    )


async def generate_cover(
    client, *, user_prompt: str, valid_keys: set[str], on_quarantine=None, session=None,
    exempt: tuple[str, ...] = (),
):
    """P4 cover letter. `user_prompt` is the assembled context (profile, selected verbatim bullets,
    evaluation angle, job); the caller supplies only real proof points and variants."""
    spec = load_prompt("cover/v2")
    messages = [
        {"role": "system", "content": spec.system},
        {"role": "user", "content": user_prompt},
    ]
    result = await _run(client, spec, messages, session=session, on_quarantine=on_quarantine)
    if not result.ok or result.value is None:
        return LetterDraft(value=None, error=result.error)
    plain = await enforce(
        spec, result.value, messages=messages, exempt=exempt,
        run=lambda m, reask=False: _run(
            client, spec, m, session=session, on_quarantine=on_quarantine, reask=reask
        ),
        accept=lambda _merged, fresh, first=result.value: same_claims(first, fresh, valid_keys),
    )
    v = plain.value
    return LetterDraft(
        style_flags=plain.flags,
        value=v,
        unverified_claims=verify_claims(v.claims_used, valid_keys),
        banned_hits=banned_hits(v.body_md),
    )


async def generate_engagement(
    client, *, user_prompt: str, valid_keys: set[str], on_quarantine=None, session=None,
    exempt: tuple[str, ...] = (),
):
    """P4C engagement outline. Needs at least three explicit exclusions."""
    spec = load_prompt("engagement_outline/v2")
    messages = [
        {"role": "system", "content": spec.system},
        {"role": "user", "content": user_prompt},
    ]
    result = await _run(client, spec, messages, session=session, on_quarantine=on_quarantine)
    if not result.ok or result.value is None:
        return LetterDraft(value=None, error=result.error)
    plain = await enforce(
        spec, result.value, messages=messages, exempt=exempt,
        run=lambda m, reask=False: _run(
            client, spec, m, session=session, on_quarantine=on_quarantine, reask=reask
        ),
        accept=lambda _merged, fresh, first=result.value: same_claims(first, fresh, valid_keys),
    )
    v = plain.value
    return LetterDraft(
        style_flags=plain.flags,
        value=v,
        unverified_claims=verify_claims(v.claims_used, valid_keys),
        banned_hits=banned_hits(v.body_md),
        too_few_exclusions=len(v.exclusions or []) < _MIN_EXCLUSIONS,
    )
