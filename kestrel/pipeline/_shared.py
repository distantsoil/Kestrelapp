"""Shared machinery for the Tier 1, Tier 2 and document-selection handlers: cached prompt specs,
the quarantine hook, the handler outcome type, and the Hosted mode send plan (SPEC.md 6.4): when
a tier's backend is external and Hosted mode is on, the handler redacts the confidential employer
before the payload is built and refuses (quarantines, never rejects) when there is nothing to
redact with, exactly as Tier 3 escalation does."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import Quarantine
from kestrel.llm.guard import GuardResult, QuarantineHook, run_prompt
from kestrel.llm.prompts import PromptSpec, load_prompt
from kestrel.privacy import EmployerMap

log = structlog.get_logger()


@dataclass
class HandlerOutcome:
    action: (
        str  # e.g. "t1_pass", "t1_reject", "t2_done", "promoted", "quarantined", "skip", "error"
    )
    tier: int
    verdict: str | None = None
    forced: bool = False
    needs_review: bool = False
    quarantined: bool = False
    error: str | None = None


@cache
def prompt_spec(version: str) -> PromptSpec:
    """Prompt specs are immutable packaged files; load each once."""
    return load_prompt(version)


# A score-again of a role already scored that fails keeps the role's stage (its earlier score
# stands). Its quarantine row is marked by this prefix on `error`, carrying the score-again task's
# id, so Failed scoring lists it apart and "Try again" re-queues that task's own payload (the
# model override with it) without touching the stage. A prefix on an existing column, not a new
# column: no migration, and the text reads plainly on the page.
RESCORE_FAILED_MARK = "[score again"


def rescore_failure_prefix(task_id: int | None) -> str:
    return f"{RESCORE_FAILED_MARK} #{task_id}] " if task_id else f"{RESCORE_FAILED_MARK}] "


def parse_rescore_failure(error: str | None) -> tuple[bool, int | None, str]:
    """(is a score-again failure, its task id or None, the error without the mark)."""
    text = error or ""
    if not text.startswith(RESCORE_FAILED_MARK):
        return False, None, text
    head, sep, rest = text.partition("] ")
    if not sep:
        return False, None, text
    tag = head[len(RESCORE_FAILED_MARK):].strip()
    task_id = int(tag[1:]) if tag.startswith("#") and tag[1:].isdigit() else None
    return True, task_id, rest


def make_quarantine_hook(
    session: AsyncSession, *, tier: int, opportunity_id: int, error_prefix: str = ""
) -> QuarantineHook:
    """A hook that records the failed call to the SAME session the handler commits, so the
    quarantine row and the stage change are atomic. Signature matches guard.QuarantineHook:
    (messages, raw_response, error, model). `error_prefix` marks a score-again failure
    (rescore_failure_prefix)."""

    async def hook(messages: list[dict], raw_response: str, error: str, model: str) -> None:
        prompt_text = "\n\n".join(f"[{m.get('role')}]\n{m.get('content')}" for m in messages)
        session.add(
            Quarantine(
                opportunity_id=opportunity_id,
                tier=tier,
                model=model,
                prompt=prompt_text[:20000],
                raw_response=(raw_response or "")[:20000],
                error=(error_prefix + (error or ""))[:4000],
                attempts=2,
            )
        )

    return hook


# ------------------------------------------------------------------------------- hosted mode
HOSTED_REFUSAL = (
    "hosted send refused: confidential mode is on but no redaction map is configured (choose "
    "your current employer under Settings, Core); refusing to send the employer unredacted to a "
    "hosted model"
)


@dataclass
class HostedPlan:
    """What a handler must do before calling the model for one tier.

    external: the tier's backend is external and Hosted mode is on and acknowledged, so the
        payload WILL leave the machine. False for local tiers, and also when the tier names an
        external backend while Hosted mode is off (the client then refuses the call as today).
    mapping: the confidential employer map whose tokens replace the employer before the send.
    refuse: a reason to quarantine instead of sending. Hard rule 4: the item is kept, not rejected.
    """

    external: bool = False
    mapping: EmployerMap = field(default_factory=EmployerMap)
    refuse: str | None = None


def tier_is_hosted(settings: Settings, tier: str) -> bool:
    """True when a call on this tier would go to an external backend under Hosted mode."""
    tcfg = settings.llm.tiers.get(tier)
    backend = settings.llm.backends.get(tcfg.backend) if tcfg else None
    return bool(backend and backend.external and settings.llm.hosted_mode.active)


async def plan_hosted_send(
    session: AsyncSession, settings: Settings, tier: str, *, master_id: int | None = None
) -> HostedPlan:
    """Decide redaction or refusal for a tier BEFORE its prompt is rendered. Reads the operator's
    own employer map from the session: the one document's map when the send is about one
    document (`master_id`, D-PRIV-1), else the union across documents."""
    if not tier_is_hosted(settings, tier):
        return HostedPlan()
    hm = settings.llm.hosted_mode
    if not settings.confidential_mode or not hm.redact_confidential:
        return HostedPlan(external=True)
    from kestrel.docs.current_employer import load_employer_map

    mapping = await load_employer_map(session, master_id=master_id)
    if not mapping:
        return HostedPlan(external=True, refuse=HOSTED_REFUSAL)
    return HostedPlan(external=True, mapping=mapping)


def encode_send(messages: list[dict], mapping):
    """(messages to send, the guard's reply filter) for one call (D-PRIV-1). With a map, every
    listed name of each current employer becomes a fresh random code for this call only, and the
    filter restores exactly those codes in the reply. Without one, (messages, None)."""
    if not mapping:
        return messages, None
    book = mapping.codebook(m.get("content") or "" for m in messages)
    return book.encode_messages(messages), book


# Tokens a Tier 2 sized prompt spends outside the description: the system prompt and global
# rules, the full profile, the organization context, the few-shot examples and the comp corpus
# line. Generous on purpose: an overestimate only trims a little earlier.
HOSTED_PROMPT_OVERHEAD = 6000
# Tiers whose description slice is a HEAD only (SPEC.md 6.2: Tier 1 decides from the first 1200
# tokens; the trigger classifier is a Tier 1 sized prompt). Their hosted slice stays what it was,
# so the disclosure's "first N tokens" stays true.
_HEAD_ONLY_TIERS = frozenset({"tier1", "trigger"})


def hosted_jd_ceiling(settings: Settings) -> int:
    """The most description a hosted Tier 2 call can carry: the figure the Hosted mode
    disclosure states (D-LLM-3), from the same terms the acknowledgement is checked against."""
    return int(settings.llm.hosted_mode.disclosure_terms()["tier2_ceiling_tokens"])


# cl100k (the counter Kestrel trims with) and a hosted model's own tokenizer disagree; a slice
# that fits by cl100k can overflow the window by this much on another tokenizer.
TOKENIZER_MARGIN = 0.15


def jd_budget(settings: Settings, tier: str, default: int, *, external: bool | None = None) -> int:
    """The description slice for this tier. Locally: `default`. On a tier whose call goes to a
    hosted model (D-LLM-3; `external` forces the answer, for the frontier escalation, which is
    hosted in either mode): sized to that model's context window as the provider listed it
    (`llm.hosted_model_caps`):

        budget = (context - prompt overhead - the tier's output budget), less a 15% margin for
        tokenizer mismatch, clamped to [0, hosted_jd_ceiling].

    Below the floor (hosted_mode.max_jd_tokens, 3000) the floor is used only when the window
    really holds it (context >= floor + overhead + output); otherwise the smaller figure, since
    a slice past the window is truncated or refused by the provider. With the context unknown,
    the local budget, at least the floor and at most the ceiling. Tier 1 (head only) keeps the
    smaller of its head and the floor."""
    hosted = tier_is_hosted(settings, tier) if external is None else external
    if not hosted:
        return default
    hm = settings.llm.hosted_mode
    if tier in _HEAD_ONLY_TIERS:
        return max(1, min(default, hm.max_jd_tokens))
    ceiling = hosted_jd_ceiling(settings)
    floor = hm.max_jd_tokens
    tcfg = settings.llm.tiers.get(tier)
    caps = settings.llm.hosted_model_caps.get(tcfg.model) if tcfg else None
    ctx = caps.context_length if caps is not None else None
    if not ctx:
        return max(1, min(ceiling, max(floor, default)))
    output = (tcfg.max_tokens or 0) if tcfg else 0
    raw = ctx - HOSTED_PROMPT_OVERHEAD - output
    budget = max(0, min(ceiling, int(raw * (1 - TOKENIZER_MARGIN))))
    if budget < floor and ctx >= floor + HOSTED_PROMPT_OVERHEAD + output:
        budget = min(floor, ceiling)
    return max(1, budget)


async def run_prompt_hosted(
    session: AsyncSession,
    client: Any,
    spec: PromptSpec,
    messages: list[dict],
    *,
    quarantine_tier: int,
    opportunity_id: int | None = None,
    on_quarantine: QuarantineHook | None = None,
    master_id: int | None = None,
    schema: dict | None = None,
) -> GuardResult:
    """guard.run_prompt with the Hosted mode plan applied first (SPEC.md 6.4): the one path for
    every prompt outside the Tier 1/2/docsel handlers (tier3 work, trigger classification) so a
    tier routed to a hosted backend redacts the confidential employer before the send and is
    refused (a quarantine row with attempts=0, never a rejection) when there is nothing to
    redact with. A local tier passes straight through. Reads the plan off `client.settings`, the
    settings the call will actually be made with. `schema` is a per-call JSON schema (the P3 and
    P13 pattern: enums of real ids); `spec.model_cls` must then be the matching per-call model."""
    settings = getattr(client, "settings", None)
    if settings is None:  # a test double with no settings: it cannot be routed anywhere hosted
        return await run_prompt(
            client, spec, messages, schema=schema, on_quarantine=on_quarantine
        )
    plan = await plan_hosted_send(session, settings, spec.tier, master_id=master_id)
    if plan.refuse:
        tcfg = client.settings.llm.tiers.get(spec.tier)
        record_refusal(
            session,
            tier=quarantine_tier,
            opportunity_id=opportunity_id,
            model=tcfg.model if tcfg else "",
            reason=plan.refuse,
            messages=messages,
        )
        return GuardResult(ok=False, quarantined=True, error=plan.refuse)
    sent, book = encode_send(messages, plan.mapping)
    return await run_prompt(
        client, spec, sent, schema=schema, on_quarantine=on_quarantine, reply_filter=book
    )


def record_refusal(
    session: AsyncSession,
    *,
    tier: int,
    opportunity_id: int | None,
    model: str,
    reason: str,
    messages: list[dict] | None = None,
    error_prefix: str = "",
) -> None:
    """A refused hosted send is a quarantine row with attempts=0 (nothing was sent) so it shows up
    where every other unprocessed item does, with the reason, never silently skipped."""
    prompt_text = (
        "\n\n".join(f"[{m.get('role')}]\n{m.get('content')}" for m in messages)
        if messages
        else None
    )
    session.add(
        Quarantine(
            opportunity_id=opportunity_id,
            tier=tier,
            model=model,
            prompt=(prompt_text or "")[:20000] or None,
            raw_response=None,
            error=(error_prefix + reason)[:4000],
            attempts=0,
        )
    )


# --------------------------------------------------------------- per-task model override
# Score again with a different model, this time only (D-UI-50). The task payload carries
# `model_override` = {backend, model, json_mode[, context_length]}; the worker builds that task's
# settings from a deep copy with the tier's model replaced, the same one-off mechanism the
# Documents Assess popup uses (kestrel/web/pages/documents.py `_assess_settings`). The worker's
# own settings, and every setting row, are never touched.
LOCAL_DERIVED_PREFIX = "kestrel-"


class ModelOverrideError(ValueError):
    """The override cannot run as given (unknown backend, Hosted mode off, a raw Ollama tag, a
    local model whose context window is below the tier's budget)."""


def derived_num_ctx(tag: str) -> int | None:
    """The context window baked into a Kestrel derived local model (`kestrel-<tier>`, SPEC.md
    8.2), from the active hardware profile in models.yml (the catalog floor when none is
    saved). None for a name that is not one of the derived tiers: its window is unknown."""
    from kestrel.llm import profiles as prof

    base = str(tag or "").split(":", 1)[0]
    tier = next((t for t in prof.TIERS if prof.derived_name(t) == base), None)
    if tier is None:
        return None
    try:
        catalog = prof.load_catalog()
        active = prof.load_active_profile()
        if active is not None and active.profile in catalog.profiles:
            return catalog.profile(active.profile).num_ctx.for_tier(tier)
        return catalog.num_ctx.for_tier(tier)
    except (KeyError, ValueError, OSError, AttributeError):
        return None


def settings_with_override(settings: Settings, tier: str, override: dict | None) -> Settings:
    """`settings` with `tier` pointed at the override's backend and model, as a copy. Returns
    `settings` itself when there is no override. A hosted override runs only while Hosted mode is
    on (its disclosure already covers the tier); a local one only on a Kestrel derived model,
    which carries its own num_ctx (a raw tag would truncate silently)."""
    if not override:
        return settings
    from kestrel.config import HOSTED_MAX_TOKENS, HostedModelCaps

    backend_name = str(override.get("backend") or "")
    model = str(override.get("model") or "")
    backend = settings.llm.backends.get(backend_name)
    tcfg = settings.llm.tiers.get(tier)
    if backend is None or not model or tcfg is None:
        raise ModelOverrideError(f"no backend {backend_name!r} or tier {tier!r} for {model!r}")
    if backend.external and not settings.llm.hosted_mode.active:
        raise ModelOverrideError(f"{model} is a hosted model and Hosted mode is off")
    if not backend.external and not model.startswith(LOCAL_DERIVED_PREFIX):
        raise ModelOverrideError(f"{model} is not one of Kestrel's local models")
    if not backend.external:
        # The pick's own window must hold this tier's prompt (CLAUDE.md traps: a short num_ctx
        # truncates silently). Unknown (not a derived tier name) is refused the same way.
        from kestrel.services.hosted_models import context_budget

        ctx, budget = derived_num_ctx(model), context_budget(tier)
        if ctx is None or ctx < budget:
            raise ModelOverrideError(
                f"{model}'s context window ({ctx or 'unknown'}) is below {tier}'s {budget}"
            )
    run = settings.model_copy(deep=True)
    update: dict[str, Any] = {"backend": backend_name, "model": model}
    if backend.external:
        update["max_tokens"] = max(tcfg.max_tokens or 0, HOSTED_MAX_TOKENS.get(tier, 0)) or None
        caps = run.llm.hosted_model_caps.get(model) or HostedModelCaps()
        json_mode = override.get("json_mode")
        if json_mode in ("schema", "json_object", "prompt", "unknown"):
            caps = caps.model_copy(update={"json_mode": json_mode})
        ctx = override.get("context_length")
        if isinstance(ctx, int) and ctx > 0:
            caps = caps.model_copy(update={"context_length": ctx})
        run.llm.hosted_model_caps[model] = caps
    run.llm.tiers[tier] = tcfg.model_copy(update=update)
    return run


def client_for(client: Any, settings: Settings) -> Any:
    """A shallow copy of `client` that resolves tiers from `settings`; the shared client (and its
    HTTP connection pool) is reused, its own settings untouched."""
    if getattr(client, "settings", None) is settings:
        return client
    import copy

    clone = copy.copy(client)
    clone.settings = settings
    return clone
