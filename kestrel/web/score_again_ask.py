"""The in-place send confirmation for the profile notice's Score again (issue #71). The notice
queues the roles `profile_readiness` names. When Tier 1 or Tier 2 would reach an online model,
the notice shows the same ask-first panel the other online actions use (partials/online_ask.html,
the Hosted mode disclosure for what each online tier sends) and nothing is queued until
it is confirmed."""

from __future__ import annotations

import json
from typing import Any

from kestrel.config import Settings
from kestrel.queue.rescore import enqueue_rescore
from kestrel.services import online_consent as oc
from kestrel.services.profile_readiness import Readiness, load_readiness

TIERS = ("tier1", "tier2")


CORRECTIONS_CHANGED = (
    "Nothing was sent. Your corrections changed since this panel opened. Check the lines below, "
    "then press Score again."
)


def queued_text(n: int) -> str:
    return f"Queued {n} role{'' if n == 1 else 's'} to score again."


async def ask_context(session, settings: Settings, ready: Readiness, *, problem: str | None = None,
                      ) -> dict[str, Any] | None:
    """The panel's facts, or None when scoring stays on this computer."""
    approval = oc.approval_for(settings, TIERS)
    if not approval:
        return None
    tier = "tier2" if "tier2" in approval else "tier1"
    n = len(ready.role_ids)
    route = await oc.online_route(
        session, settings, tier, this_send=f"{n} role{'' if n == 1 else 's'} scored again")
    steps = oc.step_disclosures(settings, TIERS)
    if len(steps) > 1:
        route = {**route, "steps": steps,
                 "provider": " and ".join(dict.fromkeys(st["provider"] for st in steps)),
                 "many_providers": len({st["provider"] for st in steps}) > 1}
    from kestrel.learning.correction_lines import dialog_digest, lines_for

    return {
        "route": route, "problem": problem,
        "approval_json": json.dumps(approval, separators=(",", ":")),
        # The lines these roles' scores would send, and the digest the form posts back, so a
        # change while the panel is open is refused (issue #178, D-PRIV-670).
        "correction_lines": await lines_for(session, ready.role_ids),
        "corrections_digest": await dialog_digest(session, ready.role_ids),
    }


async def run(session, settings: Settings, track: str, form) -> dict[str, Any]:
    """Press of Score again. {"queued": n} once queued, else {"ask": ctx} (nothing was sent or
    queued). The role list is read here, never taken from the form."""
    ready = await load_readiness(session, track)
    if not ready.role_ids:
        return {"queued": 0}
    approval = oc.approval_for(settings, TIERS)
    stamp = None
    confirmed = None
    if approval:
        # The ask form always posts `online_approval`; a press there without the tick is a
        # mistake to name. The first press (the notice's own button) has no such field.
        asked = "online_approval" in form or str(form.get("confirmed") or "") == "1"
        problem = await oc.approval_problem(session, settings, TIERS, form)
        if problem:
            # A first press has no confirmation yet: ask without scolding.
            shown = problem if asked else None
            return {"ask": await ask_context(session, settings, ready, problem=shown)}
        from kestrel.learning.correction_lines import confirm_corrections

        confirmed = await confirm_corrections(
            session, ready.role_ids, form.get("corrections_digest"))
        if confirmed is None:
            # The lines the panel listed are not the lines a score would send now: send nothing
            # and show the panel again with the current lines (D-PRIV-670, D-PRIV-674).
            return {"ask": await ask_context(session, settings, ready,
                                             problem=CORRECTIONS_CHANGED)}
        stamp = approval
    result = await enqueue_rescore(session, ready.role_ids, online_approval=stamp,
                                   corrections=confirmed)
    await session.commit()
    return {"queued": len(result.queued) + len(result.already)}
