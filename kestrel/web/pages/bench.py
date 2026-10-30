"""Model check (was Bench; D-DRIFT-11, D-UI-105; SPEC-INTERFACES 2.1 `/bench`, SPEC.md 8.6).

The route keeps its name and is reached from Models. **Check my models** queues a `bench` task (a
`task` row) that the worker runs in the background (`kestrel.bench.check`): resumable, with
progress and Cancel, so a real comparison never blocks the web loop and never needs the command
line (D-UI-129). The old Run button made no model call; it now queues that task. The comparison
table below stays what it was: the agreement between stored evaluations and the operator's own
labels (SPEC-MODULES 5.5), with the sample size stated (hard rule 6).

The page itself (D-UI-147) swaps in place: `GET /bench/panel` is the check card (polled while a
check runs), `POST /bench/start` queues from the card, `POST /bench/cancel` stops it, and
`POST /bench/apply` and `/bench/undo` switch the steps a result recommends (an online switch needs
the operator's tick, checked here). The JSON routes below stay for the CLI and tests.

JSON for the page: `POST /bench/check` queues, `GET /bench/check/status` is polled for progress and
the latest recommendation, `POST /bench/check/cancel` stops it.

Model choice is a picker, never free text (D-UI-44): the local models Ollama reports form one
group, and the hosted provider's list (services/hosted_models.py, the Models page's cached list;
fetched only on "Load hosted models") forms another, its values prefixed `hosted:`. Promoting a
hosted model saves it as that tier's hosted override, validated exactly as the Models page does.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from html import escape

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Evaluation, Label, Setting
from kestrel.llm import providers as prov
from kestrel.llm.prompts import all_versions
from kestrel.services import hosted_models as hm
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import bench_view as bv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.pages import models_ui

log = structlog.get_logger()
router = APIRouter()

# The evaluation tiers a comparison run is meaningful for (embed/docsel/trigger are not verdict-
# and-score models, so they are out of scope for the gold-set agreement metric SPEC.md 8.6 defines).
BENCH_TIERS = ("tier1", "tier2", "tier3")

QUEUED_NOTICE = (
    "Model check queued. It runs in the background and picks up after a restart; progress shows "
    "here, and you can cancel it."
)
NO_HARNESS_NOTICE = QUEUED_NOTICE  # the name older callers import


def _online_gate(
    settings, models: dict | None, online: dict | None, confirm, approval: object = None,
    steps: list[str] | None = None,
) -> str:
    """The refusal (a plain sentence) when a check names a model that would be sent online and the
    operator has not been through every gate, else "". The same three gates as /bench/apply: a
    connected provider with a saved key, approval of the current disclosure, and a ticked
    confirmation tied to the provider the sent text is shown for (`confirm` is that provider's
    name from the form, or True from the JSON route). `check.queue_check` repeats the first two."""
    from kestrel.bench import check

    if not check.online_candidates(settings, models, online):
        return ""
    refusal = check.online_refusal(settings, models, online)
    if refusal:
        return refusal
    found = check.connected_provider(settings)
    label = next((p["label"] for p in prov.PRESETS.values() if found and p["backend"] == found[0]),
                 "your provider")
    if confirm is not True and (not found or confirm != found[0]):
        return f"Tick the box to confirm what will be sent to {label}, then try again."
    selected_steps = steps or list(dict.fromkeys([
        *(models or {}).keys(), *(online or {}).keys(), *check.DEFAULT_STEPS
    ]))
    expected = check.approval_for_candidates(settings, selected_steps, models, online)
    from kestrel.services.online_consent import approval_covers

    if expected and not approval_covers(expected, approval):
        return "Confirm the model and online service destinations, then try again."
    return ""


async def _comparison_rows(session: AsyncSession) -> list[dict]:
    """Group every (evaluation, label) pair that shares an opportunity_id and track by
    (tier, model, prompt_version, track) and report exact-verdict agreement and mean absolute score
    error, with the sample size stated plainly (hard rule 6: n < 5 is labeled insufficient, never
    rendered as if it were solid). A given opportunity may carry more than one label over time (e.g.
    a calibration override); every pairing is counted, so this is an approximation of the eventual
    harness metric, not a precise replacement for it."""
    evaluations = (await session.execute(select(Evaluation))).scalars().all()
    if not evaluations:
        return []
    labels = (await session.execute(select(Label))).scalars().all()
    labels_by_key: dict[tuple[int, str], list[Label]] = defaultdict(list)
    for lbl in labels:
        labels_by_key[(lbl.opportunity_id, lbl.track)].append(lbl)
    if not labels_by_key:
        return []

    groups: dict[tuple[int, str, str, str], list[tuple[Evaluation, Label]]] = defaultdict(list)
    for ev in evaluations:
        for lbl in labels_by_key.get((ev.opportunity_id, ev.track), ()):
            key = (ev.tier, ev.model or "(unknown model)", ev.prompt_version or "(unknown version)",
                   ev.track)
            groups[key].append((ev, lbl))

    rows: list[dict] = []
    for (tier, model, prompt_version, track), pairs in sorted(groups.items()):
        n = len(pairs)
        exact = sum(
            1 for ev, lbl in pairs if ev.verdict and lbl.verdict and ev.verdict == lbl.verdict
        )
        scored = [
            (ev.score, lbl.score) for ev, lbl in pairs
            if ev.score is not None and lbl.score is not None
        ]
        mae = sum(abs(a - b) for a, b in scored) / len(scored) if scored else None
        rows.append({
            "tier": tier, "model": model, "prompt_version": prompt_version, "track": track,
            "n": n, "sufficient": n >= 5,
            "exact_match_rate": exact / n if n else None,
            "mae": mae,
        })
    return rows


def _available_models(settings) -> tuple[list[str], str | None]:
    """Models the local Ollama reports right now, or ([], reason) if it cannot be reached. Never
    raises: a not-running model must degrade this page gracefully, never 500 it. `settings` are
    the app's (load_settings_with_db), so Hosted mode switched on in the app counts."""
    from kestrel.doctor import hosted_only
    from kestrel.llm.client import OllamaClient

    if hosted_only(settings):
        # Every tier is hosted: Ollama is not part of this setup, so it is not probed and its
        # absence is not reported (D-SETUP-8); the hosted picker is the only choice.
        return [], None
    try:
        backend = settings.llm.backends["local"]
        client = OllamaClient(
            backend.base_url, settings.llm.ollama_native_url, backend.api_key or "ollama"
        )
        return client.list_models(), None
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any reachability failure degrades
        log.info("bench_local_models_unreachable", error_type=type(exc).__name__, error=str(exc))
        return [], f"The AI engine is not running ({plain_error(exc)})."


HOSTED_PREFIX = "hosted:"


async def _hosted_models(settings, *, fetch: bool) -> tuple[str, list, str | None]:
    """(provider label, picker models, error). Without `fetch` only a cached list is used, so a
    plain page load makes no network call."""
    found = hm.catalog_backend(settings)
    if found is None:
        return "", [], None
    name, backend = found
    label = next((p["label"] for p in prov.PRESETS.values() if p["backend"] == name), name)
    if not fetch:
        return label, hm.picker_models(hm.cached(name, backend) or []), None
    try:
        return label, hm.picker_models(await hm.fetch_models(name, backend)), None
    except hm.ModelListError as exc:
        return label, [], plain_error(exc)


async def _page_ctx(request: Request) -> dict:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        rows = await _comparison_rows(s)
    # The Ollama tag list is a sync httpx call; never on the event loop (CLAUDE.md).
    models, models_error = await asyncio.to_thread(_available_models, settings)
    bases = await _bases()
    hosted_label, hosted, hosted_error = await _hosted_models(
        settings, fetch=request.query_params.get("hosted") == "1"
    )
    from kestrel.bench import check
    provider = models_ui.connected_backend(settings)
    offered = {
        step: [{"model": str(m.id), "backend": provider[0]} for m in hosted]
        for step in check.DEFAULT_STEPS + ("deeper_read",)
    } if provider else {}
    approval = check.approval_for_offered(settings, offered)
    from kestrel.services.online_consent import approval_for, merge_approvals

    approval = merge_approvals(approval, approval_for(settings, ("tier1", "tier2", "tier3")))
    return {
        "hosted_label": hosted_label, "hosted_models": hosted, "hosted_error": hosted_error,
        "hosted_prefix": HOSTED_PREFIX,
        "title": "Benchmark", "query": dict(request.query_params),
        "tiers": BENCH_TIERS,
        "tier_config": {t: settings.llm.tiers.get(t) for t in BENCH_TIERS},
        "tier_now": {
            t: bv.friendly_model(cfg.model, base=bases.get(cfg.model) or bases.get(t))
            for t in BENCH_TIERS if (cfg := settings.llm.tiers.get(t))
        },
        "backends": sorted(settings.llm.backends.keys()),
        "prompt_versions": all_versions(),
        "has_previous_instructions": has_previous_instructions(),
        "available_models": models, "models_error": models_error,
        "rows": rows, "notice": "", "ran": False,
        "ck_approval": json.dumps(approval, separators=(",", ":")),
    }


# The run path does not yet pass a previous prompt version through the queue and harness, so a
# "previous" pick would silently run the current instructions. Until it does, the choice stays
# disabled even though the /v1 files are kept (owner-reported O21, 2026-10-26).
PREVIOUS_RUNS_SUPPORTED = False


def has_previous_instructions() -> bool:
    """Whether Kestrel keeps an earlier version of its instructions to the AI to compare with:
    only when a prompt past v1 still ships the version before it (the O20 `/v2` prompts keep
    their `/v1` files and registry entries for this, D-LLM-12)."""
    import re

    if not PREVIOUS_RUNS_SUPPORTED:
        return False
    versions = set(all_versions())
    for v in versions:
        m = re.search(r"^(.*)/v(\d+)$", v)
        if m and int(m.group(2)) > 1 and f"{m.group(1)}/v{int(m.group(2)) - 1}" in versions:
            return True
    return False


NO_PREVIOUS_INSTRUCTIONS = (
    "Kestrel has no earlier version of its instructions to the AI to compare with yet. A check "
    "uses the current instructions."
)


STEP_TIER_BY_LABEL = {"Quick screen": "tier1", "Rating": "tier2", "Deeper read": "tier3"}


async def _bases() -> dict[str, str | None]:
    """tier -> the model Kestrel's own model for it is built from (the active hardware profile)."""
    from kestrel.llm import profiles

    active = profiles.load_active_profile()
    async with sessionmaker()() as s:
        out: dict[str, str | None] = dict(await models_ui.custom_bases(s))
    if active is None:
        return out
    try:
        models = profiles.load_catalog().profile(active.profile).models
    except (KeyError, OSError, ValueError):
        return out
    return {**out, **{t: models.for_tier(t) for t in profiles.TIERS}}


def _estimate_minutes(rated: int) -> int:
    """A rough run time, rounded to five minutes: the roles in the answer key, two steps, about
    three models each, a few seconds a role. Said as "about" and never more exact than that."""
    return max(5, round(rated * 2 * 3 * 4.0 / 60 / 5) * 5)


async def _check_ctx(
    session: AsyncSession, settings, *, notice: str = "", notice_ok: bool = True
) -> dict:
    """The Model check card and result: live status, the rated roles it rests on, the latest
    check as cards, and the earlier ones."""
    from sqlalchemy import func

    from kestrel.bench import check

    status = await check.status(session)
    rated = await check.rated_count(session)
    kept = (
        await session.execute(
            select(func.count()).select_from(Label).where(Label.verdict != "pass")
        )
    ).scalar_one()
    passed = (
        await session.execute(
            select(func.count()).select_from(Label).where(Label.verdict == "pass")
        )
    ).scalar_one()
    summary = (status["latest"] or {}).get("summary", "")
    lead, _, rest = summary.partition(". ")
    latest = status["latest"]
    bases = await _bases()
    steps = [
        bv.step_view(v, bases, current_model=settings.llm.tiers[check.STEP_TIER[v["step"]]].model)
        for v in (latest or {}).get("steps", [])
    ]
    undo = {
        r.key: r.value.get("token", "")
        for r in (
            await session.execute(
                select(Setting).where(Setting.key.like(models_ui.UNDO_PREFIX + "%"))
            )
        ).scalars()
    }
    provider = models_ui.connected_backend(settings)
    prog = status.get("progress") or {}
    tier_of_label = {label: STEP_TIER_BY_LABEL.get(label) for label in STEP_TIER_BY_LABEL}
    prog_model = (
        bv.friendly_model(
            prog.get("model"), base=bases.get(tier_of_label.get(prog.get("step", "")) or "")
        )
        if prog.get("model")
        else ""
    )
    from kestrel.setup import tier_sends
    from kestrel.web.pages.needs_try import _employer_state

    return {
        # What an online model is sent, worded once for the whole app (the Hosted mode
        # disclosure the operator accepted), and what happens to the current employer.
        "ck_sends": tier_sends(settings),
        "ck_employer": await _employer_state(session, settings),
        "ck_held": (latest or {}).get("held") or [],
        "ck_prog_model": prog_model,
        "ck_status": status,
        "ck_rated": rated,
        "ck_kept": kept,
        "ck_passed": passed,
        "ck_lead": lead + ("." if rest and not lead.endswith(".") else ""),
        "ck_rest": rest,
        "ck_enough": rated >= check.MIN_RATINGS,
        "ck_min": check.MIN_RATINGS,
        "ck_minutes": _estimate_minutes(rated),
        "ck_key": check.answer_key_info(),
        "ck_latest": latest,
        "ck_steps": steps,
        "ck_changes": [x for x in steps if x["change"] and x["enough"]],
        "ck_quiet": [x for x in steps if not x["change"] or not x["enough"]],
        "ck_numbers": bv.numbers_view(latest, bases) if latest else [],
        "ck_when": bv.date_words(latest.get("at")) if latest else "",
        "ck_history": bv.history_view(status["history"][1:] if latest else []),
        "ck_undo": {
            t: undo[models_ui.UNDO_PREFIX + t]
            for t in check.STEP_TIER.values()
            if models_ui.UNDO_PREFIX + t in undo
        },
        "ck_provider_backend": provider[0] if provider else "",
        "ck_provider": next(
            (p["label"] for p in prov.PRESETS.values() if provider and p["backend"] == provider[0]),
            "",
        ),
        "ck_notice": notice,
        "ck_notice_ok": notice_ok,
        "ck_oob": False,
    }


async def _panel(request: Request, *, notice: str = "", ok: bool = True, oob: bool = False):
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        ctx = await _check_ctx(s, settings, notice=notice, notice_ok=ok)
    # The result and the earlier checks ride along (out of band) once a check is not running.
    ctx["ck_oob"] = oob or not ctx["ck_status"]["live"]
    if not ctx["ck_status"]["live"]:
        # The Advanced section lives inside the card, so a card that swaps in carries its pickers
        # (never while a check runs: that is the 2-second poll, which shows progress only).
        ctx = {**await _page_ctx(request), **ctx}
    return templates().TemplateResponse(request, "partials/bench_panel.html", ctx)


@router.get("/bench", response_class=HTMLResponse)
async def page(request: Request):
    ctx = await _page_ctx(request)
    if request.headers.get("HX-Request"):
        return templates().TemplateResponse(request, "partials/bench_table.html", ctx)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        ctx.update(await _check_ctx(s, settings))
    return templates().TemplateResponse(request, "bench.html", ctx)


@router.get("/bench/panel", response_class=HTMLResponse)
async def panel(request: Request):
    """The check card, polled while a check runs. Once it ends the response also carries the
    result and the earlier checks (out of band), so the page fills in without a reload."""
    return await _panel(request)


async def _online_candidates(settings, steps: list[str]) -> tuple[dict, str]:
    """Online models to include, per step: one free and one paid that fit the step. The model
    list request carries only the key. ({}, message) when nothing can be offered."""
    from kestrel.bench.check import STEP_TIER

    found = models_ui.connected_backend(settings)
    if found is None:
        return {}, ("Connect an online provider first. Online models need a key, and none is "
                    "connected. Set one up under Models.")
    name, backend = found
    try:
        listed = hm.picker_models(await hm.fetch_models(name, backend))
    except hm.ModelListError as exc:
        return {}, f"Online models could not be listed. {plain_error(exc)}"
    out: dict[str, list] = {}
    for step in steps:
        fit = [m for m in listed if not hm.block_reason(m, STEP_TIER[step], backend)]
        picks = [next((m for m in fit if m.free), None), next((m for m in fit if not m.free), None)]
        out[step] = [{"model": m.id, "backend": name} for m in picks if m]
    return out, ""


@router.post("/bench/start", response_class=HTMLResponse)
async def start(request: Request):
    """Check my models from the card: Quick screen and Rating, plus Deeper read when ticked, plus
    online models only when the operator ticked that (the consent to send the rated roles)."""
    from kestrel.bench import check

    form = await request.form()
    deeper = form.get("deeper") == "on"
    steps = list(check.DEFAULT_STEPS) + (["deeper_read"] if deeper else [])
    online: dict = {}
    notice, ok = "", True
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        if form.get("online") == "on":
            online, refusal = await _online_candidates(settings, steps)
            refusal = refusal or models_ui.online_problem(settings) or ""
            if not refusal:
                connected = check.connected_provider(settings)
                refusal = _online_gate(
                    settings, None, online, connected[0] if connected else "",
                    form.get("online_approval"), steps,
                )
            if refusal:
                notice, ok = refusal, False
        if ok:
            result = await check.queue_check(
                s, deeper=deeper, online=online, settings=settings,
                online_approval=form.get("online_approval")
            )
            notice, ok = (QUEUED_NOTICE, True) if result.queued else (result.message, False)
    return await _panel(request, notice=notice, ok=ok)


@router.post("/bench/answer-key", response_class=HTMLResponse)
async def answer_key(request: Request):
    """Make your own ratings the answer key the check scores models against (the in-app form of
    `kestrel goldset regen`). A track with fewer than five ratings keeps the shipped examples."""
    from kestrel.bench import check

    async with sessionmaker()() as s:
        if await check.live_task(s) is not None:
            return await _panel(request, notice="Wait for the running check to finish first.",
                                ok=False)
        res = await check.build_answer_key(s)
    if not res["built"]:
        return await _panel(
            request, ok=False,
            notice=(f"Rate at least {check.MIN_RATINGS} roles first. Until then the check uses "
                    "the shipped example roles, not your ratings."))
    n = sum(res["built"].values())
    return await _panel(request, notice=(
        f"Your {n} rated roles are now the answer key. The next check scores models against "
        "your own calls."))


@router.post("/bench/cancel", response_class=HTMLResponse)
async def cancel(request: Request):
    from kestrel.bench import check

    async with sessionmaker()() as s:
        n = await check.cancel_check(s)
    return await _panel(
        request, notice="The check was canceled." if n else "No check was running.", ok=bool(n)
    )


@router.post("/bench/apply", response_class=HTMLResponse)
async def apply(request: Request):
    """Switch the steps a result recommends. A local switch is an override of that step's model;
    an online one needs the tick (what is sent, what it costs) and a connected provider, and is
    saved through the same validated override as Models. Undo restores every step switched."""
    from kestrel.bench import check

    form = await request.form()
    wanted = list(dict.fromkeys(x for x in form.getlist("step") if x in check.STEP_TIER))
    confirm = form.get("confirm") == "on"
    done: list[str] = []
    refused: list[str] = []
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        history = await check.list_checks(s)
        verdicts = {
            v["step"]: v for v in ((history[0] if history else {}).get("steps") or [])
            if v.get("change")
        }
        for step in wanted:
            best = (verdicts.get(step) or {}).get("best")
            label = check.STEP_LABEL[step]
            if not best:
                refused.append(f"{label}: the check recommends no change")
                continue
            tier = check.STEP_TIER[step]
            if best.get("online"):
                found = models_ui.connected_backend(settings)
                if found is None:
                    refused.append(
                        f"{label}: no online provider is connected. Set one up under Models first"
                    )
                    continue
                problem = models_ui.online_problem(settings)
                if problem:
                    refused.append(f"{label}: {problem.rstrip('.')}")
                    continue
                if not confirm:
                    refused.append(f"{label}: tick the box to confirm what will be sent first")
                    continue
                name, backend = found
                try:
                    models = await hm.fetch_models(name, backend)
                except hm.ModelListError as exc:
                    refused.append(f"{label}: {plain_error(exc)}")
                    continue
                before = await models_ui.snapshot(s, tier, settings, online=True)
                res = await hm.save_overrides(
                    s, settings, name, backend, {tier: best["model"]}, models
                )
                if res.refused:
                    refused.append(f"{label}: {res.message}")
                else:
                    await models_ui.store_snapshot(s, tier, before)
                    await s.commit()
                    done.append(label)
            else:
                ok, why = await models_ui.switch_local(s, settings, tier, best["model"])
                if ok:
                    done.append(label)
                else:
                    refused.append(f"{label}: {why.rstrip('.')}")
    parts = []
    if done:
        parts.append(f"Switched {' and '.join(done)}. You can switch back here or on Models.")
    if refused:
        parts.append("Not switched: " + "; ".join(refused) + ".")
    return await _panel(
        request, notice=" ".join(parts) or "Nothing was picked.", ok=bool(done) and not refused,
        oob=True,
    )


@router.post("/bench/undo", response_class=HTMLResponse)
async def undo(request: Request):
    from kestrel.bench import check

    form = await request.form()
    undone = []
    async with sessionmaker()() as s:
        for step in dict.fromkeys(form.getlist("step")):
            if step in check.STEP_TIER and await models_ui.undo_tier(
                s, check.STEP_TIER[step], str(form.get("token_" + step) or ""), commit=False
            ):
                undone.append(check.STEP_LABEL[step])
            else:
                await s.rollback()
                return await _panel(
                    request, notice="This can no longer be undone.", ok=False, oob=True
                )
        await s.commit()
    msg = f"Put {' and '.join(undone)} back as it was." if undone else "There is nothing to undo."
    return await _panel(request, notice=msg, ok=bool(undone), oob=True)


_TIER_STEP = {"tier1": "quick_screen", "tier2": "rating", "tier3": "deeper_read"}


async def _form_check_args(session, tier: str, model: str, backend: str) -> dict:
    """The older form picked one tier and one model: test just that step, the model in use against
    the picked one, on the backend the picker names (a hosted pick runs on the connected hosted
    provider). The prompt-version field is not used: a check always runs Kestrel's current
    instructions to the AI, and the previous version is an Advanced choice of the check itself.
    With no pick the form is the plain Check my models."""
    from kestrel.bench import check

    step = _TIER_STEP.get(tier)
    if step is None or not model:
        return {}
    settings = await load_settings_with_db(session)
    picked: dict = {"model": model.removeprefix(HOSTED_PREFIX)}
    if model.startswith(HOSTED_PREFIX):
        found = models_ui.connected_backend(settings)
        if found is None:
            return {"error": "Connect an online provider first. That model runs online, and "
                             "none is connected. Set one up under Models."}
        picked["backend"] = found[0]
    elif backend in settings.llm.backends:
        picked["backend"] = backend
    tcfg = settings.llm.tiers[tier]
    # The model in use is tested too; named with its backend, so an online one in use is gated
    # exactly like a picked one (it is sent the same rated roles).
    in_use: str | dict = tcfg.model
    if check.is_online(settings, tier):
        in_use = {"model": tcfg.model, "backend": tcfg.backend}
    return {"steps": [step], "models": {step: [in_use, picked]}}


@router.post("/bench/run", response_class=HTMLResponse)
async def run(request: Request):
    """The Advanced form: check one step against one chosen model. Queues the background task
    (never a model call on the request). A refusal (too few ratings, one already running, an
    online pick with no connected provider) is shown in the card as a plain message."""
    from kestrel.bench import check

    form = await request.form()
    raw_model = str(form.get("model") or "")
    wants_previous = str(form.get("instructions") or "") == "previous"
    ctx = {
        "tier": str(form.get("tier") or ""),
        "model": raw_model.removeprefix(HOSTED_PREFIX),
        "backend": str(form.get("backend") or ""),
        "prompt_version": str(form.get("prompt_version") or ""),
        "notice": QUEUED_NOTICE, "ran": True, "rows": [],
    }
    try:
        async with sessionmaker()() as s:
            kwargs = await _form_check_args(s, ctx["tier"], raw_model, ctx["backend"])
            refusal = kwargs.pop("error", "")
            if wants_previous and not has_previous_instructions():
                refusal = NO_PREVIOUS_INSTRUCTIONS
            if not refusal and kwargs:
                settings = await load_settings_with_db(s)
                refusal = _online_gate(
                    settings, kwargs.get("models"), None, str(form.get("confirm") or ""),
                    form.get("online_approval"), kwargs.get("steps"),
                )
                kwargs["settings"] = settings
            result = None if refusal else await check.queue_check(
                s, **kwargs, online_approval=form.get("online_approval")
            )
            ctx["rows"] = await _comparison_rows(s)
        if refusal:
            ctx["notice"] = refusal
        elif not result.queued:
            ctx["notice"] = result.message
    except Exception as exc:  # noqa: BLE001 -- a DB hiccup must render a message, never a 500
        log.warning("bench_queue_failed", error_type=type(exc).__name__, error=str(exc))
        ctx["notice"] = f"Could not start the model check: {plain_error(exc)}"
    return await _panel(request, notice=ctx["notice"], ok=ctx["notice"] == QUEUED_NOTICE)


@router.post("/bench/check")
async def check_queue(request: Request):
    """Queue Check my models. Body (all optional JSON): steps, deeper, models, online, confirm.
    Any online candidate needs `"confirm": true` as well as a connected provider and approval."""
    from fastapi.responses import JSONResponse

    from kestrel.bench import check

    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    kwargs = {}
    if isinstance(body.get("steps"), list):
        kwargs["steps"] = [str(x) for x in body["steps"]]
    for key in ("models", "online"):
        if isinstance(body.get(key), dict):
            kwargs[key] = {
                str(k): [m if isinstance(m, dict) else str(m) for m in v]
                for k, v in body[key].items() if isinstance(v, list)
            }
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        refusal = _online_gate(
            settings, kwargs.get("models"), kwargs.get("online"), body.get("confirm") is True,
            body.get("online_approval") or body.get("approval"), kwargs.get("steps"),
        )
        if refusal:
            return JSONResponse(
                {"queued": False, "reason": "online_refused", "message": refusal, "task_id": None},
                status_code=409,
            )
        result = await check.queue_check(
            s, deeper=bool(body.get("deeper")), settings=settings, **kwargs,
            online_approval=body.get("online_approval") or body.get("approval")
        )
    return JSONResponse(
        {"queued": result.queued, "reason": result.reason, "message": result.message,
         "task_id": result.task_id},
        status_code=200 if result.queued else 409,
    )


@router.get("/bench/check/status")
async def check_status():
    from fastapi.responses import JSONResponse

    from kestrel.bench import check

    async with sessionmaker()() as s:
        return JSONResponse(await check.status(s))


@router.post("/bench/check/cancel")
async def check_cancel():
    from fastapi.responses import JSONResponse

    from kestrel.bench import check

    async with sessionmaker()() as s:
        return JSONResponse({"cancelled": await check.cancel_check(s)})


@router.post("/bench/promote", response_class=HTMLResponse)
async def promote(request: Request):
    """Use the operator's chosen model for a step, with no restart. A model on this computer goes
    through `models_ui.switch_local` (tuned for the step first, never a raw tag); an online one
    is saved as that step's hosted override. Never 500s: a bad step or model, or a DB problem,
    renders a message instead."""
    form = await request.form()
    tier = str(form.get("tier") or "").strip()
    model = str(form.get("model") or "").strip()
    if tier not in BENCH_TIERS:
        return HTMLResponse('<div class="mo-msg is-bad">Pick a step first.</div>')
    if not model:
        return HTMLResponse('<div class="mo-msg is-bad">Pick a model to promote first.</div>')
    if model.startswith(HOSTED_PREFIX):
        return await _promote_hosted(tier, model.removeprefix(HOSTED_PREFIX))
    try:
        async with sessionmaker()() as s:
            settings = await load_settings_with_db(s)
            ok, why = await models_ui.switch_local(s, settings, tier, model)
    except Exception as exc:  # noqa: BLE001 -- surface as a message, never a 500
        log.warning(
            "bench_promote_save_failed", tier=tier, model=model,
            error_type=type(exc).__name__, error=str(exc),
        )
        return HTMLResponse(
            f'<div class="mo-msg is-bad">Could not save: {escape(plain_error(exc))}</div>'
        )
    if not ok:
        return HTMLResponse(f'<div class="mo-msg is-bad">{escape(why)}</div>')
    ctx = {"tier": tier, "message": why}
    return templates().TemplateResponse(request, "partials/bench_promote_result.html", ctx)


async def _promote_hosted(tier: str, model_id: str) -> HTMLResponse:
    """A hosted pick becomes that tier's hosted override (the Models page's rules: the id must be
    in the provider's list and fit the tier's context budget)."""
    from html import escape

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        found = models_ui.connected_backend(settings)
        if found is None:
            return HTMLResponse('<div class="mo-msg is-bad">Connect an online provider first. '
                                'Set one up under Models.</div>')
        problem = models_ui.online_problem(settings)
        if problem:
            return HTMLResponse(f'<div class="mo-msg is-bad">{escape(problem)}</div>')
        name, backend = found
        try:
            models = await hm.fetch_models(name, backend)
        except hm.ModelListError as exc:
            return HTMLResponse(f'<div class="mo-msg is-bad">{escape(plain_error(exc))}</div>')
        result = await hm.save_overrides(s, settings, name, backend, {tier: model_id}, models)
    box = "mo-msg is-bad" if result.refused else "mo-msg is-good"
    return HTMLResponse(
        f'<div class="{box}" data-testid="bench-promote-hosted">{escape(result.message)} '
        'See <a href="/models#hosted-overrides">Models</a> to reset it.</div>'
    )
