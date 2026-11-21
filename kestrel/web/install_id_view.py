"""The install ID as a page shows it (D-INST-1, D-INST-2): About and Settings > Your data.

Only a request from this computer gets the ID in its page context at all. A browser on another
device (LAN access) gets None, so no template path can send the ID off the computer; the page
says "Shown on this computer only" instead."""

from __future__ import annotations

import sqlite3

import structlog
from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError

from kestrel.services import install_id
from kestrel.web.auth import request_is_local
from kestrel.web.deps import sessionmaker

log = structlog.get_logger()


# Off this computer: no ID in the context at all, and the page says where to look. The note and
# the absence of the ID come from this one decision (AGENT-RULES 8a.3).
AWAY = {"away": "Shown on this computer only."}


async def for_request(request: Request) -> dict[str, str] | None:
    """`{"short", "full"}` for a request from this computer; AWAY (no ID) for any other; None
    when the database cannot answer yet, so the page shows no ID rather than an error."""
    if not request_is_local(request):
        return AWAY
    try:
        async with sessionmaker()() as s:
            rec = await install_id.ensure(s)
            await s.commit()
    except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
        log.warning("install_id_read_failed", error_type=type(exc).__name__)
        return None
    view = install_id.view(rec)
    return view if view["full"] else None
