"""Score again (D-UI-50): any role, or any Pipeline selection, scored again with the same model
or a different one for this run only. Settings never change.

  GET  /rescore/choose?opp_ids=1,2   the themed dialog (partials/score_again_dialog.html)
  POST /rescore                      queue it: `opp_ids` (comma list, at most 200) and `model`
                                     ("same", a local `kestrel-` model, or "hosted:<id>")
  GET  /rescore/history/{opp_id}     every Tier 2+ evaluation, newest first, with its model

The one-off model is the Documents Assess popup's mechanism (kestrel/web/pages/documents.py):
Kestrel's derived local models from Ollama's own list, and the hosted provider's list only while
Hosted mode is on and acknowledged, so the disclosure already covers what a Tier 2 call sends.
Here the list is narrowed to models that return structured output or JSON mode, and to those
whose context meets the Tier 2 budget. The pick rides on each task row as `model_override`
(kestrel.queue.rescore); the worker applies it to that task alone.

A role a Tier 0 rule set aside (D-UI-73, D-UI-74): the dialog says which rule and links to where
it is changed, and offers "Score this role anyway" (`tier0_anyway`), which skips Tier 0 for that
role only, runs Tier 1 then Tier 2 on the picked model, and records the choice. Without it the
submit is refused with the same sentence and link."""

from __future__ import annotations

import asyncio
import html
from typing import Any
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from kestrel.analytics.progress import model_label, tier2_label, tier2_pace_seconds
from kestrel.db.models import Evaluation, Opportunity, Org, Quarantine
from kestrel.pipeline._shared import derived_num_ctx
from kestrel.pipeline.profile import load_profile
from kestrel.queue.rescore import MAX_ROLES, enqueue_rescore, parse_ids
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.fit import RuleWhy, explain_rule, fit_for

router = APIRouter()
log = structlog.get_logger()

HOSTED_PREFIX = "hosted:"
_TIER = "tier2"
_JSON_MODES = ("schema", "json_object")  # structured, or JSON mode (D-UI-48)


def _local_models(settings) -> tuple[list[str], str | None]:
    """Kestrel's derived local models Ollama has now (the Assess popup's own lister), narrowed
    to those whose context window meets the Tier 2 budget: a one-off pick always runs the full
    review, and a smaller window truncates the description silently (CLAUDE.md traps). So
    kestrel-tier1 and kestrel-docsel are never offered; kestrel-tier2 and kestrel-tier3 are.
    Shared by Score again, Run now and Scan now. Sync; run it in a thread."""
    from kestrel.services.hosted_models import context_budget
    from kestrel.web.pages.documents import _local_assess_models

    found, note = _local_assess_models(settings)
    budget = context_budget(_TIER)
    ok = [m for m in found if (derived_num_ctx(m) or 0) >= budget]
    left = [m.split(":", 1)[0] for m in found if m not in ok]
    if left and not note:
        names = " and ".join(sorted(dict.fromkeys(left)))
        note = (f"Not offered: {names}, whose context window is below the full review's "
                f"{budget:,} tokens.")
    return ok, note


async def _hosted_models(settings) -> tuple[str, str | None, list, list, dict, str | None]:
    """(provider label, backend name, raw models, picker rows, block reasons, note) for Tier 2,
    narrowed to structured and JSON-mode models. Empty unless Hosted mode is on and a key is
    saved."""
    from kestrel.llm import providers as prov
    from kestrel.services import hosted_models as hm

    found = hm.catalog_backend(settings)
    if found is None:
        return "", None, [], [], {}, None
    name, backend = found
    label = next((p["label"] for p in prov.PRESETS.values() if p["backend"] == name), name)
    if not settings.llm.hosted_mode.active:
        return label, name, [], [], {}, (
            f"{label} models are offered here once Hosted mode is on under Settings > Hosted."
        )
    if not (backend.api_key or prov.secret_present(backend.api_key_env)):
        return label, name, [], [], {}, None
    try:
        raw = await hm.fetch_models(name, backend)
    except hm.ModelListError as exc:
        return label, name, [], [], {}, str(exc)
    raw = [m for m in raw if (m.json_mode or "schema") in _JSON_MODES]
    picker = hm.picker_models(raw)
    reasons = {m.id: why for m in picker if (why := hm.block_reason(m, _TIER, backend))}
    return label, name, raw, picker, reasons, None


async def _override_for(settings, pick: str) -> tuple[dict | None, str, str | None]:
    """The task payload's `model_override` for `pick`, its label, or an error to show."""
    if pick.startswith(HOSTED_PREFIX):
        model_id = pick.removeprefix(HOSTED_PREFIX)
        _label, name, raw, _picker, reasons, note = await _hosted_models(settings)
        info = next((m for m in raw if m.id == model_id), None)
        if info is None or name is None:
            return None, "", note or f"{model_id!r} is not in the hosted model list."
        if reasons.get(model_id):
            return None, "", f"{model_id} cannot score roles: {reasons[model_id]}."
        override = {
            "backend": name, "model": model_id, "json_mode": info.json_mode or "schema",
            "context_length": info.context_length,
        }
        return override, model_label(settings, name, model_id) or model_id, None
    local, note = await asyncio.to_thread(_local_models, settings)
    if pick not in local:
        from kestrel.services.hosted_models import context_budget

        ctx, budget = derived_num_ctx(pick), context_budget(_TIER)
        if ctx is not None and ctx < budget:
            return None, "", (f"{pick} cannot run the full review: its context window "
                              f"{ctx:,} is below {budget:,} tokens.")
        if note and not note.startswith("Not offered:"):
            return None, "", note  # Ollama is not reachable, or the like
        return None, "", f"{pick!r} is not one of the local Kestrel models Ollama has."
    return {"backend": "local", "model": pick, "json_mode": "schema"}, f"{pick} (local)", None


def estimate_minutes(n: int, pace_s: float | None) -> int | None:
    if pace_s is None or n <= 0:
        return None
    return max(1, round(n * pace_s / 60.0))


# What a no-script Score again submit reports back (D-UI-71): a code in the URL, fixed copy here.
# `ok` False renders in the warning box.
OUTCOMES: dict[str, tuple[bool, str]] = {
    "queued": (True, "Queued {roles} for scoring."),
    "already": (False, "Nothing queued: that role is already waiting to be scored."),
    "no_pick": (False, "Not queued: pick a model, or choose Same model."),
    "no_roles": (False, "Not queued: pick at least one role to score again."),
    "not_queued": (False, "Not queued. Open the role to see why, or try again."),
    "rules": (False, "Not queued: your rules set this role aside. Open it to see which rule and "
                     "where to change it, or choose Score this role anyway."),
}


def outcome_for(params) -> dict | None:
    """The fixed sentence for `?outcome=` (and `n`), or None for no or an unknown code."""
    found = OUTCOMES.get(str(params.get("outcome") or ""))
    if found is None:
        return None
    ok, text = found
    raw = str(params.get("n") or "0")
    n = min(int(raw), MAX_ROLES) if raw.isdigit() else 0
    roles = "1 role" if n == 1 else f"{n} roles"
    return {"ok": ok, "text": text.format(roles=roles)}


def safe_next(raw: str | None, default: str = "/quarantine") -> str:
    """A same-site path to return to after a no-script submit; anything else is the default."""
    text = (raw or "").strip()
    if not text.startswith("/") or text.startswith("//") or any(c in text for c in "\\\r\n"):
        return default
    return text[:300]


def preselection(
    settings, model: str | None, local: list[str], hosted: list, reasons: dict
) -> tuple[str | None, str | None]:
    """(the picker's selected id, a note) for opening the dialog on `model` (D-UI-71: Retry opens
    on the model that failed). None when `model` is empty or is the configured full-review model
    ("Same model" covers it); a note, and "Same model", when that model is not offered now."""
    want = (model or "").strip()
    tcfg = settings.llm.tiers.get(_TIER)
    if not want or (tcfg is not None and want == tcfg.model):
        return None, None
    for tag in local:
        if want == tag or f"{want}:latest" == tag:
            return tag, None
    for m in hosted:
        if m.id == want and not reasons.get(m.id):
            return m.id, None
    return None, (
        f"The model that failed ({want}) is not offered right now, so the setting's model is "
        "selected. Pick another below if you like."
    )


def rule_explainer(session, settings):
    """An async `explain(opp)` for enqueue_rescore and the dialog: the RuleWhy for a role Tier 0
    set aside, against the operator's current profile (loaded once per track)."""
    profiles: dict[str, Any] = {}

    async def explain(opp: Opportunity) -> RuleWhy:
        track = opp.track or "employment"
        if track not in profiles:
            profiles[track] = await asyncio.to_thread(load_profile, track)
        org = await session.get(Org, opp.org_id) if opp.org_id else None
        return explain_rule(opp.stage_reason, opp=opp, profile=profiles[track],
                            settings=settings, org=org)

    return explain


async def set_aside(session, settings, ids: list[int]) -> list[dict]:
    """The selected roles a Tier 0 rule set aside, each with its RuleWhy, for the dialog."""
    if not ids:
        return []
    rows = (
        await session.execute(
            select(Opportunity).where(Opportunity.id.in_(ids), Opportunity.stage == "t0_reject")
        )
    ).scalars().all()
    explain = rule_explainer(session, settings)
    return [{"id": o.id, "why": await explain(o)} for o in rows]


@router.get("/rescore/choose", response_class=HTMLResponse)
async def choose(request: Request):
    # A comma list on the button, plus any checked rows a Pipeline selection includes.
    qp = request.query_params
    ids = parse_ids(qp.getlist("opp_ids"))
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        pace = await tier2_pace_seconds(s)
        aside = await set_aside(s, settings, ids)
    local, local_note = await asyncio.to_thread(_local_models, settings)
    label, _name, _raw, hosted, reasons, hosted_note = await _hosted_models(settings)
    preselect_id, preselect_note = preselection(
        settings, qp.get("model"), local, hosted, reasons
    )
    qid = str(qp.get("quarantine_id") or "")
    # Without script (no HX-Request) a Retry form asks for the dialog as a page (D-UI-71).
    page = qp.get("page") == "1" and not request.headers.get("HX-Request")
    ctx: dict[str, Any] = {
        "preselect": preselect_id is not None,
        "preselect_id": preselect_id,
        "preselect_note": preselect_note,
        "quarantine_id": int(qid) if qid.isdigit() else None,
        "page": page,
        "next": safe_next(qp.get("next")) if page else None,
        "opp_ids": ids,
        "n": len(ids),
        "current_label": tier2_label(settings),
        "local_models": local,
        "local_note": local_note,
        "hosted_label": label,
        "hosted_models": hosted,
        "hosted_reasons": reasons,
        "hosted_note": hosted_note,
        "hosted_prefix": HOSTED_PREFIX,
        "hosted_while_local": settings.llm.mode != "hosted",
        "minutes": estimate_minutes(len(ids), pace),
        "max_roles": MAX_ROLES,
        "set_aside": aside,
    }
    if page:
        ctx.update(title="Score again", query=dict(qp))
        return templates().TemplateResponse(request, "score_again_page.html", ctx)
    return templates().TemplateResponse(request, "partials/score_again_dialog.html", ctx)


def _note(text: str, *, ok: bool, links: list[tuple[str, str]] | None = None) -> str:
    """The status line; `links` are (label, same-site href) pairs, each escaped."""
    cls = "score-again-done" if ok else "score-again-error"
    extra = "".join(
        f' <a href="{html.escape(href)}">{html.escape(label)}</a>' for label, href in links or []
    )
    return f'<span class="{cls}" role="status">{html.escape(text)}{extra}</span>'


async def _count_retry(session, quarantine_id: str, queued: list[int]) -> None:
    """A Retry from Failed scoring (D-UI-71) counts as an attempt on its quarantine row, as the
    old direct retry did; the row and its raw reply are otherwise untouched (hard rule 3)."""
    if not quarantine_id.isdigit():
        return
    q = await session.get(Quarantine, int(quarantine_id))
    if q is not None and q.opportunity_id in queued:
        q.attempts = (q.attempts or 0) + 1


@router.post("/rescore", response_class=HTMLResponse)
async def rescore(request: Request):
    form = await request.form()
    ids = parse_ids([str(v) for v in form.getlist("opp_ids")])
    pick = str(form.get("model") or "same").strip()[:240]
    choice = str(form.get("model_choice") or "")
    anyway = str(form.get("tier0_anyway") or "") == "1"
    if choice == "same":
        pick = "same"  # the radio wins over a model left picked in the select
    # Without script, a no-HX submit returns to the page it came from with the outcome.
    back = None
    if form.get("next") is not None and not request.headers.get("HX-Request"):
        back = safe_next(str(form.get("next")))

    def answer(text: str, *, ok: bool, headers: dict | None = None, code: str = "not_queued",
               n: int = 0, links: list[tuple[str, str]] | None = None):
        if back is not None:  # a fixed outcome code, never free text in the URL (D-UI-71)
            sep = "&" if "?" in back else "?"
            query = urlencode({"outcome": code, "n": n})
            return RedirectResponse(f"{back}{sep}{query}", status_code=303)
        return HTMLResponse(_note(text, ok=ok, links=links), headers=headers or {})

    if not ids:
        return answer("Pick at least one role to score again.", ok=False, code="no_roles")
    if choice == "other" and pick == "same":
        # "A different model" with nothing picked (the script stops this; a plain form cannot).
        return answer("Not queued: pick a model, or choose Same model.", ok=False,
                      code="no_pick")
    try:
        async with sessionmaker()() as s:
            settings = await load_settings_with_db(s)
            override = None
            if pick and pick != "same":
                override, _label, why = await _override_for(settings, pick)
                if override is None:
                    return answer(f"Not queued: {why}", ok=False)
            result = await enqueue_rescore(
                s, ids, model_override=override, tier0_anyway=anyway,
                explain=rule_explainer(s, settings),
            )
            await _count_retry(s, str(form.get("quarantine_id") or ""), result.queued)
            await s.commit()
    except Exception as exc:  # a write path never turns into a 500 (CLAUDE.md)
        log.warning("rescore_failed", error=f"{type(exc).__name__}: {exc}")
        return answer(f"Not queued: {type(exc).__name__}: {exc}", ok=False)
    n = len(result.queued)
    head = f"Queued {n} role{'s' if n != 1 else ''} for scoring." if n else "Nothing queued."
    body = " ".join([head, *result.messages()])
    headers = {"HX-Trigger": "pipelineRefresh"} if n else {}
    code = "queued" if n else (
        "already" if result.already else ("rules" if result.rules else "not_queued")
    )
    # Where each refused role's rule is changed, as links after the sentence (D-UI-73).
    links: list[tuple[str, str]] = []
    for why in result.rule_whys.values():
        if (f"Open {why.place}", why.href) not in links:
            links.append((f"Open {why.place}", why.href))
    return answer(body, ok=bool(n), headers=headers, code=code, n=n, links=links)


def short_model(model: str | None) -> str:
    """"google/gemma-4-31b:free" reads "Gemma"; a Kestrel local model keeps its tag."""
    if not model:
        return "an unknown model"
    base = model.split("/")[-1].split(":")[0]
    if base.startswith("kestrel-"):
        return base
    head = base.split("-")[0]
    return head[:1].upper() + head[1:] if head else model


def comparison(rows: list[dict]) -> str | None:
    """One line comparing the newest score with the one before it."""
    scored = [r for r in rows if r["score"] is not None]
    if len(scored) < 2:
        return None
    new, old = scored[0], scored[1]
    a, b = short_model(new["model"]), short_model(old["model"])
    if new["model"] != old["model"] and a == b:
        a, b = new["model"], old["model"]
    diff = round(new["score"] - old["score"], 1)
    if new["model"] == old["model"]:
        if diff == 0:
            return f"{a} gave the same score both times."
        return f"{a} scored {abs(diff):.1f} {'higher' if diff > 0 else 'lower'} the second time."
    if diff == 0:
        return f"{a} and {b} gave the same score."
    if diff > 0:
        return f"{a} scored {diff:.1f} higher than {b}."
    return f"{b} scored {-diff:.1f} higher than {a}."


async def history_rows(session, opp_id: int) -> list[dict]:
    evs = (
        await session.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id == opp_id, Evaluation.tier >= 2)
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
        )
    ).scalars().all()
    rows = []
    for ev in evs:
        fit = fit_for(ev, None)
        score = ev.score if ev.score is not None else ev.weighted_score
        rows.append({
            "date": (ev.created_at or "")[:10],
            "model": ev.model,
            "fit": fit.word,
            "tone": fit.tone,
            "score": round(score, 1) if score is not None else None,
        })
    return rows


@router.get("/rescore/history/{opp_id}", response_class=HTMLResponse)
async def history(request: Request, opp_id: int):
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        rows = await history_rows(s, opp_id) if opp is not None else []
    ctx = {"opp_id": opp_id, "rows": rows, "compare": comparison(rows)}
    return templates().TemplateResponse(request, "partials/score_history_list.html", ctx)
