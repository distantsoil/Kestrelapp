"""Preflight (SPEC-INTERFACES.md 1.2, BUILD_PROMPT Phase 0). Each check returns pass / warn / fail
with a specific remediation and NEVER raises. The live-model checks (num_ctx, needle, thinking)
require Ollama and the derived models; run `kestrel models build` first.

The num_ctx check is per-tier against that tier's own computed budget (SPEC.md 8.2): a single global
threshold would wrongly fail tier1. The needle test is empirical because /api/show metadata can lie.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from dataclasses import dataclass, field

import httpx

from kestrel import paths
from kestrel.config import Settings, get_settings
from kestrel.llm import profiles
from kestrel.llm.client import OllamaClient
from kestrel.llm.profiles import check_model_fit, ollama_needed
from kestrel.rules import helpers
from kestrel.rules.sample import sample_context

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"

# Logical tiers that use a derived num_ctx model (embed uses nomic-embed-text directly).
CTX_TIERS = ("tier1", "tier2", "tier3", "docsel", "trigger")


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    remediation: str = ""
    blocking: bool = False


@dataclass
class DoctorReport:
    results: list[CheckResult] = field(default_factory=list)
    # False when the setting-table layer could not be read, so the tier routing (and with it
    # whether Ollama is needed at all) is unknown; Ollama rows then never block.
    routing_known: bool = True

    def add(self, *a, **k) -> None:
        self.results.append(CheckResult(*a, **k))

    @property
    def has_blocking_failure(self) -> bool:
        return any(r.status == FAIL and r.blocking for r in self.results)


# ------------------------------------------------------------------------------- budget math
def input_budget(tier: str, s: Settings) -> int:
    jd = s.pipeline.max_jd_tokens
    return {
        "tier1": 1200 + 400 + 400,
        "tier2": jd + 2500 + 1500 + 1000,
        "tier3": jd + 2500 + 2000 + 1000,
        "docsel": 2000 + 1500 + 500,
        "trigger": 1500 + 400,
    }[tier]


def required_num_ctx(tier: str, s: Settings) -> int:
    max_tokens = s.llm.tiers[tier].max_tokens or 512
    return input_budget(tier, s) + max_tokens + 200  # 200 = structural/template overhead


def _model_present(model: str, tags: list[str]) -> bool:
    base = {t.split(":")[0] for t in tags}
    return model in tags or f"{model}:latest" in tags or model.split(":")[0] in base


# ------------------------------------------------------------------------------------ checks
def _check_python(rep: DoctorReport) -> None:
    v = sys.version_info
    ok = (v.major, v.minor) == (3, 12)
    rep.add(
        "Python 3.12",
        PASS if ok else FAIL,
        f"running {v.major}.{v.minor}.{v.micro}",
        ""
        if ok
        else "Kestrel targets Python 3.12; recreate the venv with `uv venv --python 3.12`.",
        blocking=not ok,
    )


def _check_uv(rep: DoctorReport) -> None:
    found = shutil.which("uv")
    rep.add(
        "uv present",
        PASS if found else WARN,
        found or "not on PATH",
        "" if found else "Install uv: https://docs.astral.sh/uv/ (only needed for development).",
    )


def _check_config(rep: DoctorReport) -> None:
    try:
        paths.seed_config()
        s = get_settings()  # validates the whole typed model
    except Exception as exc:
        rep.add(
            "Config valid",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "Fix config/settings.yml.",
            blocking=True,
        )
        return
    try:
        filters_path = paths.config_file("filters.yml")
        filters = helpers.load_filters(str(filters_path))
        ctx = sample_context()
        helpers.check_filters(filters, settings=s, **ctx)
    except helpers.FilterConfigError as exc:
        rep.add(
            "Config valid",
            FAIL,
            f"filters.yml: {exc}",
            "A Tier 0 rule references an unknown symbol or a generator expression. "
            "Use only the whitelisted helpers (SPEC.md 6.1).",
            blocking=True,
        )
        return
    except Exception as exc:
        rep.add(
            "Config valid",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "Fix config/filters.yml.",
            blocking=True,
        )
        return
    rep.add("Config valid", PASS, "settings.yml + filters.yml load; all Tier 0 rules evaluate")


def _check_profile(rep: DoctorReport) -> None:
    profile = paths.config_dir() / "profile.yml"
    if profile.exists():
        rep.add("Profile configured", PASS, str(profile))
    else:
        # WARN, not blocking, in Phase 0: the profile editor is a later phase; the example exists.
        rep.add(
            "Profile configured",
            WARN,
            "no profile.yml (only profile.example.yml)",
            "Copy config/profile.example.yml to config/profile.yml and edit it.",
        )


def _check_ollama(rep: DoctorReport, client: OllamaClient) -> bool:
    try:
        ver = client.version()
        rep.add("Ollama reachable", PASS, f"version {ver} at {client.native_url}")
        return True
    except Exception as exc:
        if not rep.routing_known:
            # The saved routing could not be read (Settings layer row above): Ollama may not be
            # part of this setup at all, so its absence is not a reason to block.
            rep.add(
                "Ollama reachable",
                WARN,
                f"{type(exc).__name__}: {exc} (tier routing unknown: settings not read)",
                "Fix the Settings layer row; start Ollama only if a tier runs locally.",
            )
            return False
        rep.add(
            "Ollama reachable",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "Start Ollama (https://ollama.com/download), then retry.",
            blocking=True,
        )
        return False


def _check_models_present(rep: DoctorReport, client: OllamaClient, s: Settings) -> set[str]:
    try:
        tags = client.list_models()
    except Exception as exc:
        rep.add(
            "Models present",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "Could not list Ollama models.",
            blocking=True,
        )
        return set()
    # Only LOCAL-backend tiers are expected in Ollama. The external Tier 3 frontier (OpenRouter) is
    # checked separately and never required for the local pipeline to run.
    # The in-process embed engine is not an Ollama model; doctor checks it separately.
    wanted = {
        t.model
        for t in s.llm.tiers.values()
        if not s.llm.backends[t.backend].external and s.llm.backends[t.backend].engine == "http"
    }
    missing = {m for m in wanted if not _model_present(m, tags)}
    present = wanted - missing
    if missing:
        derived = {m for m in missing if m.startswith("kestrel-")}
        base = missing - derived
        # The button first (app first, release round R1); the command for a terminal after it.
        rem = []
        if base:
            rem.append("download them with the Pull button on Settings > System (terminal: "
                       + ", ".join(f"`ollama pull {m}`" for m in sorted(base)) + ")")
        if derived:
            rem.append("build them with the Build button on Settings > System (terminal: "
                       "`kestrel models build`)")
        rep.add(
            "Models present",
            FAIL,
            "missing: " + ", ".join(sorted(missing)),
            "; ".join(rem),
            blocking=True,
        )
    else:
        rep.add("Models present", PASS, "all configured models found: " + ", ".join(sorted(wanted)))
    return present


def _check_num_ctx(rep: DoctorReport, client: OllamaClient, s: Settings, present: set[str]) -> None:
    for tier in CTX_TIERS:
        if s.llm.backends[s.llm.tiers[tier].backend].external:
            continue  # external backends (Tier 3 frontier) set no local num_ctx
        model = s.llm.tiers[tier].model
        req = required_num_ctx(tier, s)
        if model not in present:
            rep.add(
                f"num_ctx {tier}",
                FAIL,
                f"{model} not built",
                "run `kestrel models build`",
                blocking=True,
            )
            continue
        try:
            actual = client.effective_num_ctx(model)
        except Exception as exc:
            rep.add(f"num_ctx {tier}", FAIL, f"{type(exc).__name__}: {exc}", blocking=True)
            continue
        if actual is None:
            rep.add(
                f"num_ctx {tier}",
                FAIL,
                f"{model}: num_ctx not set (Ollama default applies)",
                f"rebuild {model} with PARAMETER num_ctx (need >= {req})",
                blocking=True,
            )
        elif actual < req:
            rep.add(
                f"num_ctx {tier}",
                FAIL,
                f"{model}: num_ctx {actual} < required {req}",
                f"raise PARAMETER num_ctx to >= {req} and rebuild",
                blocking=True,
            )
        else:
            rep.add(f"num_ctx {tier}", PASS, f"{model}: num_ctx {actual} >= required {req}")


def _needle_prompt(target_tokens: int) -> tuple[str, str]:
    needle = f"NEEDLE-{secrets.token_hex(4).upper()}"
    # This filler encodes at ~1.1 tokens/word, so words == target_tokens slightly OVER-fills the
    # budget. A truncation probe must meet-or-exceed the budget; the ~10% overshoot stays well
    # under every tier's ship num_ctx.
    words = max(1, target_tokens)
    filler = (
        "infrastructure security compliance migration vendor budget roadmap governance "
        "resilience remediation "
    ).split()
    body = " ".join(filler[i % len(filler)] for i in range(words))
    user = (
        f"MEMORIZE THIS SECRET CODE: {needle}\n\n{body}\n\n"
        "QUESTION: Repeat the secret code from the very top of this message. "
        "Reply with ONLY the code and nothing else."
    )
    return needle, user


def _check_needle(rep: DoctorReport, client: OllamaClient, s: Settings, present: set[str]) -> None:
    # One test per unique derived model, filled to the largest budget of the tiers that use it.
    by_model: dict[str, int] = {}
    for tier in CTX_TIERS:
        if s.llm.backends[s.llm.tiers[tier].backend].external:
            continue  # no needle test for a hosted external model
        model = s.llm.tiers[tier].model
        by_model[model] = max(by_model.get(model, 0), input_budget(tier, s))
    for model, budget in sorted(by_model.items()):
        if model not in present:
            rep.add(
                f"needle {model}", FAIL, "not built", "run `kestrel models build`", blocking=True
            )
            continue
        try:
            needle, prompt = _needle_prompt(budget)
            out = client.strip_think(
                client.chat(
                    model,
                    [{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=32,
                    reasoning_effort="none",
                )
            )
            ok = needle in out
            rep.add(
                f"needle {model}",
                PASS if ok else FAIL,
                f"filled ~{budget} tok; {'recovered' if ok else 'LOST'} the code near the start"
                + ("" if ok else f" (model got: {out[:60]!r})"),
                "" if ok else f"effective context is below {budget}; rebuild with a larger num_ctx",
                blocking=not ok,
            )
        except httpx.TimeoutException as exc:
            # A timeout is evidence the model was COLD, not evidence its context is short. The
            # first model tested is the one Ollama has to load from disk, so a blocking FAIL here
            # makes `doctor` non-reproducible: the same machine passes or fails depending on
            # whether the weights happened to be resident. Report it as a warning with the remedy
            # that actually addresses it, and keep blocking for a real wrong answer.
            rep.add(
                f"needle {model}",
                WARN,
                f"timed out loading the model ({type(exc).__name__}); context not verified",
                "re-run with the model warm, or set OLLAMA_KEEP_ALIVE=2h so it stays resident",
            )
        except Exception as exc:
            rep.add(f"needle {model}", FAIL, f"{type(exc).__name__}: {exc}", blocking=True)


def _check_thinking(
    rep: DoctorReport, client: OllamaClient, s: Settings, present: set[str]
) -> None:
    models = {s.llm.tiers[t].model for t in CTX_TIERS if s.llm.tiers[t].model in present}
    for model in sorted(models):
        try:
            raw = client.chat(
                model,
                [{"role": "user", "content": "Reply with exactly one word: ready"}],
                temperature=0.0,
                max_tokens=24,
                reasoning_effort="none",
            )
            if client.has_think(raw):
                rep.add(
                    f"thinking off {model}",
                    WARN,
                    "a <think> span survived reasoning_effort=none",
                    "confirm the backend honors reasoning_effort:'none' (Ollama) — SPEC.md 8.4",
                )
            else:
                rep.add(f"thinking off {model}", PASS, "no <think> span")
        except Exception as exc:
            rep.add(f"thinking off {model}", WARN, f"{type(exc).__name__}: {exc}")


_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["ready"]}, "n": {"type": "integer"}},
    "required": ["answer", "n"],
    "additionalProperties": False,
}


def _check_schema(
    rep: DoctorReport, client: OllamaClient, s: Settings, present: set[str]
) -> None:
    """A model that ignores response_format with thinking off (Ollama #14645, seen on the qwen3.5
    family) returns prose and every pipeline call quarantines. Probe each tier model with a tiny
    schema and reasoning_effort none, exactly as the pipeline calls it, and FAIL if the reply is
    not the JSON the schema demands. Blocking: there is no point running a tier that cannot
    answer in JSON."""
    import json

    models = {s.llm.tiers[t].model for t in CTX_TIERS if s.llm.tiers[t].model in present}
    for model in sorted(models):
        try:
            raw = client.chat(
                model,
                [{"role": "user", "content": "Reply with answer ready and n 7."}],
                temperature=0.0,
                max_tokens=64,
                reasoning_effort="none",
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "probe", "schema": _PROBE_SCHEMA},
                },
            )
            data = json.loads(client.strip_think(raw).strip())
            ok = isinstance(data, dict) and data.get("answer") == "ready"
        except (ValueError, TypeError) as exc:  # not JSON at all
            data, ok = None, False
            raw = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            rep.add(f"schema honored {model}", WARN, f"{type(exc).__name__}: {exc}")
            continue
        if ok:
            rep.add(f"schema honored {model}", PASS, "JSON matched the probe schema")
        else:
            rep.add(
                f"schema honored {model}",
                FAIL,
                f"reply did not match the schema: {str(raw)[:120]!r}",
                "This model ignores response_format with thinking off (Ollama issue 14645). "
                "Pick another base in models.yml and re-run `kestrel setup`.",
                blocking=True,
            )


def _check_browser(rep: DoctorReport) -> None:
    """Which browser the PDF render ladder will use (kestrel.docs.render.launch_plan): Edge, then
    Chrome, then Playwright's Chromium, with KESTREL_BROWSER_PATH ahead of all. PASS on any; WARN,
    never FAIL, when none, because everything but PDF rendering still works without one."""
    from kestrel.docs import render

    try:
        found = render.detect_browser()
        bad_path = render.browser_path_warning()
    except Exception as exc:  # a probe error is not a missing browser; say so and move on
        rep.add("Browser (PDF)", WARN, f"probe failed: {exc}", render.browser_remediation())
        return
    note = f"; {bad_path}" if bad_path else ""
    if found:
        label, where = found
        rep.add("Browser (PDF)", WARN if bad_path else PASS, f"{label}: {where}{note}")
    else:
        rep.add("Browser (PDF)", WARN, f"no Edge, Chrome, or Playwright Chromium found{note}",
                render.browser_remediation())


def _check_mcp_port(rep: DoctorReport, s: Settings) -> None:
    """The MCP server needs its OWN free port (SPEC-INTERFACES 4): the operator has been bitten
    by two MCP servers sharing a port, one silently serving the other's tools. Only when enabled."""
    if not s.mcp.enabled:
        rep.add("MCP port", PASS, f"disabled (would use {s.mcp.port})", "")
        return
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", s.mcp.port))
            rep.add("MCP port", PASS, f"{s.mcp.port} free", "")
        except OSError:
            rep.add(
                "MCP port",
                FAIL,
                f"{s.mcp.port} already in use",
                f"Another process holds {s.mcp.port}; change settings.mcp.port so Kestrel's MCP "
                "server does not collide with (or get shadowed by) another MCP server.",
                True,
            )


def _check_ollama_env(rep: DoctorReport, s: Settings) -> None:
    need = max(s.concurrency.tier1, s.concurrency.tier2, s.concurrency.embed)
    par = os.environ.get("OLLAMA_NUM_PARALLEL")
    if par and par.isdigit() and int(par) >= need:
        rep.add("OLLAMA_NUM_PARALLEL", PASS, par)
    else:
        rep.add(
            "OLLAMA_NUM_PARALLEL",
            WARN,
            par or "unset",
            f"set OLLAMA_NUM_PARALLEL >= {need} or requests serialize (SPEC.md 6.6)",
        )
    keep = os.environ.get("OLLAMA_KEEP_ALIVE")
    rep.add(
        "OLLAMA_KEEP_ALIVE",
        PASS if keep else WARN,
        keep or "unset",
        "" if keep else "set OLLAMA_KEEP_ALIVE=2h to avoid reloading weights between tiers",
    )


def _check_ingest_contact(rep: DoctorReport, s: Settings) -> None:
    """Hard rule 7: every request carries a descriptive User-Agent with a REAL contact address
    (SEC EDGAR blocks requests without one, and the EDGAR sources refuse to run with a placeholder).
    The shipped default names example.com, so a fresh install WARNS until the operator sets it."""
    from kestrel.ingest.http import has_contact_address

    ua = s.ingest.user_agent
    if has_contact_address(ua):
        rep.add("Ingest contact", PASS, ua)
    else:
        rep.add(
            "Ingest contact",
            WARN,
            f"placeholder contact in User-Agent: {ua}",
            "Set ingest.user_agent in settings.yml to a descriptive UA with your real contact "
            "address, e.g. 'Kestrel/1.0 (your.name@your-domain.com)'. SEC EDGAR sources and "
            "EDGAR research are skipped until it is set.",
        )


_ATS_KINDS = (
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "rippling", "adp", "paylocity",
    "workable", "recruitee", "teamtailor",
    "personio", "breezy",
)


async def _probe_ats(settings) -> list[tuple[str, str, str]]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Source
    from kestrel.ingest.adapters import get_adapter
    from kestrel.ingest.http import HttpClient

    # Doctor runs under asyncio.run on whatever thread called it (the CLI, the tray, the Setup
    # page's action thread). The app engine belongs to uvicorn's loop, so this probe opens its own
    # engine on this loop and disposes only that one, exactly as _settings_for_doctor does. Touching
    # get_sessionmaker() or reset_engine() here would dispose the live app engine from a foreign
    # loop (cross-loop aiosqlite errors that look like corruption).
    engine = make_async_engine(paths.db_path())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            sources = (
                (
                    await session.execute(
                        select(Source).where(
                            Source.enabled.is_(True), Source.kind.in_(_ATS_KINDS)
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    if not sources:
        return []
    out: list[tuple[str, str, str]] = []
    async with HttpClient(
        user_agent=settings.ingest.user_agent,
        timeout=settings.ingest.request_timeout_s,
        respect_robots=settings.ingest.respect_robots,
    ) as http:
        for src in sources:
            try:
                # Cap to one result so the probe never runs a full board scrape (Workday/SR would
                # fetch every per-job detail); shape-checking the first posting is enough.
                postings = await get_adapter(src.kind).fetch({**src.config, "max_results": 1}, http)
            except Exception as exc:  # a shape/HTTP problem is a WARN, never a crash
                out.append((src.name, WARN, f"{type(exc).__name__}: {exc}"))
                continue
            if not postings:
                out.append((src.name, WARN, "no postings returned"))
                continue
            p = postings[0]
            missing = [f for f in ("title",) if getattr(p, f) is None]
            if not (p.url or p.external_id):
                missing.append("url/external_id")
            if not (p.description_md or p.description_html):
                missing.append("description")
            if missing:
                out.append((src.name, WARN, "missing field(s): " + ", ".join(missing)))
            else:
                out.append((src.name, PASS, f"{len(postings)} postings, shape ok"))
    return out


def _check_ats_shapes(rep: DoctorReport, settings) -> None:
    """Live-verify each enabled ATS source still returns the fields the adapter maps (Appendix A).
    A missing field is a WARN naming it, never a crash."""
    import asyncio

    from kestrel.db import migrate

    if migrate.current_revision() is None:
        rep.add("ATS shapes", INFO, "skipped (database not initialized)")
        return
    try:
        results = asyncio.run(_probe_ats(settings))
    except Exception as exc:
        rep.add("ATS shapes", WARN, f"could not probe: {type(exc).__name__}: {exc}")
        return
    if not results:
        rep.add("ATS shapes", INFO, "no enabled ATS sources to verify")
        return
    for name, status, detail in results:
        rep.add(
            f"ATS {name}",
            status,
            detail,
            "upstream shape may have changed; check the adapter's Appendix A entry"
            if status == WARN
            else "",
        )


def _check_database(rep: DoctorReport) -> None:
    from kestrel.db import migrate

    rev = migrate.current_revision()
    if not rev:
        rep.add(
            "Database",
            WARN,
            "not initialized",
            "run `kestrel db upgrade` (or launch the app; migrations run at startup)",
        )
        return
    # "Has a revision" is not "is up to date". A database left at an older revision by a previous
    # build is missing that migration's tables, so the feature it added fails at runtime while a
    # bare existence check happily reports clean. Compare against the shipped head.
    head = migrate.head_revision()
    if head is None:
        # The shipped migration scripts could not be read, which in a frozen build means they did
        # not ship. Report the uncertainty instead of rendering it as a clean PASS: "cannot verify"
        # is not "verified" (hard rule 6).
        rep.add(
            "Database",
            WARN,
            f"at revision {rev}, but the shipped migrations could not be read to verify it",
            "check that the migrations directory shipped (packaging/kestrel.spec datas)",
        )
    elif rev != head:
        rep.add(
            "Database",
            WARN,
            f"revision mismatch: database at {rev}, shipped head is {head}",
            "run `kestrel db upgrade` (or launch the app; migrations run at startup)"
            if rev < head
            else "this database was written by a NEWER build; upgrading Kestrel is the fix",
        )
    else:
        rep.add("Database", PASS, f"migrated (revision {rev})")


def _check_hosted(rep: DoctorReport, s: Settings) -> None:
    """Hosted mode (SPEC 6.4) is optional and opt-in, and never blocks: the local pipeline runs
    without it. Report whether it is on, which tiers it routes off-machine and to what, a tier
    that names an external backend while the mode is off (it will not be used), and the key
    status of every external backend in use. The Tier 3 frontier row is kept as before."""
    from kestrel.llm import providers as prov

    hm = s.llm.hosted_mode
    rows = prov.tier_status(s)
    external_rows = [r for r in rows if r.external and r.tier != prov.FRONTIER_TIER]
    if hm.active:
        routed = ", ".join(f"{r.tier} -> {r.backend}/{r.model}" for r in external_rows)
        rep.add(
            "Hosted mode",
            PASS,
            f"on since {hm.acknowledged_at}; tiers: {routed or 'none (local models only)'}",
        )
    else:
        rep.add("Hosted mode", INFO, "off (every tier runs locally; deeper reads only)")
        if external_rows:
            named = ", ".join(f"{r.tier} -> {r.backend}" for r in external_rows)
            rep.add(
                "Hosted routing",
                WARN,
                f"{named}: names an external backend but will not be used until Hosted mode is "
                "turned on",
                "Turn on Hosted mode under Settings > Hosted after reading the disclosure, or "
                "point the tier back at `local`.",
            )
    if hm.enabled and not hm.acknowledged_at:
        rep.add(
            "Hosted mode",
            WARN,
            "llm.hosted_mode.enabled is set but the disclosure was never acknowledged; hosted "
            "tiers stay off",
            "Accept the disclosure under Settings > Hosted (it writes acknowledged_at).",
        )
    embed = s.llm.tiers.get("embed")
    embed_backend = s.llm.backends.get(embed.backend) if embed else None
    if embed_backend is not None and embed_backend.external:
        rep.add(
            "Embeddings",
            WARN,
            f"llm.tiers.embed.backend is {embed.backend!r}, an external backend; embeddings "
            "have no hosted path, so the client refuses every embed call (few-shot and "
            "reranker off)",
            "Point llm.tiers.embed.backend back at `local` (settings.yml or the setting table).",
        )

    # Key presence per external backend any hosted tier (not the frontier) is routed to.
    seen: set[str] = set()
    for r in external_rows:
        if r.backend in seen or not hm.active:
            continue
        seen.add(r.backend)
        if r.key_present:
            rep.add("Hosted key", PASS, f"{r.backend}: key saved")
        else:
            rep.add(
                "Hosted key",
                WARN,
                f"{r.backend}: no key saved; calls on this backend will fail",
                f"Add your {_backend_label(r.backend)} key in Settings > Hosted.",
            )

    st = prov.provider_status(s)
    if not prov.external_backends(s):
        return
    where = f"{st.backend_name} ({st.provider}, {st.protocol}) -> {st.base_url} ({st.model})"
    if st.key_present:
        rep.add("Tier 3 frontier", PASS, f"{where}; key saved")
    else:
        rep.add(
            "Tier 3 frontier",
            WARN,
            f"{where}; no key saved",
            f"Add your {_backend_label(st.backend_name)} key in Settings > Hosted to enable "
            "deeper reads.",
        )


def _backend_label(name: str | None) -> str:
    """The provider's name for a backend ("OpenRouter"), for an instruction a user can follow;
    key names stay out of doctor's advice (release round R1)."""
    from kestrel.llm import providers as prov

    return next(
        (str(p["label"]) for p in prov.PRESETS.values() if p["backend"] == name), name or "API"
    )


# ------------------------------------------------------------------------------ hosted-only
def local_tiers(s: Settings) -> list[str]:
    """The derived-model tiers whose backend is local, so Ollama must serve them."""
    return [
        t
        for t in CTX_TIERS
        if t in s.llm.tiers and not s.llm.backends[s.llm.tiers[t].backend].external
    ]


def hosted_only(s: Settings) -> bool:
    """True when Hosted mode is on and acknowledged and no tier needs a local derived model
    (`kestrel setup --hosted`). Only `embed` may still be local; it is reported separately."""
    return s.llm.hosted_mode.active and not local_tiers(s)


HOSTED_SKIPPED = "skipped: every tier is hosted"
OLLAMA_NOT_NEEDED = "not needed: no tier runs on Ollama"
# Every Ollama row, reported as INFO when no tier needs Ollama (D-SETUP-8).
OLLAMA_ROWS: tuple[str, ...] = (
    "Ollama reachable",
    "Models present",
    "num_ctx",
    "needle",
    "thinking off",
    "schema honored",
    "OLLAMA_NUM_PARALLEL",
    "OLLAMA_KEEP_ALIVE",
)


def _skip_ollama_checks(rep: DoctorReport) -> None:
    """No tier in settings.llm.tiers runs on the `local` backend, so Ollama is not part of this
    setup: every Ollama row is INFO (never WARN or FAIL, never blocking) and nothing is probed."""
    for name in OLLAMA_ROWS:
        rep.add(name, INFO, OLLAMA_NOT_NEEDED)


def _skip_local_checks(rep: DoctorReport, client: OllamaClient, s: Settings) -> None:
    """Hosted-only: the Ollama checks become INFO rows (never FAIL, never blocking). Embeddings
    have no hosted path, so the embed tier still wants Ollama: PASS when it answers and the model
    is there, otherwise a WARN that says exactly which features stay off."""
    for name in (
        "Ollama reachable",
        "Models present",
        "num_ctx",
        "needle",
        "thinking off",
        "schema honored",
    ):
        rep.add(name, INFO, HOSTED_SKIPPED)
    embed = s.llm.tiers.get("embed")
    if embed is None or s.llm.backends[embed.backend].external:
        return
    if s.llm.backends[embed.backend].engine == "inprocess":
        _check_inprocess_embed(rep, s)
        return
    try:
        tags = client.list_models()
        if _model_present(embed.model, tags):
            rep.add("Embeddings", PASS, f"Ollama answers and {embed.model} is present")
            return
        detail = f"{embed.model} not pulled"
    except Exception as exc:  # Ollama absent is the expected case on this profile
        detail = f"Ollama not reachable ({type(exc).__name__})"
    rep.add(
        "Embeddings",
        WARN,
        f"embeddings unavailable without Ollama: few-shot and reranker off ({detail})",
        f"set llm.tiers.embed.backend to `inprocess` and .model to `{_inprocess_model_id()}` "
        "(the bundled CPU model, no Ollama), or install Ollama and "
        f"`ollama pull {embed.model}`; everything else works without either",
    )


def _inprocess_model_id() -> str:
    from kestrel.llm.inprocess_embed import BUNDLED

    return BUNDLED.model_id


def embed_is_inprocess(s: Settings) -> bool:
    embed = s.llm.tiers.get("embed")
    backend = s.llm.backends.get(embed.backend) if embed else None
    return backend is not None and backend.engine == "inprocess"


def _check_inprocess_embed(rep: DoctorReport, s: Settings) -> None:
    """The bundled CPU embedding model (SPEC.md 8.1.1): files present and the configured model id
    is the bundled one. WARN, never FAIL: without embeddings few-shot and the reranker are off and
    everything else works. Hashes are verified when the engine loads, not here (90 MB)."""
    from kestrel.llm import inprocess_embed as ipe

    model = s.llm.tiers["embed"].model
    if model != ipe.BUNDLED.model_id:
        rep.add(
            "Embeddings",
            WARN,
            f"llm.tiers.embed.model is {model!r}; the in-process engine bundles "
            f"{ipe.BUNDLED.model_id!r}, so every embed call is refused",
            f"set llm.tiers.embed.model to {ipe.BUNDLED.model_id!r}",
        )
        return
    missing = ipe.missing_files()
    if missing:
        rep.add(
            "Embeddings",
            WARN,
            f"in-process model files missing in {ipe.model_dir()}: {', '.join(missing)}; "
            "few-shot and reranker off",
            "a source checkout: `uv run python scripts/fetch_embed_model.py`; an installed app: "
            "reinstall Kestrel",
        )
        return
    rep.add(
        "Embeddings",
        PASS,
        f"in-process {ipe.BUNDLED.model_id} ({ipe.BUNDLED.dims} dims, CPU, no Ollama needed)",
    )
    # Moved from BYOK to a GPU profile: the in-process engine still works, but the profile's own
    # embed model is available. Say so; never switch it silently (D-EMBED-3 review).
    active = profiles.load_active_profile()
    if active is not None:
        try:
            local = not profiles.load_catalog().profile(active.profile).hosted
        except KeyError:
            local = False
        if local:
            rep.add(
                "Embeddings engine",
                INFO,
                f"profile {active.profile} runs locally but embeddings still use the in-process "
                "model from a Hosted setup",
                "To use Ollama's embed model instead, set llm.tiers.embed.backend to `local` and "
                ".model to `nomic-embed-text`; the corpus re-embeds on its own.",
            )


def _db_overrides_sync() -> dict | None:
    """The setting-table layer read with a plain sync engine, the same rows and filters as
    services.setting_overrides.db_override_dict. Sync on purpose: doctor runs on the desktop main
    thread at launch, on a worker thread for Verify, and inside request handlers, and an
    asyncio.run() there either fails (a running loop) or races the app's own engine. When the
    layer differed between those callers, startup preflight read YAML only, saw every tier on
    Ollama and FAILed while the saved doctor report said "not needed". None when there is no
    database or no setting table yet (a fresh install: YAML is then the whole truth)."""
    from sqlalchemy.exc import OperationalError

    from kestrel.db.engine import make_sync_engine
    from kestrel.services.setting_overrides import config_rows_select, overrides_from_rows

    db = paths.db_path()
    if not db.exists():
        return None
    engine = make_sync_engine(db)
    try:
        with engine.connect() as conn:
            rows = conn.execute(config_rows_select()).all()
    except OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    finally:
        engine.dispose()
    return overrides_from_rows(rows)


def _settings_for_doctor(rep: DoctorReport) -> Settings:
    """The Settings the app actually runs with: setting-table rows on top of env and YAML, which
    is where `kestrel setup --hosted` and Settings > Hosted write the tier routing. Every caller
    (launch preflight, Verify, `kestrel doctor`, the diagnostics bundle) reads the layer the same
    way, so they agree on whether Ollama is needed. Falls back to the YAML layer, saying so, when
    the database cannot be read; `rep.routing_known` is then False and an unreachable Ollama is a
    warning, not a blocking failure, because the routing it would block on is unknown."""
    from kestrel.config import load_settings

    try:
        overrides = _db_overrides_sync()
    except Exception as exc:  # report it; doctor never raises
        rep.routing_known = False
        rep.add(
            "Settings layer",
            WARN,
            f"setting-table rows not applied: {type(exc).__name__}: {exc}",
            "run `kestrel db upgrade`; until then doctor reads settings.yml only",
        )
        return get_settings()
    if overrides is None:
        return get_settings()
    try:
        return load_settings(db_overrides=overrides)
    except Exception as exc:  # a malformed row: report it, fall back to YAML
        rep.routing_known = False
        rep.add(
            "Settings layer",
            WARN,
            f"setting-table rows not applied: {type(exc).__name__}: {exc}",
            "fix or reset the setting under Settings; until then doctor reads settings.yml only",
        )
        return get_settings()


# ---------------------------------------------------------------------------------- entrypoint
def run_doctor(*, quick: bool = False) -> DoctorReport:
    rep = DoctorReport()
    _check_python(rep)
    _check_uv(rep)
    _check_config(rep)
    _check_profile(rep)
    s = _settings_for_doctor(rep)
    client = OllamaClient(
        s.llm.backends["local"].base_url,
        s.llm.ollama_native_url,
        s.llm.backends["local"].api_key or "ollama",
    )
    needed = ollama_needed(s)
    hosted = hosted_only(s)
    if not needed:
        _skip_ollama_checks(rep)
        check_model_fit(rep, s, client)
    elif hosted:
        _skip_local_checks(rep, client, s)
        check_model_fit(rep, s, client)
    elif _check_ollama(rep, client):
        present = _check_models_present(rep, client, s)
        check_model_fit(rep, s, client)
        _check_num_ctx(rep, client, s, present)
        if not quick:
            _check_needle(rep, client, s, present)
            _check_thinking(rep, client, s, present)
            _check_schema(rep, client, s, present)
        else:
            rep.add("Needle + thinking probes", INFO, "skipped (--quick)")
    # The hosted branch reports the in-process engine itself; every other branch reports it here,
    # including a BYOK install where no tier needs Ollama (the case this engine exists for).
    if embed_is_inprocess(s) and not (needed and hosted):
        _check_inprocess_embed(rep, s)
    _check_browser(rep)
    if not needed:
        pass  # both env rows were reported by _skip_ollama_checks
    elif hosted:
        rep.add("OLLAMA_NUM_PARALLEL", INFO, HOSTED_SKIPPED)
        rep.add("OLLAMA_KEEP_ALIVE", INFO, HOSTED_SKIPPED)
    else:
        _check_ollama_env(rep, s)
    _check_ingest_contact(rep, s)
    _check_hosted(rep, s)
    _check_mcp_port(rep, s)
    _check_database(rep)
    if not quick:
        _check_ats_shapes(rep, s)
    return rep


def format_report(rep: DoctorReport) -> str:
    icon = {PASS: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[ -- ]"}
    lines = ["", "Kestrel preflight (doctor)", "=" * 60]
    for r in rep.results:
        lines.append(f"{icon[r.status]}  {r.name:<22} {r.detail}")
        if r.remediation and r.status in (WARN, FAIL):
            lines.append(f"         -> {r.remediation}")
    n_fail = sum(1 for r in rep.results if r.status == FAIL)
    n_warn = sum(1 for r in rep.results if r.status == WARN)
    lines += [
        "=" * 60,
        f"{n_fail} failing, {n_warn} warning, {sum(1 for r in rep.results if r.status == PASS)} ok",
        "",
    ]
    return "\n".join(lines)
