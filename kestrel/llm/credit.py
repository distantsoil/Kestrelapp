"""An online key that has run out of credit, said plainly (#29 follow-up, D-LLM-8).

No provider except OpenRouter shows a balance to an ordinary key, so the useful substitute is one
clear message at the moment credit runs out. This module is deliberately free of imports from the
rest of the LLM package so the client, the guard, the worker and the pages can all use it.

What it does: classifies a provider's HTTP error as "out of credit" (a distinct, non-retryable
failure: not a schema failure, not a transient error), remembers per backend that it happened so
nothing more is sent to that backend until the key changes, the operator starts a new run, or the
hold times out, and holds the one authored notice the status rail and the Runs page show. The
message is written here; no provider text reaches a page (D-SEC-2). It never falls back to another
online provider: the hold is per backend and nothing else is chosen in its place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from kestrel.errors import UserFacingError

# Provider -> (name used in the sentence, billing page). Generic OpenAI-compatible services have
# no known page, so the sentence says to check the service's own billing page.
PROVIDER_BILLING: dict[str, tuple[str, str]] = {
    "openrouter": ("OpenRouter", "openrouter.ai/settings/credits"),
    "openmodel": ("OpenModel", "console.openmodel.ai"),
    "anthropic": ("Anthropic", "platform.claude.com/settings/billing"),
    "openai": ("OpenAI", "platform.openai.com/settings/organization/billing"),
}

# How long a hold lasts if nothing clears it: long enough that the 5 second worker beat does not
# probe a dead key, short enough that a top-up is noticed without a restart.
HOLD_SECONDS = 1800.0

_CREDIT_CODES = {"insufficient_balance", "credit_balance_exhausted", "billing_error"}
# OpenAI answers an unfunded account with 429 and this code. Treated as credit, not a rate limit.
_OPENAI_QUOTA_CODES = {"insufficient_quota"}


class OutOfCredit(UserFacingError, RuntimeError):
    """The provider said the key has no credit left. The message is authored (names the provider
    and its billing page); it never contains the provider's own text."""

    def __init__(self, provider: str, *, sent: bool = True) -> None:
        super().__init__(credit_message(provider))
        self.provider = provider
        # False when the hold stopped the call before anything was sent.
        self.sent = sent


def credit_message(provider: str) -> str:
    name, page = PROVIDER_BILLING.get(provider, ("", ""))
    if not name:
        return (
            "Your online model service says the account has run out of credit. Add credit on "
            "that service's billing page, then use Score again."
        )
    return (
        f"Your {name} credit has run out. Top up at {page}, then use Score again."

    )


def is_out_of_credit(
    provider: str, status: int, codes: tuple[str, ...] = (), message: str = ""
) -> bool:
    """Whether an HTTP error from `provider` means no credit. `codes` are the lowercased error
    code/type strings from the body."""
    got = {c.lower() for c in codes}
    if got & _CREDIT_CODES:
        return True
    if provider == "openai" and got & _OPENAI_QUOTA_CODES:
        return True
    if status == 402:
        if provider == "openrouter" and "max_tokens" in message.lower():
            # "requires more credits, or fewer max_tokens": the account cannot afford THIS
            # request size, not an empty account. Not a hold (see TOO_BIG_NOTE).
            return False
        return True  # OpenModel, OpenRouter, Anthropic and generic services
    # Anthropic documents 402 billing_error; its older "credit balance is too low" arrives as a
    # 400 invalid_request_error (unverified in the docs, so matched only for that provider).
    return provider == "anthropic" and status == 400 and "credit balance" in message.lower()


# Appended to a provider's 402 that is about request size, so the quarantine record and the Runs
# page say it plainly. The backend is not held: a smaller request may still go through.
TOO_BIG_NOTE = "Your OpenRouter credit is too low for this request size."


def _digest(key: str) -> str:
    # An in-memory lookup key for the hold, never stored or compared with a stored value, so
    # Python's per-process salted hash() is enough; the raw key is never kept.
    return format(hash(("credit-hold", key or "")) & 0xFFFFFFFFFFFFFFFF, "016x")


@dataclass
class _Hold:
    provider: str
    key: str
    at: float


_HOLDS: dict[str, _Hold] = {}


def _now() -> float:
    return time.monotonic()


def mark_out_of_credit(backend: str, provider: str, api_key: str) -> bool:
    """Hold `backend`. Returns True when this is a new hold (the caller logs once)."""
    held = _HOLDS.get(backend)
    fresh = held is None or held.key != _digest(api_key) or _now() - held.at > HOLD_SECONDS
    _HOLDS[backend] = _Hold(provider, _digest(api_key), _now())
    return fresh


def held_provider(backend: str, api_key: str) -> str | None:
    """The provider name if `backend` is on hold for this key, else None. A different key, or an
    expired hold, releases it."""
    held = _HOLDS.get(backend)
    if held is None:
        return None
    if held.key != _digest(api_key) or _now() - held.at > HOLD_SECONDS:
        _HOLDS.pop(backend, None)
        return None
    return held.provider


def any_held() -> bool:
    """Whether any backend is on hold right now (cheap pre-check before per-task resolution)."""
    return any(_now() - h.at <= HOLD_SECONDS for h in _HOLDS.values())


def clear(backend: str | None = None) -> None:
    """Release one backend, or all. Called when the operator starts a run themselves."""
    if backend is None:
        _HOLDS.clear()
    else:
        _HOLDS.pop(backend, None)


def notices() -> list[str]:
    """The authored notice for each backend on hold, one per provider, for the rail and the Runs
    page."""
    out: list[str] = []
    for backend in list(_HOLDS):
        held = _HOLDS[backend]
        if _now() - held.at > HOLD_SECONDS:
            _HOLDS.pop(backend, None)
            continue
        msg = credit_message(held.provider)
        if msg not in out:
            out.append(msg)
    return out
