"""Connect a hosted provider in one step (D-UI-7), shared by Settings > Hosted and the onboarding
model step (D-UI-30).

With the disclosure acknowledged: verify the key (a pasted key is checked before it replaces the
stored one), list the models that can honor a JSON schema, pick one per tier the way
`kestrel setup --hosted` does, and write the same setting rows (acknowledgement, hosted
assignments, frontier, and the mode switch unless `switch` is off). A blank key uses the stored
one. Nothing is written until the provider accepts the key; past that only the key is kept if no
model fits. The only network calls are the provider's key check and model list, which carry
nothing about the operator or any opportunity (hard rule 1 holds: no action on their behalf)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting
from kestrel.llm import providers as prov
from kestrel.llm.service_name import service_name

log = structlog.get_logger()


DEFAULTS_NOTE = (
    "These are defaults: override any tier on the Models page, and connecting again keeps "
    "your overrides."
)


# Providers with free models, listed first with "Free models available" beside them in the
# onboarding model step and Settings > Hosted (release round R1).
FREE_PROVIDERS: tuple[str, ...] = ("openrouter",)


def provider_order() -> list[str]:
    """Every preset id, the free-model providers first, otherwise in PRESETS order."""
    ids = list(prov.PRESETS)
    return [p for p in ids if p in FREE_PROVIDERS] + [p for p in ids if p not in FREE_PROVIDERS]


@dataclass
class ConnectOutcome:
    """One Connect's result (D-UI-40): `headline` is the one sentence the operator must read
    (connected, and which model Tier 1 and Tier 2 use; or not connected), `detail` the rest (the
    reason on a failure; prices, scope and next step on a success)."""

    ok: bool
    headline: str
    detail: str = ""
    key: Any = None  # the provider's key check, when it rejected the key
    # True when the models named are Connect's automatic defaults: the result box then links to
    # the Models page, where any tier can be overridden (D-UI-42).
    defaults: bool = False

    @property
    def message(self) -> str:
        return f"{self.headline} {self.detail}".strip()


# Plain names for tier and backend keys, as the Models and Bench pages use them
# (kestrel.web.deps.tier_label, kestrel.web.words.PLAIN). Lower case: these sit inside sentences.
_TIER_PLAIN = {
    "tier1": "quick screen",
    "tier2": "full review",
    "tier3": "deeper read",
    "frontier": "deeper reads",
    "docsel": "document selection",
    "trigger": "trigger classification",
}


def tier_label(tier: str) -> str:
    """A tier as the user knows it: "full review (Tier 2)", with the setting name beside the plain
    one, or "deeper reads" for the hosted escalation backend. Never a bare internal key such as
    "frontier" or "tier2"."""
    plain = _TIER_PLAIN.get(tier)
    if plain and tier.startswith("tier") and tier[4:].isdigit():
        return f"{plain} (Tier {tier[4:]})"
    return plain or tier


def tiers_sentence(tier1: str | None, tier2: str | None) -> str:
    """"Tier 1 and Tier 2 will use X." or one clause per tier when the picks differ."""
    if tier1 and tier1 == tier2:
        return f"Tier 1 and Tier 2 will use {tier1}."
    parts = [f"Tier 1 will use {tier1}" if tier1 else "", f"Tier 2 will use {tier2}" if tier2
             else ""]
    joined = " and ".join(p for p in parts if p)
    return f"{joined}." if joined else ""


def _refused(provider: str, reason: str, why: str, *, label: str | None = None,
             key: Any = None) -> ConnectOutcome:
    """A refusal: logged with a short code (never the key), headline says not connected."""
    log.info("hosted_connect_result", provider=provider, ok=False, reason=reason)
    head = f"Not connected to {label}." if label else "Not connected."
    return ConnectOutcome(False, head, why, key=key)


async def _set(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def connect_provider(
    session: AsyncSession, settings: Settings, *, provider: str, api_key: str,
    acknowledged: bool, free_only: bool, switch: bool, profile: str | None,
    allow_training: bool = False,
) -> ConnectOutcome:
    from kestrel.private_logging import private_values, redact

    # Protect a pasted replacement key during verification as well as saved credentials.
    with private_values(api_key if provider == "other" else ""):
        out = await _connect_provider(
            session, settings, provider=provider, api_key=api_key, acknowledged=acknowledged,
            free_only=free_only, switch=switch, profile=profile, allow_training=allow_training,
        )
        if provider == "other":
            out.headline, out.detail = redact(out.headline), redact(out.detail)
            if out.key is not None:
                out.key = replace(out.key, detail=redact(out.key.detail))
        return out


async def _connect_provider(
    session: AsyncSession,
    settings: Settings,
    *,
    provider: str,
    api_key: str,
    acknowledged: bool,
    free_only: bool,
    switch: bool,
    profile: str | None,
    allow_training: bool = False,
) -> ConnectOutcome:
    """Run the connect. Commits the session on success; writes nothing on a refusal."""
    from kestrel import setup as setup_mod
    from kestrel.services.setting_overrides import load_settings_with_db

    value = api_key.strip()
    preset = prov.presets(settings).get(provider)
    backend = prov.external_backends(settings).get(preset["backend"]) if preset else None
    if preset is None or backend is None or not backend.api_key_env:
        return _refused(provider, "no_provider", "Choose a provider.")
    label, env = preset["label"], backend.api_key_env
    if not value and not prov.secret_present(env):
        return _refused(provider, "no_key", f"Paste your {label} API key.", label=label)
    if not acknowledged:
        return _refused(
            provider, "not_acknowledged",
            'Tick "I understand" under the disclosure to connect; nothing was saved.',
            label=label,
        )
    # Free OpenRouter models are almost all served by endpoints that train on prompts, and
    # Kestrel sends `data_collection: deny` on every request (D-BYOK privacy default), which
    # overrides the account's own setting. Free-only without allowing that connects and then
    # fails every call with "No endpoints found matching your data policy" (D-UI-32).
    if free_only and backend.provider == "openrouter" and not allow_training:
        return _refused(
            provider, "free_needs_training_opt_in",
            "Free models on OpenRouter are offered by providers that may store or train on "
            "what Kestrel sends, and Kestrel blocks those by default. To use free models, also "
            'tick "Allow providers that may train on what Kestrel sends". Otherwise clear '
            '"Free models only" to use paid models. Nothing was saved.',
            label=label,
        )
    candidate = backend.model_copy(update={"api_key": value}) if value else backend
    key = await prov.verify_key(candidate)
    if not key.ok:
        kept = " Your saved key was not changed." if value else ""
        return _refused(provider, "key_rejected", f"{label} rejected the key: {key.detail}.{kept}",
                        label=label, key=key)
    try:
        fit = prov.models_fit_for_tier3(await prov.list_models(candidate), candidate)
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError,
            AttributeError) as exc:
        return _refused(
            provider, "model_list_failed",
            f"{label} accepted the key, but the model list failed: "
            f"{type(exc).__name__}: {exc}. Nothing was saved.",
            label=label,
        )
    if value:
        try:
            prov.write_secret(env, value)
        except ValueError as exc:
            return _refused(provider, "key_not_saved", str(exc), label=label)
    saved = "Key saved. " if value else ""
    pool = prov.free_models(fit) if free_only else fit
    picks = setup_mod.default_picks(pool, backend.provider)
    if len(picks) < len(setup_mod.PICK_TIERS):
        why = (
            "no free model here can honor a JSON schema (only OpenRouter reports prices, "
            "and data collection deny can exclude every free model)"
            if free_only
            else "the provider lists no model that can honor a JSON schema"
        )
        return _refused(provider, "no_fitting_model", f"{saved}No model fits: {why}.",
                        label=label)
    try:
        rows = setup_mod.hosted_rows(
            preset["backend"], backend, picks, utcnow_iso(), free_only=free_only, profile=profile,
            terms=settings.llm.hosted_mode.disclosure_terms(),
        )
    except ValueError as exc:
        return _refused(provider, "rows_refused", f"{saved}{exc}.", label=label)
    if not switch:
        rows.pop("llm.mode")
    if backend.provider == "openrouter":
        # The privacy choice follows the model choice: free needs allow; paid goes back to the
        # private default unless the operator set allow on Settings > Hosted themselves.
        hosted = f"llm.backends.{preset['backend']}.hosted.data_collection"
        if free_only:
            rows[hosted] = "allow"
        elif not allow_training and backend.hosted.data_collection == "allow" and \
                settings.llm.hosted_mode.free_only:
            rows[hosted] = "deny"
    # Record the automatic picks as the defaults and put every standing Models page override
    # back into the rows, so connecting again never clobbers one (D-UI-42).
    # Every kept override is re-checked against the backend as this Connect leaves it (its
    # data_collection may have just changed), so a now-blocked one falls back to the default.
    from kestrel.services import hosted_models

    hosted_key = f"llm.backends.{preset['backend']}.hosted.data_collection"
    after = candidate.model_copy(update={"hosted": candidate.hosted.model_copy(
        update={"data_collection": rows.get(hosted_key, candidate.hosted.data_collection)}
    )})
    # One lock with Save all and Reset to default: the state is read, merged and committed
    # without another writer in between.
    async with hosted_models.STATE_LOCK:
        ov = await hosted_models.apply_connect(session, rows, preset["backend"], fit, after)
        for k, v in rows.items():
            await _set(session, k, v)
        await session.commit()
    hosted_models.prime(preset["backend"], candidate, fit)
    settings = await load_settings_with_db(session)
    free_ids = {m.id for m in prov.free_models(fit)}
    chosen = "; ".join(
        setup_mod.pick_reason(t, picks[t], free_ids) for t in setup_mod.PICK_TIERS
    )
    tail = (
        "Kestrel now runs on hosted models; no restart needed."
        if settings.llm.mode == "hosted"
        else "Local models stay active; use the switch above or Runs to go hosted."
    )
    paid = ov.paid
    if free_only and paid:
        # Mostly free with a paid override is allowed; it is never presented as all free.
        scope = (
            "Defaults picked from free models only, but your paid override for "
            f"{', '.join(tier_label(t) for t in paid)} is billed by "
            f"{service_name(backend.base_url)}."
        )
    elif free_only:
        scope = "Picked from free models only; to use paid models, connect again with the box " \
            "cleared."
    else:
        scope = (
            "Picked from every model that honors a JSON schema; tick Free models only and "
            "connect again to stay at zero cost."
        )
    tier1 = rows["llm.hosted_tiers.tier1.model"]
    tier2 = rows["llm.hosted_tiers.tier2.model"]
    if "tier1" in paid:
        tier1 = f"{tier1} (paid override)"
    if "tier2" in paid:
        tier2 = f"{tier2} (paid override)"
    overrides = "".join(
        f" Your override for {tier_label(t)} stays: {m}{' (paid)' if p else ''}."
        for t, m, p in ov.kept
    )
    if ov.dropped:
        overrides += (
            " These overrides cannot run and were cleared, so the tier uses its default: "
            f"{'; '.join(ov.dropped)}."
        )
    log.info(
        "hosted_connect_result", provider=provider, ok=True, backend=preset["backend"],
        tier1=tier1, tier2=tier2, free_only=free_only, mode=settings.llm.mode,
        key_saved=bool(value), overrides_kept=len(ov.kept), overrides_dropped=len(ov.dropped),
        overrides_paid=len(paid),
    )
    return ConnectOutcome(
        True,
        f"Connected to {label}. {tiers_sentence(tier1, tier2)}",
        f"{saved}Default models: {chosen}. Tier 3 work and escalation use the tier2 model."
        f"{overrides} {scope} {tail} {DEFAULTS_NOTE}",
        defaults=True,
    )
