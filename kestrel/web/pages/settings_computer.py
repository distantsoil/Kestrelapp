"""Settings > This computer (D-UI-106, SPEC-INTERFACES 2.6 item 1).

  POST /settings/computer/mode            On this computer, Online model or Both
  POST /settings/computer/mode/undo       take the choice back, only while it is still that one
  POST /settings/computer/key             Test, Save or Remove an online key (never echoed)
  GET  /settings/computer/checklist       hardware line and the four setup buttons (polls)
  POST /settings/computer/action/{name}   engine | download | pause | tune | check
  POST /settings/computer/profile         the hardware profile, in plain words
  POST /settings/computer/option          keep awake, start with my computer, tray, updates

The key store is the one the Models page uses (`.env` through providers.write_secret): this is a
quick entry, and every other manager of keys stays on Models. Choosing Online or Both sends
nothing: an online call is only ever made after the person approves what is sent, on the page that
sends it (hard rule 1, the Privacy section of CLAUDE.md). A local tier keeps its "nothing leaves"
wording; the page never says it for an online tier."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from kestrel.config import hosted_mode_problem
from kestrel.db.models import Setting
from kestrel.llm import profiles
from kestrel.llm import providers as prov
from kestrel.services import autostart, keep_awake, pursuit_undo
from kestrel.services import setup_state as ss
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import settings_io as io
from kestrel.web.deps import CLOSE_TO_TRAY_KEY, sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.pages.settings_roles import trigger

log = structlog.get_logger()
router = APIRouter()

MODES = (
    ("local", "local", "On this computer", "laptop",
     ("Free to use", "Stays private", "Needs a capable computer")),
    ("online", "hosted", "Online model", "cloud",
     ("Fast", "You pay the service per use", "Asks before sending")),
    ("both", "both", "Both", "both",
     ("Local for most roles", "Online for the deep read", "Asks before going online")),
)
STEPS = (
    ("Quick sort", "Drops roles that clearly don't fit"),
    ("Fit score", "Scores how well a role matches you"),
    ("Deep read", "Reads the whole posting and explains the fit"),
)
ACTIONS = {"engine": "check", "download": "pull", "pause": "cancel", "tune": "build",
           "check": "verify"}


def _provider_rows(settings) -> list[dict]:
    externals = prov.external_backends(settings)
    rows = []
    for pid, preset in prov.PRESETS.items():
        b = externals.get(preset["backend"])
        saved = bool(b) and (bool(b.api_key) or prov.secret_present(b.api_key_env))
        label = preset["label"].split(" (")[0]
        tail = ""
        if saved and b is not None:
            from kestrel.web.pages.settings import key_tail

            tail = key_tail(b)
        rows.append({"id": pid, "label": label, "full": preset["label"], "saved": saved,
                     "tail": tail, "console": preset["console"], "mono": label[:1].upper(),
                     "prefix": preset["key_prefix"]})
    from kestrel.services.other_service import KEY_ENV

    rows.append({"id": "other", "label": "Other", "full": "Other service",
                 "saved": prov.secret_present(KEY_ENV), "tail": "", "console": "",
                 "mono": "O", "prefix": ""})
    return rows


async def mode_ctx(*, message: str = "", ok: bool = True, key_result: dict | None = None,
                   selected: str = "", free_message: str = "", free_ok: bool = True,
                   free_form: dict | None = None) -> dict:
    from kestrel.web.pages.setup import _rows

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        rows = await _rows(s)
        # Hosted chosen before a key was connected: it starts once it can run.
        if await ss.apply_pending_hosted_mode(s, settings, rows):
            await s.commit()
            rows = await _rows(s)
    choice, explicit = ss.run_choice(settings, rows)
    providers = _provider_rows(settings)
    sel = selected if any(p["id"] == selected for p in providers) else next(
        (p["id"] for p in providers if p["saved"]), providers[0]["id"])
    problem = hosted_mode_problem(settings) if choice in ("hosted", "both") else None
    return {
        "modes": MODES, "choice": choice, "explicit": explicit, "providers": providers,
        "selected": sel, "online_problem": problem, "steps": STEPS,
        "mode_message": message, "mode_ok": ok, "key_result": key_result,
        # Free models only (OpenRouter): the saved choice, or the ticks of a refused save.
        "free_on": (free_form["free_only"] if free_form
                    else settings.llm.hosted_mode.free_only),
        "free_train": bool(free_form and free_form["allow_training"]),
        "free_message": free_message, "free_ok": free_ok,
        "any_key": any(p["saved"] for p in providers),
        "provider_names": [p["label"] for p in providers if p["saved"]],
        "other_url": settings.llm.backends["other"].base_url
        if "other" in settings.llm.backends else "",
    }


def _mode_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_mode.html", ctx,
                                        headers=headers or {})


@router.get("/settings/computer/mode", response_class=HTMLResponse)
async def mode_card(request: Request):
    return _mode_card(request, await mode_ctx())


_SAID = {
    "local": "Kestrel will run on this computer.",
    "hosted": "Kestrel will use an online model and ask before sending.",
    "both": "Kestrel will use both and ask before going online.",
}


@router.post("/settings/computer/mode", response_class=HTMLResponse)
async def mode_save(request: Request):
    form = await request.form()
    asked = str(form.get("mode") or "")
    choice = {m[0]: m[1] for m in MODES}.get(asked, asked)
    if choice not in ss.CHOICES:
        return HTMLResponse("Choose one of the three.", status_code=400)
    from kestrel.web.pages.setup import _rows

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        prev, _ = ss.run_choice(settings, await _rows(s))
        if prev == choice:
            return _mode_card(request, await mode_ctx())
        before = {k: await io.read(s, k) for k in (ss.CHOICE_KEY, ss.MODE_KEY)}
        await ss.set_run_mode(s, choice, settings)
        await s.flush()
        after = {k: await io.read(s, k) for k in before}
        changed = [k for k in before if before[k] != after[k]]
        token = await pursuit_undo.save(s, {"kind": "run_mode",
            "before": {k: io.dumps(before[k]) for k in changed},
            "after": {k: io.dumps(after[k]) for k in changed}})
        await s.commit()
    undo = f"/settings/computer/mode/undo?token={token}"
    return _mode_card(
        request, await mode_ctx(),
        trigger(_SAID[choice], undo=undo,
                refresh={"refresh": "/settings/computer/mode", "target": "#st-mode"}),
    )


@router.post("/settings/computer/mode/undo")
async def mode_undo(request: Request):
    async with sessionmaker()() as s:
        row = await s.get(Setting, pursuit_undo.PREFIX + request.query_params.get("token", ""))
        if (row is None or row.value.get("kind") != "run_mode"
                or row.updated_at < (datetime.now(UTC) - pursuit_undo.KEEP).isoformat()):
            return io.refuse(io.NO_UNDO)
        for key, value in row.value["after"].items():
            if io.dumps(await io.read(s, key)) != value:
                return io.refuse(io.NO_UNDO)
        for key, value in row.value["before"].items():
            await io.write(s, key, io.loads(value))
        await s.delete(row)
        await s.commit()
    return io.reply("Put back.")


@router.post("/settings/computer/key", response_class=HTMLResponse)
async def key_save(request: Request):
    """Test, Save or Remove one provider's key. Test writes nothing. The value is never echoed
    back or logged; only the last four characters can appear."""
    from kestrel.web.pages.settings import _keybox_op

    form = await request.form()
    pid = str(form.get("provider") or "")
    op = str(form.get("op") or "test")
    op = op if op in ("test", "save", "remove") else "test"
    value = str(form.get("api_key") or "").strip()
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        preset = prov.PRESETS.get(pid)
        backend = prov.external_backends(settings).get(preset["backend"]) if preset else None
        if pid == "other":
            from kestrel.services.other_service import key_action

            result = await key_action(op, str(form.get("base_url") or ""), value, s, settings)
        elif preset is None or backend is None or not backend.api_key_env:
            result = {"ok": False, "text": "Choose a service first."}
        else:
            result = await _keybox_op(op, backend, preset["label"].split(" (")[0], value)
    log.info("settings_key_action", provider=pid if pid in {*prov.PRESETS, "other"}
             else "unknown", op=op, ok=result["ok"], pasted=bool(value))
    ctx = await mode_ctx(key_result=result, selected=pid)
    if pid == "other" and (op == "test" or not result["ok"]):
        # Keep the address draft after a test or refusal, but never echo a pasted key.
        ctx["other_url"] = str(form.get("base_url") or "")
    headers = trigger(result["text"]) if op in ("save", "remove") and result["ok"] else None
    return _mode_card(request, ctx, headers)


@router.post("/settings/computer/free", response_class=HTMLResponse)
async def free_save(request: Request):
    """Free models only (OpenRouter). On needs the training tick; without it nothing is written
    and the card comes back with the ticks as they were. On with the tick goes through the one
    connect path (the saved key, no new key, run mode untouched) so the tiers really use free
    models. Off stores off and leaves the picks."""
    from kestrel.services.hosted_connect import connect_provider
    from kestrel.web.pages.settings import _active_profile_name

    form = await request.form()
    on = "on" in form.getlist("free_only")
    train = "on" in form.getlist("allow_training")
    ticks = {"free_only": on, "allow_training": train}

    async def redraw(text: str, ok: bool, *, keep: bool = False):
        ctx = await mode_ctx(selected="openrouter", free_message=text, free_ok=ok,
                             free_form=ticks if keep else None)
        return _mode_card(request, ctx)

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        if not on:
            await io.write(s, "llm.hosted_mode.free_only", False)
            await s.commit()
            return await redraw("Free models only is off. Your current models stay; "
                                "change them on Models.", True)
        if not train:
            return await redraw(
                "Free models may train on what Kestrel sends. Tick the box to allow that, or "
                "leave this off. Nothing was changed.", False, keep=True)
        if not settings.llm.hosted_mode.acknowledgement_current:
            return await redraw(
                "Approve what is sent to an online model on Models first. Nothing was "
                "changed.", False, keep=True)
        out = await connect_provider(
            s, settings, provider="openrouter", api_key="", acknowledged=True, free_only=True,
            switch=False, profile=_active_profile_name(), allow_training=True)
    if not out.ok:
        return await redraw(out.message, False, keep=True)
    return await redraw(out.headline, True)


# ---------------------------------------------------------------------------- the checklist
async def checklist_ctx(message: str = "", ok: bool = True) -> dict:
    from kestrel.web.pages.setup import action_ctx, models_snapshot, setup_state

    st = await setup_state()
    snap = await models_snapshot()
    act = action_ctx(snap)
    cat = profiles.load_catalog()
    items = snap.get("items", [])
    pulls = [i for i in items if i["kind"] == "pull"]
    pct = next((i["percent"] for i in pulls if i.get("percent") is not None
                and i["status"] in ("pending", "leased")), None)
    live = bool(snap.get("live"))
    kind = snap.get("kind")
    steps = {s.key: s for s in st.steps}
    n_total = len(st.base_tags)
    n_ready = n_total - len(st.missing_tags) if st.ollama_version else 0
    gb = sum(cat.model_sizes_gb.get(t, 0) for t in st.missing_tags)
    verify = act["action"]
    verify_running = bool(verify and verify.get("status") == "running")
    built = len(st.derived_names) - len(st.missing_derived)

    def tone(done: bool, now: bool = False) -> str:
        return "good" if done else ("info" if now else "muted")

    engine_ok = st.ollama_version is not None
    dl_done = engine_ok and not st.missing_tags
    tune_done = dl_done and not st.missing_derived
    check_done = tune_done and st.verified == "ok"
    rows = [
        {"key": "engine", "title": "Install the engine",
         "line": (f"Ollama is running. Version {st.ollama_version}." if engine_ok else
                  "Ollama is the free program that runs the models on this computer. "
                  "Install it like any other program, open it, then press Check again."),
         "done": engine_ok, "chip": ("Done", "good") if engine_ok else ("Needs you", "warn"),
         "button": ("Check again", "engine"), "primary": False,
         "link": None if engine_ok else (ss.ollama_download_url(st.hardware.os),
                                         "Download Ollama"),
         "commands": (steps["ollama"].commands if "ollama" in steps else []),
         "show_progress": False},
        {"key": "download", "title": "Download models",
         "line": (f"{n_ready} of {n_total} ready." if engine_ok else "Needs the engine first.")
                 + (f" About {gb:.1f} GB to download once." if st.missing_tags and engine_ok
                    and not live else "")
                 + (f" Getting the last one, {pct}%." if live and kind == "pull"
                    and pct is not None else ""),
         "done": dl_done,
         "chip": (("Done", "good") if dl_done else ("Downloading", "info")
                  if live and kind == "pull" else ("Up next", "muted")),
         "button": (("Pause", "pause") if live and kind == "pull" else
                    ("Download", "download") if st.missing_tags else ("Check again", "engine")),
         "primary": bool(engine_ok and st.missing_tags and not live),
         "disabled": not engine_ok, "pct": pct if live and kind == "pull" else None,
         "show_progress": live and kind == "pull",
         "note": "Pause now and resume on a faster network. It picks up where it stopped."
                 if live and kind == "pull" else
                 ("Downloads resume if they are interrupted." if st.missing_tags else ""),
         "commands": [f"ollama pull {t}" for t in st.missing_tags]},
        {"key": "tune", "title": "Tune them for Kestrel",
         "line": ("Gives each model room for a whole job posting, so nothing is cut off."
                  + (f" {built} of {len(st.derived_names)} done." if dl_done else "")),
         "done": tune_done,
         "chip": (("Done", "good") if tune_done else ("Tuning", "info")
                  if live and kind == "build" else ("Up next", "muted")),
         "button": ("Tune", "tune") if not tune_done else ("Tune again", "tune"),
         "primary": bool(dl_done and not tune_done and not live),
         "disabled": not dl_done or live, "show_progress": live and kind == "build",
         "pct": None, "commands": steps["build"].commands if "build" in steps else []},
        {"key": "check", "title": "Check everything works",
         "line": ("Tests that Kestrel reads whole postings. " + st.verified_detail
                  if st.verified != "none" else "Tests that Kestrel reads whole postings."),
         "done": check_done,
         "chip": (("Done", "good") if check_done else ("Failed", "warn")
                  if st.verified == "failed" else ("Checking", "info") if verify_running
                  else ("Up next", "muted")),
         "button": ("Run check", "check"), "primary": bool(tune_done and not check_done),
         "disabled": not engine_ok or verify_running, "link2": ("/bench", "Open Model check"),
         "commands": ["kestrel doctor"], "show_progress": False},
    ]
    n_done = sum(1 for r in rows if r["done"])
    cards_profiles = [
        {"name": n, "label": cat.profile(n).description, "current": n == st.profile,
         "hosted": cat.profile(n).hosted}
        for n in cat.names()
    ]
    return {
        "ck_rows": rows, "ck_done": n_done, "ck_total": len(rows),
        "ck_live": live or verify_running,
        "ck_message": message, "ck_ok": ok, "hardware_line": st.hardware_line,
        "recommended_online": st.recommended == "hosted_only",
        "recommended_reason": st.recommended_reason, "ck_profiles": cards_profiles,
        "ck_profile": st.profile, "ck_choice": st.choice,
        "ck_low_spec": st.low_spec_warning, "ck_log": (verify or {}).get("lines", [])[-8:]
        if verify else [], "ck_profile_code": st.profile,
        "ck_models": [
            {"tag": t, "ready": t not in st.missing_tags} for t in st.base_tags
        ],
    }


def _ck_card(request: Request, ctx: dict) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_checklist.html", ctx)


@router.get("/settings/computer/checklist", response_class=HTMLResponse)
async def checklist(request: Request):
    return _ck_card(request, await checklist_ctx())


@router.post("/settings/computer/action/{name}", response_class=HTMLResponse)
async def checklist_action(request: Request, name: str):
    from kestrel.web.pages.setup import start_action

    if name not in ACTIONS:
        return HTMLResponse("Unknown action.", status_code=404)
    state, message, status = await start_action(ACTIONS[name])
    if name == "pause" and not message:
        message = "Paused. Press Download to continue where it stopped."
    return _ck_card(request, await checklist_ctx(message or "", status is None))


@router.post("/settings/computer/profile", response_class=HTMLResponse)
async def profile_save(request: Request):
    form = await request.form()
    name = str(form.get("profile") or "").strip()
    try:
        await asyncio.to_thread(ss.record_profile, name)
        msg, ok = "Saved.", True
    except KeyError:
        msg, ok = "That is not one of the choices shown.", False
    except OSError as exc:
        msg, ok = f"Could not save that: {plain_error(exc)}", False
    return _ck_card(request, await checklist_ctx(msg, ok))


# --------------------------------------------------------------------- everyday options
async def option_state() -> dict:
    async with sessionmaker()() as s:
        awake = await keep_awake.load(s)
        settings = await load_settings_with_db(s)
    return {
        "awake": awake, "autostart": await asyncio.to_thread(autostart.is_enabled),
        "tray": settings.ui.close_to_tray, "updates": settings.updates.enabled,
        "windows": autostart.platform_name() == "windows",
        "platform": autostart.platform_name(),
    }


@router.post("/settings/computer/option")
async def option_save(request: Request) -> Response:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    key = str(body.get("key") or "")
    on = bool(body.get("on"))
    if key == "awake":
        async with sessionmaker()() as s:
            prev = await keep_awake.load(s)
            await keep_awake.save(s, on)
            await s.commit()
        problem = keep_awake.apply(on)
        msg = ("Kestrel will keep this computer awake during runs." if on
               else "This computer can sleep during runs.")
        if problem and on:
            msg = f"Saved, but {problem}."
        return io.reply(msg, undo=_toggle_undo("runs.keep_awake", prev, on))
    if key == "autostart":
        problem = await asyncio.to_thread(autostart.set_enabled, on)
        if problem:
            return io.refuse(problem, value=not on)
        return io.reply("Kestrel will start with your computer." if on
                        else "Kestrel will not start with your computer.",
                        undo="/settings/computer/option/undo?key=autostart&on=" + ("0" if on
                                                                                    else "1"))
    if key == "tray":
        async with sessionmaker()() as s:
            prev = await io.read(s, CLOSE_TO_TRAY_KEY)
            await io.write(s, CLOSE_TO_TRAY_KEY, on)
            await s.commit()
        return io.reply("Kestrel will keep running in the tray." if on
                        else "Kestrel will quit when you close the window.",
                        undo=io.undo_url(CLOSE_TO_TRAY_KEY, prev, on))
    if key == "updates":
        async with sessionmaker()() as s:
            prev = await io.read(s, "updates.enabled")
            await io.write(s, "updates.enabled", on)
            await s.commit()
        return io.reply("Kestrel will check for updates." if on
                        else "Kestrel will not check for updates.",
                        undo=io.undo_url("updates.enabled", prev, on))
    return io.refuse("That option is not one Kestrel has.")


def _toggle_undo(key: str, prev: Any, now: Any) -> str:
    from urllib.parse import urlencode

    return "/settings/computer/option/undo?" + urlencode(
        {"key": key, "prev": io.dumps(prev), "now": io.dumps(now)})


@router.post("/settings/computer/option/undo")
async def option_undo(request: Request) -> Response:
    q = request.query_params
    key = q.get("key", "")
    if key == "autostart":
        on = q.get("on") == "1"
        current = await asyncio.to_thread(autostart.is_enabled)
        if current == on:
            return io.refuse(io.NO_UNDO)
        problem = await asyncio.to_thread(autostart.set_enabled, on)
        return io.refuse(problem) if problem else io.reply("Put back.")
    if key == "runs.keep_awake":
        try:
            prev, now = io.loads(q.get("prev", "absent")), io.loads(q.get("now", "absent"))
        except ValueError:
            return io.refuse(io.NO_UNDO)
        async with sessionmaker()() as s:
            if (await io.read(s, key)) != now:
                return io.refuse(io.NO_UNDO)
            await io.write(s, key, prev)
            await s.commit()
        keep_awake.apply(bool(prev) if prev is not io.ABSENT else False)
        return io.reply("Put back.")
    return io.refuse(io.NO_UNDO)


