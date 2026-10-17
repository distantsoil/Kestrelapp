"""`kestrel <command>` (SPEC-INTERFACES.md 7). The application is the product; this is the service
entrance for debugging and the build gates. doctor and `models build` are implemented; the rest are
clear stubs until their phase lands."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import typer

from kestrel import __version__, paths
from kestrel import doctor as doctor_mod

app = typer.Typer(help="Kestrel — local-LLM career operations engine.", no_args_is_help=True)
models_app = typer.Typer(help="Derived num_ctx model management.", no_args_is_help=True)
app.add_typer(models_app, name="models")
from kestrel.setup import setup_app  # noqa: E402

app.add_typer(setup_app, name="setup")
db_app = typer.Typer(
    help="Database migrations (also run automatically at app startup).", no_args_is_help=True
)
app.add_typer(db_app, name="db")


def _todo(command: str, phase: str) -> None:
    typer.echo(f"`kestrel {command}` is not yet implemented (arrives in {phase}).")


async def _db_settings(mode: str | None = None):
    """Settings including the setting-table layer, so an accepted weight calibration (or any UI
    setting change) takes effect on the next run without a restart. `mode` ("local" or "hosted")
    overrides the stored `llm.mode` for this command; HostedModeError is the caller's to show."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services.setting_overrides import load_settings_with_db

    async with get_sessionmaker()() as s:
        return await load_settings_with_db(s, mode=mode)


def _mode_flag(hosted: bool, local: bool) -> str | None:
    """`--hosted` / `--local` into a run mode; both at once is a usage error, neither keeps the
    Settings choice."""
    if hosted and local:
        raise typer.BadParameter("--hosted and --local are mutually exclusive; pass one of them")
    if hosted:
        return "hosted"
    if local:
        return "local"
    return None


_HOSTED_FLAG = typer.Option(
    False, "--hosted", help="Run on the hosted models assigned under Settings > Hosted."
)
_LOCAL_FLAG = typer.Option(False, "--local", help="Run on the local (Ollama) models.")


@app.callback()
def _main(ctx: typer.Context) -> None:
    """Seed config on first run so every command sees the packaged defaults. Not before
    `reset`: seeding would write Kestrel's config into the folder reset is about to judge, and
    make any folder look like Kestrel's (review finding 8)."""
    if ctx.invoked_subcommand == "reset":
        return
    try:
        paths.seed_config()
    except Exception:  # never let bootstrap seeding block a command from running
        pass


def _app_is_running() -> bool:
    """True when the desktop app holds its single-instance lock (the same Local\\ mutex or
    flock the shell takes), or answers on the port its runtime.json names. The lock is the
    authority: a busy app can be slow to answer /health (review finding 8)."""
    import json
    import sys

    from kestrel.desktop import shell

    try:
        free = shell._acquire_windows_mutex() if sys.platform.startswith("win") \
            else shell._acquire_lock_file()
    except OSError:
        free = True
    if not free:
        return True
    try:
        info = json.loads(paths.runtime_json().read_text(encoding="utf-8"))
        import httpx

        return httpx.get(f"http://127.0.0.1:{info['port']}/health", timeout=2.0).status_code < 500
    except Exception:  # no file, no port, nothing answering: not running
        return False


def _looks_like_kestrel_home(home) -> bool:
    """reset deletes everything under the data home. Refuse a folder that is not plainly
    Kestrel's, such as a KESTREL_DATA_HOME pointed at Documents (review finding 8)."""
    if not home.exists():
        return True
    if "kestrel" in home.name.lower():
        return True
    return (home / "data" / "kestrel.db").exists() or (home / "config" / "settings.yml").exists()


@app.command()
def reset(
    keep_keys: bool = typer.Option(
        False, "--keep-keys", help="Keep .env (API keys); everything else is removed."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Start from a fresh install: back up, then delete the database, config, logs and renders
    (D-UI-27). For validating a build from nothing. The backup zip is written beside the data
    folder, never inside it."""
    from kestrel.services.reset import backup_dir, reset_everything

    if _app_is_running():
        typer.echo("Kestrel is running. Quit it from the tray first, then run reset again.")
        raise typer.Exit(code=2)
    home = paths.data_home()
    if not _looks_like_kestrel_home(home):
        typer.echo(f"{home} does not look like a Kestrel data folder. Nothing was deleted.")
        raise typer.Exit(code=2)
    what = "everything except .env (your API keys)" if keep_keys else "everything, keys included"
    typer.echo(f"This deletes {what} under {home}.")
    typer.echo(f"A backup zip goes to {backup_dir()} first.")
    if not keep_keys:
        typer.echo("API keys are never put in a backup, so they will be gone for good.")
    if not yes and not typer.confirm("Continue?", default=False):
        raise typer.Exit(code=1)
    try:
        result = reset_everything(keep_keys=keep_keys)
    except (RuntimeError, OSError) as exc:
        typer.echo(f"Reset stopped: {exc}")
        typer.echo(f"Your backup is in {backup_dir()}.")
        raise typer.Exit(code=1) from exc
    typer.echo(f"Removed: {', '.join(result.removed) or 'nothing'}")
    if result.kept:
        typer.echo(f"Kept: {', '.join(result.kept)}")
    if result.backup:
        typer.echo(f"Backup: {result.backup}")
    typer.echo("Done. The next launch is a first run.")


@app.command()
def version() -> None:
    """Print the Kestrel version."""
    typer.echo(__version__)


@app.command()
def doctor(
    quick: bool = typer.Option(False, "--quick", help="Skip the slow live needle/thinking probes."),
) -> None:
    """Full preflight as a text report. Non-zero exit on a blocking failure."""
    report = doctor_mod.run_doctor(quick=quick)
    typer.echo(doctor_mod.format_report(report))
    raise typer.Exit(code=1 if report.has_blocking_failure else 0)


@models_app.command("build")
def models_build() -> None:
    """Create the derived num_ctx models by running `ollama create` for each Modelfile."""
    models_dir = paths.packaged_models()
    modelfiles = sorted(p for p in models_dir.iterdir() if p.name.startswith("Modelfile."))
    if not modelfiles:
        typer.echo("No Modelfiles found.")
        raise typer.Exit(code=1)
    failures = 0
    for mf in modelfiles:
        tier = mf.name.split(".", 1)[1]  # Modelfile.tier1 -> tier1
        name = f"kestrel-{tier}"
        with paths.resource_path(mf) as real_path:
            typer.echo(f"Building {name} from {mf.name} ...")
            proc = subprocess.run(
                ["ollama", "create", name, "-f", str(real_path)],
                capture_output=True,
                text=True,
            )
        if proc.returncode == 0:
            typer.echo(f"  ok: {name}")
        else:
            failures += 1
            typer.echo(f"  FAILED: {name}\n{proc.stderr.strip()}")
    if failures:
        typer.echo(f"{failures} model(s) failed to build.")
        raise typer.Exit(code=1)
    typer.echo("All derived models built.")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Run all pending migrations against the app database (creates the schema on first run)."""
    from kestrel.db import migrate

    migrate.run_migrations()
    typer.echo(f"Database at head: revision {migrate.current_revision()}")


@db_app.command("current")
def db_current() -> None:
    """Show the applied migration revision."""
    from kestrel.db import migrate

    rev = migrate.current_revision()
    typer.echo(rev or "not initialized")


# --------------------------------------------------------------------------- not-yet-implemented
@app.command()
def scan(
    source: str = typer.Option(None, "--source", help="Run one source by name."),
    track: str = typer.Option(None, "--track", help="Run all enabled sources on a track."),
    all: bool = typer.Option(False, "--all", help="Run all enabled sources."),
    hosted: bool = _HOSTED_FLAG,
    local: bool = _LOCAL_FLAG,
) -> None:
    """Run ingestion: fetch, normalize, dedup, enqueue embeds. A source error is a partial run.
    Only signal sources (filings, feeds) call a model, through the `trigger` tier; --hosted and
    --local pick its tier set exactly as `kestrel run` does. Job boards and ATS sources never
    call a model."""
    import asyncio

    mode = _mode_flag(hosted, local)
    asyncio.run(_scan(source, track, all, mode))


async def _scan(
    source_name: str | None, track: str | None, run_all: bool, mode: str | None = None
) -> None:
    from sqlalchemy import select

    from kestrel.config import HostedModeError
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.models import Source
    from kestrel.ingest.http import HttpClient
    from kestrel.ingest.runner import run_source
    from kestrel.ingest.signal_runner import run_signal_source
    from kestrel.ingest.signal_sources import SIGNAL_SOURCE_KINDS
    from kestrel.ingest.sources import sync_sources
    from kestrel.llm.client import LLMClient

    migrate.run_migrations()
    try:
        settings = await _db_settings(mode)
    except HostedModeError as exc:
        typer.echo(f"cannot scan on hosted models: {exc}")
        await reset_engine()
        raise typer.Exit(code=1) from None
    sm = get_sessionmaker()
    async with sm() as session:
        await sync_sources(session)
        stmt = select(Source)
        if source_name:
            stmt = stmt.where(Source.name == source_name)
        elif track:
            stmt = stmt.where(Source.track == track, Source.enabled.is_(True))
        elif run_all:
            stmt = stmt.where(Source.enabled.is_(True))
        else:
            typer.echo("specify --source NAME, --track T, or --all")
            await reset_engine()
            return
        sources = (await session.execute(stmt)).scalars().all()

    if not sources:
        typer.echo("no matching sources")
        await reset_engine()
        return

    signal_kinds = SIGNAL_SOURCE_KINDS
    client = LLMClient(settings) if any(s.kind in signal_kinds for s in sources) else None
    async with HttpClient(
        user_agent=settings.ingest.user_agent,
        default_rate_per_sec=settings.ingest.default_rate_per_sec,
        timeout=settings.ingest.request_timeout_s,
        respect_robots=settings.ingest.respect_robots,
    ) as http:
        for src in sources:
            if src.kind in signal_kinds:
                outcome = await run_signal_source(
                    sm, src.id, http=http, client=client, settings=settings
                )
            else:
                outcome = await run_source(sm, src.id, http=http)
            line = f"{src.name} [{src.kind}] -> {outcome.status}: {outcome.counts}"
            if outcome.error:
                line += f"  error={outcome.error}"
            typer.echo(line)
    await reset_engine()


@app.command()
def run(
    tier: int = typer.Option(
        None, help="Drain only this tier (1 or 2); default runs the pipeline."
    ),
    track: str = typer.Option(None, help="Restrict the Tier 0 sweep to one track."),
    limit: int = typer.Option(None, help="Cap how many new opportunities enter Tier 0 this run."),
    hosted: bool = _HOSTED_FLAG,
    local: bool = _LOCAL_FLAG,
) -> None:
    """Run the tiered pipeline: Tier 0 sweep, then drain Tier 1, then Tier 2. Ctrl+C stops
    gracefully (in-flight items finish; the rest stay pending and resumable). --hosted or --local
    picks the tier set for this run; the default is the Settings choice."""
    import asyncio

    mode = _mode_flag(hosted, local)
    asyncio.run(_run(tier, track, limit, mode))


async def _run(
    tier: int | None, track: str | None, limit: int | None, mode: str | None = None
) -> None:
    import asyncio
    import signal

    import httpx

    from kestrel.config import HostedModeError
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.llm.client import LLMClient
    from kestrel.pipeline.tier1 import handle_t1
    from kestrel.pipeline.tier2 import handle_t2
    from kestrel.pipeline.worker import Worker

    migrate.run_migrations()
    try:
        settings = await _db_settings(mode)
    except HostedModeError as exc:
        typer.echo(f"cannot run on hosted models: {exc}")
        await reset_engine()
        raise typer.Exit(code=1) from None
    typer.echo(f"run mode: {settings.llm.mode}")
    sm = get_sessionmaker()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    try:
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop.set))
    except (ValueError, OSError):  # not the main thread: rely on the caller
        pass

    async with httpx.AsyncClient() as http:
        client = LLMClient(settings, http=http)
        worker = Worker(sm, client, settings, stop=stop, progress=typer.echo)
        if tier in (1, 2):
            from kestrel.pipeline.worker import TierStats

            ts = TierStats()
            drain = handle_t1 if tier == 1 else handle_t2
            await worker.drain_tier(
                f"t{tier}", getattr(settings.concurrency, f"tier{tier}"), drain, ts
            )
            typer.echo(
                f"tier {tier}: {ts.completed} done, {ts.errors} errors, {ts.per_minute:.1f}/min"
            )
        else:
            stats = await worker.run(track=track, limit=limit, mode=mode)
            typer.echo(
                f"done. tier0={stats.tier0} "
                f"tier1={stats.tier1.completed} ({stats.tier1.per_minute:.1f}/min) "
                f"tier2={stats.tier2.completed} ({stats.tier2.per_minute:.1f}/min)"
            )
    await reset_engine()


@app.command()
def evaluate(opportunity_id: int) -> None:
    """Force a single opportunity through Tier 1 then Tier 2, regardless of its current stage."""
    import asyncio

    asyncio.run(_evaluate(opportunity_id))


async def _evaluate(opportunity_id: int) -> None:
    import httpx

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.models import Opportunity
    from kestrel.llm.client import LLMClient
    from kestrel.pipeline.profile import load_profile
    from kestrel.pipeline.tier1 import handle_t1
    from kestrel.pipeline.tier2 import handle_t2

    migrate.run_migrations()
    settings = await _db_settings()
    sm = get_sessionmaker()
    profiles = {t: load_profile(t) for t in ("employment", "consulting")}

    async with sm() as s:
        opp = await s.get(Opportunity, opportunity_id)
        if opp is None:
            typer.echo(f"opportunity {opportunity_id} not found")
            await reset_engine()
            return
        # Force entry: put it back at t0_pass so Tier 1 processes it. Prior Evaluation rows are
        # kept as history; this force-run appends new ones.
        opp.stage = "t0_pass"
        await s.commit()

    async with httpx.AsyncClient() as http:
        client = LLMClient(settings, http=http)
        r1 = await handle_t1(sm, client, settings, opportunity_id=opportunity_id, profiles=profiles)
        typer.echo(f"tier1: {r1.action} verdict={r1.verdict} forced={r1.forced}")
        if r1.action in ("t1_pass", "promoted"):
            r2 = await handle_t2(
                sm, client, settings, opportunity_id=opportunity_id, profiles=profiles
            )
            typer.echo(
                f"tier2: {r2.action} verdict={r2.verdict} needs_review={r2.needs_review} "
                f"forced={r2.forced}"
            )
    await reset_engine()


@app.command()
def escalate(
    opportunity_id: int,
    confirm: bool = typer.Option(
        False, "--confirm", help="Actually transmit to the hosted frontier. Omit to preview only."
    ),
    show_payload: bool = typer.Option(
        False, "--show-payload", help="Print the full payload that would be transmitted."
    ),
) -> None:
    """Tier 3: escalate ONE opportunity to the hosted frontier (SPEC 6.4). Without --confirm this
    only PREVIEWS what would be sent (destination, model, token cost, payload) and transmits
    nothing. Tier 3 is the only path that sends operator data off the machine; it is never
    automatic."""
    import asyncio

    asyncio.run(_escalate(opportunity_id, confirm=confirm, show_payload=show_payload))


async def _escalate(opportunity_id: int, *, confirm: bool, show_payload: bool) -> None:
    import httpx

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.llm.client import LLMClient
    from kestrel.pipeline.tier3 import build_escalation_preview, transmit_escalation

    migrate.run_migrations()
    settings = await _db_settings()
    sm = get_sessionmaker()

    async with sm() as s:
        preview = await build_escalation_preview(s, settings, opportunity_id)
    if preview.error:
        typer.echo(f"escalate: {preview.error}")
        await reset_engine()
        raise typer.Exit(code=2)

    typer.echo("Tier 3 escalation PREVIEW (nothing has been transmitted):")
    typer.echo(f"  destination : {preview.provider}  {preview.base_url}")
    typer.echo(f"  model       : {preview.model}")
    typer.echo(f"  confidential: {'ON (employer redacted)' if preview.confidential else 'off'}")
    typer.echo(f"  est. tokens : ~{preview.prompt_tokens_estimate} in")
    typer.echo(f"  confirm tok : {preview.confirmation_token[:16]}...")
    if show_payload:
        for m in preview.messages:
            typer.echo(f"  --- {m['role']} ---")
            typer.echo(m["content"])
    if preview.unavailable:
        typer.echo(f"  UNAVAILABLE : {preview.unavailable}")

    if not confirm:
        typer.echo("Not transmitted. Re-run with --confirm to send this exact payload.")
        await reset_engine()
        return
    if preview.unavailable:
        typer.echo("Refusing to transmit: see UNAVAILABLE above.")
        await reset_engine()
        raise typer.Exit(code=2)

    async with httpx.AsyncClient() as http:
        client = LLMClient(settings, http=http)
        async with sm() as s:
            res = await transmit_escalation(
                s,
                client,
                settings,
                opportunity_id,
                confirmation_token=preview.confirmation_token,
            )
    if res.sent and res.evaluation_id:
        typer.echo(f"Transmitted (record #{res.transmission_id}). Tier 3 verdict={res.verdict}.")
    elif res.sent:
        typer.echo(f"Transmitted (record #{res.transmission_id}) but no usable result: {res.error}")
    else:
        typer.echo(f"Not transmitted: {res.error}")
    await reset_engine()


doc_app = typer.Typer(help="Document engine: seed, import, and inspect.", no_args_is_help=True)
provider_app = typer.Typer(help="Hosted, bring-your-own-key backend for Tier 3 (SPEC 6.4).")
app.add_typer(provider_app, name="provider")


@provider_app.command("status")
def provider_status() -> None:
    """Which external backend the frontier tier uses, and whether its key is present."""
    from kestrel.config import get_settings
    from kestrel.llm import providers as prov

    st = prov.provider_status(get_settings())
    typer.echo(f"frontier tier -> {st.backend_name} ({st.provider}, {st.protocol})")
    typer.echo(f"  base_url : {st.base_url}")
    typer.echo(f"  model    : {st.model}")
    typer.echo(f"  key      : {'present' if st.key_present else 'NOT SET'} ({st.env_name})")


@provider_app.command("test")
def provider_test(
    backend: str = typer.Option(None, help="External backend name (default: the frontier's)."),
    models: int = typer.Option(15, help="How many schema-capable models to list (0 = none)."),
) -> None:
    """Verify the key with the provider and list models that can honor a JSON schema. Sends no
    operator data: one authenticated metadata request."""
    from kestrel.config import get_settings
    from kestrel.llm import providers as prov

    settings = get_settings()
    name = backend or prov.frontier_backend(settings)[0]
    b = prov.external_backends(settings).get(name)
    if b is None:
        typer.echo(f"no external backend named {name!r}", err=True)
        raise typer.Exit(2)

    async def _go() -> int:
        key = await prov.verify_key(b)
        typer.echo(f"{name} ({b.provider}): {'OK' if key.ok else 'FAILED'} - {key.detail}")
        if key.label:
            typer.echo(f"  label: {key.label}")
        if key.usage_usd is not None:
            typer.echo(f"  usage: ${key.usage_usd:.2f}")
        if key.limit_usd is not None:
            left = f", ${key.limit_remaining_usd:.2f} left" if key.limit_remaining_usd else ""
            typer.echo(f"  limit: ${key.limit_usd:.2f}{left}")
        if key.is_free_tier:
            typer.echo("  free tier: daily request caps apply")
        if not key.ok:
            return 1
        if models:
            fit = prov.models_fit_for_tier3(await prov.list_models(b), b)
            typer.echo(f"  {len(fit)} models can honor a JSON schema; first {models}:")
            for m in sorted(fit, key=lambda m: m.id)[:models]:
                price = (
                    f" ${m.prompt_usd_per_m:.2f}/M in, ${m.completion_usd_per_m:.2f}/M out"
                    if m.prompt_usd_per_m is not None and m.completion_usd_per_m is not None
                    else ""
                )
                ctx = f" {m.context_length:,} ctx" if m.context_length else ""
                typer.echo(f"    {m.id}{ctx}{price}")
        return 0

    raise typer.Exit(asyncio.run(_go()))


_BACKEND_HELP = "External backend name: frontier (OpenRouter), openmodel, anthropic or openai."


@provider_app.command("set-key")
def provider_set_key(
    backend: str = typer.Argument(..., help=_BACKEND_HELP),
    key: str = typer.Option(..., prompt=True, hide_input=True, help="The API key (prompted)."),
) -> None:
    """Store a provider key in the data-home .env (never in YAML, never committed)."""
    from kestrel.config import get_settings
    from kestrel.llm import providers as prov

    b = prov.external_backends(get_settings()).get(backend)
    if b is None or not b.api_key_env:
        typer.echo(f"no external backend named {backend!r}", err=True)
        raise typer.Exit(2)
    prov.write_secret(b.api_key_env, key.strip())
    typer.echo(f"{b.api_key_env} written to {paths.dotenv_path()}")


@provider_app.command("use")
def provider_use(
    backend: str = typer.Argument(..., help=_BACKEND_HELP),
    model: str = typer.Argument(..., help="Model id at that provider."),
) -> None:
    """Point Tier 3 escalation at one external backend and model (setting table, no restart)."""
    from kestrel.config import get_settings
    from kestrel.llm import providers as prov

    if backend not in prov.external_backends(get_settings()):
        typer.echo(f"no external backend named {backend!r}", err=True)
        raise typer.Exit(2)

    async def _go() -> None:
        from kestrel.db.engine import get_sessionmaker
        from kestrel.db.models import Setting

        async with get_sessionmaker()() as s:
            for k, v in (
                ("llm.tiers.frontier.backend", backend),
                ("llm.tiers.frontier.model", model),
            ):
                row = await s.get(Setting, k)
                if row is None:
                    s.add(Setting(key=k, value=v))
                else:
                    row.value = v
            await s.commit()

    asyncio.run(_go())
    typer.echo(f"Tier 3 escalation now uses {backend} / {model}")


app.add_typer(doc_app, name="doc")


@app.command()
def render(
    master_id: int = typer.Argument(..., help="doc_master id to render."),
    opportunity: int = typer.Option(None, help="Run P3 selection for this opportunity."),
    redact: bool = typer.Option(False, "--redact", help="Redact organization names."),
) -> None:
    """Render a doc_master to a PDF (default config, or P3-selected for an opportunity)."""
    import asyncio

    asyncio.run(_render(master_id, opportunity, redact))


async def _render(master_id: int, opportunity: int | None, redact: bool) -> None:
    import httpx

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.docs.catalog import default_config, load_catalog
    from kestrel.docs.select import select_config
    from kestrel.docs.service import render_document
    from kestrel.llm.client import LLMClient

    migrate.run_migrations()
    settings = await _db_settings()
    sm = get_sessionmaker()
    async with sm() as s:
        catalog = await load_catalog(s, master_id)
        if catalog is None:
            typer.echo(f"doc_master {master_id} not found (try `kestrel doc seed`).")
            await reset_engine()
            return
        defaults_used, rationale = False, {}
        if opportunity is not None:
            async with httpx.AsyncClient() as http:
                sel = await select_config(
                    s, LLMClient(settings, http=http), catalog, opportunity_id=opportunity
                )
            config, defaults_used, rationale = sel.config, sel.defaults_used, sel.rationale
        else:
            config = default_config(catalog)
        # Confidential mode is a standing instruction, not a per-invocation flag. The web render
        # path already ORs it in; the CLI did not, so `kestrel render` produced the one artifact
        # this control exists for, the document handed to an employer, with the name intact.
        config["redact_orgs"] = (
            redact or settings.confidential_mode or config.get("redact_orgs", False)
        )
        result = await render_document(
            s,
            settings,
            master_id,
            config=config,
            opportunity_id=opportunity,
            defaults_used=defaults_used,
            rationale=rationale,
        )
    typer.echo(
        f"rendered {result.filename} ({result.page_count} pages) -> {result.pdf_path}"
        + (f"  [OVER {settings.docs.max_pages}-PAGE LIMIT]" if result.over_page_limit else "")
        + ("  [DEFAULTS USED — selection failed, flagged]" if result.defaults_used else "")
    )
    if result.over_page_limit and result.hide_suggestions:
        typer.echo(f"  try hiding optional slots: {result.hide_suggestions}")
    await reset_engine()


@doc_app.command("seed")
def doc_seed() -> None:
    """Seed the fictional example doc_masters (resume + capability) and the shipped layouts."""
    import asyncio

    asyncio.run(_doc_seed())


async def _doc_seed() -> None:
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.docs.seed import seed_all

    migrate.run_migrations()
    sm = get_sessionmaker()
    async with sm() as s:
        ids = await seed_all(s)
    typer.echo(f"seeded doc_masters: {ids}")
    await reset_engine()


@doc_app.command("import")
def doc_import(
    file: str = typer.Argument(..., help="Markdown resume/capability to decompose via P7."),
    accept: bool = typer.Option(False, "--accept", help="Write the proposal to the database."),
    track: str = typer.Option("employment"),
    doc_kind: str = typer.Option("resume"),
) -> None:
    """Propose a decomposition of a pasted resume (P7). Nothing writes unless --accept."""
    import asyncio

    asyncio.run(_doc_import(file, accept, track, doc_kind))


async def _doc_import(file: str, accept: bool, track: str, doc_kind: str) -> None:
    import httpx

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.docs.imports import accept_import, propose_import
    from kestrel.llm.client import LLMClient

    markdown = open(file, encoding="utf-8").read()
    migrate.run_migrations()
    settings = await _db_settings()  # setting-table rows (a hosted-only install) over YAML
    sm = get_sessionmaker()
    async with httpx.AsyncClient() as http:
        proposal = await propose_import(LLMClient(settings, http=http), markdown)
    if proposal.value is None:
        typer.echo(f"import failed: {proposal.error}")
        await reset_engine()
        return
    typer.echo(f"proposed {len(proposal.value.roles or [])} roles.")
    if proposal.flags.non_verbatim:
        typer.echo(
            f"  NON-VERBATIM derived:false variants (flagged): {proposal.flags.non_verbatim}"
        )
    if proposal.flags.too_many_derived:
        typer.echo(f"  TOO MANY derived variants: {proposal.flags.too_many_derived}")
    if proposal.flags.clean and accept:
        async with sm() as s:
            mid = await accept_import(
                s,
                proposal.value,
                track=track,
                doc_kind=doc_kind,
                name=f"Imported {doc_kind}",
                source_markdown=markdown,
            )
        typer.echo(f"accepted -> doc_master {mid}")
    elif accept:
        typer.echo("NOT accepted: flags present; resolve them first.")
    await reset_engine()


@app.command()
def research(org: str) -> None:
    """Run company research."""
    _todo("research", "Phase 9")


@app.command()
def calibrate(
    track: str = typer.Option(None, help="employment | consulting (default: both)"),
) -> None:
    """Recompute dimension-weight proposals from the operator's labels. Never applies them."""
    import asyncio

    asyncio.run(_calibrate(track))


async def _calibrate(track: str | None) -> None:
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.learning.calibration import propose_calibration

    migrate.run_migrations()
    settings = await _db_settings()
    sm = get_sessionmaker()
    tracks = [track] if track else ["employment", "consulting"]
    async with sm() as s:
        for t in tracks:
            cal = await propose_calibration(s, t, settings)
            if cal is None:
                typer.echo(f"{t}: not enough labeled examples for a proposal yet.")
            else:
                typer.echo(
                    f"{t}: proposal #{cal.id} (n={cal.n_samples}) "
                    f"cv_mae {cal.cv_mae_previous} -> {cal.cv_mae_proposed}; "
                    f"weights {cal.proposed_weights}. Accept in the UI/API; not applied."
                )
    await reset_engine()


@app.command()
def train(track: str = typer.Option(None, help="employment | consulting (default: both)")) -> None:
    """Train and evaluate the reranker; promote it only if all four criteria hold on held-out."""
    import asyncio

    asyncio.run(_train(track))


async def _train(track: str | None) -> None:
    from kestrel.config import get_settings
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.learning.reranker import train_reranker

    migrate.run_migrations()
    settings = get_settings()
    sm = get_sessionmaker()
    tracks = [track] if track else ["employment", "consulting"]
    async with sm() as s:
        for t in tracks:
            r = await train_reranker(s, t, settings)
            if r.reason:
                typer.echo(f"{t}: {r.reason}")
            else:
                typer.echo(
                    f"{t}: n_labeled={r.n_labeled} promoted={r.promoted} "
                    f"criteria={r.criteria} metrics={r.metrics}"
                )
    await reset_engine()


@app.command()
def screenshots() -> None:
    """Capture the documentation screenshots from a real running app on FICTIONAL seed data.

    Local-required: needs Edge via Playwright. Writes to docs/images/ and deletes its throwaway
    database afterward. It never opens the operator's data directory (scripts/capture_screenshots.py
    redirects KESTREL_DATA_HOME before importing kestrel), because these images are destined for a
    public README.
    """
    import subprocess
    from pathlib import Path

    # Source-checkout only, so it resolves relative to this module rather than through paths.py
    # (which deliberately knows nothing about the repo layout).
    script = Path(__file__).resolve().parent.parent / "scripts" / "capture_screenshots.py"
    if not script.exists():
        typer.echo(f"not found: {script} (this command runs from a source checkout)")
        raise typer.Exit(code=2)
    raise typer.Exit(code=subprocess.run([sys.executable, str(script)]).returncode)


@app.command()
def bench(
    models: str = typer.Option(
        ..., "--models", help="Comma-separated Ollama model names to benchmark, e.g. a,b."
    ),
    prompt_version: str = typer.Option(
        None,
        "--prompt-version",
        help="Comma-separated prompt version(s) (e.g. evaluate/v1). Vary --models OR "
        "--prompt-version, never both in the same run.",
    ),
) -> None:
    """The benchmark harness (SPEC.md 8.6): run the tier-2 evaluation prompt against the gold set
    for one or more models (prompt version fixed) or one model across prompt versions (model
    fixed), reporting first-attempt JSON validity, tokens, latency, items/minute, gold-set
    agreement, and a projected 500-item wall-clock, per model and per track."""
    import asyncio

    asyncio.run(_bench(models, prompt_version))


def _ollama_probe_client(settings):
    """The NATIVE client, which is the one that can read num_ctx.

    LLMClient talks to the OpenAI-compatible /v1 endpoint, which cannot report num_ctx at all; only
    OllamaClient reaches /api/show. Passing the wrong one made the fitness check fail on every
    model and emit a spurious "could not read num_ctx" against healthy tier-2 candidates, which is
    worse than no check: a guard that cries wolf on good models trains the operator to ignore it.
    """
    from kestrel.llm.client import OllamaClient

    return OllamaClient(
        settings.llm.backends["local"].base_url,
        settings.llm.ollama_native_url,
        settings.llm.backends["local"].api_key or "ollama",
    )


def _warn_unfit_models(models: list[str], settings, client) -> None:
    """Print the fitness warnings BEFORE the results table (AFTERACTION 3.2).

    The audit's finding was not a wrong number, it was a number read as if it meant something it
    did not: an off-label triage model's 70% structured-output rate on a placeholder gold set was
    recorded as a tier-1 quality signal. The number itself was fine. The framing was the defect, so
    the fix belongs where the number is presented.
    """
    from kestrel.bench.harness import check_model_fitness

    warnings = check_model_fitness(models, settings, client=client)
    if not warnings:
        return
    typer.echo("")
    typer.echo("!! These models' numbers are NOT comparable to a real tier-2 candidate:")
    for w in warnings:
        typer.echo(f"   [{w.kind}] {w.model}: {w.detail}")
    typer.echo("")


def _goldset_is_placeholder(track: str) -> bool:
    """True if the gold set is still the shipped placeholder rather than the operator's own labels.

    Agreement and MAE against invented items measure nothing about this operator's judgment. The
    harness must say so rather than print a rate that reads like a result.
    """
    from kestrel import paths

    path = paths.evals_dir() / f"goldset.{track}.jsonl"
    regenerated = paths.evals_dir() / f"goldset.{track}.regenerated.jsonl"
    return path.exists() and not regenerated.exists()


async def _bench(models_csv: str, prompt_version_csv: str | None) -> None:
    import httpx

    from kestrel.bench.harness import BenchArgError, GoldSetError, render_table, run_bench
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.llm.client import LLMClient

    model_list = [m.strip() for m in models_csv.split(",") if m.strip()]
    version_list = (
        [v.strip() for v in prompt_version_csv.split(",") if v.strip()]
        if prompt_version_csv
        else []
    )

    migrate.run_migrations()
    settings = await _db_settings()
    try:
        async with httpx.AsyncClient() as http:
            _warn_unfit_models(model_list, settings, _ollama_probe_client(settings))
            report = await run_bench(
                settings=settings,
                models=model_list,
                prompt_versions=version_list,
                make_client=lambda s: LLMClient(s, http=http),
            )
    except (BenchArgError, GoldSetError) as exc:
        typer.echo(f"bench: {exc}")
        await reset_engine()
        raise typer.Exit(code=2) from exc

    for track, reason in report.skipped_tracks.items():
        typer.echo(f"[{track}] {reason}")
    if report.variants:
        typer.echo(render_table(report.variants))
        placeholder = [t for t in {v.track for v in report.variants} if _goldset_is_placeholder(t)]
        if placeholder:
            typer.echo("")
            typer.echo(
                "NOTE: agreement and MAE above are measured against the SHIPPED PLACEHOLDER gold "
                f"set for {', '.join(sorted(placeholder))}, which is invented data. They are not "
                "signal about this operator's judgment and must not be reported as accuracy. "
                "Structured-output validity, tokens, and latency ARE real. Label real items and "
                "run `kestrel goldset regen --track <track>` to make agreement meaningful."
            )
    elif not report.skipped_tracks:
        typer.echo("no bench variants ran (check --models / --prompt-version).")
    await reset_engine()


goldset_app = typer.Typer(help="The bench-harness gold set (SPEC.md 8.6).", no_args_is_help=True)
app.add_typer(goldset_app, name="goldset")


@goldset_app.command("regen")
def goldset_regen(
    track: str = typer.Option(..., help="employment | consulting"),
) -> None:
    """Turn the operator's accumulated labels for TRACK into
    evals/goldset.<track>.regenerated.jsonl. Never overwrites the shipped
    evals/goldset.<track>.jsonl placeholder."""
    import asyncio

    asyncio.run(_goldset_regen(track))


async def _goldset_regen(track: str) -> None:
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.enums import TRACKS
    from kestrel.evals.goldset import regenerate_goldset

    if track not in TRACKS:
        typer.echo(f"unknown track: {track!r} (expected one of {TRACKS})")
        raise typer.Exit(code=1)

    migrate.run_migrations()
    sm = get_sessionmaker()
    async with sm() as s:
        path = await regenerate_goldset(s, track)
    typer.echo(f"{track}: gold set regenerated -> {path}")
    await reset_engine()


@app.command()
def export(
    format: str = typer.Option("json", help="json or csv."),
    track: str = typer.Option(None, help="Filter to a track."),
    out: str = typer.Option(None, help="Output file (default: stdout)."),
) -> None:
    """Dump the pipeline (opportunities with their latest evaluation) to json or csv."""
    import asyncio

    asyncio.run(_export(format, track, out))


async def _export(fmt: str, track: str | None, out: str | None) -> None:
    import csv
    import io
    import json

    from sqlalchemy import select

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.models import Evaluation, Opportunity
    from kestrel.web.redact import confidential, redact_name, redact_text

    migrate.run_migrations()
    sm = get_sessionmaker()
    async with sm() as s:
        # An export is a file on disk: exactly the generated output confidential mode covers.
        conf, mapping = await confidential(s)
        stmt = select(Opportunity).order_by(Opportunity.id)
        if track:
            stmt = stmt.where(Opportunity.track == track)
        opps = (await s.execute(stmt)).scalars().all()
        rows = []
        for o in opps:
            ev = (
                await s.execute(
                    select(Evaluation)
                    .where(Evaluation.opportunity_id == o.id, Evaluation.tier == 2)
                    .order_by(Evaluation.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            rows.append(
                {
                    "id": o.id,
                    "track": o.track,
                    "title": redact_text(o.title, mapping, enabled=conf),
                    "org": redact_name(o.org_name, mapping, enabled=conf),
                    "stage": o.stage,
                    "verdict": ev.verdict if ev else None,
                    "score": ev.score if ev else None,
                    "ghost_score": o.ghost_score,
                    "liveness": o.liveness,
                    "url": o.url,
                }
            )
    await reset_engine()
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["id"])
        w.writeheader()
        w.writerows(rows)
        text = buf.getvalue()
    else:
        text = json.dumps(rows, indent=2, default=str)
    if out:
        from pathlib import Path

        Path(out).write_text(text, encoding="utf-8")
        typer.echo(f"exported {len(rows)} rows to {out}")
    else:
        typer.echo(text)


@app.command()
def backup(
    out: str = typer.Option(None, help="Directory for the backup zip (default: data dir)."),
) -> None:
    """Timestamped zip of the database (with its WAL), the YAML config, and the generated
    documents/prep packs. Never includes .env, so secrets never leave the machine in a backup."""
    from kestrel.services.backup import create_backup

    archive = create_backup(out)
    typer.echo(f"backup written to {archive}")


@app.command()
def restore(path: str) -> None:
    """Restore the database, config, and generated documents from a backup zip.

    Safety: the zip is validated (and fully staged) before anything on disk is touched; the
    current database is copied aside to kestrel.db.pre-restore first, so a bad restore is always
    recoverable."""
    from kestrel.services.backup import InvalidBackupError, restore_backup

    try:
        result = restore_backup(path)
    except InvalidBackupError as exc:
        typer.echo(f"Restore refused: {exc}")
        raise typer.Exit(code=1) from exc

    for line in result.summary_lines():
        typer.echo(line)
    typer.echo("Restart Kestrel (or run `kestrel db upgrade`) to pick up the restored data.")


@app.command()
def serve(
    no_worker: bool = typer.Option(False, "--no-worker", help="Serve the API without the worker."),
    open_browser: bool | None = typer.Option(
        None,
        "--open/--no-open",
        help="Open the UI in the default browser once the server is healthy. Default: on macOS "
        "and Linux when a display is available (not under KESTREL_HEADLESS=1); off on Windows.",
    ),
) -> None:
    """Start the API and worker (no desktop window). Binds 127.0.0.1 on a free port, written to
    runtime.json, and prints the URL. Ctrl+C stops. This is how Kestrel runs on macOS and Linux
    from a source checkout: `uv run kestrel serve`."""
    from kestrel.web.serve import serve as _serve

    _serve(worker=not no_worker, open_browser=open_browser, announce=typer.echo)


@app.command()
def worker() -> None:
    """Drain the pipeline queue (Tier 1 then Tier 2) without a new Tier 0 sweep. Ctrl+C stops
    gracefully."""
    import asyncio

    asyncio.run(_worker())


async def _worker() -> None:
    import asyncio
    import signal

    import httpx

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.llm.client import LLMClient
    from kestrel.pipeline.tier1 import handle_t1
    from kestrel.pipeline.tier2 import handle_t2
    from kestrel.pipeline.worker import TierStats, Worker

    migrate.run_migrations()
    settings = await _db_settings()
    sm = get_sessionmaker()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    try:
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop.set))
    except (ValueError, OSError):
        pass

    async with httpx.AsyncClient() as http:
        client = LLMClient(settings, http=http)
        worker_obj = Worker(sm, client, settings, stop=stop, progress=typer.echo)
        await worker_obj.reclaim()
        emb, t1, t2 = TierStats(), TierStats(), TierStats()
        if not stop.is_set():
            # Embed first: tier 1 and tier 2 both consult the vector.
            await worker_obj.drain_embed(emb)
        if not stop.is_set():
            await worker_obj.drain_tier("t1", settings.concurrency.tier1, handle_t1, t1)
        if not stop.is_set():
            await worker_obj.drain_tier("t2", settings.concurrency.tier2, handle_t2, t2)
        typer.echo(
            f"drained. embed={emb.completed} ({emb.per_minute:.1f}/min) "
            f"tier1={t1.completed} ({t1.per_minute:.1f}/min) "
            f"tier2={t2.completed} ({t2.per_minute:.1f}/min)"
        )
    await reset_engine()


@app.command()
def mcp(
    port: int = typer.Option(None, help="Override the MCP port (default: settings.mcp.port)."),
) -> None:
    """Start the MCP server alone (SSE), the copilot-mcp pattern. Ctrl+C stops."""
    import asyncio

    try:
        asyncio.run(_mcp(port))
    except KeyboardInterrupt:
        typer.echo("mcp stopped.")


async def _mcp(port: int | None) -> None:
    import asyncio

    from kestrel.config import get_settings
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.mcp.server import serve_mcp

    migrate.run_migrations()
    settings = get_settings()
    stop = asyncio.Event()
    the_port = port or settings.mcp.port
    typer.echo(f"kestrel mcp serving SSE on 127.0.0.1:{the_port} (Ctrl+C to stop)")
    try:
        await serve_mcp(stop, port=the_port, bind_all=settings.server.bind_all)
    finally:
        await reset_engine()


if __name__ == "__main__":
    app()
