"""What is left on the user's own online key, for the status rail (#29, D-BYOK-12).

Only OpenRouter can say: `GET /key` returns the key's spend and limit. Anthropic and OpenAI have no
balance endpoint for an ordinary key (their usage and cost APIs need an admin key, which Kestrel
never asks for), so those providers, and every other, make **zero network calls** here and show
what the local transmission log recorded this month instead.

The one outbound request is made only when all of these hold: Hosted mode is on and acknowledged,
the backend is OpenRouter, a key is saved, and the user did not choose "free models only" (unless
they turned on Always show my OpenRouter balance). It carries the Authorization header and nothing
else: no body, no query, no attribution headers, nothing about the user. The answer is cached for
five minutes under a digest of the key (never the key itself), and a failure for one minute, so the
sidebar's 10 second poll costs a request about every five minutes at most. The key is never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import BackendConfig, Settings
from kestrel.db.models import FrontierTransmission, Setting
from kestrel.llm import providers as prov

log = structlog.get_logger()

SHOW_BALANCE_KEY = "ui.show_balance"
CACHE_TTL_S = 300.0
FAILURE_TTL_S = 60.0
REQUEST_TIMEOUT_S = 5.0
UNAVAILABLE = "Balance unavailable"

_cache: dict[tuple[str, str, str], tuple[float, Reading]] = {}


def _now() -> float:
    return time.monotonic()


def clear_cache() -> None:
    _cache.clear()
    _locks.clear()


@dataclass(frozen=True)
class Reading:
    ok: bool
    usage: float | None = None
    limit: float | None = None
    remaining: float | None = None
    usage_monthly: float | None = None


def usd(value: float) -> str:
    """Dollars for a line of text: cents above a cent, "less than $0.01" below it."""
    if value <= 0:
        return "$0.00"
    return f"${value:.2f}" if value >= 0.01 else "less than $0.01"


def _key_value(backend: BackendConfig) -> str:
    return backend.api_key or prov.secret_value(backend.api_key_env)


def _cache_key(name: str, backend: BackendConfig) -> tuple[str, str, str]:
    # The same shape as hosted_models._cache_key: backend, base URL, a digest of the key.
    digest = hashlib.sha256(_key_value(backend).encode("utf-8")).hexdigest()
    return (name, backend.base_url.rstrip("/"), digest)


# ------------------------------------------------------------------------------- who is asked
def eligible_backend(settings: Settings, *, show_balance: bool) -> tuple[str, BackendConfig] | None:
    """The OpenRouter backend whose balance may be fetched now, else None (no network at all)."""
    hosted = settings.llm.hosted_mode
    if not hosted.active:
        return None
    if hosted.free_only and not show_balance:
        return None
    # Only a backend hosted mode actually uses (a hosted tier's or the frontier tier's): a spare
    # OpenRouter key in .env must not be asked about, or shown, for an Anthropic user.
    in_use = {t.backend for t in settings.llm.hosted_tiers.values()}
    in_use.add(prov.frontier_backend(settings)[0])
    for name, backend in prov.external_backends(settings).items():
        if name in in_use and backend.provider == "openrouter" and _key_value(backend):
            return name, backend
    return None


async def show_balance_enabled(session: AsyncSession) -> bool:
    row = await session.get(Setting, SHOW_BALANCE_KEY)
    return bool(row.value) if row is not None else False


async def set_show_balance(session: AsyncSession, on: bool) -> None:
    row = await session.get(Setting, SHOW_BALANCE_KEY)
    if row is None:
        session.add(Setting(key=SHOW_BALANCE_KEY, value=on))
    else:
        row.value = on


# ---------------------------------------------------------------------------------- fetching
def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


async def _fetch(backend: BackendConfig) -> Reading:
    url = f"{backend.base_url.rstrip('/')}/key"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as http:
            r = await http.get(url, headers={"Authorization": f"Bearer {_key_value(backend)}"})
        if r.status_code != 200:
            log.info("balance_http_status", provider="openrouter", status=r.status_code)
            return Reading(False)
        data = (r.json() or {}).get("data") or {}
        return Reading(
            True, usage=_num(data.get("usage")), limit=_num(data.get("limit")),
            remaining=_num(data.get("limit_remaining")),
            usage_monthly=_num(data.get("usage_monthly")),
        )
    except httpx.HTTPError as exc:
        log.info("balance_unreachable", provider="openrouter", error_type=type(exc).__name__)
        return Reading(False)
    except (ValueError, AttributeError, TypeError) as exc:  # a 200 that is not the expected JSON
        log.info("balance_unreadable", provider="openrouter", error_type=type(exc).__name__)
        return Reading(False)


_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


def _fresh(key: tuple[str, str, str]) -> Reading | None:
    hit = _cache.get(key)
    if hit is not None:
        at, reading = hit
        if _now() - at < (CACHE_TTL_S if reading.ok else FAILURE_TTL_S):
            return reading
    return None


async def _reading(name: str, backend: BackendConfig) -> Reading:
    key = _cache_key(name, backend)
    if (reading := _fresh(key)) is not None:
        return reading
    # One fetch per key at a time: the rail, the tray's /api/status and a LAN client can all poll
    # as the cache expires, and each would otherwise wait on and send its own request.
    async with _locks.setdefault(key, asyncio.Lock()):
        if (reading := _fresh(key)) is not None:
            return reading
        reading = await _fetch(backend)
        _cache[key] = (_now(), reading)
        return reading


# A normal OpenRouter key reports only on itself: its own spend, and a cap if one was set on
# openrouter.ai. The account balance needs a management key (/api/v1/credits), which can create and
# delete keys, far more access than a balance line justifies, so Kestrel never asks for one.
KEY_ONLY_NOTE = (
    "This key's own figures. Your account balance is on openrouter.ai; OpenRouter shares it only "
    "with a management key."
)


def _row_from(reading: Reading) -> dict[str, str]:
    """One short line, the same with or without a key cap (owner decision): this month's spend on
    the key. The rail has room for nothing longer."""
    if not reading.ok or reading.usage_monthly is None:
        return {"kind": "unavailable", "label": UNAVAILABLE, "state": "warn"}
    return {"kind": "spent_month", "label": f"{usd(reading.usage_monthly)} spent this month",
            "state": "good", "note": KEY_ONLY_NOTE}


# ----------------------------------------------------------------------- local spend (no network)
def _month_start() -> str:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


async def local_spend(session: AsyncSession) -> dict[str, str] | None:
    """This month's deeper reads from the transmission log: the sum of the costs the provider
    reported. A send with no reported cost is unknown, never zero. None when nothing was sent."""
    total, sent, unknown = (
        await session.execute(
            select(
                func.coalesce(func.sum(FrontierTransmission.cost_usd), 0.0),
                func.count(),
                func.count() - func.count(FrontierTransmission.cost_usd),
            ).where(
                # Every send that left the machine was billed, including a reply that then
                # quarantined or failed (tier3 records its cost), not only status "sent".
                FrontierTransmission.egressed.is_(True),
                FrontierTransmission.created_at >= _month_start(),
            )
        )
    ).one()
    if not sent:
        return None
    if unknown == sent:
        label = "Deeper reads this month: cost not reported"
    elif unknown:
        label = f"{usd(total)} spent this month on deeper reads, plus some unreported"
    else:
        label = f"{usd(total)} spent this month on deeper reads"
    return {"kind": "local_spend", "label": label, "state": "idle"}


# ------------------------------------------------------------------------------ the rail row
async def rail_row(session: AsyncSession, settings: Settings) -> dict[str, str] | None:
    """The rail's balance line: {"kind", "label", "state"}, or None for no line. At most one
    request per five minutes, and none unless the user is opted in (see the module docstring)."""
    eligible = eligible_backend(settings, show_balance=await show_balance_enabled(session))
    if eligible is not None:
        return _row_from(await _reading(*eligible))
    has_key = any(
        _key_value(b) for b in prov.external_backends(settings).values()
    )
    return await local_spend(session) if has_key else None
