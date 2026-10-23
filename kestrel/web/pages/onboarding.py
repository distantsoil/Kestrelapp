"""The onboarding wizard (D-UI-30): one self-contained flow from how Kestrel thinks to the first
search, with a help panel on every step.

Every step renders from the real state each time (kestrel.services.onboarding): the progress
indicator and the step's own status are recomputed on every response, and every save inside the
wizard returns the whole step (#ob-step), so no indicator can go stale after a save. The local
model actions poll only their own box; when an action finishes, the poll answers with the
`ob-refresh` HX-Trigger and the whole step reloads.

Back and Next are forms: with htmx they swap the step in place, without it they answer 303 to
/onboarding. Either way the step is written to the setting table first, so a killed app resumes
where the operator was (hard rule 5).

Nothing here acts externally on the operator's behalf (hard rule 1): the hosted key check is a
metadata request to the provider the operator chose, the model actions run on this machine, and
"Start searching" is the only control that turns scraping on (nothing scrapes unattended until
the operator says so). The resume import reuses the Documents import routes unchanged."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from kestrel.db.base import utcnow_iso
from kestrel.db.models import DocMaster, Setting, Source
from kestrel.llm import providers as prov
from kestrel.services import onboarding as ob
from kestrel.services import profile_edit as pe
from kestrel.services import setup_state as ss
from kestrel.services import tracks as tracks_svc
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.flash import flash_redirect

log = structlog.get_logger()
router = APIRouter()

PROFILE_STEPS = ("about", "employment", "consulting", "location")
# Steps with a "Skip for now" (D-UI-65): each can be finished later from Profile or Sources.
SKIPPABLE = ("resume", "employment", "consulting", "location", "searches")
LOCAL_STEP_KEYS = ("ollama", "profile", "pull", "build")

# The help panel: what the step is, why it matters, what happens next. Plain words.
HELP: dict[str, dict[str, str]] = {
    "welcome": {
        "what": "A short guided setup. Each step saves as you go, so you can close Kestrel at "
        "any point and pick up at the same step next time.",
        "why": "Kestrel can only find and rank the right work once it knows how to think, who "
        "you are and what you want. This guide asks for exactly that and nothing more.",
        "next": "You choose how Kestrel runs its AI models: on this computer, or through a "
        "provider you already pay for.",
    },
    "models": {
        "what": "Kestrel reads every job posting with an AI model. The model can run on this "
        "computer (Local, free, private) or at a provider such as OpenRouter, OpenAI or "
        "Anthropic under your own key (Hosted).",
        "why": "Without a working model nothing gets read or scored. Local needs a capable "
        "graphics card; Hosted works on any computer.",
        "next": "You tell Kestrel whether you are after a job, consulting work, or both.",
    },
    "tracks": {
        "what": "Kestrel runs two kinds of search. Employment finds permanent roles. Consulting "
        "finds fractional and interim engagements, and watches public filings for companies "
        "that are about to need IT leadership.",
        "why": "Choosing only what you want keeps the other kind of search, its settings and its "
        "steps out of your way. You can change this later in Settings.",
        "next": "You tell Kestrel your name and contact details.",
    },
    "about": {
        "what": "Your name, contact details and headline, plus anything a role must have or "
        "must avoid.",
        "why": "Your name and headline go at the top of every tailored document. The SEC "
        "filings watch needs your email, because the SEC requires a contact on every request. "
        "Must-have and must-avoid guide the score; they never reject a posting outright.",
        "next": "You bring in your resume, so Kestrel can tailor it for each role.",
    },
    "resume": {
        "what": "Upload your resume (Word works best) or paste it. Kestrel splits it into roles "
        "and bullets and shows you the result before anything is saved.",
        "why": "Tailored documents are built only from your own words: Kestrel picks the bullets "
        "that fit each role, it never writes new ones. No resume means no tailored documents.",
        "next": "You set the terms you want: titles, level and pay.",
    },
    "employment": {
        "what": "The titles you want, titles to exclude, the lowest level you would take, and "
        "your pay floor and target.",
        "why": "Your titles become the searches Kestrel runs. Excluded titles, the level floor "
        "and the pay floor reject postings that do not fit, so set them honestly but not "
        "tightly: a wrongly rejected job is one you never see.",
        "next": "Consulting terms if you chose consulting, otherwise where you will work.",
    },
    "consulting": {
        "what": "The services you sell, the sectors and company sizes you want, and your day "
        "rate and engagement length.",
        "why": "These decide whether a posted engagement or a company's news is worth your "
        "time, and they shape the consulting searches.",
        "next": "You tell Kestrel where you will work.",
    },
    "location": {
        "what": "Whether remote work suits you, and the cities and states where you would work "
        "on site.",
        "why": "Kestrel matches places by name, not by distance on a map, so list every town "
        "and suburb you would accept. A posting in a town you did not list is not matched.",
        "next": "You review the searches Kestrel built from your answers and start them.",
    },
    "searches": {
        "what": "The job searches Kestrel built from your titles, places and services. You never "
        "write a search yourself; when your profile changes, the searches follow.",
        "why": "Nothing is searched until you press Start searching. Kestrel then reads public "
        "job boards politely, a few times a day, and never applies or sends anything for you.",
        "next": "The first results arrive in a few minutes.",
    },
    "done": {
        "what": "Setup is complete.",
        "why": "From here Kestrel works in the background: it searches, reads, scores and ranks, "
        "and leaves every decision and every application to you.",
        "next": "Open Today each day to see what needs you.",
    },
}

CONSULTING_PLAIN = (
    "Consulting means working as an independent consultant or contractor in your own field, "
    "sold as engagements rather than applied for as jobs. Kestrel finds posted contract, "
    "interim and fractional work, and also watches public SEC filings for companies that may "
    "soon need that help, so you can reach out first. It reads your field from your profile."
)


# ------------------------------------------------------------------------------------- state
async def _load_state(s) -> ob.OnboardingState:
    state = await ob.load(s)
    if state is None:
        state = ob.OnboardingState()
        await ob.save(s, state)
        await s.commit()
    return state


async def _rows(s) -> dict[str, Any]:
    keys = (ss.MODE_KEY, ss.CHOICE_KEY)
    found = await s.execute(select(Setting.key, Setting.value).where(Setting.key.in_(keys)))
    return {k: v for k, v in found.all()}


async def _setup_state() -> ss.SetupState:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        rows = await _rows(s)
        if await ss.apply_pending_hosted_mode(s, settings, rows):
            await s.commit()
            rows = await _rows(s)
    return await asyncio.to_thread(ss.assess, settings, rows)


def models_ready(st: ss.SetupState) -> tuple[bool, str]:
    """(ready, one line). Ready means the chosen way of running can run now."""
    if not st.choice_explicit:
        return False, "Not chosen yet"
    local_ok = (
        st.ollama_version is not None and not st.missing_tags and not st.missing_derived
    )
    has_key = any(st.hosted_keys.values())
    hosted_ok = st.hosted_problem is None and has_key
    if hosted_ok:
        hosted_line = "Hosted provider connected"
    elif has_key and st.hosted_problem:
        hosted_line = f"Hosted provider not ready: {st.hosted_problem}"
    else:
        hosted_line = "No provider connected"
    if st.choice == "local":
        return local_ok, "Local models ready" if local_ok else "Local models not ready yet"
    if st.choice == "hosted":
        return hosted_ok, hosted_line
    if local_ok and hosted_ok:
        return True, "Local and hosted ready"
    if hosted_ok:
        return False, "Hosted provider connected; local models not ready yet"
    if local_ok:
        return False, "Local models ready; no hosted provider connected yet"
    return False, "Local and hosted not both ready yet"


def models_status(
    st: ss.SetupState, settings: Any, connect_ok: bool | None = None
) -> tuple[bool, str]:
    """The model step's status box, worded to agree with a Connect result shown beside it
    (D-UI-40): a ready hosted setup names the models Tier 1 and Tier 2 use, and a failed attempt
    over an earlier working connection says the earlier one still stands."""
    from kestrel.services.hosted_connect import tiers_sentence

    ready, line = models_ready(st)
    if not (st.choice_explicit and st.choice in ("hosted", "both")):
        return ready, line
    if line.startswith("Hosted provider connected") or line == "Local and hosted ready":
        t1 = settings.llm.hosted_tiers.get("tier1")
        t2 = settings.llm.hosted_tiers.get("tier2")
        tiers = tiers_sentence(t1.model if t1 else None, t2.model if t2 else None)
        if connect_ok is False:
            line = f"{line}, using the earlier connection; this attempt changed nothing"
        if tiers:
            line = f"{line}. {tiers.rstrip('.')}"
    return ready, line


def _primary_track(tracks: tuple[str, ...]) -> str:
    return "employment" if "employment" in tracks else tracks[0]


def _section_saved(pstate: pe.ProfileState, track: str, section: str) -> bool:
    return track in pstate.present and section not in pstate.from_example.get(track, ())


async def _statuses(
    s, state: ob.OnboardingState, tracks: tuple[str, ...], st: ss.SetupState
) -> dict[str, str]:
    """Each step's status, read from the real thing: ok, skipped or todo."""
    pstate = pe.load_state()
    primary = _primary_track(tracks)
    n_docs = (await s.execute(select(func.count()).select_from(DocMaster))).scalar() or 0
    searching = (
        await s.execute(
            select(func.count())
            .select_from(Source)
            .where(Source.origin == "generated", Source.enabled.is_(True))
        )
    ).scalar() or 0
    done = {
        "welcome": "welcome" in state.visited,
        "models": models_ready(st)[0],
        "tracks": await s.get(Setting, tracks_svc.KEY) is not None,
        "about": pstate.exists and pstate.problem is None and not pe.about_missing(pstate),
        "resume": n_docs > 0,
        "employment": _section_saved(pstate, "employment", "employment_terms"),
        "consulting": _section_saved(pstate, "consulting", "consulting_terms"),
        "location": _section_saved(pstate, primary, "geo_rules"),
        "searches": searching > 0,
        "done": state.completed,
    }
    out = {}
    for key, ok in done.items():
        if ok:
            out[key] = "ok"
        elif key in state.skipped:
            out[key] = "skipped"
        else:
            out[key] = "todo"
    return out


# ------------------------------------------------------------------------------ step context
async def _searches_ctx(s) -> dict[str, Any]:
    from kestrel.ingest.generated import rebuild_generated_sources

    result = await rebuild_generated_sources(s, dry_run=True)
    await s.rollback()  # a dry run writes nothing; leave the session clean
    by_track: dict[str, list[Any]] = {}
    for spec in result.specs:
        by_track.setdefault(spec.track, []).append(spec)
    on = (
        await s.execute(
            select(func.count())
            .select_from(Source)
            .where(Source.origin == "generated", Source.enabled.is_(True))
        )
    ).scalar() or 0
    return {"specs": result.specs, "specs_by_track": by_track, "searching_on": on > 0}


def _search_gaps(tracks: tuple[str, ...], pstate: pe.ProfileState) -> list[tuple[str, str]]:
    """Why a track has no search yet: (the step to fill, what is missing)."""
    gaps: list[tuple[str, str]] = []
    if "employment" in tracks:
        block = pstate.tracks["employment"]
        terms = block.get("employment_terms") or {}
        geo = block.get("geo_rules") or {}
        if "employment" not in pstate.present or not terms.get("target_titles"):
            gaps.append(("employment", "Add the titles you want under What you want in a job."))
        elif not geo.get("remote_ok") and not geo.get("onsite_metros"):
            gaps.append(("location", "Allow remote work or list a city under Where you will work."))
    if "consulting" in tracks:
        email = any(
            str((pstate.tracks[t] or {}).get("email") or "").strip() for t in pstate.present
        )
        if not email:
            gaps.append(("about", "Add your email under About you to watch SEC filings (the SEC "
                                  "requires a contact address)."))
    return gaps


def _hosted_ctx(settings) -> dict[str, Any]:
    from kestrel.web.pages.settings import _tier_sends

    backends = prov.external_backends(settings)
    presets = []
    # OpenRouter first, marked "Free models available"; then the others (release round R1).
    from kestrel.services.hosted_connect import FREE_PROVIDERS, provider_order

    for pid in provider_order():
        p = prov.PRESETS[pid]
        b = backends.get(p["backend"])
        if b is None:
            continue
        presets.append({
            "id": pid, "label": p["label"], "console": p["console"],
            "key_prefix": p["key_prefix"], "free": pid in FREE_PROVIDERS,
            "key_saved": bool(b.api_key) or prov.secret_present(b.api_key_env),
        })
    connected = [p["label"] for p in presets if p["key_saved"]]
    return {
        "presets": presets,
        "tier_sends": _tier_sends(settings),
        "hosted_mode": settings.llm.hosted_mode,
        "connected_labels": connected,
        "default_provider": next((p["id"] for p in presets if p["key_saved"]), "openrouter"),
    }


async def _local_ctx(st: ss.SetupState) -> dict[str, Any]:
    from kestrel.web.pages.setup import action_ctx, models_snapshot

    return {
        "local_steps": [x for x in st.steps if x.key in LOCAL_STEP_KEYS],
        **action_ctx(await models_snapshot()),
    }


async def _ctx(request: Request, key: str | None = None, **extra) -> dict[str, Any]:
    st = await _setup_state()
    async with sessionmaker()() as s:
        state = await _load_state(s)
        tracks = await tracks_svc.load_enabled(s)
        settings = await load_settings_with_db(s)
        key = ob.current_key(key or state.step, tracks)
        statuses = await _statuses(s, state, tracks, st)
        steps = ob.steps_for(tracks)
        keys = [x.key for x in steps]
        ctx: dict[str, Any] = {
            "title": "Set up Kestrel",
            "query": dict(request.query_params),
            "key": key,
            "info": ob.step_info(key),
            "steps": steps,
            "number": keys.index(key) + 1,
            "total": len(keys),
            "statuses": statuses,
            "reachable": set(state.visited) | {key} | {k for k in keys if keys.index(k) <=
                                                     keys.index(key)},
            "help": HELP[key],
            "tracks": tracks,
            "tracks_chosen": statuses["tracks"] == "ok",
            "consulting_plain": CONSULTING_PLAIN,
            "state": state,
            "is_first": keys.index(key) == 0,
            "onboarding_complete": state.completed,
        }
        if key == "models":
            ready, line = models_status(st, settings, extra.get("connect_ok"))
            ctx.update(st=st, models_ready=ready, models_line=line, choices=ss.CHOICES,
                       **_hosted_ctx(settings), **(await _local_ctx(st)))
        elif key == "resume":
            masters = (await s.execute(select(DocMaster).order_by(DocMaster.id))).scalars().all()
            ctx["masters"] = [
                {"id": m.id, "name": m.name, "track": m.track, "kind": m.doc_kind}
                for m in masters
            ]
        elif key == "searches":
            ctx.update(await _searches_ctx(s))
            ctx["gaps"] = _search_gaps(tracks, pe.load_state())
    if key in PROFILE_STEPS:
        ctx.update(_profile_form_ctx(key, tracks))
    ctx.update(extra)
    return ctx


def _profile_form_ctx(key: str, tracks: tuple[str, ...]) -> dict[str, Any]:
    from kestrel.web.pages.profile import _section_ctx

    track = key if key in ("employment", "consulting") else _primary_track(tracks)
    section = _section_ctx(track)
    return {
        **section,
        "form_post": f"/onboarding/save/{key}",
        "form_target": "#ob-step",
        "form_swap": "outerHTML",
        "ob_next": True,
        "saved": False,
        "problem": None,
        "searches_updated": False,
    }


def _render_step(request: Request, ctx: dict[str, Any], headers: dict | None = None):
    return templates().TemplateResponse(
        request, "partials/onboarding_step.html", ctx, headers=headers
    )


async def _respond(request: Request, key: str | None = None, **extra) -> Response:
    """The step: the partial for htmx, a redirect to /onboarding otherwise."""
    if request.headers.get("HX-Request"):
        return _render_step(request, await _ctx(request, key, **extra))
    return RedirectResponse("/onboarding", status_code=303)


async def _go(key: str, *, visited: str | None = None, skipped: str | None = None) -> None:
    """Store the current step (and mark one visited or skipped). Reaching Done completes."""
    async with sessionmaker()() as s:
        state = await _load_state(s)
        state.step = key
        if visited and visited not in state.visited:
            state.visited.append(visited)
        if skipped and skipped not in state.skipped:
            state.skipped.append(skipped)
        if visited and skipped is None and visited in state.skipped:
            state.skipped.remove(visited)
        if key == "done" and not state.completed:
            state.completed = True
            state.completed_at = utcnow_iso()
            if "done" not in state.visited:
                state.visited.append("done")
        await ob.save(s, state)
        await s.commit()


# ------------------------------------------------------------------------------------ routes
@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    """The wizard at the stored step. ?step= opens a step already reached (the progress links)."""
    want = request.query_params.get("step")
    if want in ob.STEP_KEYS:
        ctx = await _ctx(request)
        if want in ctx["reachable"]:
            await _go(want)
    ctx = await _ctx(request)
    from kestrel.desktop import linux_check

    ctx["linux_gap"] = linux_check.fallback()
    return templates().TemplateResponse(request, "onboarding.html", ctx)


@router.get("/onboarding/step", response_class=HTMLResponse)
async def onboarding_step(request: Request):
    """The current step as a partial (the ob-refresh reload). ?fresh=1 is Check again: it asks
    Ollama now instead of answering from the cached probe."""
    if request.query_params.get("fresh"):
        ss.forget_ollama_probe()
    return _render_step(request, await _ctx(request))


async def _advance_problem(key: str, form: Any) -> str | None:
    """Why Next cannot leave this step yet, or None. Saves the tracks choice on the way."""
    if key == "models":
        st = await _setup_state()
        if not st.choice_explicit:
            return "Choose Local, Hosted or Both first. You can finish connecting later."
    elif key == "tracks":
        picked = [t for t in form.getlist("track") if t in tracks_svc.BOTH]
        if not picked:
            return "Choose at least one: employment, consulting, or both."
        async with sessionmaker()() as s:
            await tracks_svc.save_enabled(s, picked)
            await s.commit()
        await _rebuild_searches()
    elif key == "about":
        pstate = pe.load_state()
        if pstate.problem:
            return f"Your profile file cannot be read: {pstate.problem}"
        if not pstate.exists or pe.about_missing(pstate):
            return "Add your full name and save before you continue. It goes on every document."
    elif key in ("employment", "consulting", "location"):
        async with sessionmaker()() as s:
            tracks = await tracks_svc.load_enabled(s)
        pstate = pe.load_state()
        track = key if key != "location" else _primary_track(tracks)
        section = {"employment": "employment_terms", "consulting": "consulting_terms",
                   "location": "geo_rules"}[key]
        if not _section_saved(pstate, track, section):
            return ("Save this step first with Save and continue. Until you do, these are the "
                    "example's values, not yours, and your searches would use them.")
    return None


@router.post("/onboarding/next")
async def onboarding_next(request: Request):
    form = await request.form()
    async with sessionmaker()() as s:
        state = await _load_state(s)
        tracks = await tracks_svc.load_enabled(s)
    # The step is the stored one; a posted step counts only when the operator already reached
    # it (review finding 11), so a crafted post can never jump past a step whose requirement
    # is unmet: the requirement below is computed here for the step the server says it is.
    stored = ob.current_key(state.step, tracks)
    keys = [x.key for x in ob.steps_for(tracks)]
    reachable = set(state.visited) | {k for k in keys if keys.index(k) <= keys.index(stored)}
    posted = str(form.get("step") or "")
    key = ob.current_key(posted, tracks) if posted in reachable else stored
    if key == "done":
        return _finish(request)
    # "Skip for now" (D-UI-65) leaves a step that can wait without its save check; the step is
    # marked skipped and the progress says so. The tracks and models choices, and your name, are
    # never skippable (no Skip button renders there, and the check still runs).
    skipping = bool(form.get("skip")) and key in SKIPPABLE
    problem = None if skipping else await _advance_problem(key, form)
    if problem:
        if request.headers.get("HX-Request"):
            return _render_step(request, await _ctx(request, key, nav_problem=problem))
        return RedirectResponse("/onboarding", status_code=303)
    async with sessionmaker()() as s:
        tracks = await tracks_svc.load_enabled(s)  # the tracks step may have just changed them
        n_docs = (await s.execute(select(func.count()).select_from(DocMaster))).scalar() or 0
    skipped = key if skipping or (key == "resume" and not n_docs) else None
    nxt = ob.next_key(key, tracks)
    await _go(nxt, visited=key, skipped=skipped)
    return await _respond(request, nxt)


async def advance_after(request: Request, key: str) -> Response:
    """Move the wizard on from `key` after the step's own save (the resume import's "Save resume
    and continue", D-UI-65) and send the browser to it."""
    async with sessionmaker()() as s:
        tracks = await tracks_svc.load_enabled(s)
    nxt = ob.next_key(key, tracks)
    await _go(nxt, visited=key)
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": f"/onboarding?step={nxt}"})
    return RedirectResponse(f"/onboarding?step={nxt}", status_code=303)


def _finish(request: Request) -> Response:
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/"})
    return RedirectResponse("/", status_code=303)


@router.post("/onboarding/back")
async def onboarding_back(request: Request):
    form = await request.form()
    async with sessionmaker()() as s:
        state = await _load_state(s)
        tracks = await tracks_svc.load_enabled(s)
    key = ob.current_key(str(form.get("step") or state.step), tracks)
    prev = ob.prev_key(key, tracks)
    await _go(prev)
    return await _respond(request, prev)


async def _rebuild_searches() -> None:
    from kestrel.web.pages.profile import _rebuild_searches as rebuild

    await rebuild()


@router.post("/onboarding/save/{key}", response_class=HTMLResponse)
async def onboarding_save(request: Request, key: str):
    """Save a profile step through the Profile page's own service, then return the whole step
    (or the next one, for Save and continue)."""
    if key not in PROFILE_STEPS:
        return HTMLResponse("Unknown step.", status_code=404)
    form = await request.form()
    async with sessionmaker()() as s:
        tracks = await tracks_svc.load_enabled(s)
    if key in ("employment", "consulting"):
        targets = [key]
    else:
        # About you and the location apply to every track in use, so both tracks' searches and
        # documents read the same answers.
        targets = list(tracks)
    section = "about" if key == "about" else key
    problem: str | None = None
    try:
        values = pe.section_from_form(section, form)
        for track in targets:
            pe.save_section(track, section, values)
        await _rebuild_searches()
        from kestrel.web.pages.profile import _queue_recheck

        await _queue_recheck(targets)  # a first profile may release roles set aside before it
    except pe.ProfileEditError as exc:
        problem = plain_error(exc)
    except OSError as exc:
        log.warning("profile_write_failed", error_type=type(exc).__name__, error=str(exc))
        problem = f"Could not write profile.yml: {plain_error(exc)}"
    if problem is None and form.get("_next"):
        nav_problem = await _advance_problem(key, form)
        if nav_problem is None:
            nxt = ob.next_key(key, tracks)
            await _go(nxt, visited=key)
            return _render_step(request, await _ctx(request, nxt))
        return _render_step(
            request, await _ctx(request, key, saved=True, nav_problem=nav_problem)
        )
    return _render_step(request, await _ctx(request, key, saved=problem is None, problem=problem))


@router.post("/onboarding/models/mode", response_class=HTMLResponse)
async def onboarding_mode(request: Request):
    form = await request.form()
    choice = str(form.get("mode") or "")
    if choice not in ss.CHOICES:
        return HTMLResponse("unknown run mode", status_code=400)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        msg = await ss.set_run_mode(s, choice, settings)
        await s.commit()
    # The page's own wording names Settings steps; here the next thing is on this page.
    msg = {
        "local": "Local it is. Check the steps below: Kestrel installs nothing for you, but it "
        "downloads and builds the models with one click each.",
        "hosted": "Hosted it is. Connect your provider below.",
        "both": "Both it is. Set up the local models and connect a provider below.",
    }.get(choice, msg)
    return _render_step(request, await _ctx(request, "models", models_message=msg))


@router.post("/onboarding/models/connect", response_class=HTMLResponse)
async def onboarding_connect(request: Request):
    from kestrel.services.hosted_connect import connect_provider
    from kestrel.web.pages.settings import _active_profile_name

    form = await request.form()
    provider = str(form.get("provider") or "")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        rows = await _rows(s)
        # Hosted only: switch now. Both: local stays the daily runner.
        switch = rows.get(ss.CHOICE_KEY) == "hosted"
        out = await connect_provider(
            s,
            settings,
            provider=provider,
            api_key=str(form.get("api_key") or ""),
            acknowledged="on" in form.getlist("ack"),
            free_only="on" in form.getlist("free_only"),
            switch=switch,
            allow_training="on" in form.getlist("allow_training"),
            profile=_active_profile_name(),
        )
    return _render_step(
        request,
        await _ctx(request, "models", connect_result=out, connect_ok=out.ok,
                   connect_provider=provider),
    )


@router.post("/onboarding/models/action/{name}", response_class=HTMLResponse)
async def onboarding_action(request: Request, name: str):
    from kestrel.web.pages.setup import ACTIONS, start_action

    if name not in ACTIONS:
        return HTMLResponse("unknown action", status_code=404)
    st, message, _status = await start_action(name)
    st = await _setup_state()
    return templates().TemplateResponse(
        request, "partials/onboarding_local.html",
        {"st": st, "local_message": message, **(await _local_ctx(st))},
    )


@router.get("/onboarding/models/local", response_class=HTMLResponse)
async def onboarding_local(request: Request):
    """The local box, polled while an action runs. When the poll finds it finished, the whole
    step reloads (HX-Trigger ob-refresh) so the progress indicator moves with it."""
    st = await _setup_state()
    ctx = {"st": st, **(await _local_ctx(st))}
    headers = None
    if request.query_params.get("poll") and not ctx["running"]:
        headers = {"HX-Trigger": "ob-refresh"}
    return templates().TemplateResponse(
        request, "partials/onboarding_local.html", ctx, headers=headers
    )


@router.post("/onboarding/searches/start", response_class=HTMLResponse)
async def onboarding_start_searching(request: Request):
    """The one control in the wizard that turns scraping on: switch every generated search on,
    commit, and queue the first scan the way the tray's Run scan now does."""
    from kestrel.ingest.generated import rebuild_generated_sources
    from kestrel.ingest.scheduler import enqueue_due_scrapes
    from kestrel.queue.queue import TaskQueue

    sm = sessionmaker()
    async with sm() as s:
        result = await rebuild_generated_sources(s, enable=True)
        await s.commit()
    if not result.specs:
        msg = "There is nothing to search yet. Fill in the step named below, then come back."
        return _render_step(request, await _ctx(request, "searches", search_message=msg))
    async with sm() as s:
        queued = await enqueue_due_scrapes(s, TaskQueue(sm), operator=True)
    n = len(result.specs)
    msg = (
        f"Searching is on: {n} search{'es' if n != 1 else ''}, {queued} queued to run now. "
        "The first results appear in Pipeline within a few minutes."
    )
    log.info("onboarding_searching_started", searches=n, queued=queued)
    return _render_step(
        request, await _ctx(request, "searches", search_message=msg, search_ok=True)
    )


@router.post("/onboarding/restart")
async def onboarding_restart(request: Request):
    """Restart onboarding from Settings > System (D-UI-31): keep my details, or clear
    everything (typed RESET, a backup first, keys kept unless unticked)."""
    form = await request.form()
    choice = str(form.get("choice") or "")
    if choice == "keep":
        async with sessionmaker()() as s:
            await ob.restart(s)
            await s.commit()
        return RedirectResponse("/onboarding", status_code=303)
    if choice != "clear":
        return HTMLResponse("Choose keep or clear.", status_code=400)
    if str(form.get("confirm") or "").strip() != "RESET":
        return flash_redirect("/settings", "reset_confirm_needed", "system")
    from kestrel.services.reset import ResetBackupFailed, ResetBusy, reset_in_app

    keep_keys = "on" in form.getlist("keep_keys")
    try:
        result = await reset_in_app(sessionmaker(), keep_keys=keep_keys)
    except Exception as exc:  # the app is put back in reset_in_app's finally; say what happened
        log.warning("onboarding_reset_failed", error=f"{type(exc).__name__}: {exc}")
        code = {ResetBusy: "reset_busy", ResetBackupFailed: "reset_backup_failed"}.get(
            type(exc), "reset_stopped"
        )
        return flash_redirect("/settings", code, "system")
    async with sessionmaker()() as s:
        await ob.restart(s)
        await s.commit()
    if result.failed:
        log.warning("onboarding_reset_files_in_use", files=list(result.failed))
        return flash_redirect("/onboarding", "reset_files_in_use")
    return RedirectResponse("/onboarding", status_code=303)
