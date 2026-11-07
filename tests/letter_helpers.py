"""Shared helpers for the cover letter tests.

A draft is a queued `letter_draft` task that only the serve worker runs (after the tiers, never
while paused), so a test that wants the finished letter plays the worker's step itself: it posts
the form, runs one letter pass, and asks the status route the drafting card polls."""

from __future__ import annotations

import asyncio

from bs4 import BeautifulSoup


def form_fields(html: str, *, ticked: bool = True) -> dict[str, str]:
    """What a browser posts for the form in `html`: every named hidden field, and a named
    checkbox only when it is ticked (an unticked box posts nothing). `ticked=False` is the
    person who pressed the button without ticking the box."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("form#cl-ask") or soup
    out: dict[str, str] = {}
    for i in form.find_all("input"):
        name = i.get("name")
        if not name:
            continue
        if i.get("type") == "hidden":
            out[name] = i.get("value", "")
        elif i.get("type") == "checkbox" and ticked:
            out[name] = i.get("value", "on")
    return out


def drain_letters() -> int:
    """The worker's letter step, once."""
    from kestrel.web.pages import role as role_page

    return asyncio.run(role_page.run_letter_drafts())


def generate(c, url: str, data=None):
    """Post the generate form. When that queued a draft (the response is the watching card), run
    the worker's letter step and return what the status route then shows."""
    r = c.post(url, data=data or {})
    if "data-watching" not in r.text:
        return r
    drain_letters()
    return c.get(url.rsplit("/", 1)[0] + "/status")


def letter_body(render_id: int) -> str:
    """The stored text of the letter attached to this render (the `letter` table, D-LETTER-1)."""
    from sqlalchemy import select

    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Letter

    async def go() -> str:
        async with get_sessionmaker()() as s:
            row = (await s.execute(
                select(Letter).where(Letter.render_id == render_id))).scalars().one()
            return row.body_md

    return asyncio.run(go())
