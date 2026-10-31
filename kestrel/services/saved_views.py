"""Pipeline's saved views (D-UI-96): a name, the filters and the sort, backed by `saved_view`,
with at most one default per track.

A view stores only the parameters the operator chose (`FILTER_KEYS`), never paging, the open
panel or the tab, so applying one is the same URL they would have built by hand. Deleting a view
changes nothing else; nothing here touches a role."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import SavedView

# The query parameters a view may hold: the chips, the search, More filters and the sort.
FILTER_KEYS: tuple[str, ...] = (
    "q", "stage", "needs_review", "shortlisted", "new_today", "saved", "hidden", "pay", "work",
    "posted", "warn", "live", "legit", "org", "source", "score_min", "score_max", "ghost_min",
    "verdict", "type",
)
NAME_MAX = 60


def clean_filters(params: Mapping[str, Any]) -> dict[str, str]:
    """The non-empty filter parameters from a form or a query, as plain strings."""
    out: dict[str, str] = {}
    for key in FILTER_KEYS:
        value = params.get(key)
        if value is not None and str(value).strip() != "":
            out[key] = str(value).strip()
    return out


def params_for(view: SavedView) -> dict[str, str]:
    """The query parameters that apply a view: its filters, then its sort."""
    out = clean_filters(view.filters or {})
    sort = (view.sort or {}).get("sort")
    if sort:
        out["sort"] = str(sort)
    return out


async def list_views(session: AsyncSession, track: str | None) -> list[SavedView]:
    """The track's views, default first, then by name."""
    stmt = select(SavedView).where(SavedView.track == track).order_by(
        SavedView.is_default.desc(), SavedView.name
    )
    return list((await session.execute(stmt)).scalars())


async def default_for(session: AsyncSession, track: str | None) -> SavedView | None:
    stmt = select(SavedView).where(SavedView.track == track, SavedView.is_default.is_(True))
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def save_view(
    session: AsyncSession, track: str | None, name: str, params: Mapping[str, Any]
) -> SavedView | None:
    """Create a view, or replace the filters of the one with the same name. None when the name
    is empty. The caller commits."""
    name = " ".join((name or "").split())[:NAME_MAX]
    if not name:
        return None
    filters = clean_filters(params)
    sort = filters.pop("sort", None) or str(params.get("sort") or "")
    existing = (
        await session.execute(
            select(SavedView).where(SavedView.track == track, SavedView.name == name).limit(1)
        )
    ).scalar_one_or_none()
    view = existing or SavedView(track=track, name=name, is_default=False)
    view.filters = filters
    view.sort = {"sort": sort} if sort else {}
    if existing is None:
        session.add(view)
    await session.flush()
    return view


async def set_default(session: AsyncSession, view_id: int, *, on: bool = True) -> bool:
    """Make a view its track's default (or clear it). The caller commits."""
    view = await session.get(SavedView, view_id)
    if view is None:
        return False
    if on:
        await session.execute(
            update(SavedView).where(SavedView.track == view.track).values(is_default=False)
        )
    view.is_default = on
    return True


async def delete_view(session: AsyncSession, view_id: int) -> bool:
    view = await session.get(SavedView, view_id)
    if view is None:
        return False
    await session.delete(view)
    return True
