"""Global chrome endpoints (SPEC-INTERFACES 2.1): the notification bell reading the notification
table, and the PWA service worker. The track switcher and command palette are client-side in
base.html; the run-status strip is served by routes_pages (/runs/strip)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from kestrel.db.models import Notification
from kestrel.web.deps import sessionmaker, templates

router = APIRouter()


@router.get("/notifications/bell", response_class=HTMLResponse)
async def bell():
    """Unread count for the bell badge. Empty string when zero so the badge hides (CSS :empty)."""
    async with sessionmaker()() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(Notification).where(Notification.read_at.is_(None))
            )
        ).scalar_one()
    return HTMLResponse(str(n) if n else "")


@router.get("/notifications/list", response_class=HTMLResponse)
async def notifications_list(request: Request):
    async with sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(Notification).order_by(Notification.created_at.desc()).limit(20)
                )
            )
            .scalars()
            .all()
        )
    return templates().TemplateResponse(request, "partials/notifications_list.html", {"rows": rows})


_SW = """
const CACHE = 'kestrel-v1';
const ASSETS = ['/static/css/kestrel.css', '/static/js/htmx.min.js',
  '/static/js/alpine.min.js', '/static/icon.svg', '/static/manifest.webmanifest'];
self.addEventListener('install', e =>
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting())));
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});
""".strip()


@router.get("/sw.js")
async def service_worker():
    # Served from the root so its scope covers the whole app (a /static/ path scopes too narrow).
    return Response(content=_SW, media_type="application/javascript")
