"""The onboarding wizard (D-UI-30, D-UI-155): six steps from how Kestrel thinks to the first
search, with a help column on every step. Welcome, How Kestrel will think, About you, Your resume,
What you want, Your searches; the walkthrough that follows is Today's (`/?tour=1`).

Every step renders from the real state each time (kestrel.services.onboarding): the progress
row and the step's own status are recomputed on every response, and every save inside the wizard
returns the whole step (#ob-step), so no indicator can go stale after a save. The local model
actions poll only their own box; when an action finishes, the poll answers with the `ob-refresh`
HX-Trigger and the whole step reloads.

Back and Next are forms: with htmx they swap the step in place, without it they answer 303 to
/onboarding. Either way the step is written to the setting table first, so a killed app resumes
where the operator was (hard rule 5). The server decides every requirement; the disabled primary
and its hint are a courtesy that onboarding.js repeats live.

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

# Steps with a "Skip for now" (D-UI-65): only the resume can wait; Kestrel still finds and scores
# roles without one.
SKIPPABLE = ("resume",)
LOCAL_STEP_KEYS = ("ollama", "profile", "pull", "build")
# Weeks to billed days for the shortest project length: the stored field counts days (D-UI-155).
DAYS_PER_WEEK = 5
DEFAULT_MARGIN = 5

# The help column on every step (what this is, why it matters, what happens next): each card is
# (title, accent, icon, short lines, flyout paragraphs). Plain words, from the locked mockup.
_CARD_META = (("What this is", "info", "info"), ("Why it matters", "warn", "target"),
              ("What happens next", "good", "arrow"))
_HELP_RAW: dict[str, list[tuple[list[str], list[tuple[str, str]]]]] = {
    "welcome": [
        (["Kestrel finds roles, scores how well they fit, and tailors your own resume lines.",
          "It tracks what you apply to."],
         [("What Kestrel does", "It looks at public job boards and company career pages, then "
           "scores each role against what you tell it you want."),
          ("What it never does", "It never applies, emails or messages anyone. It drafts and "
           "hands over, and you decide.")]),
        (["Setup takes about 5 minutes.", "You can stop anytime."],
         [("Stopping early", "Close the window whenever you like. Your answers save as you go, "
           "and Kestrel opens on the same step next time."),
          ("Changing your mind", "Every answer can be changed later in Settings and Profile.")]),
        (["Next you choose where the AI runs.", "Then you answer a few short questions."],
         [("The six steps", "Welcome, how Kestrel thinks, about you, your resume, what you "
           "want, and your searches. After that a short tour shows you around.")]),
    ],
    "models": [
        (["The AI model reads each role and scores your fit.",
          "It can run on this computer or online."],
         [("On this computer", "Free and private. Your computer does the work."),
          ("Online model", "Uses your own key from a service you pick. You pay that service "
           "for what you use.")]),
        (["On this computer, nothing leaves it.", "Online only happens if you choose it."],
         [("Asking first", "Before anything goes to an online model, Kestrel tells you "
           "exactly what will be sent and waits for your yes."),
          ("Your key", "It stays on this computer. Kestrel never shows it again after you "
           "save it.")]),
        (["Models download in the background.", "You can keep going while they finish."],
         [("How long it takes", "A few minutes on a typical connection. A progress bar shows "
           "where it is."),
          ("If it stalls", "Kestrel keeps trying. You can also pick the online option and "
           "switch later.")]),
    ],
    "about": [
        (["Your name, email and city go on your resume.", "Choose jobs, consulting, or both."],
         [("Consulting and contract work", "Short projects, contracts and fractional roles. If "
           "you choose it, Kestrel adds a short section on the next steps."),
          ("Optional details", "Headline, phone and LinkedIn link sit behind Add more "
           "details.")]),
        (["Only your first name is required.", "Nothing here is sent anywhere."],
         [("Where it is used", "These details fill the top of the resumes Kestrel prepares for "
           "you. They stay on this computer.")]),
        (["Next, add your resume.", "You can skip that and add it later."],
         [("If you skip it", "Kestrel still finds and scores roles. It just cannot tailor "
           "resumes until you add one.")]),
    ],
    "resume": [
        (["Kestrel reads your resume and breaks it into lines.",
          "It picks from them for each role."],
         [("Why lines", "Each bullet is kept as you wrote it, so a tailored resume only ever "
           "contains your own words."),
          ("File types", "Word, PDF or plain text, or paste it in.")]),
        (["A line that does not match your file is flagged.", "You decide what to keep."],
         [("How to tell", "A flagged line differs from your file, often from a line break or a "
           "number format. Fix it in your file and read it again, or leave it out.")]),
        (["After you check the text, Kestrel saves it.", "You can edit lines later in Profile."],
         [("Later changes", "Add variants of a bullet in Profile. Kestrel chooses between them "
           "per role.")]),
    ],
    "wants": [
        (["These answers shape what Kestrel searches for.", "A few short fields per section."],
         [("Add more details", "Level, pay flexibility, deal-breakers and more sit behind that "
           "link, so you only see them if you want them.")]),
        (["Better answers mean better matches.", "Leave a field blank if unsure."],
         [("Places", "Kestrel matches places by name, not distance. Add nearby towns you would "
           "commute to."),
          ("Pay", "Roles with no listed pay are still shown, marked as estimated.")]),
        (["Next you see the exact searches.", "You can edit any of them."],
         [("Nothing runs yet", "Searching only starts after you press Start searching on the "
           "last step.")]),
    ],
    "searches": [
        (["A plain list of what Kestrel will search.", "Built from your answers."],
         [("Sources", "Public job boards and company career pages. LinkedIn is never searched, "
           "but you can paste a posting by hand.")]),
        (["Check it reads right before you start.", "Use Edit to fix anything."],
         [("Politeness", "Kestrel searches slowly and follows each site's rules. It never logs "
           "in anywhere for you.")]),
        (["Start searching turns searching on.", "Then a short tour shows the app."],
         [("First results", "Your first roles arrive within a few minutes. Today shows what is "
           "new."),
          ("Restart setup", "Settings, then Your data, lets you run setup again anytime.")]),
    ],
}
HELP: dict[str, list[dict[str, Any]]] = {
    key: [
        {"title": _CARD_META[i][0], "accent": _CARD_META[i][1], "icon": _CARD_META[i][2],
         "lines": lines, "more": more, "id": f"ob-h-{key}-{i}"}
        for i, (lines, more) in enumerate(cards)
    ]
    for key, cards in _HELP_RAW.items()
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


def models_done(st: ss.SetupState) -> bool:
    """Step 2 is done when a way of running is chosen and, for Online or Both, a key is
    connected. Local models may still be downloading: the operator continues meanwhile."""
    if not st.choice_explicit:
        return False
    if st.choice == "local":
        return True
    return st.hosted_problem is None and any(st.hosted_keys.values())


def _terms(pstate: pe.ProfileState, track: str, section: str) -> dict:
    """A saved term section of a track, or {} while it is still only the example's values."""
    if not _section_saved(pstate, track, section):
        return {}
    block = pstate.tracks.get(track) or {}
    value = block.get(section)
    return value if isinstance(value, dict) else {}


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
    wants = _section_saved(pstate, primary, "geo_rules")
    if "employment" in tracks:
        wants = wants and _section_saved(pstate, "employment", "employment_terms")
    if "consulting" in tracks:
        wants = wants and _section_saved(pstate, "consulting", "consulting_terms")
    done = {
        "welcome": "welcome" in state.visited,
        "models": models_done(st),
        "about": pstate.exists and pstate.problem is None and not pe.about_missing(pstate)
        and await s.get(Setting, tracks_svc.KEY) is not None,
        "resume": n_docs > 0,
        "wants": wants,
        "searches": searching > 0 and state.completed,
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
    from kestrel.ingest.board_discovery import PLATFORMS
    from kestrel.ingest.generated import _BOARD_NAMES, SITES, US_NOTE

    companies = (
        await s.execute(
            select(func.count())
            .select_from(Source)
            .where(Source.kind.in_(tuple(PLATFORMS)), Source.enabled.is_(True))
        )
    ).scalar() or 0
    return {
        "specs": result.specs, "specs_by_track": by_track, "searching_on": on > 0,
        "boards": " and ".join(_BOARD_NAMES.get(x, x) for x in SITES),
        "companies": companies, "us_note": US_NOTE,
    }


def _search_gaps(tracks: tuple[str, ...], pstate: pe.ProfileState) -> list[tuple[str, str]]:
    """Why a track has no search yet: (the step to fill, what is missing)."""
    gaps: list[tuple[str, str]] = []
    if "employment" in tracks:
        block = pstate.tracks["employment"]
        terms = block.get("employment_terms") or {}
        geo = block.get("geo_rules") or {}
        if "employment" not in pstate.present or not terms.get("target_titles"):
            gaps.append(("wants", "Add the jobs you want under What you want."))
        elif not geo.get("remote_ok") and not geo.get("onsite_metros"):
            gaps.append(("wants", "Add a place or turn on remote under What you want."))
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


def _hardware_ctx(st: ss.SetupState) -> dict[str, Any]:
    hw = st.hardware
    if hw.gpu_vendor == "nvidia" and hw.vram_gb is not None:
        gpu = f"{hw.vram_gb:g} GB"
    elif hw.apple_silicon:
        gpu = "Apple silicon"
    else:
        gpu = "Not found"
    return {
        "hw_memory": f"{hw.ram_gb:g} GB" if hw.ram_gb is not None else "Unknown",
        "hw_gpu": gpu,
        "hw_gpu_found": gpu != "Not found",
    }


def _wants_ctx(tracks: tuple[str, ...], pstate: pe.ProfileState) -> dict[str, Any]:
    """Prefill for step 5 from what is already saved. A section that is still only the
    shipped example's values starts blank: example values never become searches."""
    from kestrel.rules.margin import normalize_margin
    from kestrel.web.pages.profile import MARGIN_OPTIONS

    primary = _primary_track(tracks)
    emp = _terms(pstate, "employment", "employment_terms")
    con = _terms(pstate, "consulting", "consulting_terms")
    geo = _terms(pstate, primary, "geo_rules")
    block = pstate.tracks.get(primary) or {}
    ex_emp = (pstate.examples.get("employment") or {}).get("employment_terms") or {}
    ex_con = (pstate.examples.get("consulting") or {}).get("consulting_terms") or {}
    margin = normalize_margin(emp.get(pe.EMPLOYMENT_MARGIN)) if emp else DEFAULT_MARGIN
    min_days = con.get("min_engagement_days")
    weeks = ""
    if isinstance(min_days, int) and min_days > 0:
        weeks = str(max(1, round(min_days / DAYS_PER_WEEK)))
    metros = pe.metros_text({"geo_rules": geo}).splitlines()
    return {
        "w_titles": list(emp.get("target_titles") or []),
        "w_pay": emp.get("comp_floor_usd") or "",
        "w_modes": list(emp.get("work_modes") or []),
        "w_level": emp.get("seniority_floor") or "",
        "w_margin": str(margin or DEFAULT_MARGIN),
        "w_under": bool(emp) and margin != 0,
        "w_margin_options": tuple(o for o in MARGIN_OPTIONS if o[0] != "0"),
        "w_deal": list(block.get("must_avoid") or []) if primary in pstate.present else [],
        "w_services": list(con.get("service_lines") or []),
        "w_skip": list(con.get("excluded_sectors") or []),
        "w_rate": con.get("day_rate_floor") or "",
        "w_weeks": weeks,
        "w_places": metros,
        "w_remote": bool(geo.get("remote_ok")),
        "w_headline": str(block.get("headline") or ""),
        "w_city": str(block.get("location") or ""),
        "w_ex_title": ((ex_emp.get("target_titles") or ["Operations Analyst"])[0]),
        "w_ex_service": ((ex_con.get("service_lines") or ["Process audits"])[0]),
        "seniority_floors": pe.SENIORITY_FLOORS,
        "work_modes": pe.WORK_MODES,
    }


def _about_ctx(tracks: tuple[str, ...], pstate: pe.ProfileState) -> dict[str, Any]:
    block = pstate.tracks.get(_primary_track(tracks)) or {}
    return {
        "a": {k: str(block.get(k) or "") for k in pe.ABOUT_TEXT},
        "track_jobs": "employment" in tracks,
        "track_consulting": "consulting" in tracks,
    }


def _hint(key: str, ctx: dict[str, Any]) -> str:
    """Why the step's primary button is off, or ''. The server decides; onboarding.js repeats
    the same rule live for the two forms."""
    if key == "models":
        st = ctx.get("st")
        if st is None or not st.choice_explicit:
            return "Choose how Kestrel should think first"
        if st.choice != "local" and not models_done(st):
            return "Connect your key first"
    elif key == "about":
        a = ctx.get("a") or {}
        if not a.get("first_name", "").strip():
            return "Add your first name first"
        if not (ctx.get("track_jobs") or ctx.get("track_consulting")):
            return "Pick at least one to continue"
    elif key == "resume":
        if not ctx.get("masters"):
            return "Skip for now: no tailored resumes until you add one."
    elif key == "wants":
        tracks = ctx["tracks"]
        if "employment" in tracks and not ctx.get("w_titles"):
            return "Add a job title first"
        if not ctx.get("w_places") and not ctx.get("w_remote"):
            return "Add a place or turn on remote first"
    return ""


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
        pstate = pe.load_state()
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
            "consulting_plain": CONSULTING_PLAIN,
            "state": state,
            "is_first": keys.index(key) == 0,
            "onboarding_complete": state.completed,
            "ret": state.ret if state.ret and state.ret != key else None,
            "skippable": key in SKIPPABLE,
            "st": st,
        }
        if key == "models":
            ready, line = models_status(st, settings, extra.get("connect_ok"))
            ctx.update(models_ready=ready, models_line=line, choices=ss.CHOICES,
                       **_hardware_ctx(st), **_hosted_ctx(settings), **(await _local_ctx(st)))
        elif key == "about":
            ctx.update(_about_ctx(tracks, pstate))
            ctx["tracks_chosen"] = await s.get(Setting, tracks_svc.KEY) is not None
        elif key == "resume":
            masters = (await s.execute(select(DocMaster).order_by(DocMaster.id))).scalars().all()
            ctx["masters"] = [
                {"id": m.id, "name": m.name, "track": m.track, "kind": m.doc_kind}
                for m in masters
            ]
            from kestrel.web.pages.documents import _import_model_label

            label = await _import_model_label()
            ctx["import_label"] = label
            ctx["import_online"] = settings.llm.mode == "hosted" or "(hosted)" in label
        elif key == "wants":
            ctx.update(_wants_ctx(tracks, pstate))
        elif key == "searches":
            ctx.update(await _searches_ctx(s))
            ctx["gaps"] = _search_gaps(tracks, pstate)
            ctx["rows"] = await _search_rows(s, tracks, pstate)
    ctx.update(extra)
    ctx["hint"] = _hint(key, ctx)
    return ctx


async def _search_rows(s, tracks: tuple[str, ...], pstate: pe.ProfileState) -> dict[str, Any]:
    """The two plain lines beside the searches: what is hidden and the resume."""
    primary = _primary_track(tracks)
    block = pstate.tracks.get(primary) or {}
    hidden = list(block.get("must_avoid") or []) if primary in pstate.present else []
    for track in tracks:
        hidden += [str(x) for x in _terms(pstate, track, "consulting_terms").get(
            "excluded_sectors") or []]
    masters = (await s.execute(select(DocMaster.name).order_by(DocMaster.id))).scalars().all()
    return {"hidden": hidden, "resume_names": list(masters)}


def _render_step(request: Request, ctx: dict[str, Any], headers: dict | None = None):
    return templates().TemplateResponse(
        request, "partials/onboarding_step.html", ctx, headers=headers
    )


async def _respond(request: Request, key: str | None = None, **extra) -> Response:
    """The step: the partial for htmx, a redirect to /onboarding otherwise."""
    if request.headers.get("HX-Request"):
        return _render_step(request, await _ctx(request, key, **extra))
    return RedirectResponse("/onboarding", status_code=303)


async def _go(
    key: str, *, visited: str | None = None, skipped: str | None = None,
    ret: str | None = None, clear_ret: bool = False,
) -> None:
    """Store the current step (and mark one visited or skipped)."""
    async with sessionmaker()() as s:
        state = await _load_state(s)
        state.step = key
        if visited and visited not in state.visited:
            state.visited.append(visited)
        if skipped and skipped not in state.skipped:
            state.skipped.append(skipped)
        if visited and skipped is None and visited in state.skipped:
            state.skipped.remove(visited)
        if ret:
            state.ret = ret
        elif clear_ret or key == state.ret:
            state.ret = None
        await ob.save(s, state)
        await s.commit()


async def _after(key: str, tracks: tuple[str, ...], *, skipped: str | None = None) -> str:
    """Leave `key` going forward: back to the step an Edit came from, else the next step."""
    async with sessionmaker()() as s:
        state = await _load_state(s)
    nxt = state.ret if state.ret and state.ret != key else ob.next_key(key, tracks)
    await _go(nxt, visited=key, skipped=skipped, clear_ret=True)
    return nxt


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
    ctx["resumed"] = bool(ctx["state"].visited)  # a page load at a later step is a resume
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


async def _advance_problem(key: str, tracks: tuple[str, ...]) -> str | None:
    """Why Next cannot leave this step yet, or None. The server decides; the disabled button is
    only a courtesy."""
    if key == "models":
        st = await _setup_state()
        if not st.choice_explicit:
            return "Choose how Kestrel should think first. You can finish connecting later."
        if not models_done(st):
            return "Connect your key first, or choose On this computer."
    elif key == "about":
        pstate = pe.load_state()
        if pstate.problem:
            return pstate.problem
        if not pstate.exists or pe.about_missing(pstate):
            return (
                "Add your first name and save before you continue. "
                "Your name goes on every document."
            )
        async with sessionmaker()() as s:
            if await s.get(Setting, tracks_svc.KEY) is None:
                return "Choose at least one: jobs, consulting, or both."
    elif key == "resume":
        async with sessionmaker()() as s:
            n_docs = (await s.execute(select(func.count()).select_from(DocMaster))).scalar() or 0
        if not n_docs:
            return "Add your resume first, or choose Skip for now."
    elif key == "wants":
        pstate = pe.load_state()
        primary = _primary_track(tracks)
        saved = _section_saved(pstate, primary, "geo_rules")
        for track, section in (("employment", "employment_terms"),
                               ("consulting", "consulting_terms")):
            if track in tracks:
                saved = saved and _section_saved(pstate, track, section)
        if not saved:
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
    if key == "searches":
        return await onboarding_start_searching(request)
    # "Skip for now" (D-UI-65) leaves a step that can wait without its save check; the step is
    # marked skipped and the progress says so. The models choice and your name are never
    # skippable (no Skip button renders there, and the check still runs).
    skipping = bool(form.get("skip")) and key in SKIPPABLE
    problem = None if skipping else await _advance_problem(key, tracks)
    if problem:
        if request.headers.get("HX-Request"):
            return _render_step(request, await _ctx(request, key, nav_problem=problem))
        return RedirectResponse("/onboarding", status_code=303)
    skipped = key if skipping else None
    nxt = await _after(key, tracks, skipped=skipped)
    return await _respond(request, nxt)


async def advance_after(request: Request, key: str) -> Response:
    """Move the wizard on from `key` after the step's own save (the resume import's "Save resume
    and continue", D-UI-65) and send the browser to it."""
    async with sessionmaker()() as s:
        tracks = await tracks_svc.load_enabled(s)
    nxt = await _after(key, tracks)
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": f"/onboarding?step={nxt}"})
    return RedirectResponse(f"/onboarding?step={nxt}", status_code=303)


@router.post("/onboarding/back")
async def onboarding_back(request: Request):
    form = await request.form()
    async with sessionmaker()() as s:
        state = await _load_state(s)
        tracks = await tracks_svc.load_enabled(s)
    key = ob.current_key(str(form.get("step") or state.step), tracks)
    prev = state.ret if state.ret and state.ret != key else ob.prev_key(key, tracks)
    await _go(prev, clear_ret=True)
    return await _respond(request, prev)


@router.post("/onboarding/edit/{key}")
async def onboarding_edit(request: Request, key: str):
    """An Edit link on the searches step: open that step, and come back to the searches after
    its save (Back says so too)."""
    if key not in ob.STEP_KEYS or key == "searches":
        return HTMLResponse("Unknown step.", status_code=404)
    await _go(key, ret="searches")
    return await _respond(request, key)


async def _rebuild_searches() -> None:
    from kestrel.web.pages.profile import _rebuild_searches as rebuild

    await rebuild()


def _existing_form(track: str, section: str, pstate: pe.ProfileState) -> dict[str, str]:
    """The saved values of a section as a flat form, so a wizard save that asks for only some
    fields leaves the others (a restart that kept the operator's details) exactly as they were."""
    block = pstate.tracks.get(track) or {}
    out: dict[str, str] = {}
    if section == "about":
        for k in pe.ABOUT_TEXT:
            out[k] = str(block.get(k) or "")
        for k in pe.ABOUT_LISTS:
            out[k] = "\n".join(block.get(k) or [])
        if block.get("requires_sponsorship"):
            out["requires_sponsorship"] = "on"
    elif section == "employment":
        t = _terms(pstate, track, "employment_terms")
        for k in ("target_titles", "anti_titles"):
            out[k] = "\n".join(t.get(k) or [])
        for typ in t.get("excluded_employment_types") or []:
            out[f"type_{typ}"] = "on"
        out["seniority_floor"] = str(t.get("seniority_floor") or "")
        for k in pe.EMPLOYMENT_INTS:
            out[k] = "" if t.get(k) is None else str(t.get(k))
    elif section == "consulting":
        t = _terms(pstate, track, "consulting_terms")
        for k in pe.CONSULTING_LISTS:
            out[k] = "\n".join(t.get(k) or [])
        for k in pe.CONSULTING_INTS:
            out[k] = "" if t.get(k) is None else str(t.get(k))
        out["travel_tolerance"] = str(t.get("travel_tolerance") or "")
        for m in t.get("work_modes") or []:
            out[f"mode_{m}"] = "on"
        if t.get(pe.CONSULTING_MARGIN) is not None:
            out[pe.CONSULTING_MARGIN] = str(t[pe.CONSULTING_MARGIN])
    elif section == "location":
        g = _terms(pstate, track, "geo_rules")
        for k in pe.GEO_FLAGS:
            if g.get(k):
                out[k] = "on"
        for k in pe.GEO_LISTS:
            out[k] = "\n".join(g.get(k) or [])
        out["onsite_metros"] = pe.metros_text({"geo_rules": g})
        if g.get("travel_days_per_month_max") is not None:
            out["travel_days_per_month_max"] = str(g["travel_days_per_month_max"])
    return out


class _Form(dict):
    """A flat dict that answers `.get` and `.getlist` like a request form."""

    def getlist(self, key: str) -> list[str]:
        v = self.get(key)
        return [] if v is None else [v]


def _merged(track: str, section: str, pstate: pe.ProfileState, posted: dict[str, str]) -> _Form:
    base = _existing_form(track, section, pstate)
    base.update(posted)
    return _Form(base)


async def _save_everywhere(tracks: tuple[str, ...]) -> None:
    from kestrel.web.pages.profile import _queue_recheck

    await _rebuild_searches()
    await _queue_recheck(list(tracks))  # a first profile may release roles set aside before it


@router.post("/onboarding/save/about", response_class=HTMLResponse)
async def onboarding_save_about(request: Request):
    """Step 3: who you are, and the tracks in use. Saved through the Profile service for every
    track in use, leaving fields the wizard does not ask for as they were."""
    form = await request.form()
    picked = [t for t in tracks_svc.BOTH if t in form.getlist("track")]
    typed = {k: str(form.get(k) or "").strip() for k in pe.ABOUT_TEXT if k in form}
    typed.setdefault("first_name", "")
    pstate = pe.load_state()
    problem: str | None = None
    if not typed["first_name"]:
        problem = "Add your first name first. Kestrel greets you by it."
    elif not picked:
        problem = "Choose at least one: jobs, consulting, or both."
    elif pstate.problem:
        problem = pstate.problem
    tracks = tuple(picked)
    if problem is None:
        try:
            for track in tracks:
                values = pe.section_from_form("about", _merged(track, "about", pstate, typed))
                pe.save_section(track, "about", values)
            async with sessionmaker()() as s:
                await tracks_svc.save_enabled(s, list(tracks))
                await s.commit()
            await _save_everywhere(tracks)
        except pe.ProfileEditError as exc:
            problem = plain_error(exc)
        except OSError as exc:
            log.warning("profile_write_failed", error_type=type(exc).__name__, error=str(exc))
            problem = f"Could not write profile.yml: {plain_error(exc)}"
    if problem is not None:
        ctx = await _ctx(request, "about", nav_problem=problem)
        ctx["a"] = {**ctx["a"], **typed}
        ctx["track_jobs"] = "employment" in picked
        ctx["track_consulting"] = "consulting" in picked
        ctx["hint"] = _hint("about", ctx)
        return _render_step(request, ctx)
    nxt = await _after("about", tracks)
    return _render_step(request, await _ctx(request, nxt))


def _chip_lines(form: Any, name: str) -> list[str]:
    return pe.lines(str(form.get(name) or ""))


def _on(form: Any, name: str) -> bool:
    return str(form.get(name) or "") in ("on", "true", "1")


@router.post("/onboarding/save/wants", response_class=HTMLResponse)
async def onboarding_save_wants(request: Request):
    """Step 5: what you want, per track in use, then where. Each section goes through the
    Profile service (so the generated searches and the Tier 0 re-check follow), and fields the
    wizard does not ask for keep their saved values."""
    form = await request.form()
    async with sessionmaker()() as s:
        tracks = await tracks_svc.load_enabled(s)
    pstate = pe.load_state()
    titles = _chip_lines(form, "target_titles")
    places = _chip_lines(form, "onsite_metros")
    # Choosing Remote as a work style implies remote roles are included.
    remote = _on(form, "remote_ok") or _on(form, "mode_remote")
    problem: str | None = None
    if "employment" in tracks and not titles:
        problem = "Add a job title first."
    elif not places and not remote:
        problem = "Add a place or turn on remote first."
    elif pstate.problem:
        problem = pstate.problem
    elif not pstate.exists or any(t not in pstate.present for t in tracks):
        problem = "Add your first name under About you first."
    if problem is None:
        try:
            _save_wants(form, tracks, pstate, titles, places, remote)
            await _save_everywhere(tracks)
        except pe.ProfileEditError as exc:
            problem = plain_error(exc)
        except OSError as exc:
            log.warning("profile_write_failed", error_type=type(exc).__name__, error=str(exc))
            problem = f"Could not write profile.yml: {plain_error(exc)}"
    if problem is not None:
        ctx = await _ctx(request, "wants", nav_problem=problem)
        ctx.update(w_titles=titles, w_places=places, w_remote=remote,
                   w_pay=str(form.get("comp_floor_usd") or ""),
                   w_deal=_chip_lines(form, "must_avoid"),
                   w_services=_chip_lines(form, "service_lines"),
                   w_skip=_chip_lines(form, "excluded_sectors"),
                   w_rate=str(form.get("day_rate_floor") or ""),
                   w_weeks=str(form.get("engagement_weeks") or ""))
        ctx["hint"] = _hint("wants", ctx)
        return _render_step(request, ctx)
    nxt = await _after("wants", tracks)
    return _render_step(request, await _ctx(request, nxt))


def _save_wants(
    form: Any, tracks: tuple[str, ...], pstate: pe.ProfileState, titles: list[str],
    places: list[str], remote: bool,
) -> None:
    if "employment" in tracks:
        posted = {
            "target_titles": "\n".join(titles),
            "comp_floor_usd": str(form.get("comp_floor_usd") or ""),
            "seniority_floor": str(form.get("seniority_floor") or ""),
            pe.EMPLOYMENT_MARGIN: (
                str(form.get(pe.EMPLOYMENT_MARGIN) or DEFAULT_MARGIN) if _on(form, "under")
                else "0"
            ),
        }
        for key, _label in pe.WORK_MODES:
            if _on(form, f"mode_{key}"):
                posted[f"mode_{key}"] = "on"
        pe.save_section("employment", "employment", pe.section_from_form(
            "employment", _merged("employment", "employment", pstate, posted)))
    if "consulting" in tracks:
        weeks = str(form.get("engagement_weeks") or "").strip()
        posted = {
            "service_lines": "\n".join(_chip_lines(form, "service_lines")),
            "excluded_sectors": "\n".join(_chip_lines(form, "excluded_sectors")),
            "day_rate_floor": str(form.get("day_rate_floor") or ""),
            "min_engagement_days": "",
        }
        if weeks:
            try:
                posted["min_engagement_days"] = str(int(float(weeks)) * DAYS_PER_WEEK)
            except ValueError as exc:
                raise pe.ProfileEditError(
                    "The project length must be a whole number of weeks."
                ) from exc
        pe.save_section("consulting", "consulting", pe.section_from_form(
            "consulting", _merged("consulting", "consulting", pstate, posted)))
    for track in tracks:
        existing = _terms(pstate, track, "geo_rules")
        posted = {"onsite_metros": "\n".join(places)}
        # Hybrid as a work style implies hybrid roles must be in a listed metro. The wizard has
        # no control to untick it, so nothing here overrides an explicit choice.
        if _on(form, "mode_hybrid") and "hybrid_requires_metro" not in form:
            posted["hybrid_requires_metro"] = "on"
        if remote:
            posted["remote_ok"] = "on"
            if not existing.get("remote_country_allowlist"):
                posted["remote_country_allowlist"] = "US"
        merged = _merged(track, "location", pstate, posted)
        if not remote:
            merged.pop("remote_ok", None)
        pe.save_section(track, "location", pe.section_from_form("location", merged))
    # Deal-breakers read by the scoring model (must avoid), on every track in use.
    avoid = _chip_lines(form, "must_avoid")
    for track in tracks:
        if track in pstate.present:
            pe.save_section(track, "about", {"must_avoid": avoid})


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
        "local": "On this computer it is. Install the helper and download the models below; "
        "you can keep going while they finish.",
        "hosted": "Online model it is. Connect your service below.",
        "both": "Both it is. Set up the models on this computer and connect a service below.",
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
    # A refused Connect redraws the step with what was ticked still ticked, so the operator
    # fixes the one thing named instead of meeting a form that looks untouched.
    ticked = {k: "on" in form.getlist(k) for k in ("ack", "free_only", "allow_training")}
    return _render_step(
        request,
        await _ctx(request, "models", connect_result=out, connect_ok=out.ok,
                   connect_provider=provider, connect_form=ticked),
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
    commit, queue the first scan the way the tray's Run scan now does, finish onboarding and open
    Today (the first-search state, where the walkthrough begins)."""
    from kestrel.ingest.generated import rebuild_generated_sources
    from kestrel.ingest.scheduler import enqueue_due_scrapes
    from kestrel.queue.queue import TaskQueue

    async with sessionmaker()() as s:
        tracks = await tracks_svc.load_enabled(s)
    for gate in ("models", "about"):
        problem = await _advance_problem(gate, tracks)
        if problem:
            return _render_step(request, await _ctx(request, "searches", nav_problem=problem))
    sm = sessionmaker()
    async with sm() as s:
        result = await rebuild_generated_sources(s, enable=True)
        await s.commit()
    if not result.specs:
        msg = "There is nothing to search yet. Fill in the step named below, then come back."
        return _render_step(request, await _ctx(request, "searches", nav_problem=msg))
    async with sm() as s:
        queued = await enqueue_due_scrapes(s, TaskQueue(sm), operator=True)
    async with sm() as s:
        state = await _load_state(s)
        if not state.completed:
            state.completed = True
            state.completed_at = utcnow_iso()
        state.step = "searches"
        if "searches" not in state.visited:
            state.visited.append("searches")
        state.ret = None
        await ob.save(s, state)
        await s.commit()
    log.info("onboarding_searching_started", searches=len(result.specs), queued=queued)
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/?tour=1"})
    return RedirectResponse("/?tour=1", status_code=303)


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
