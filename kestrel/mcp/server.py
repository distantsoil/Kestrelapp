"""The Kestrel MCP tool surface (SPEC-INTERFACES 4). build_mcp() registers ten read tools and five
action tools; sse_app() serves them over SSE behind the same three request-path controls as the web
app: Host/Origin pinned to this machine's own addresses (the DNS-rebinding disable SPEC §4 asked
for; fastmcp 3.4.5 moved it from the constructor to http_app), and the LAN shared key for any
non-loopback peer. Every tool reuses the same query/ingest code the web UI uses, and NONE of them
sends anything externally, submits anything, or makes a final document unseen."""

from __future__ import annotations

from fastmcp import FastMCP
from sqlalchemy import func, select

from kestrel.db.base import utcnow_iso
from kestrel.db.engine import get_sessionmaker
from kestrel.db.enums import valid_pursuit_status
from kestrel.db.models import (
    Contact,
    Evaluation,
    Opportunity,
    Org,
    OrgResearch,
    Pursuit,
    Source,
    StatusEvent,
    Story,
)
from kestrel.web.redact import confidential, redact_json, redact_name, redact_text

TOOL_NAMES = [
    "list_opportunities", "get_opportunity", "search_opportunities", "get_shortlist",
    "get_pursuits", "get_org", "get_story_bank", "get_comp_intel", "get_analytics",
    "get_run_status", "ingest_opportunity", "trigger_scan", "trigger_evaluation",
    "set_pursuit_status", "add_note",
]


def _opp_summary(
    opp: Opportunity,
    ev: Evaluation | None = None,
    mapping: dict[str, str] | None = None,
    *,
    conf: bool = False,
) -> dict:
    """MCP is an LLM agent's view of the operator's search, so it is the LAST place the current
    employer should leak. Confidential mode redacts everywhere, not only in the HTML views."""
    return {
        "id": opp.id, "title": redact_text(opp.title, mapping or {}, enabled=conf),
        "org": redact_name(opp.org_name, mapping or {}, enabled=conf), "track": opp.track,
        "stage": opp.stage, "verdict": ev.verdict if ev else None,
        "score": ev.score if ev else None, "ghost_score": opp.ghost_score,
        "liveness": opp.liveness, "closed_reason": opp.closed_reason, "url": opp.url,
    }


async def _client():
    """An LLM client on the settings the app runs with: the setting-table layer (hosted rows, a
    Settings-page model change) over YAML, never the YAML layer alone."""
    import httpx

    from kestrel.llm.client import LLMClient
    from kestrel.services.setting_overrides import load_settings_with_db

    async with get_sessionmaker()() as s:
        settings = await load_settings_with_db(s)
    return LLMClient(settings, http=httpx.AsyncClient())


def build_mcp() -> FastMCP:
    # SPEC §4 wrote the DNS-rebinding disable as FastMCP(transport_security=...); fastmcp 3.4.5
    # removed that kwarg, so it lives on http_app (see sse_app). Spec correction.
    mcp = FastMCP("kestrel")
    sm = get_sessionmaker

    # ---------------------------------------------------------------------- read tools
    @mcp.tool
    async def list_opportunities(
        track: str | None = None, stage: str | None = None, verdict: str | None = None,
        score_min: float | None = None, limit: int = 50,
    ) -> list[dict]:
        """Filter the pipeline by track/stage/verdict/score. Returns opportunity summaries."""
        from kestrel.web.queries import OppFilters
        from kestrel.web.queries import list_opportunities as _list

        f = OppFilters(track=track, stage=stage, verdict=verdict, score_min=score_min,
                       page_size=min(limit, 200))
        async with sm()() as s:
            page = await _list(s, f)
            conf, mapping = await confidential(s)
        return [_opp_summary(r.opp, r.ev, mapping, conf=conf) for r in page.rows]

    @mcp.tool
    async def get_opportunity(opportunity_id: int) -> dict:
        """Full opportunity record with its latest evaluation blocks and dimensions."""
        async with sm()() as s:
            opp = await s.get(Opportunity, opportunity_id)
            if opp is None:
                return {"error": "not found"}
            ev = (
                await s.execute(
                    select(Evaluation).where(Evaluation.opportunity_id == opportunity_id)
                    .order_by(Evaluation.id.desc()).limit(1)
                )
            ).scalar_one_or_none()
            conf, mapping = await confidential(s)
        out = _opp_summary(opp, ev, mapping, conf=conf)
        # The JD names the employer throughout. Redacting `org` while shipping the description raw
        # is redaction theater, and an MCP client is usually a HOSTED model, so this is the payload
        # Tier 3 already refuses to send unredacted.
        out["description_md"] = redact_text(opp.description_md, mapping, enabled=conf)
        if ev is not None:
            # The Tier 2 prose (dimension rationale, blocks, legitimacy signals) names the
            # employer as freely as the JD does; every string leaf goes through the same map.
            out["dimensions"] = redact_json(ev.dimensions, mapping, enabled=conf)
            out["blocks"] = redact_json(ev.blocks, mapping, enabled=conf)
            out["legitimacy"] = redact_json(ev.legitimacy, mapping, enabled=conf)
        return out

    @mcp.tool
    async def search_opportunities(
        query: str, track: str | None = None, limit: int = 10
    ) -> list[dict]:
        """Semantic search over opportunity descriptions using the embedding index."""
        from kestrel.intelligence.search import search_opportunities as _search

        client = await _client()
        try:
            async with sm()() as s:
                hits = await _search(s, client, query, track=track, limit=limit)
                conf, mapping = await confidential(s)
        finally:
            await client._http.aclose() if getattr(client, "_http", None) else None
        # SearchHit carries org_name and title directly rather than going through _opp_summary, so
        # returning `h.__dict__` raw made this the one read tool that leaked the current employer.
        return [
            {
                **h.__dict__,
                "org_name": redact_name(h.org_name, mapping, enabled=conf),
                "title": redact_text(h.title, mapping, enabled=conf),
            }
            for h in hits
        ]

    @mcp.tool
    async def get_shortlist(track: str | None = None, limit: int = 50) -> list[dict]:
        """Everything whose LATEST tier-2 verdict is apply/pursue, ranked by score."""
        async with sm()() as s:
            # Only each opportunity's latest tier-2 evaluation counts, so a re-evaluated reject
            # leaves the shortlist and no opportunity appears twice.
            latest = (
                select(Evaluation.opportunity_id,
                       func.max(Evaluation.id).label("mid"))
                .where(Evaluation.tier == 2)
                .group_by(Evaluation.opportunity_id)
                .subquery()
            )
            stmt = (
                select(Opportunity, Evaluation)
                .join(latest, latest.c.opportunity_id == Opportunity.id)
                .join(Evaluation, (Evaluation.id == latest.c.mid)
                      & (Evaluation.verdict.in_(("apply", "pursue"))))
                .order_by(Evaluation.score.desc().nulls_last())
                .limit(min(limit, 200))
            )
            if track:
                stmt = stmt.where(Opportunity.track == track)
            rows = (await s.execute(stmt)).all()
            conf, mapping = await confidential(s)
        return [_opp_summary(o, e, mapping, conf=conf) for o, e in rows]

    @mcp.tool
    async def get_pursuits(track: str | None = None) -> list[dict]:
        """The pursuit pipeline with statuses and next actions."""
        async with sm()() as s:
            stmt = select(Pursuit, Opportunity).join(
                Opportunity, Opportunity.id == Pursuit.opportunity_id
            )
            if track:
                stmt = stmt.where(Pursuit.track == track)
            rows = (await s.execute(stmt)).all()
            conf, mapping = await confidential(s)
        return [
            {"pursuit_id": p.id, "opportunity_id": p.opportunity_id, "track": p.track,
             "status": p.status, "next_action": p.next_action, "next_action_at": p.next_action_at,
             "title": redact_text(o.title, mapping, enabled=conf),
             "org": redact_name(o.org_name, mapping, enabled=conf)}
            for p, o in rows
        ]

    @mcp.tool
    async def get_org(org_id: int) -> dict:
        """An organization with its accumulated signals, latest research, and contacts."""
        async with sm()() as s:
            org = await s.get(Org, org_id)
            if org is None:
                return {"error": "not found"}
            research = (
                await s.execute(
                    select(OrgResearch).where(OrgResearch.org_id == org_id)
                    .order_by(OrgResearch.id.desc()).limit(1)
                )
            ).scalar_one_or_none()
            contacts = (
                (await s.execute(select(Contact).where(Contact.org_id == org_id))).scalars().all()
            )
            conf, mapping = await confidential(s)
        # Research prose, org signals, and contact records ("VP Engineering at Northwind") all
        # carry the employer in free text, so the whole payload is redacted leaf by leaf.
        return redact_json(
            {
                "id": org.id, "name": redact_name(org.name, mapping, enabled=conf),
                "size_band": org.size_band, "sector": org.sector,
                "ownership": org.ownership, "signals": org.signals or [],
                "research": ({"summary_md": research.summary_md, "signals": research.signals,
                              "unknowns": research.unknowns} if research else None),
                "contacts": [
                    {"name": c.name, "title": c.title, "type": c.contact_type} for c in contacts
                ],
            },
            mapping,
            enabled=conf,
        )

    @mcp.tool
    async def get_story_bank() -> dict:
        """Canonical stories with themes, plus theme coverage and gaps."""
        from kestrel.interview.coverage import theme_coverage

        async with sm()() as s:
            report = await theme_coverage(s)
            stories = (
                (await s.execute(select(Story).where(Story.is_canonical.is_(True)))).scalars().all()
            )
        return {
            "coverage": {"total": report.total_canonical, "gaps": report.gaps,
                         "covered": report.covered_themes, "note": report.note},
            "stories": [{"id": st.id, "title": st.title, "themes": st.themes,
                         "use_count": st.use_count, "operator_edited": st.operator_edited}
                        for st in stories],
        }

    @mcp.tool
    async def get_comp_intel(
        title: str, geo_bucket: str | None = None, org_size_band: str | None = None,
        currency: str = "USD",
    ) -> dict:
        """Comp corpus by title/geography/size WITH the sample size (insufficient below five)."""
        from kestrel.intelligence.comp import comp_range

        async with sm()() as s:
            rng = await comp_range(s, normalized_title=title, geo_bucket=geo_bucket,
                                   org_size_band=org_size_band, currency=currency)
        return rng.as_dict()

    @mcp.tool
    async def get_analytics(track: str = "employment") -> dict:
        """Funnel and score-vs-outcome summary for a track, with sample sizes."""
        from kestrel.analytics.metrics import collect_metrics

        async with sm()() as s:
            m = await collect_metrics(s, track)
        return {"track": track, "funnel": m["funnel"], "score_vs_outcome": m["score_vs_outcome"]}

    @mcp.tool
    async def get_run_status() -> dict:
        """Live progress of the active run."""
        from kestrel.web.queries import live_activity

        async with sm()() as s:
            return await live_activity(s)

    # ------------------------------------------------------------ action tools (none external)
    @mcp.tool
    async def ingest_opportunity(
        url: str | None = None, text: str | None = None, title: str | None = None,
        org_name: str | None = None, track: str = "employment",
    ) -> dict:
        """Add a URL or pasted text to the pipeline. Reads handed-over content; never crawls or acts
        externally."""
        from kestrel.ingest.manual import manual_ingest

        async with sm()() as s:
            opp = await manual_ingest(s, url=url, text=text, title=title, org_name=org_name,
                                      track=track)
        if opp is None:
            return {"error": "provide a url or text"}
        return {"id": opp.id, "title": opp.title, "stage": opp.stage, "track": opp.track}

    @mcp.tool
    async def trigger_scan(source_name: str) -> dict:
        """Enqueue a scrape for a named source. Only enqueues; the worker later fetches PUBLIC pages
        politely. Never acts externally in the tool itself."""
        from kestrel.queue.queue import TaskQueue

        async with sm()() as s:
            src = (
                await s.execute(select(Source).where(Source.name == source_name))
            ).scalar_one_or_none()
            if src is None:
                return {"error": f"no source named {source_name!r}"}
            if src.kind == "manual":
                # A manual source has no scrape adapter; enqueuing it would just churn a task.
                return {"error": "manual sources are not scrapeable; capture pages instead"}
        # Asked for by name: a switched-off track never holds it (finding 10).
        await TaskQueue(sm()).enqueue("scrape", {"source_id": src.id, "requested": True})
        return {"enqueued": True, "source_id": src.id}

    @mcp.tool
    async def trigger_evaluation(opportunity_id: int) -> dict:
        """Force a re-evaluation: reset the opportunity to a state Tier 0 sweeps again."""
        async with sm()() as s:
            opp = await s.get(Opportunity, opportunity_id)
            if opp is None:
                return {"error": "not found"}
            opp.stage = "new"
            # A re-evaluation runs on the configured models: no one-run pick rides along.
            from kestrel.services import run_model

            await run_model.discard(s, opportunity_id)
            await s.commit()
        return {"id": opportunity_id, "requeued": True}

    @mcp.tool
    async def set_pursuit_status(pursuit_id: int, status: str, note: str | None = None) -> dict:
        """Move a pursuit to a new status, writing a status_event. Rejects a status invalid for
        the pursuit's track."""
        async with sm()() as s:
            p = await s.get(Pursuit, pursuit_id)
            if p is None:
                return {"error": "not found"}
            if not valid_pursuit_status(p.track, status):
                return {"error": f"invalid status {status!r} for track {p.track}"}
            if status != p.status:
                s.add(StatusEvent(pursuit_id=p.id, from_status=p.status, to_status=status,
                                  note=note, occurred_at=utcnow_iso()))
                p.status = status
                p.last_activity_at = utcnow_iso()
            await s.commit()
        return {"pursuit_id": pursuit_id, "status": status}

    @mcp.tool
    async def add_note(pursuit_id: int, note: str) -> dict:
        """Append a dated note to a pursuit."""
        async with sm()() as s:
            p = await s.get(Pursuit, pursuit_id)
            if p is None:
                return {"error": "not found"}
            line = f"[{utcnow_iso()[:16]}] {note}"
            p.notes_md = (p.notes_md + "\n\n" + line) if p.notes_md else line
            p.last_activity_at = utcnow_iso()
            await s.commit()
        return {"pursuit_id": pursuit_id, "noted": True}

    return mcp


# Numeric loopback peers only, the same set kestrel/web/auth.py exempts and for the same reason:
# scope["client"] is always an address, and "localhost" here would be a trust entry that could
# activate under any server that put a hostname there.
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1"})


class RequireLanKey:
    """ASGI middleware: a non-loopback peer must present the LAN shared key in X-Kestrel-Key.

    This is the same third control the web app installs (kestrel/web/auth.py), applied to the MCP
    port. Without it, `bind_all` put every tool, including trigger_scan and the action tools, in
    front of any device on the LAN with no credential at all. Loopback stays exempt so a local
    MCP client (Claude Desktop, the CLI) needs no key, exactly like the web UI. Fails closed: with
    no secret generated yet, a LAN peer is refused rather than let in.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        peer = client[0] if client else None
        if peer in _LOOPBACK_PEERS:
            await self.app(scope, receive, send)
            return
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse

        from kestrel.web.auth import current_key, key_is_valid

        expected = await current_key()
        if not key_is_valid(Headers(scope=scope).get("x-kestrel-key"), expected):
            hint = (
                "no shared secret has been generated yet; open Kestrel on the host machine and "
                "generate one under Settings > LAN"
                if not expected
                else "present it as the X-Kestrel-Key header"
            )
            response = JSONResponse(
                {"detail": f"Kestrel MCP requires the LAN shared secret: {hint}"}, status_code=401
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _own_origins(addresses: list[str]) -> list[str]:
    """The Origin allowlist for bind_all: this machine's own addresses on any port. fastmcp
    normalizes an origin to scheme://host:port, and a trailing `:*` is the one wildcard it
    honors, so a bare address would only ever match port 80."""
    out = []
    for addr in addresses:
        host = f"[{addr}]" if ":" in addr else addr
        out.append(f"http://{host}:*")
    return out


def sse_app(*, bind_all: bool = False):
    """The MCP SSE ASGI app, wrapped so a non-loopback peer must present the LAN shared key.

    Under bind_all the Host and Origin allowlists are PINNED to local_addresses(), never "*". The
    first cut widened both to "*", which re-opened DNS rebinding at exactly the moment the port
    was most exposed: a rebound request arrives on loopback, where the key is exempt, so with any
    Host accepted every control was off at once. Same reasoning and same allowlist as app.py."""
    from fastmcp.server.http import HostOriginGuardMiddleware
    from starlette.middleware import Middleware

    from kestrel.web.auth import local_addresses

    mcp = build_mcp()
    # The guard is installed EXPLICITLY through `middleware`, which is the only one of http_app's
    # arguments the SSE transport honors. fastmcp 4.x's create_sse_app has no allowed_hosts,
    # allowed_origins or host_origin_protection parameters, so passing them to http_app (the
    # first cut) was silently dropped and the port had no Host guard at all, in either bind mode.
    # This is the same class fastmcp installs itself for the streamable-HTTP transport. Strict
    # mode always accepts the loopback names and adds the explicit lists, exactly the web app's
    # allowlist; a non-matching Host is a 421 and a foreign Origin a 403.
    own = local_addresses() if bind_all else None
    guard = Middleware(
        HostOriginGuardMiddleware,
        allowed_hosts=own,
        allowed_origins=_own_origins(own) if own else None,
        mode="strict",
    )
    inner = mcp.http_app(transport="sse", middleware=[guard])
    # Installed unconditionally, like the web app's: on a loopback-only bind every peer is
    # loopback and it is inert, so flipping bind_all can never expose an unkeyed MCP port.
    return RequireLanKey(inner)


async def serve_mcp(stop, *, port: int = 9110, bind_all: bool = False):
    """Serve the MCP SSE app on its own port until `stop` is set. Started from the app lifespan when
    settings.mcp.enabled, and by `kestrel mcp` standalone. Binds loopback unless bind_all, in which
    case it listens on the LAN with Host/Origin pinned to this machine's addresses and the shared
    key required of every non-loopback peer (see sse_app)."""
    import asyncio
    import contextlib

    import uvicorn

    host = "0.0.0.0" if bind_all else "127.0.0.1"  # noqa: S104 — LAN bind is opt-in (bind_all)
    config = uvicorn.Config(
        sse_app(bind_all=bind_all), host=host, port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve())
    waiter = asyncio.create_task(stop.wait())
    try:
        # Whichever ends first (review finding 7): the operator's stop, or the inner server
        # itself (a failed startup, a crash). Either way serve_mcp ends, so the runtime's
        # running() reflects a dead server instead of reporting Running.
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        await task  # re-raises the server's own exception, if it had one
    if not stop.is_set():
        raise RuntimeError("the MCP server stopped unexpectedly")
