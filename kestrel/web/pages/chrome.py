"""Global chrome endpoints (SPEC-INTERFACES 2.1, 6): the notification bell and its panel reading
the notification table, and the PWA service worker. The track switcher and command palette are
client-side in base.html; the run-status strip is served by routes_pages (/runs/strip).

The panel (D-UI-94, D-UI-152) has the tabs All and Needs attention; Dismissed, Dismiss and
Turn off this kind need new storage and are deferred. Every action
answers with the panel again, so only the list changes, and with the bell count out of band."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import and_, func, select

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Notification, Opportunity, Org, Pursuit
from kestrel.services.tracks import enabled_tracks_cached
from kestrel.web import notifications_view as nv
from kestrel.web.deps import sessionmaker, static_token, static_url, templates
from kestrel.web.words import words

log = structlog.get_logger()
router = APIRouter()


def _tab(raw: str | None) -> str:
    return raw if raw in nv.TABS else "all"


async def _unread(s) -> int:
    return (
        await s.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.read_at.is_(None))
        )
    ).scalar_one()


async def _tracks_of(s, rows: list[Notification]) -> tuple[dict[int, str], dict[int, str]]:
    """track by opportunity id and by pursuit id, for items with no stored track."""
    opp_ids = {n.opportunity_id for n in rows if n.opportunity_id}
    pur_ids = {n.pursuit_id for n in rows if n.pursuit_id}
    opp: dict[int, str] = {}
    pur: dict[int, str] = {}
    if opp_ids:
        found = await s.execute(
            select(Opportunity.id, Opportunity.track).where(Opportunity.id.in_(opp_ids))
        )
        opp = {i: t for i, t in found.all() if t}
    if pur_ids:
        found = await s.execute(select(Pursuit.id, Pursuit.track).where(Pursuit.id.in_(pur_ids)))
        pur = {i: t for i, t in found.all() if t}
    return opp, pur


async def _panel_ctx(s, tab: str) -> dict:
    now = datetime.now(UTC)
    rows = list(
        (await s.execute(select(Notification).order_by(Notification.created_at.desc()).limit(20)))
        .scalars()
        .all()
    )
    both = len(enabled_tracks_cached()) > 1
    opp_track, pur_track = await _tracks_of(s, rows) if both else ({}, {})
    from kestrel.services.follow_up import eligible

    due = [n for n in rows if n.kind == "next_action_due"]
    pursuits = (await s.execute(select(Pursuit).where(
        Pursuit.id.in_([n.pursuit_id for n in due if n.pursuit_id])
        | Pursuit.opportunity_id.in_([n.opportunity_id for n in due if n.opportunity_id]),
    ))).scalars().all() if due else []
    by_pursuit = {p.id: p for p in pursuits if eligible(p)}
    by_opp = {p.opportunity_id: p for p in pursuits if eligible(p)}
    items = []
    for n in rows:
        draft = by_pursuit.get(n.pursuit_id) or by_opp.get(n.opportunity_id)
        word, tone, attention = nv.human_severity(n.severity)
        track = opp_track.get(n.opportunity_id or 0) or pur_track.get(n.pursuit_id or 0)
        items.append({
            "id": n.id,
            "kind": n.kind,
            "title": n.title,
            "body": n.body,
            "unread": not n.read_at,
            "unresolved": attention,
            "severity_word": word,
            "tone": tone,
            "when": nv.relative_time(n.created_at, now),
            "group": nv.day_group(n.created_at, now),
            "track": words(track)["track_name"] if (both and track) else "",
            "action": nv.context_action(
                n.kind, draft.opportunity_id if draft else n.opportunity_id,
                follow_up=draft is not None,
            ),
            "attention": attention,
        })
    shown = items
    if tab == "attention":
        shown = [i for i in items if i["unresolved"]]
    groups = [(g, [i for i in shown if i["group"] == g]) for g in ("Today", "Yesterday", "Earlier")]
    return {
        "tab": tab,
        "groups": [(g, xs) for g, xs in groups if xs],
        "attention_n": sum(1 for i in items if i["unresolved"]),
        "unread": await _unread(s),
        "count": len(shown),
    }


def _panel(request: Request, ctx: dict) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/notifications_list.html", ctx)


@router.get("/notifications/bell", response_class=HTMLResponse)
async def bell():
    """Unread count for the bell badge (dismissed items do not count). Empty string when zero so
    the badge hides (CSS :empty)."""
    async with sessionmaker()() as s:
        n = await _unread(s)
    return HTMLResponse(str(n) if n else "")


@router.get("/notifications/list", response_class=HTMLResponse)
async def notifications_list(request: Request, tab: str = "all"):
    async with sessionmaker()() as s:
        ctx = await _panel_ctx(s, _tab(tab))
    return _panel(request, ctx)


_ACTIONS = ("read", "unread")


@router.post("/notifications/read-all", response_class=HTMLResponse)
async def notifications_read_all(request: Request):
    """Mark as read every notification not yet read (the panel header's Mark all as read). The
    answer is the panel again on the tab it was on (sent as a form field), with the bell count
    out of band, so the badge clears even when the panel body had not loaded yet."""
    from sqlalchemy import update

    form = await request.form()
    tab = str(form.get("tab") or request.query_params.get("tab") or "all")

    async with sessionmaker()() as s:
        await s.execute(
            update(Notification).where(Notification.read_at.is_(None)).values(read_at=utcnow_iso())
        )
        await s.commit()
        ctx = await _panel_ctx(s, _tab(tab))
    return _panel(request, ctx)


@router.post("/notifications/{note_id}/{action}", response_class=HTMLResponse)
async def notification_action(request: Request, note_id: int, action: str, tab: str = "all"):
    """Mark as read or unread. The answer is the panel again. An unknown id or action changes
    nothing."""
    async with sessionmaker()() as s:
        n = await s.get(Notification, note_id) if action in _ACTIONS else None
        if n is not None:
            if action == "read":
                n.read_at = n.read_at or utcnow_iso()
            elif action == "unread":
                n.read_at = None
            await s.commit()
        ctx = await _panel_ctx(s, _tab(tab))
    return _panel(request, ctx)


# ------------------------------------------------------------------------- command palette
_PALETTE_LIMIT = 5


@router.get("/palette/search")
async def palette_search(q: str = ""):
    """Roles and companies for the command palette (D-UI-91; surfaces-mockup-locked.html): up to
    five of each whose title or name holds the query, the canonical copy of a role only, never a
    hidden company, with the current employer redacted under confidential mode. Read only; the
    palette navigates, it never sends anything."""
    from kestrel.services.hidden_orgs import opp_visible_clause
    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.web.redact import redact_name, redact_text, redaction_map

    term = " ".join(q.split())[:80]
    if len(term) < 2:
        return {"roles": [], "companies": []}
    like = f"%{term}%"
    async with sessionmaker()() as s:
        conf = bool((await load_settings_with_db(s)).confidential_mode)
        mapping = await redaction_map(s) if conf else None
        best = (
            select(Evaluation.opportunity_id, func.max(Evaluation.score).label("score"))
            .group_by(Evaluation.opportunity_id)
            .subquery()
        )
        rows = (
            await s.execute(
                select(Opportunity.id, Opportunity.title, Opportunity.org_name, best.c.score)
                .outerjoin(best, best.c.opportunity_id == Opportunity.id)
                .where(Opportunity.title.ilike(like), Opportunity.is_canonical.is_(True),
                       opp_visible_clause())
                .order_by(best.c.score.desc().nulls_last(), Opportunity.id.desc())
                .limit(_PALETTE_LIMIT)
            )
        ).all()
        roles = []
        for oid, title, org, score in rows:
            org = redact_name(org, mapping, enabled=conf) if conf else org
            title = redact_text(title, mapping, enabled=conf) if conf else title
            sub = " · ".join(x for x in (org, f"Fit {score:.1f}" if score is not None else "") if x)
            roles.append({"t": title or "Untitled role", "s": sub, "href": f"/opportunities/{oid}"})
        # A company's count is its canonical roles (one row per role across boards, as the
        # Pipeline counts them); hidden companies are filtered before the limit.
        n_roles = func.count(Opportunity.id)
        orgs = (
            await s.execute(
                select(Org.id, Org.name, n_roles)
                .outerjoin(
                    Opportunity,
                    and_(Opportunity.org_id == Org.id, Opportunity.is_canonical.is_(True)),
                )
                .where(Org.name.ilike(like), Org.hidden_at.is_(None))
                .group_by(Org.id)
                .order_by(n_roles.desc(), Org.name)
                .limit(_PALETTE_LIMIT)
            )
        ).all()
        companies = []
        for org_id, name, n in orgs:
            shown = redact_name(name, mapping, enabled=conf) if conf else name
            noun = "role" if n == 1 else "roles"
            companies.append({"t": shown, "s": f"{n} {noun} in your Pipeline",
                              "href": f"/orgs/{org_id}"})
    return {"roles": roles, "companies": companies}


_SW = """
const CACHE = 'kestrel-__TOKEN__';
const ASSETS = __ASSETS__;
self.addEventListener('install', e =>
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting())));
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request).then(res => {
      if (res.status === 200) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return res;
    })));
  }
});
""".strip()

# The cache is named for this build's static files, so an upgrade replaces the old copies (a fixed
# name kept a LAN browser on the previous build's CSS and scripts), and the files are cached under
# the same versioned URLs the pages ask for.
_SW_ASSETS = ("css/kestrel.css", "js/htmx.min.js", "js/alpine.min.js", "icon.svg",
              "manifest.webmanifest")


@router.get("/sw.js")
async def service_worker():
    # Served from the root so its scope covers the whole app (a /static/ path scopes too narrow).
    body = _SW.replace("__TOKEN__", static_token()).replace(
        "__ASSETS__", json.dumps([static_url(p) for p in _SW_ASSETS]))
    return Response(content=body, media_type="application/javascript")
