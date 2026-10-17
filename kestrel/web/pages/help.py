"""Help page (SPEC-INTERFACES 2.1, 1.5), served at /help.

What Kestrel does, the pipeline tiers, where opportunities come from (rendered from the source
catalog so it can never disagree with the registries), what never happens (hard rule 1), what
leaves the machine, backups, and the install facts (version, port, data and log folders, hardware
profile, dependency versions) the extension README and the setup flow point the operator to. The
version, the project links and the diagnostics download are on /about (pages/about.py).

The "Check for updates" button posts to /updates/check (pages/updates.py), which owns the
update checker UI. Kestrel never downloads or installs anything."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, metadata
from importlib.metadata import version as pkg_version

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

import kestrel
from kestrel import paths
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting
from kestrel.ingest.catalog import REGION_NOTE, REQUEST_NEW_PROVIDER, source_catalog
from kestrel.web.deps import (
    GUIDES,
    GUIDES_KEY,
    dismissed_guides,
    forget_dismissed_guides,
    sessionmaker,
    templates,
)

router = APIRouter()

# A handful of key deps (per the brief), each looked up independently so one missing package
# (e.g. playwright not installed in a minimal test env) skips a row rather than 500ing the page.
_DEPENDENCIES = ("fastapi", "sqlalchemy", "pydantic", "httpx", "playwright", "scikit-learn")

# The pipeline in one table (SPEC.md 6). Plain words; the spec holds the mechanics.
TIERS = (
    {
        "tier": "Rules check",
        "tech": "Tier 0",
        "what": "Fixed rules from your filters. No AI model.",
        "decides": "Sets a role aside only on a hard blocker, and records the rule that fired. "
        "An ambiguous title passes.",
        "cost": "Microseconds.",
    },
    {
        "tier": "Quick screen",
        "tech": "Tier 1",
        "what": "A small AI model reads the key fields and the opening of the description.",
        "decides": "Keep or set aside. When it is unsure, the role is kept. "
        "Replaced by a ranking learned from your ratings once that proves itself.",
        "cost": "Seconds per item.",
    },
    {
        "tier": "Full review",
        "tech": "Tier 2",
        "what": "The main AI model reads the full description and your full profile, "
        "with examples drawn from your own past decisions.",
        "decides": "Scores each dimension, states blockers, and gives the verdict you review.",
        "cost": "Tens of seconds per item.",
    },
    {
        "tier": "Deeper read",
        "tech": "Tier 3",
        "what": "Optional, one role at a time. May use an online model with your own key.",
        "decides": "A second opinion on a role, cover letter or engagement outline, negotiation "
        "framing, research summaries.",
        "cost": "Shown before you confirm, with exactly what will be sent.",
    },
)

# The tier NAMES as the Settings pages print them (SPEC.md 8.2 and providers.HOSTED_TIERS), for
# the operator who meets "docsel" or "frontier" in a table before reading the spec.
TIER_NAMES = (
    {"name": "tier1", "label": "Quick screen (Tier 1)",
     "what": "The fast keep-or-set-aside read of every new posting."},
    {"name": "tier2", "label": "Full review (Tier 2)",
     "what": "The deep evaluation that scores a role and gives a verdict."},
    {
        "name": "tier3",
        "label": "Deeper read (Tier 3)",
        "what": "The model behind the work you start yourself: cover letters, engagement "
        "outlines, research summaries, contact finding, negotiation prep.",
    },
    {
        "name": "docsel",
        "label": "Document selection",
        "what": "Document selection: picks which of your own resume variants fill each slot. "
        "It only selects; every line in the PDF is your text.",
    },
    {
        "name": "trigger",
        "label": "Consulting signals",
        "what": "The consulting-track classifier for filings and feeds (SEC EDGAR, RSS, PE "
        "portfolio pages).",
    },
    {
        "name": "frontier",
        "label": "Online deeper read",
        "what": "The online model a deeper read goes to, one opportunity at a time, after "
        "you preview and confirm the exact payload.",
    },
    {
        "name": "embed",
        "label": "Matching",
        "what": "The embedding model behind duplicate detection, few-shot examples and the "
        "learned ranking. Always on your computer.",
    },
)


def _running_port() -> str:
    """The port this instance is serving on, per runtime.json. Never raises: Help must render even
    when the file is absent (a test app, or an instance started without writing it)."""
    import json

    try:
        return str(json.loads(paths.runtime_json().read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError):
        return "not recorded"


def _kestrel_license() -> str:
    try:
        value = metadata("kestrel").get("License")
    except PackageNotFoundError:
        return "unknown"
    return value or "unknown"


def _dependency_versions() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name in _DEPENDENCIES:
        try:
            rows.append({"name": name, "version": pkg_version(name)})
        except PackageNotFoundError:
            continue
    return rows


def _hardware_profile() -> dict[str, str] | None:
    """The hardware profile `kestrel setup` chose, when the profiles module and its record exist.
    Imported defensively: the Help page must render on a build without it."""
    try:
        from kestrel.llm.profiles import load_active_profile
    except ImportError:
        return None
    try:
        active = load_active_profile()
    except Exception:  # a corrupt record is a missing row on Help, never a 500
        return None
    if active is None:
        return None
    return {"profile": active.profile, "reason": active.reason, "chosen_at": active.chosen_at}


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    ctx = {
        "title": "Help",
        "query": dict(request.query_params),
        "version": kestrel.__version__,
        "license": _kestrel_license(),
        "data_dir": str(paths.data_dir()),
        "logs_dir": str(paths.logs_dir()),
        # The extension's README and options page both tell the operator the running port is shown
        # here, so it has to actually be shown here. Read from runtime.json, which is the one
        # authority on which ladder rung this instance took.
        "api_port": _running_port(),
        "dependencies": _dependency_versions(),
        "tiers": TIERS,
        "tier_names": TIER_NAMES,
        "catalog": source_catalog(),
        "region_note": REGION_NOTE,
        "request_new_provider": REQUEST_NEW_PROVIDER,
        "hardware_profile": _hardware_profile(),
    }
    return templates().TemplateResponse(request, "help.html", ctx)


async def _write_dismissed(keys: list[str]) -> None:
    async with sessionmaker()() as s:
        row = await s.get(Setting, GUIDES_KEY)
        if row is None:
            s.add(Setting(key=GUIDES_KEY, value=keys))
        else:
            row.value = keys
            row.updated_at = utcnow_iso()
        await s.commit()
    forget_dismissed_guides()  # guide_for caches the list per process


@router.post("/guides/{key}/dismiss", response_class=HTMLResponse)
async def dismiss_guide(key: str):
    """Dismiss one page's guide strip. The strip is replaced by nothing."""
    if key not in GUIDES:
        return HTMLResponse("unknown guide", status_code=404)
    current = dismissed_guides()
    if key not in current:
        await _write_dismissed([*current, key])
    return HTMLResponse("")


@router.post("/guides/restore", response_class=HTMLResponse)
async def restore_guides():
    """Help's "Show the guides again": clear every dismissal."""
    await _write_dismissed([])
    return HTMLResponse(
        '<span class="saved" role="status" id="guides-result">Done. The guides are back on '
        "every page.</span>"
    )

