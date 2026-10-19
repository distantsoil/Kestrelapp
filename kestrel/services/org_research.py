"""One shared entry point for company research (P5), used by the role page's research action and
`kestrel research`. Both callers get identical behavior: settings from the database layer, a polite
fetching client for PUBLIC pages only (descriptive User-Agent, per-host rate limit, robots.txt
respected, backoff), the model reached only through `research_org` (so hosted-mode policy and
redaction apply unchanged), and a commit at the end. Nothing is sent or posted anywhere."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Org, OrgResearch
from kestrel.ingest.http import HttpClient
from kestrel.llm.client import LLMClient
from kestrel.research.company import research_org
from kestrel.services.setting_overrides import load_settings_with_db


async def run_org_research(
    session: AsyncSession,
    org: Org,
    *,
    researcher: Callable[..., Any] | None = None,
) -> OrgResearch:
    """Research `org` and commit the resulting record.

    A real fetching client, not http=None: with no client every fetching tier in acquire_excerpts
    short-circuits and only the DB-derived ATS tier contributes, which yields one excerpt where
    research_org requires two. `researcher` defaults to `research_org`; a caller may pass its own
    reference to it (the web action does, so its module-level seam stays patchable)."""
    run = researcher if researcher is not None else research_org
    settings = await load_settings_with_db(session)
    async with (
        httpx.AsyncClient() as llm_http,
        HttpClient(
            user_agent=settings.ingest.user_agent,
            default_rate_per_sec=settings.ingest.default_rate_per_sec,
            timeout=settings.ingest.request_timeout_s,
            respect_robots=settings.ingest.respect_robots,
        ) as http,
    ):
        client = LLMClient(settings, http=llm_http)
        record = await run(session, client, org, settings=settings, http=http)
    await session.commit()  # research_org flushes but does not commit
    return record
