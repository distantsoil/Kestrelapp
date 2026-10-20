"""Settings (SPEC-INTERFACES 2.1). Models per tier with a test button, concurrency, thresholds,
scoring weights per track with proposed recalibration to accept or reject, profile editors, the
filter-rule editor WITH the "test against the last 200 opportunities" preview (how many each rule
would reject), learning settings, model status, notifications, confidential mode, LAN binding with
a generated shared secret, and backup/restore.

The filter preview and the weight-recalibration approval both touch correctness, so this page is
built by the parent, not the fan-out. Settings changes are written to the `setting` table (the
highest-precedence config layer), so they take effect on the next worker cycle with no restart."""

from __future__ import annotations

import asyncio
import contextlib
import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import config as config_mod
from kestrel import paths
from kestrel.config import HOSTED_TIER_SENDS, UI_SCHEMES, Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Calibration, Opportunity, Org, Setting, Source
from kestrel.learning.calibration import accept_calibration, reject_calibration
from kestrel.pipeline.profile import load_profile
from kestrel.rules import helpers
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.auth import SETTING_KEY as SHARED_SECRET_KEY
from kestrel.web.deps import sessionmaker, set_ui_scheme_cache, templates
from kestrel.web.errors import plain_error
from kestrel.web.uploads import closing_upload

# Settings > Appearance (docs/UI_PROPOSAL.html): the two shipped schemes with their swatches.
# Light/dark/system is a per-browser choice (localStorage) and is never stored server-side.
SCHEME_CARDS: tuple[dict, ...] = (
    {
        "id": "indigo", "name": "Paper and Indigo",
        "blurb": "Warm paper in light, true charcoal in dark, indigo accent. The default.",
        "swatches": ("#FFFFFF", "#FAF9F6", "#4F5BD5", "#1B1B1F", "#141417", "#7B86F0"),
    },
    {
        "id": "teal", "name": "Graphite and Teal",
        "blurb": "Neutral graphite with a calm teal accent. Verdict colors stay the same.",
        "swatches": ("#FFFFFF", "#F4F6F7", "#0F8F7E", "#17191D", "#111316", "#2CC4AE"),
    },
)
UI_SCHEME_KEY = "ui.scheme"


async def _filter_preview(session: AsyncSession, settings: Settings, sample: int = 200) -> dict:
    """For each hard-blocker rule, how many of the last `sample` opportunities it would reject:
    what the operator sees before changing filters.yml, the real effect on real data per rule."""
    filters = helpers.load_filters(str(paths.config_file("filters.yml")))
    opps = (
        (
            await session.execute(
                select(Opportunity).order_by(Opportunity.first_seen_at.desc()).limit(sample)
            )
        )
        .scalars()
        .all()
    )
    profiles = {t: load_profile(t) for t in ("employment", "consulting")}
    orgs: dict[int, Org] = {}
    sources: dict[int, Source] = {}
    out: dict[str, list[dict]] = {}
    for track in ("employment", "consulting"):
        track_opps = [o for o in opps if o.track == track]
        rules = (filters.get(track) or {}).get("hard_blockers", []) or []
        counts = {r.get("id", "?"): 0 for r in rules}
        for opp in track_opps:
            org = orgs.get(opp.org_id) if opp.org_id else None
            if opp.org_id and org is None:
                org = orgs[opp.org_id] = await session.get(Org, opp.org_id)
            src = sources.get(opp.source_id) if opp.source_id else None
            if opp.source_id and src is None:
                src = sources[opp.source_id] = await session.get(Source, opp.source_id)
            ev = helpers.build_evaluator(
                opp=opp, org=org, profile=profiles.get(track), source=src, settings=settings
            )
            for rule in rules:
                try:
                    if ev.eval(rule["when"]):
                        counts[rule.get("id", "?")] += 1
                except Exception as exc:  # a bad rule shows 0, never breaks the preview
                    _ = exc
        out[track] = [
            {
                "id": r.get("id", "?"),
                "reason": r.get("reason"),
                "rejects": counts[r.get("id", "?")],
                "of_n": len(track_opps),
            }
            for r in rules
        ]
    return out


async def _proposed_calibrations(session: AsyncSession) -> list[Calibration]:
    return list(
        (
            await session.execute(
                select(Calibration).where(Calibration.status == "proposed").order_by(Calibration.id)
            )
        )
        .scalars()
        .all()
    )


router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        preview = await _filter_preview(s, settings)
        calibrations = await _proposed_calibrations(s)
        secret_row = await s.get(Setting, SHARED_SECRET_KEY)
        from kestrel.web.redact import confidential_is_inert

        conf_inert = await confidential_is_inert(s)
        from kestrel.docs.current_employer import choose_href, current_employers

        # D-PRIV-1: the operator's own config screen is the one place that names the employer
        # confidential mode hides, so they can see what it does and change it.
        employers = await current_employers(s) if settings.confidential_mode else []
        employer_choose_href = await choose_href(s)
        from kestrel.services import updater
        from kestrel.web.pages.updates import format_last_checked

        update_last_checked = format_last_checked((await updater.load_state(s)).last_checked_at)
    lan_secret = secret_row.value if secret_row and isinstance(secret_row.value, str) else None
    ctx = {
        "title": "Settings",
        "query": dict(request.query_params),
        "settings": settings,
        "preview": preview,
        "calibrations": calibrations,
        "lan_secret": lan_secret,
        "data_dir": str(paths.data_dir()),
        "confidential_inert": conf_inert,
        "confidential_employers": [e for e in employers if not e["inferred"]],
        "employer_banner": [e for e in employers if e["inferred"]],
        "employer_choose_href": employer_choose_href,
        "update_last_checked": update_last_checked,
        **_appearance_ctx(settings),
    }
    if request.headers.get("HX-Request"):
        return templates().TemplateResponse(request, "partials/filter_preview.html", ctx)
    return templates().TemplateResponse(request, "settings.html", ctx)


@router.get("/settings/filters/preview", response_class=HTMLResponse)
async def filters_preview(request: Request):
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        preview = await _filter_preview(s, settings)
    return templates().TemplateResponse(
        request, "partials/filter_preview.html", {"preview": preview}
    )


async def _set(session: AsyncSession, key: str, value) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def _unset(session: AsyncSession, key: str) -> None:
    row = await session.get(Setting, key)
    if row is not None:
        await session.delete(row)


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request):
    """Persist core settings to the setting table. Only known keys are written; anything else is
    ignored so a stray field can never corrupt config loading."""
    form = await request.form()
    async with sessionmaker()() as s:
        # An unchecked checkbox is not submitted, so a bare `"confidential_mode" in form` could only
        # ever turn the mode ON. The template posts a hidden "off" companion; "on" present means
        # checked, so unchecking + Save correctly writes False and the mode can be turned off.
        await _set(s, "confidential_mode", "on" in form.getlist("confidential_mode"))
        for key in ("concurrency.tier1", "concurrency.tier2"):
            v = form.get(key)
            if v:
                with contextlib.suppress(ValueError):
                    await _set(s, key, int(v))
        # The posting age rules (Tier 0 stale and stale_trigger, D-UI-73): the place a "Set
        # aside by your posting age rule" sentence sends the operator. Whole days, 1 to 3650.
        for key in ("max_posting_age_days", "consulting.max_trigger_age_days"):
            v = str(form.get(key) or "").strip()
            if v.isdigit() and 1 <= int(v) <= 3650:
                await _set(s, key, int(v))
        for key in ("thresholds.keep", "thresholds.shortlist"):
            v = form.get(key)
            if v:
                with contextlib.suppress(ValueError):
                    await _set(s, key, float(v))
        await s.commit()
    return HTMLResponse("Saved. Changes apply on the next worker cycle.")


def _appearance_ctx(settings: Settings) -> dict:
    return {
        "scheme_cards": SCHEME_CARDS,
        "scheme": settings.ui.scheme,
        "close_to_tray": settings.ui.close_to_tray,
    }


@router.post("/settings/desktop", response_class=HTMLResponse)
async def save_desktop(request: Request):
    """Store `ui.close_to_tray` (D-UI-8). The desktop shell reads the row on every window close,
    so the change applies to the next close with no restart."""
    from kestrel.web.deps import CLOSE_TO_TRAY_KEY

    form = await request.form()
    on = "on" in form.getlist("close_to_tray")
    async with sessionmaker()() as s:
        await _set(s, CLOSE_TO_TRAY_KEY, on)
        await s.commit()
    return HTMLResponse(
        "Saved. Closing the window now hides Kestrel to the tray; the worker keeps running."
        if on
        else "Saved. Closing the window now quits Kestrel and stops the worker."
    )


@router.post("/settings/updates", response_class=HTMLResponse)
async def save_updates(request: Request):
    """Store `updates.enabled`. The update loop re-reads it every pass, so no restart is needed."""
    form = await request.form()
    on = "on" in form.getlist("updates_enabled")
    async with sessionmaker()() as s:
        await _set(s, "updates.enabled", on)
        await s.commit()
    return HTMLResponse("Saved." if on else "Saved. Kestrel will not check for updates.")


@router.post("/settings/appearance", response_class=HTMLResponse)
async def save_appearance(request: Request):
    """Store the color scheme as `ui.scheme` so the server renders it on <html> for the first
    paint in every browser. The theme (light/dark/system) is not posted: it stays in the
    browser. An unknown scheme is refused with a 400 and nothing is written, because a bad row
    would fail every settings load (the field is a Literal)."""
    form = await request.form()
    scheme = str(form.get("scheme") or "").strip().lower()
    if scheme not in UI_SCHEMES:
        known = ", ".join(UI_SCHEMES)
        return HTMLResponse(
            f"Unknown scheme {html.escape(scheme or '(empty)')}; expected one of {known}.",
            status_code=400,
        )
    async with sessionmaker()() as s:
        await _set(s, UI_SCHEME_KEY, scheme)
        await s.commit()
    set_ui_scheme_cache(scheme)
    return HTMLResponse(f"Saved. {scheme.capitalize()} is now the default scheme on every device.")


@router.post("/settings/calibration/{calibration_id}/{action}", response_class=HTMLResponse)
async def decide_calibration(calibration_id: int, action: str):
    """Accept or reject a proposed weight recalibration. Never applied silently: only the operator's
    click here changes the live weights."""
    async with sessionmaker()() as s:
        if action == "accept":
            await accept_calibration(s, calibration_id)
            msg = "Recalibration accepted. New weights take effect next cycle."
        elif action == "reject":
            await reject_calibration(s, calibration_id)
            msg = "Recalibration rejected. Weights unchanged."
        else:
            return HTMLResponse("unknown action", status_code=400)
    return HTMLResponse(msg)


@router.post("/settings/restore", response_class=HTMLResponse)
async def restore_from_upload(request: Request):
    """The 'restore from the setup window' the spec asks for: accept an uploaded backup zip and
    restore it via the same kestrel.services.backup logic the CLI uses. A bad upload never 500s --
    a missing file, a non-zip, or a zip that is not a Kestrel backup all come back as a readable
    message in the Backup section instead of a crash."""
    import html
    import sqlite3
    import tempfile
    import zipfile
    from pathlib import Path

    from kestrel.db import migrate
    from kestrel.services.backup import InvalidBackupError, restore_backup
    from kestrel.services.reset import IDLE_WAIT_S, _forget_caches
    from kestrel.web import app as app_mod

    form = await request.form()
    upload = form.get("backup_file")
    if upload is None or not getattr(upload, "filename", ""):
        return HTMLResponse('<div class="warnbox">Choose a backup .zip file first.</div>')

    try:
        # `closing_upload` because the upload owns a spooled temp file that holds an OS handle
        # once it has rolled to disk. Reading it is not closing it, and nothing else will
        # (D-RR1-4). This route is also the one that later replaces kestrel.db, so a handle left
        # open here is a sharing violation against the restore itself.
        async with closing_upload(upload):
            data = await upload.read()
    except OSError as exc:
        log.warning("restore_upload_read_failed", error_type=type(exc).__name__, error=str(exc))
        return HTMLResponse(
            f'<div class="warnbox">Could not read the upload: '
            f"{html.escape(plain_error(exc))}</div>"
        )

    # `InvalidBackupError` messages quote the archive's own entry names, which the uploader fully
    # controls (a rejected member may be named `config/<img src=x onerror=...>/../../x.yml`).
    # Escaped here so a crafted archive cannot execute markup in the app's own origin, which is
    # the origin holding the LAN shared secret and every mutating endpoint. `/backup/download`
    # escapes the same way; documents.py and bench.py do too.
    # The database is written through SQLite while the app's connections stay open (R1), so the
    # pool is NOT torn down here: disposing it used to leave the background loops on the old
    # engine and every new request on a second one, both open on the same file. What must stop is
    # the worker, so a cycle that read the old data does not write its results into the restored
    # data. The hold is counted, so an overlapping reset or a tray Resume cannot release it, and
    # the maintenance lock keeps a second restore (or a reset) from running at the same time.
    lock = app_mod.maintenance_lock()
    if lock.locked():
        return HTMLResponse(
            '<div class="warnbox">A restore or reset is already running, so nothing was restored. '
            "Try again when it finishes.</div>"
        )
    from alembic.util.exc import CommandError
    from sqlalchemy.exc import SQLAlchemyError

    async with lock, app_mod.worker_held():
        if not await app_mod.wait_for_idle_cycle(IDLE_WAIT_S):
            return HTMLResponse(
                '<div class="warnbox">Kestrel is in the middle of a run, so nothing was restored. '
                "Try again in a minute.</div>"
            )
        try:
            with tempfile.TemporaryDirectory(prefix="kestrel-upload-") as tmp:
                tmp_zip = Path(tmp) / "uploaded-backup.zip"
                tmp_zip.write_bytes(data)
                # Extraction, validation and the database write are blocking I/O on a path shared
                # with every request on uvicorn's one loop, so they run in a thread.
                result = await asyncio.to_thread(restore_backup, tmp_zip)
        except InvalidBackupError as exc:
            return HTMLResponse(f'<div class="warnbox">{html.escape(plain_error(exc))}</div>')
        except (OSError, zipfile.BadZipFile, sqlite3.Error) as exc:
            log.warning("restore_failed", error_type=type(exc).__name__, error=str(exc))
            return HTMLResponse(
                f'<div class="warnbox">Restore failed: '
                f"{html.escape(plain_error(exc))}</div>"
            )
        try:
            # A backup from an older Kestrel is on an older schema. Startup migrates; a restore in
            # a running app has to as well. A NEWER backup was refused in staging, so a failure
            # here is a lock or a broken migration; the previous data is in the snapshot.
            await asyncio.to_thread(migrate.run_migrations)
        except (CommandError, SQLAlchemyError, OSError) as exc:
            log.warning("restore_migration_failed", error_type=type(exc).__name__, error=str(exc))
            saved = html.escape(str(result.pre_restore_backup or "the Kestrel-backups folder"))
            return HTMLResponse(
                '<div class="warnbox">The backup was restored, but bringing it up to date with '
                f"this version failed ({html.escape(type(exc).__name__)}). Restart Kestrel to "
                f"retry. Your previous database is saved at {saved}.</div>"
            )
        # Process caches that would otherwise answer from the data just replaced (the restored
        # database carries its own tracks, UI scheme and status counts; restore_backup put this
        # machine's own LAN key back, but the auth cache is reset all the same), and the shipped
        # sources and layouts a backup from an older Kestrel does not have yet. The same
        # put-back the in-app reset does.
        from kestrel.web.auth import reset_cache

        reset_cache()
        _forget_caches()
        await app_mod._seed_shipped_sources()
        await app_mod._seed_shipped_layouts()
        await app_mod._load_enabled_tracks()

    # Escaped: the restored config and document file names come from the archive, which the
    # uploader controls (the same reason InvalidBackupError messages are escaped above).
    lines = "<br>".join(html.escape(line) for line in result.summary_lines())
    return HTMLResponse(
        f'<div class="muted">Restore complete. Kestrel is using the restored data now.<br>{lines}'
        "</div>"
    )


def _copy_row(value: str, *, code_testid: str, button_testid: str, label: str) -> str:
    """A value shown as selectable text with a one-click Copy beside it (#26). Escaped for both
    the text and the attribute. Deliberately not an anchor: the join URL carries the secret, and a
    click would land it in the desktop browser's history (D-UI-79)."""
    safe = html.escape(value, quote=True)
    return (
        '<div class="copyrow">'
        f'<code class="mono" data-testid="{code_testid}">{safe}</code>'
        f'<button type="button" class="btn ghost small" data-testid="{button_testid}" '
        f'data-path="{safe}" @click="navigator.clipboard.writeText($el.dataset.path)" '
        f'title="Copy {label}">Copy</button></div>'
    )


@router.post("/settings/lan/secret", response_class=HTMLResponse)
async def generate_secret(request: Request):
    """Generate (or rotate) the LAN shared secret and show the QR a phone can scan to join.

    The secret is now genuinely enforced: kestrel/web/auth.py requires it on every non-loopback
    request. Rotating it here takes effect immediately, with no restart, and instantly locks out any
    device holding the previous key, which is the only revocation mechanism a single shared
    credential can offer.
    """
    from kestrel.web.auth import generate_key, join_url, lan_host_ip, qr_svg, set_cached_key

    token = generate_key()
    async with sessionmaker()() as s:
        await _set(s, SHARED_SECRET_KEY, token)
        await s.commit()
    set_cached_key(token)  # the middleware reads the cache, so rotation is live at once

    # Everything past this point is presentation. The rotation is already committed, so a failure
    # here must still show the operator the new secret: 500ing after a commit would lock out every
    # joined device AND withhold the key needed to rejoin.
    try:
        host = lan_host_ip()
        port = request.url.port or 8765
    except (ValueError, OSError):
        host, port = None, 8765
    secret_row = _copy_row(
        token, code_testid="lan-secret", button_testid="copy-secret", label="the secret"
    )
    if host is None:
        return HTMLResponse(
            f"{secret_row}"
            '<p class="muted">No LAN address was detected, so there is no join link to show. '
            "Pass the key as the X-Kestrel-Key header.</p>"
        )
    url = join_url(host, port, token)
    svg = qr_svg(url)
    qr_block = (
        f'<div class="qr" data-testid="lan-qr">{svg}</div>'
        if svg
        else '<p class="muted">QR rendering is unavailable; use the link below.</p>'
    )
    url_row = _copy_row(
        url, code_testid="join-url", button_testid="copy-join-url", label="the join link"
    )
    return HTMLResponse(
        f"{secret_row}"
        f"{qr_block}"
        '<p class="muted">Scan the code on a phone that is on the same network, or copy the join '
        "link below and paste it into that phone's browser once. Any device holding the previous "
        "key has just been locked out.</p>"
        f"{url_row}"
    )


# ------------------------------------------------------------------ hosted provider (BYOK)
# The one place a hosted key is entered, and the one place Hosted mode (SPEC 6.4) is turned on:
# an explicit, acknowledged opt-in that lets any tier use an external backend. The key goes to
# the data-home .env; everything else is a setting-table row.
import structlog  # noqa: E402

from kestrel.llm import providers as prov  # noqa: E402
from kestrel.services.hosted_connect import FREE_PROVIDERS, provider_order, tier_label  # noqa: E402

log = structlog.get_logger()

# What each hostable tier transmits once it is routed to a hosted backend. Shown in the
# disclosure and beside each row of the per-tier table; keep it exact.
_TIER_SENDS: dict[str, str] = HOSTED_TIER_SENDS


def _tier_sends(settings: Settings) -> dict[str, str]:
    # One source with the acknowledgement record (config.HostedMode.disclosure_terms), so
    # the text shown is the text an acknowledgement is checked against.
    return dict(settings.llm.hosted_mode.disclosure_terms()["sends"])


def _local_model_hint(tier: str) -> str | None:
    """The active hardware profile's base model for a local tier, so the operator sees what the
    derived `kestrel-<tier>` model is built from. None when no profile was chosen."""
    from kestrel.llm import profiles

    active = profiles.load_active_profile()
    if active is None:
        return None
    try:
        return profiles.load_catalog().profile(active.profile).models.for_tier(tier)
    except (KeyError, AttributeError, OSError, ValueError):
        return None


def _active_profile_name() -> str | None:
    from kestrel.llm import profiles

    active = profiles.load_active_profile()
    return active.profile if active else None


async def _mode_cards(session: AsyncSession, settings: Settings) -> dict:
    """What the two switch buttons say under themselves. The Local card reads the LOCAL tier set
    (settings resolved with mode local, since under mode hosted `tiers` is already overlaid);
    the Hosted card reads the hosted assignments."""
    from kestrel.config import hosted_mode_problem

    local = await load_settings_with_db(session, mode="local")
    local_models = {
        t: local.llm.tiers[t].model for t in ("tier1", "tier2", "docsel") if t in local.llm.tiers
    }
    # The backend each local-set tier actually names, so a legacy external row (llm.tiers.<tier>
    # .backend=frontier, written before D-HOSTED-2) is visible on the Local card, not hidden.
    local_backends = {t: local.llm.tiers[t].backend for t in local_models}
    assignments = prov.assignment_status(settings)
    hosted_rows = [r for r in assignments if r.external and r.tier != prov.FRONTIER_TIER]
    providers = {
        next((p["label"] for p in prov.PRESETS.values() if p["backend"] == r.backend), r.backend)
        for r in hosted_rows
    }
    return {
        "mode": settings.llm.mode,
        "hosted_problem": hosted_mode_problem(settings),
        "local_profile": _active_profile_name(),
        "local_models": local_models,
        "local_backends": local_backends,
        "hosted_models": {r.tier: r.model for r in hosted_rows},
        "hosted_providers": sorted(providers),
        "assignment_rows": assignments,
    }


async def _hosted_ctx(session: AsyncSession, settings: Settings, **extra) -> dict:
    status = prov.provider_status(settings)
    backends = {
        name: {
            "label": next(
                (p["label"] for p in prov.PRESETS.values() if p["backend"] == name), name
            ),
            "provider": b.provider,
            "protocol": b.protocol,
            "base_url": b.base_url,
            "env": b.api_key_env,
            "key_present": bool(b.api_key) or prov.secret_present(b.api_key_env),
            "key_tail": key_tail(b),
            "inline_key": bool(b.api_key),
            "hosted": b.hosted,
        }
        for name, b in prov.external_backends(settings).items()
    }
    tier_rows = prov.tier_status(settings)
    local_hints = {t: _local_model_hint(t) for t in prov.HOSTED_TIERS if t != prov.FRONTIER_TIER}
    models = extra.get("models") or []
    extra.setdefault("free_ids", {m.id for m in prov.free_models(models)})
    # The checkbox shows the standing choice (the last Connect's), not an empty box each time.
    extra.setdefault("free_only", settings.llm.hosted_mode.free_only)
    # "setup" when the operator came from the Setup page's step 6 (D-UI-11): the partial then
    # carries the value through its forms and offers "Return to setup" once connected.
    extra["return_to"] = _return_to(extra.get("return_to"))
    return {
        "hosted": status,
        "hosted_backends": backends,
        "effort_levels": prov.EFFORT_LEVELS,
        "presets": prov.PRESETS,
        "preset_order": provider_order(),
        "free_providers": FREE_PROVIDERS,
        "system_keys_ignored": sorted(
            b["env"] for b in backends.values() if config_mod.system_secret_ignored(b["env"])
        ),
        "kestrel_keys": sorted(
            b["env"] for b in backends.values() if config_mod.secret_saved_in_kestrel(b["env"])
        ),
        "use_system_keys": config_mod.use_system_keys(),
        "settings_file": paths.config_file("settings.yml"),
        "connect_default": next(
            (pid for pid, p in prov.PRESETS.items() if p["backend"] == status.backend_name),
            "openrouter",
        ),
        "hosted_mode": settings.llm.hosted_mode,
        "tier_rows": tier_rows,
        "tier_sends": _tier_sends(settings),
        "local_hints": local_hints,
        "confidential_mode": settings.confidential_mode,
        **(await _mode_cards(session, settings)),
        **extra,
    }


def key_tail(backend) -> str:
    """The last four characters of a saved key, the only part ever rendered (release round R1).
    Empty when no key is saved or it is too short to show any of it safely."""
    value = backend.api_key or prov.secret_value(backend.api_key_env)
    if not value or len(value) <= 8:
        return ""
    return value[-4:]


def _hosted_partial(request: Request, ctx: dict) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/hosted_status.html", ctx)


def _return_to(value: object) -> str:
    """Only "setup" is a known origin; anything else is dropped rather than echoed into a link."""
    return "setup" if value == "setup" else ""


@router.get("/settings/hosted", response_class=HTMLResponse)
async def hosted_status(request: Request):
    back = request.query_params.get("return_to")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        ctx = await _hosted_ctx(s, settings, return_to=back)
    return _hosted_partial(request, ctx)


@router.post("/settings/hosted/key", response_class=HTMLResponse)
async def hosted_save_key(request: Request):
    """Store (or clear, when blank) one provider's key in the data-home .env. The value is never
    echoed back and never logged; the partial shows only that a key is present."""
    form = await request.form()
    backend_name = str(form.get("backend") or "")
    value = str(form.get("api_key") or "").strip()
    back = form.get("return_to")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        backend = prov.external_backends(settings).get(backend_name)
        if backend is None or not backend.api_key_env:
            ctx = await _hosted_ctx(
                s, settings, message="Unknown hosted backend.", ok=False, return_to=back
            )
            return _hosted_partial(request, ctx)
        try:
            prov.write_secret(backend.api_key_env, value)
        except ValueError as exc:
            ctx = await _hosted_ctx(s, settings, message=plain_error(exc), ok=False, return_to=back)
            return _hosted_partial(request, ctx)
        settings = await load_settings_with_db(s)
        label = _backend_label(backend_name)
        msg = (
            f"{label} key saved. Test it below."
            if value
            else f"{label} key removed."
        )
        ctx = await _hosted_ctx(s, settings, message=msg, ok=True, return_to=back)
    return _hosted_partial(request, ctx)


def _backend_label(name: str) -> str:
    return next((p["label"] for p in prov.PRESETS.values() if p["backend"] == name), name)


@router.post("/settings/hosted/keybox", response_class=HTMLResponse)
async def hosted_keybox(request: Request):
    """Your keys (release round R1): Test a pasted key (or the saved one when the box is blank)
    with one cheap authenticated call, Save it, or Remove it. The key value is never echoed,
    never logged and never rendered back; the page shows only its last four characters. Test
    writes nothing."""
    form = await request.form()
    pid = str(form.get("provider") or "")
    op = str(form.get("op") or "test")
    value = str(form.get("api_key") or "").strip()
    back = form.get("return_to")
    preset = prov.PRESETS.get(pid)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        backend = prov.external_backends(settings).get(preset["backend"]) if preset else None
        if preset is None or backend is None or not backend.api_key_env:
            ctx = await _hosted_ctx(s, settings, return_to=back,
                                    key_result={"ok": False, "text": "Choose a provider."})
            return _hosted_partial(request, ctx)
        label = preset["label"]
        result = await _keybox_op(op, backend, label, value)
        log.info("hosted_key_action", provider=pid, op=op, ok=result["ok"],
                 pasted=bool(value))
        settings = await load_settings_with_db(s)
        ctx = await _hosted_ctx(s, settings, return_to=back, key_result=result)
    return _hosted_partial(request, ctx)


async def _keybox_op(op: str, backend, label: str, value: str) -> dict:
    """Run one Your keys button. Returns {"ok", "text"}; the text never holds the key or a key
    name."""
    saved = bool(backend.api_key) or prov.secret_present(backend.api_key_env)
    if op == "remove":
        try:
            prov.write_secret(backend.api_key_env, "")
        except ValueError as exc:
            return {"ok": False, "text": f"The {label} key was not removed: {plain_error(exc)}."}
        return {"ok": True, "text": f"{label} key removed."}
    if not value and not saved:
        return {"ok": False, "text": f"Paste your {label} key first."}
    if op == "save":
        if not value:
            return {"ok": False, "text": f"Paste your {label} key first. The saved one is "
                    "unchanged."}
        try:
            prov.write_secret(backend.api_key_env, value)
        except ValueError as exc:
            return {"ok": False, "text": f"The {label} key was not saved: {plain_error(exc)}."}
        tail = key_tail(backend.model_copy(update={"api_key": value}))
        ending = f", ending in {tail}" if tail else ""  # a key of 8 or fewer shows none of it
        return {"ok": True, "text": f"{label} key saved{ending}. Press Test "
                "to check it, or connect below to pick models."}
    candidate = backend.model_copy(update={"api_key": value}) if value else backend
    status = await prov.verify_key(candidate)
    which = "This key" if value else "Your saved key"
    if status.ok:
        tail = " Press Save to keep it." if value else ""
        extra = " It is on OpenRouter's free tier (daily limits apply)." if status.is_free_tier \
            else ""
        return {"ok": True, "text": f"{which} works with {label}.{extra}{tail}"}
    detail = _scrub(str(status.detail or "rejected"), value)
    return {"ok": False, "text": f"{label} did not accept {which.lower()}: {detail}. Nothing was "
            "saved."}


def _scrub(text: str, value: str) -> str:
    """Belt and braces: never let a pasted key ride back in a provider's error text."""
    if value and len(value) >= 4:
        text = text.replace(value, "****")
    return text[:300]


@router.post("/settings/hosted/test", response_class=HTMLResponse)
async def hosted_test(request: Request):
    """Verify the key with the provider and fetch the models that can honor a JSON schema. This is
    the only network call on the page, it sends no operator data, and it is operator-triggered."""
    form = await request.form()
    backend_name = str(form.get("backend") or "")
    free_only = "on" in form.getlist("free_only")
    back = form.get("return_to")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        backend = prov.external_backends(settings).get(backend_name)
        if backend is None:
            ctx = await _hosted_ctx(
                s, settings, message="Unknown hosted backend.", ok=False, return_to=back
            )
            return _hosted_partial(request, ctx)
        key = await prov.verify_key(backend)
        models: list = []
        model_error = None
        if key.ok:
            try:
                models = prov.models_fit_for_tier3(await prov.list_models(backend), backend)
            except (RuntimeError, ValueError, KeyError) as exc:
                log.info("hosted_models_list_failed", backend=backend_name,
                         error_type=type(exc).__name__, error=str(exc))
                model_error = f"{type(exc).__name__}: {plain_error(exc)}"
        if free_only:
            # Zero-priced for both prompt and completion. Only OpenRouter reports pricing, so
            # on any other backend this filter empties the list and the partial says so.
            models = prov.free_models(models)
        models.sort(key=lambda m: m.id)
        ctx = await _hosted_ctx(
            s,
            settings,
            key=key,
            models=models,
            model_error=model_error,
            tested_backend=backend_name,
            free_only=free_only,
            message=key.detail,
            ok=key.ok,
            return_to=back,
        )
    return _hosted_partial(request, ctx)


@router.post("/settings/hosted/connect", response_class=HTMLResponse)
async def hosted_connect(request: Request):
    """The one-form path (D-UI-7): kestrel.services.hosted_connect.connect_provider verifies the
    key, picks a model per tier and writes the rows; the onboarding model step calls the same
    service (D-UI-30). Nothing is written until the provider accepts the key."""
    from kestrel.services.hosted_connect import connect_provider

    form = await request.form()
    provider = str(form.get("provider") or "")
    free_only = "on" in form.getlist("free_only")
    back = form.get("return_to")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        out = await connect_provider(
            s,
            settings,
            provider=provider,
            api_key=str(form.get("api_key") or ""),
            acknowledged="on" in form.getlist("ack"),
            free_only=free_only,
            switch="on" in form.getlist("switch"),
            profile=_active_profile_name(),
            allow_training="on" in form.getlist("allow_training"),
        )
        if out.ok:
            settings = await load_settings_with_db(s)
        extra = {"key": out.key} if out.key is not None else {}
        ctx = await _hosted_ctx(
            s,
            settings,
            message=out.message,
            ok=out.ok,
            connect_result=out,
            connect_provider=provider,
            free_only=free_only,
            return_to=back,
            **extra,
        )
    return _hosted_partial(request, ctx)


@router.post("/settings/hosted/mode", response_class=HTMLResponse)
async def hosted_mode_toggle(request: Request):
    """Turn Hosted mode on (only with the disclosure acknowledged; writes enabled AND
    acknowledged_at) or off (clears enabled; the acknowledgement stays as a record). No other
    row changes: which tiers go hosted is the per-tier table's job."""
    form = await request.form()
    action = str(form.get("action") or "")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        if action == "on":
            if "on" not in form.getlist("ack"):
                ctx = await _hosted_ctx(
                    s, settings, ok=False,
                    message="Tick \"I understand\" after reading the disclosure to turn on "
                    "Hosted mode.",
                )
                return _hosted_partial(request, ctx)
            now = utcnow_iso()
            await _set(s, "llm.hosted_mode.enabled", True)
            await _set(s, "llm.hosted_mode.acknowledged_at", now)
            # What was accepted, so a later change to the disclosure voids this acceptance.
            await _set(
                s, "llm.hosted_mode.acknowledged_terms",
                settings.llm.hosted_mode.disclosure_terms(),
            )
            msg = (
                f"Disclosure acknowledged ({now}). Switch to hosted models above once tier1 and "
                "tier2 have hosted assignments; a hosted send happens only on a hosted run. "
                "No restart needed."
            )
        elif action == "off":
            await _set(s, "llm.hosted_mode.enabled", False)
            await _set(s, "llm.mode", "local")
            msg = (
                "Hosted mode is off and Kestrel is back on local models. Your hosted "
                "assignments are kept; they are not used until you turn it back on."
            )
        else:
            ctx = await _hosted_ctx(s, settings, message="Unknown action.", ok=False)
            return _hosted_partial(request, ctx)
        await s.commit()
        settings = await load_settings_with_db(s)
        ctx = await _hosted_ctx(s, settings, ok=True, message=msg)
    return _hosted_partial(request, ctx)


@router.post("/settings/hosted/switch", response_class=HTMLResponse)
async def hosted_switch(request: Request):
    """The Local / Hosted switch: write `llm.mode`. "hosted" is refused, with what is missing,
    until the disclosure is acknowledged and tier1 and tier2 have hosted assignments
    (config.hosted_mode_problem, the same rule the CLI and Run now apply). The local tier set
    is never touched by either choice, so switching back costs nothing."""
    from kestrel.config import MODES, hosted_mode_problem

    form = await request.form()
    mode = str(form.get("mode") or "")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        if mode not in MODES:
            ctx = await _hosted_ctx(s, settings, message="Unknown mode.", ok=False)
            return _hosted_partial(request, ctx)
        if mode == "hosted":
            problem = hosted_mode_problem(settings)
            if problem is not None:
                ctx = await _hosted_ctx(
                    s, settings, ok=False, message=f"Still on local models: {problem}."
                )
                return _hosted_partial(request, ctx)
        await _set(s, "llm.mode", mode)
        moved: list[str] = []
        stuck: list[str] = []
        if mode == "local":
            moved, stuck = await _unroute_legacy_external_tiers(s)
        await s.commit()
        settings = await load_settings_with_db(s)
        msg = (
            "Kestrel now runs on hosted models: every run and the serve loop's next cycle use "
            "the hosted assignments below and send automatically. No restart needed."
            if mode == "hosted"
            else "Kestrel now runs on local models. Hosted assignments are kept and not used "
            "until you switch back or choose hosted for one run."
        )
        if moved:
            msg += (
                " Also moved back to the local model: " + ", ".join(moved) + " (a setting row "
                "from before the Local/Hosted switch pointed the local tier set at a hosted "
                "backend; it was removed so nothing sends on a local run)."
            )
        if stuck:
            msg += (
                " Still on a hosted backend: " + ", ".join(stuck) + " (named in settings.yml "
                "itself; edit it or pick a local backend there)."
            )
        ctx = await _hosted_ctx(s, settings, ok=not stuck, message=msg)
    return _hosted_partial(request, ctx)


async def _unroute_legacy_external_tiers(s: AsyncSession) -> tuple[list[str], list[str]]:
    """Rows written before D-HOSTED-2 named the effective tier directly (`llm.tiers.<tier>.backend
    = frontier`), so a plain `llm.mode = local` left that tier sending. Drop those override rows so
    the YAML (the profile's local model) applies again; the frontier tier is hosted by design and is
    never touched. Returns (tiers moved back, tiers still external after the rows are gone)."""
    local = await load_settings_with_db(s, mode="local")
    legacy = [
        r.tier
        for r in prov.tier_status(local)
        if r.external and r.tier != prov.FRONTIER_TIER
    ]
    for tier in legacy:
        await _unset(s, f"llm.tiers.{tier}.backend")
        await _unset(s, f"llm.tiers.{tier}.model")
    if not legacy:
        return [], []
    await s.flush()
    after = await load_settings_with_db(s, mode="local")
    still = {r.tier for r in prov.tier_status(after) if r.external and r.tier in legacy}
    moved = [f"{t}: {after.llm.tiers[t].backend}/{after.llm.tiers[t].model}" for t in legacy
             if t not in still]
    return moved, sorted(still)


@router.post("/settings/hosted/tiers", response_class=HTMLResponse)
async def hosted_save_tiers(request: Request):
    """Write each tier's HOSTED assignment (`llm.hosted_tiers.<tier>.backend` and `.model`,
    setting-table rows, no restart). A tier set to "stay local" loses its assignment rows and
    runs locally in either mode; the local tier set (`llm.tiers`) is never written here. The
    frontier tier is hosted in both modes and is written under `llm.tiers` as before.

    The form posts every tier, so only a tier whose posted value differs from what it runs now
    is a change: an untouched row (a "Set elsewhere" tier included) is neither rewritten nor
    turned into an override because another row was saved."""
    from kestrel.config import hosted_mode_problem
    from kestrel.services.hosted_models import STATE_LOCK, note_manual_assignments

    form = await request.form()
    async with STATE_LOCK, sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        externals = prov.external_backends(settings)
        now = {st.tier: st for st in prov.assignment_status(settings)}
        problems: list[str] = []
        changed: list[str] = []
        manual: dict[str, tuple[str, str] | None] = {}
        for tier in prov.HOSTED_TIERS:
            backend_name = str(form.get(f"backend.{tier}") or "").strip()
            model = str(form.get(f"model.{tier}") or "").strip()[:200]
            if not backend_name:
                continue
            cur = now.get(tier)
            if cur is not None and (
                (backend_name == "local" and not cur.external)
                or (cur.external and (backend_name, model) == (cur.backend, cur.model))
            ):
                continue  # unchanged: leave the row and its Default/Override state alone
            prefix = "llm.tiers" if tier == prov.FRONTIER_TIER else "llm.hosted_tiers"
            if backend_name == "local":
                if tier == prov.FRONTIER_TIER:
                    problems.append("deeper reads must stay on an online model service")
                    continue
                await _unset(s, f"{prefix}.{tier}.backend")
                await _unset(s, f"{prefix}.{tier}.model")
                changed.append(f"{tier}: stays local")
                manual[tier] = None
                continue
            if backend_name not in externals:
                problems.append(f"{tier_label(tier)}: unknown online service {backend_name!r}")
                continue
            if not model:
                problems.append(f"{tier_label(tier)}: a model id is required")
                continue
            await _set(s, f"{prefix}.{tier}.backend", backend_name)
            await _set(s, f"{prefix}.{tier}.model", model)
            changed.append(f"{tier}: {backend_name}/{model}")
            manual[tier] = (backend_name, model)
        # A pick here that differs from Connect's default counts as an override, so connecting
        # again keeps it, exactly as one made on the Models page (D-UI-42).
        await note_manual_assignments(s, settings, manual)
        await s.commit()
        settings = await load_settings_with_db(s)
        parts = []
        if changed:
            parts.append("Saved: " + "; ".join(changed) + ".")
            problem = hosted_mode_problem(settings)
            if settings.llm.mode == "hosted":
                parts.append("Kestrel is on hosted models, so this applies to the next cycle.")
            elif problem is not None:
                parts.append(f"Hosted models cannot be chosen yet: {problem}.")
            else:
                parts.append(
                    "Kestrel is on local models; switch above, or choose hosted for one run, "
                    "to use these."
                )
        if problems:
            parts.append("Not saved: " + "; ".join(problems) + ".")
        ctx = await _hosted_ctx(
            s, settings, ok=not problems, message=" ".join(parts) or "Nothing to save."
        )
    return _hosted_partial(request, ctx)


# ------------------------------------------------------------------ learning ledger
# Settings > Learning (D-UI-5): what Agree / Adjust / Override and the Calibrate queue teach,
# where it lives, and the one place to view, export and reset it. All reads and writes go
# through kestrel.learning.ledger; this is presentation plus the typed-confirmation gate.
from kestrel.learning import ledger  # noqa: E402

LEDGER_PAGE = 50
RESET_WORD = "RESET"


def _page_arg(request: Request) -> int:
    try:
        return max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        return 1


def _track_arg(value: str | None) -> str | None:
    return value if value in ledger.TRACKS else None


async def _labels_ctx(session: AsyncSession, *, track: str | None, page: int) -> dict:
    rows, total = await ledger.recent_labels(
        session, track=track, limit=LEDGER_PAGE, offset=(page - 1) * LEDGER_PAGE
    )
    pages = max(1, -(-total // LEDGER_PAGE))
    if page > pages:  # a stale pager link after deletes lands on the last page, never on empty
        page = pages
        rows, total = await ledger.recent_labels(
            session, track=track, limit=LEDGER_PAGE, offset=(page - 1) * LEDGER_PAGE
        )
    return {
        "rows": rows, "total": total, "page": page, "pages": pages,
        "track": track, "tracks": ledger.TRACKS, "page_size": LEDGER_PAGE,
    }


async def _ledger_ctx(session: AsyncSession, settings: Settings, **extra) -> dict:
    return {
        "summary": await ledger.summary(session, settings),
        "reset_word": RESET_WORD,
        **(await _labels_ctx(session, track=None, page=1)),
        **extra,
    }


def _ledger_partial(request: Request, ctx: dict) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/learning_ledger.html", ctx)


@router.get("/settings/learning", response_class=HTMLResponse)
async def learning_ledger(request: Request):
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        ctx = await _ledger_ctx(s, settings)
    return _ledger_partial(request, ctx)


@router.get("/settings/learning/labels", response_class=HTMLResponse)
async def learning_labels(request: Request):
    """One page of the ledger table, filtered by track, redacted under confidential mode."""
    track = _track_arg(request.query_params.get("track"))
    async with sessionmaker()() as s:
        ctx = await _labels_ctx(s, track=track, page=_page_arg(request))
    return _ledger_partial(request, {"only_table": True, **ctx})


@router.post("/settings/learning/labels/{label_id}/delete", response_class=HTMLResponse)
async def learning_delete_label(label_id: int, request: Request):
    """Delete one decision. `kind` says which table (label: Calibrate queue; override: Agree /
    Adjust / Override). The table re-renders on the same track and page."""
    form = await request.form()
    kind = str(form.get("kind") or "label")
    track = _track_arg(str(form.get("track") or "") or None)
    try:
        page = max(1, int(form.get("page") or 1))
    except ValueError:
        page = 1
    async with sessionmaker()() as s:
        removed = await ledger.delete_label(s, label_id, kind=kind)
        ctx = await _labels_ctx(s, track=track, page=page)
    msg = "Deleted." if removed else "Nothing deleted: that decision no longer exists."
    return _ledger_partial(request, {"only_table": True, "message": msg, "ok": removed, **ctx})


@router.get("/settings/learning/export")
async def learning_export():
    """Every decision as a JSON download. Not redacted: it is the operator's own data and the
    page says so beside the button."""
    import json
    from datetime import UTC, datetime

    from fastapi.responses import Response

    async with sessionmaker()() as s:
        rows = await ledger.export_labels(s)
    body = json.dumps(
        {"exported_at": utcnow_iso(), "n": len(rows), "redacted": False, "labels": rows},
        indent=2,
    )
    name = f"kestrel-labels-{datetime.now(UTC).date().isoformat()}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/settings/learning/reset", response_class=HTMLResponse)
async def learning_reset(request: Request):
    """Reset the chosen parts. Refused unless the confirmation field holds exactly RESET and at
    least one part is ticked; the refusal re-renders the tab with the reason and deletes nothing."""
    form = await request.form()
    parts = {p: "on" in form.getlist(p) for p in ("labels", "reranker", "calibrations")}
    confirm = str(form.get("confirm") or "").strip()
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        if confirm != RESET_WORD:
            ctx = await _ledger_ctx(
                s, settings, ok=False,
                message=f"Nothing was reset. Type {RESET_WORD} in capitals to confirm.",
            )
            return _ledger_partial(request, ctx)
        if not any(parts.values()):
            ctx = await _ledger_ctx(
                s, settings, ok=False, message="Nothing was reset. Tick at least one part."
            )
            return _ledger_partial(request, ctx)
        result = await ledger.reset_learning(s, settings, **parts)
        settings = await load_settings_with_db(s)
        ctx = await _ledger_ctx(s, settings, ok=True, message=_reset_message(result))
    return _ledger_partial(request, ctx)


def _reset_message(result: dict) -> str:
    bits = [
        f"{result['labels_deleted']} Calibrate-queue label(s) and "
        f"{result['overrides_deleted']} Agree / Adjust / Override decision(s) deleted"
        if result["labels_deleted"] or result["overrides_deleted"]
        else None,
        f"{result['ml_models_deleted']} reranker record(s) deleted and "
        f"{len(result['ml_files_removed'])} file(s) removed"
        + (f" ({', '.join(result['ml_files_removed'])})" if result["ml_files_removed"] else "")
        if result["ml_models_deleted"] or result["ml_files_removed"]
        else None,
        f"{result['calibrations_reset']} calibration(s) marked rejected and "
        f"{len(result['weight_overrides_removed'])} weight override(s) removed; shipped weights "
        "apply on the next cycle"
        if result["calibrations_reset"] or result["weight_overrides_removed"]
        else None,
    ]
    done = [b for b in bits if b]
    if not done:
        return "Reset ran, and there was nothing to remove in the parts you ticked."
    return (
        "Reset complete: " + "; ".join(done) + ". Opportunities, evaluations, pursuits and "
        "documents were not touched."
    )


@router.post("/settings/hosted/model", response_class=HTMLResponse)
async def hosted_save_model(request: Request):
    """Point the frontier tier at one external backend and model, and store that backend's privacy
    options. Only the frontier tier is written here; the other tiers are the per-tier table."""
    form = await request.form()
    backend_name = str(form.get("backend") or "")
    model = str(form.get("model") or "").strip()
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        backend = prov.external_backends(settings).get(backend_name)
        if backend is None or not model:
            ctx = await _hosted_ctx(
                s, settings, message="Pick an external backend and a model id.", ok=False
            )
            return _hosted_partial(request, ctx)
        await _set(s, "llm.tiers.frontier.backend", backend_name)
        await _set(s, "llm.tiers.frontier.model", model[:200])
        from kestrel.services.hosted_models import CAPS_KEY, caps_for_picks

        # How the model takes JSON, looked up now (or "unknown", the safe JSON mode) so the
        # escalation never sends a strict schema to a model that cannot route one (D-UI-48).
        await _set(
            s, CAPS_KEY,
            await caps_for_picks(s, settings, {"frontier": (backend_name, model[:200])}),
        )
        protocol = str(form.get("protocol") or "")
        if backend.provider == "openmodel" and protocol in ("messages", "responses"):
            await _set(s, f"llm.backends.{backend_name}.protocol", protocol)
        if backend.provider == "openrouter":
            hosted = f"llm.backends.{backend_name}.hosted"
            await _set(s, f"{hosted}.zdr", "on" in form.getlist("zdr"))
            await _set(
                s,
                f"{hosted}.data_collection",
                "allow" if form.get("data_collection") == "allow" else "deny",
            )
        # Reasoning effort is one row per external backend that has a control (providers.
        # EFFORT_LEVELS); a value outside that provider's list is stored as the model default
        # rather than sent and refused on every call.
        for name, b in prov.external_backends(settings).items():
            levels = prov.EFFORT_LEVELS.get(b.provider)
            if not levels or f"reasoning_effort.{name}" not in form:
                continue
            effort = str(form.get(f"reasoning_effort.{name}") or "")
            key = f"llm.backends.{name}.hosted.reasoning_effort"
            await _set(s, key, effort if effort in levels else None)
        await s.commit()
        settings = await load_settings_with_db(s)
        # The service by its own name (its host), never the backend key ("frontier").
        from kestrel.llm.service_name import service_name

        chosen = settings.llm.backends.get(backend_name)
        service = service_name(chosen.base_url) if chosen else "your online model service"
        ctx = await _hosted_ctx(
            s,
            settings,
            ok=True,
            message=f"Deeper reads now use {model} on {service}. No restart needed.",
        )
    return _hosted_partial(request, ctx)


# ------------------------------------------------------------------ Settings > LAN > MCP server
MCP_ENABLED_KEY = "mcp.enabled"


async def _mcp_ctx(s: AsyncSession, *, message: str | None = None, ok: bool = True) -> dict:
    """The MCP switch panel (D-MCP-1): on or off, the port, the address to paste into an MCP
    client, whether it is serving now, and the shared-secret note when bind_all is on. Off by
    default; the switch writes the `mcp.enabled` setting row, no YAML edit."""
    from kestrel.mcp import runtime as mcp_runtime

    settings = await load_settings_with_db(s)
    bind_all = config_mod.get_settings().server.bind_all
    secret_row = await s.get(Setting, SHARED_SECRET_KEY)
    return {
        "mcp_enabled": settings.mcp.enabled,
        "mcp_port": settings.mcp.port,
        "mcp_address": mcp_runtime.address(settings.mcp.port, bind_all),
        "mcp_running": mcp_runtime.running(),
        "mcp_stopped_reason": mcp_runtime.stopped_reason(),
        "mcp_live": mcp_runtime.is_live(),
        "mcp_bind_all": bind_all,
        "mcp_has_secret": bool(secret_row and isinstance(secret_row.value, str)
                               and secret_row.value),
        "mcp_message": message,
        "mcp_ok": ok,
    }


@router.get("/settings/mcp", response_class=HTMLResponse)
async def mcp_panel(request: Request):
    async with sessionmaker()() as s:
        ctx = await _mcp_ctx(s)
    return templates().TemplateResponse(request, "partials/mcp_switch.html", ctx)


@router.post("/settings/mcp", response_class=HTMLResponse)
async def mcp_switch(request: Request):
    """Turn the MCP server on or off. Saved as a setting row; in the running app it starts or
    stops at once (kestrel.mcp.runtime), with the same Host/Origin pinning and shared-secret
    rule as ever (kestrel.mcp.server.sse_app). A process that cannot host it says the change
    applies when Kestrel next starts. MCP tools never act externally (SPEC-INTERFACES 4)."""
    from kestrel.mcp import runtime as mcp_runtime

    form = await request.form()
    on = str(form.get("enabled") or "") in ("1", "on", "true")
    async with sessionmaker()() as s:
        await _set(s, MCP_ENABLED_KEY, on)
        await s.commit()
        settings = await load_settings_with_db(s)
        message, ok = None, True
        # Starting or stopping never turns into a 500 (review finding 4): the panel comes back
        # with a plain error instead.
        try:
            if not mcp_runtime.is_live():
                message = "Saved. Restart Kestrel to apply."
            elif on:
                problem = await mcp_runtime.start(
                    settings.mcp.port, bind_all=config_mod.get_settings().server.bind_all
                )
                message, ok = ((problem, False) if problem
                               else ("On. The MCP server is running.", True))
            else:
                await mcp_runtime.stop()
                message = "Off. The MCP server has stopped."
        except Exception as exc:
            log.warning("mcp_switch_failed", on=on, error=f"{type(exc).__name__}: {exc}")
            message, ok = ("The MCP server could not be "
                           f"{'started' if on else 'stopped'}: {plain_error(exc)}"), False
        ctx = await _mcp_ctx(s, message=message, ok=ok)
    return templates().TemplateResponse(request, "partials/mcp_switch.html", ctx)
