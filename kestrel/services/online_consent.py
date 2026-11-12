"""Ask-first consent for work that would go to an online model (hard rule: asks before anything
goes to an online model). One place for the facts every surface needs:

- `tier_goes_online`: would a call on this tier leave the computer with the settings in effect now.
- `online_route`: what the ask-first panel shows (provider, model, what is sent from the Hosted
  mode disclosure, and what happens to a confidential current employer).
- `consent_problem`: the server-side re-check of a posted confirmation. A page is never trusted.
- `payload_consent` / `held_for_consent`: a queued task carries the model and destination the
  operator confirmed;
  a drain that finds the tier online without a matching confirmation holds the task instead of
  sending (an automatic task never sends without a click)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import netloc
from kestrel.config import Settings

CONSENT_KEY = "consented_model"
CONSENT_DEST_KEY = "consented_destination"
APPROVAL_KEY = "online_approval"
# The research and summary tasks name theirs differently; same meaning.
CONFIRMED_ONLINE_KEY = "confirmed_online"
CONFIRMED_MODEL_KEY = "confirmed_model"
CONFIRMED_DEST_KEY = "confirmed_dest"
# The digest of the correction lines a Score again confirmation showed for the role
# (kestrel.learning.correction_lines, D-SCORE-3): Tier 2 holds the task when they changed.
CORRECTIONS_KEY = "consented_corrections"
# EVERY task payload key that records an online confirmation, in one place (D-SEC-10). A task
# writer records a confirmation only under one of these, and a restore removes all of them: a
# confirmation is a click on THIS computer, so a restored task must be held for a fresh one.
CONSENT_PAYLOAD_KEYS = (
    CONSENT_KEY, CONSENT_DEST_KEY, APPROVAL_KEY,
    CONFIRMED_ONLINE_KEY, CONFIRMED_MODEL_KEY, CONFIRMED_DEST_KEY, CORRECTIONS_KEY,
)
# Every task type, sorted by whether it can send anything to an online model. A new type must be
# added to one of the two (tests/test_online_task_types.py fails until it is), and a type in the
# first needs a test that a restored one is held.
ONLINE_TASK_TYPES = frozenset({
    "t1", "t2", "letter_draft", "build_stories", "interview_questions", "title_suggestions",
    "org_summary", "org_research", "org_contacts", "follow_up", "bench",
})
LOCAL_TASK_TYPES = frozenset({
    "embed", "scrape", "t0_recheck", "suggestion_check", "train_reranker", "ollama_pull",
    "ollama_create",
})
SCORING_TIERS = ("tier1", "tier2")
RUN_TIERS = ("tier1", "tier2", "trigger")
APPROVAL_HOLD = "held: online destination changed or was not confirmed"
CHANGED = (
    "Nothing was sent. The model or online service changed since the dialog opened. Press the "
    "button again to see what would be sent."
)
NOT_CONFIRMED = (
    "Nothing was sent. Confirm the online send first: tick the box that says what will be sent."
)
HELD = (
    "Nothing was sent. Confidential mode is on and no current employer is chosen, so Kestrel "
    "will not send this. Choose your employer in Settings."
)


def tier_goes_online(settings: Settings, tier: str) -> bool:
    """True when a call on `tier` could reach an external backend: Hosted mode active for it, or
    the tier simply pointed at an external backend. Deliberately the wider reading, so an
    automatic task holds rather than risks a send."""
    from kestrel.pipeline._shared import tier_is_hosted

    if tier_is_hosted(settings, tier):
        return True
    llm = settings.llm
    if llm.mode == "hosted" and tier in llm.hosted_tiers:
        return True
    tcfg = llm.tiers.get(tier)
    backend = llm.backends.get(tcfg.backend) if tcfg is not None else None
    return backend_is_off_machine(backend)


def backend_is_off_machine(backend: Any) -> bool:
    """True when a call to BACKEND could leave the person's own network (D-SEC-10, D-LLM-15): the
    `external` flag is set, or its base URL ADDRESS is on the internet. This computer and a
    server on the person's local network (home or office) are not off-machine: no consent dialog
    and no Hosted mode, for any server type. No name is looked up (no DNS, D-LLM-15 amendment):
    only a literal loopback or private address is local, and every host name counts as the
    internet. The in-process engine never leaves."""
    if backend is None:
        return False
    return bool(backend.external)


def configured_model(settings: Settings, tier: str) -> str:
    tcfg = settings.llm.tiers.get(tier)
    return tcfg.model if tcfg is not None else ""


def destination_id(settings: Settings, tier: str) -> str:
    """Where a `tier` call goes: backend id, base URL and model, as one stable string. This is
    what a confirmation is bound to (a dialog that named provider A must not approve provider B
    that happens to use the same model name)."""
    tcfg = settings.llm.tiers.get(tier)
    if tcfg is None:
        return ""
    backend = settings.llm.backends.get(tcfg.backend)
    url = str(getattr(backend, "base_url", "") or "")
    return f"{tcfg.backend}|{url}|{tcfg.model}"


def approval_for(settings: Settings, tiers: Iterable[str]) -> dict[str, list[str]]:
    """Destinations for the resolved online tiers used by one action or candidate."""
    return {
        tier: [destination_id(settings, tier)]
        for tier in tiers
        if tier_goes_online(settings, tier) and tier in settings.llm.tiers
    }


def merge_approvals(*approvals: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """Combine the destinations disclosed for several candidates of the same tier."""
    merged: dict[str, list[str]] = {}
    for approval in approvals:
        for tier, destinations in approval.items():
            merged[tier] = sorted(set(merged.get(tier, [])) | set(destinations))
    return merged


def read_approval(value: object) -> dict[str, list[str]]:
    """Read a posted JSON approval or the same value persisted on a task."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    if not isinstance(value, dict):
        return {}
    if any(
        not isinstance(tier, str)
        or not isinstance(destinations, list)
        or any(not isinstance(destination, str) for destination in destinations)
        for tier, destinations in value.items()
    ):
        return {}
    return {tier: sorted(set(destinations)) for tier, destinations in value.items()}


def approval_covers(expected: Mapping[str, list[str]], value: object) -> bool:
    """Every destination actually used must have appeared in the posted approval."""
    approved = read_approval(value)
    return all(set(destinations) <= set(approved.get(tier, []))
               for tier, destinations in expected.items())


async def approval_problem(
    session: AsyncSession, settings: Settings, tiers: Iterable[str], form: Mapping[str, Any],
    *, expected: Mapping[str, list[str]] | None = None,
) -> str | None:
    """Validate the disclosed destinations against resolved settings, including defaults."""
    approval = approval_for(settings, tiers) if expected is None else expected
    if not approval:
        return None
    if str(form.get("confirmed") or "") != "1":
        return NOT_CONFIRMED
    if not approval_covers(approval, form.get(APPROVAL_KEY)):
        return CHANGED
    if not settings.llm.hosted_mode.active:
        return "Nothing was sent. Online models need Hosted mode, which is off."
    if await employer_state(session, settings) == "held":
        return HELD
    return None


def held_for_approval(
    settings: Settings, tiers: Iterable[str], payload: Mapping[str, Any] | None,
) -> bool:
    """Hold queued work an operator confirmed for named destinations when its actual client's
    destinations are no longer among them, or Hosted mode is off. Unstamped work is the
    scheduled pipeline: the accepted Hosted mode disclosure is its standing consent (SPEC 6.4)
    and the client enforces it per call, so it is not held here, with one exception: a task
    that names its own model (`model_override`) on an online destination is never the scheduled
    pipeline, so without a stamp it is held, whoever queued it (D-SEC-22)."""
    stamped = (payload or {}).get(APPROVAL_KEY)
    if stamped is None:
        return _unstamped_override_online(settings, tiers, payload)
    expected = approval_for(settings, tiers)
    return bool(expected) and (
        not settings.llm.hosted_mode.active or not approval_covers(expected, stamped)
    )


def _unstamped_override_online(
    settings: Settings, tiers: Iterable[str], payload: Mapping[str, Any] | None,
) -> bool:
    """True when the payload's `model_override` would send a `tiers` call online. An override
    that cannot run falls back to the configured model (the worker's rule), which the standing
    disclosure covers, so it is not counted."""
    override = (payload or {}).get("model_override")
    if not isinstance(override, dict) or not override.get("model"):
        return False
    from kestrel.pipeline._shared import ModelOverrideError, settings_with_override

    for tier in tiers:
        if tier not in settings.llm.tiers:
            continue
        try:
            effective = settings_with_override(settings, tier, override)
        except ModelOverrideError:
            continue
        if tier_goes_online(effective, tier):
            return True
    return False


def confirmation_matches(
    route: Mapping[str, Any], form: Mapping[str, Any], *, model_field: str = "model"
) -> bool:
    """True when the posted model AND destination equal the ones configured now (`route` carries
    "model" and "dest"). A missing destination never matches."""
    return (
        str(form.get(model_field) or "") == route["model"]
        and bool(route.get("dest"))
        and str(form.get("dest") or "") == route["dest"]
    )


async def employer_state(session: AsyncSession, settings: Settings) -> str:
    """ "hidden" (confidential mode, an employer chosen), "held" (confidential mode, none chosen:
    the send is refused) or "off"."""
    if not settings.confidential_mode or not settings.llm.hosted_mode.redact_confidential:
        return "off"
    from kestrel.docs.current_employer import load_employer_map

    return "hidden" if await load_employer_map(session) else "held"


def _provider_label(settings: Settings, tier: str) -> str:
    from kestrel.llm import providers as prov

    tcfg = settings.llm.tiers.get(tier)
    name = tcfg.backend if tcfg is not None else None
    if name == "other":
        from kestrel.llm.service_name import service_name

        backend = settings.llm.backends.get(name)
        return service_name(backend.base_url if backend else None)
    label = next(
        (p["label"] for p in prov.presets(settings).values() if p["backend"] == name), None)
    return label or name or "the online service"


async def online_route(
    session: AsyncSession, settings: Settings, tier: str, *, this_send: str = "",
    reask: bool = False, sends: str = "",
) -> dict[str, Any]:
    """Where a `tier` call goes. {"online": False} when it stays on this computer. Otherwise the
    provider, the configured model, `sends` (the tier's line of the Hosted mode disclosure, the
    text the operator acknowledged), `this_send` (what this action adds) and the employer state.
    The server re-checks the same facts when the action is asked for. `reask=True` marks an
    action whose reply goes through the plain-writing check, which can send one more request to
    the same model; the panel then says so (disclosure only, it changes nothing that is sent).
    `sends` replaces the tier's general line with exactly what this action sends, for an action
    that sends less than the tier's whole line (a story build, D-STORY-3); it must never name
    less than the action actually sends."""
    if not tier_goes_online(settings, tier):
        from kestrel.services import lan_servers

        loc, host = lan_servers.tier_where(settings, tier)
        # `where` names a model on another computer on the person's network (D-LLM-15); empty
        # when it is on this computer.
        if loc == netloc.LOCAL_NETWORK:
            return {"online": False, "where": netloc.label_for(loc, host)}
        return {"online": False}
    sends = sends or settings.llm.hosted_mode.disclosure_terms()["sends"][tier]
    return {
        "online": True,
        "hosted_active": settings.llm.hosted_mode.active,
        "tier": tier,
        "model": configured_model(settings, tier),
        "dest": destination_id(settings, tier),
        "provider": _provider_label(settings, tier),
        "sends": sends[:1].upper() + sends[1:],
        "this_send": this_send,
        "reask": reask,
        "employer": await employer_state(session, settings),
    }


STEP_LABELS = {"tier1": "Quick screen", "tier2": "Full review"}


def step_disclosures(settings: Settings, tiers: Iterable[str]) -> list[dict[str, str]]:
    """One entry per online tier in `tiers`, in order: the step's name, provider, configured model
    and the Hosted mode line it sends (the text the operator acknowledged). Every ask-first panel
    for an action that reaches several tiers lists these, so the disclosure covers exactly the
    tiers `approval_for` asks the operator to approve."""
    terms = settings.llm.hosted_mode.disclosure_terms()["sends"]
    out = []
    for tier in approval_for(settings, tiers):
        sends = terms.get(tier, "")
        out.append({
            "tier": tier, "label": STEP_LABELS.get(tier, tier),
            "provider": _provider_label(settings, tier), "model": configured_model(settings, tier),
            "sends": sends[:1].upper() + sends[1:],
        })
    return out


def consent_problem(route: Mapping[str, Any], form: Mapping[str, Any]) -> str | None:
    """None when the action may go ahead; else the plain reason nothing was sent. A local route
    always passes. An online one needs confirmed=1, the posted model equal to the configured one,
    and no confidential hold."""
    if not route.get("online"):
        return None
    if str(form.get("confirmed") or "") != "1":
        return NOT_CONFIRMED
    if not confirmation_matches(route, form):
        return CHANGED
    if not route.get("hosted_active"):
        return "Nothing was sent. Online models need Hosted mode, which is off."
    if route.get("employer") == "held":
        return HELD
    return None


def consent_payload(route: Mapping[str, Any]) -> dict[str, Any]:
    """The task payload fragment recording what the operator confirmed (empty for a local route)."""
    if not route.get("online"):
        return {}
    return {CONSENT_KEY: route["model"], CONSENT_DEST_KEY: route.get("dest", "")}


def held_for_consent(settings: Settings, tier: str, payload: Mapping[str, Any] | None) -> bool:
    """Run-time check for a drain: True when `tier` is online now and the task does not carry a
    confirmation for the model configured now. Such a task must not send."""
    if not tier_goes_online(settings, tier):
        return False
    got = payload or {}
    return got.get(CONSENT_KEY) != configured_model(settings, tier) or (
        got.get(CONSENT_DEST_KEY) != destination_id(settings, tier)
    )


async def held_at_run_time(
    sm: Any, settings: Settings, tier: str, payload: Mapping[str, Any] | None
) -> bool:
    """`held_for_consent` against the settings in effect now (the worker's copy can be stale: the
    operator may have switched Tier 2 online after the task was queued) as well as the worker's."""
    from kestrel.services.setting_overrides import load_settings_with_db

    async with sm() as s:
        fresh = await load_settings_with_db(s)
    return held_for_consent(fresh, tier, payload) or (
        isinstance(settings, Settings) and held_for_consent(settings, tier, payload)
    )


RUN_UNCONFIRMED = "Not started. Nothing was sent: confirm in the sheet first."
RUN_HELD = (
    "Not started. Nothing was sent: confidential mode is on and no current employer is chosen. "
    "Choose it in Settings first."
)


async def online_run_refusal(
    session: AsyncSession, settings: Settings, *, mode: str, confirmed: object,
    approval: object = None,
) -> tuple[str, int] | None:
    """The checks every route that starts a run on an online model makes (the Runs page sheet,
    POST /api/run/now, POST /runs/now): `confirmed=1`, and no confidential hold. (message,
    status) when the run must not start, else None."""
    if mode not in ("hosted", "both"):
        return None
    if str(confirmed or "") != "1":
        return RUN_UNCONFIRMED, 400
    expected = approval_for(settings, RUN_TIERS)
    if expected and not approval_covers(expected, approval):
        return CHANGED, 400
    if await employer_state(session, settings) == "held":
        return RUN_HELD, 409
    return None
