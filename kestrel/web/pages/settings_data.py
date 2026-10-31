"""Settings > Your data (D-UI-106, D-UI-114, D-UI-115, SPEC-INTERFACES 2.6 item 5).

  GET  /settings/data/backup                     a timestamped zip (the Save dialog) + the stamp
  GET  /settings/data/applied  POST .../read | review | add | undo   Import past applications
  GET  /settings/data/applied/sample             the sample file
  GET  /settings/data/contacts POST .../read | apply | remove        Your contacts (LinkedIn)
  GET  /settings/data/contacts/how | sample      the four steps, and the sample file

Restore and Download diagnostics reuse `POST /settings/restore` and the About routes. Restart
setup and Clear everything post to `/onboarding/restart`, which already keeps the order the spec
asks for (backup first, RESET typed, keys kept unless unticked).

Nothing here acts externally (hard rule 1): both imports read a file the operator chose, the
LinkedIn file is never fetched, Kestrel never signs in there, and the email column of the file is
never read (D-UI-131)."""

from __future__ import annotations

import asyncio
import csv
import io as _io
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask

from kestrel import paths
from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    Contact,
    ContactImport,
    Opportunity,
    Org,
    Pursuit,
    Setting,
    StatusEvent,
)
from kestrel.research import connections as cx
from kestrel.services import applied_import as svc
from kestrel.services import backup as backup_service
from kestrel.services import pursuit_undo, xlsx_text
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import desktop_bridge
from kestrel.web import settings_io as io
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import fixed_reason, plain_error
from kestrel.web.pages.settings_roles import trigger
from kestrel.web.uploads import UploadTooLarge, large_form, read_upload

log = structlog.get_logger()
router = APIRouter()

LAST_BACKUP_KEY = "backup.last_at"
MAX_FILE = 25 * 1024 * 1024  # a LinkedIn export of 100,000 people is a few megabytes
UNDO_APPLIED = "undo.applied."
UNDO_KEEP_S = 24 * 3600

# ------------------------------------------------------------------------------ backups
_UNITS = (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10))


def human_size(n: int) -> str:
    for unit, size in _UNITS:
        if n >= size:
            return f"{n / size:.1f} {unit}" if unit == "GB" else f"{round(n / size)} {unit}"
    return f"{n} bytes"


def dir_size(root: Path, *, budget_s: float = 1.5) -> int | None:
    """Bytes under `root`, or None when counting did not finish in `budget_s`. Never raises."""
    end = time.monotonic() + budget_s
    total = 0
    stack = [str(root)]
    while stack:
        if time.monotonic() > end:
            return None
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "never"
    secs = (datetime.now(UTC) - then).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} minutes ago"
    if secs < 86400:
        n = int(secs // 3600)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = int(secs // 86400)
    return f"{n} day{'s' if n != 1 else ''} ago"


async def backup_ctx() -> dict:
    async with sessionmaker()() as s:
        last = await io.read(s, LAST_BACKUP_KEY)
    size = await asyncio.to_thread(dir_size, paths.data_home())
    return {
        "data_dir": str(paths.data_home()),
        "data_size": human_size(size) if size is not None else "",
        "last_backup": _ago(last if isinstance(last, str) else None),
        "can_open_folder": desktop_bridge.can_open_folder(),
        "desktop": desktop_bridge.can_reveal(),
    }


@router.get("/settings/data/backup")
async def backup_download():
    """A fresh backup zip, named with the time. The data and settings go in; online model keys and
    logs never do (kestrel.services.backup). The stamp is recorded for "Last backup"."""
    try:
        archive = await asyncio.to_thread(backup_service.create_backup_to_temp)
    except OSError as exc:
        log.warning("backup_build_failed", error_type=type(exc).__name__, error=str(exc))
        return Response(f"Could not build the backup: {fixed_reason(exc)}", status_code=500,
                        media_type="text/plain")
    async with sessionmaker()() as s:
        await io.write(s, LAST_BACKUP_KEY, utcnow_iso())
        await s.commit()
    return FileResponse(
        archive, media_type="application/zip", filename=archive.name,
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(backup_service.discard_temp_backup, archive),
    )


# ------------------------------------------------------------- Import past applications
_SAMPLE_APPLIED = (
    "Company,Job title,Applied on,Status,Notes\n"
    "Birchwood Freight,Operations Analyst,2026-09-04,No response,Easy Apply\n"
    "Northgate Supply,Supply Chain Planner,2026-08-29,Interview,Phone screen 9/5\n"
    "Harbor and Pine,Logistics Coordinator,2026-08-12,Rejected,\n"
    "Cedar Ridge Foods,Planning Manager,2026-08-05,No response,Company site\n"
    "Alder Works,Process Engineer,2026-07-30,Applied,Referral from a friend\n"
)
APPLIED_LABELS = {
    "company": "Company", "title": "Role", "date": "Date applied", "status": "Stage",
    "source": "Where you found it", "url": "Link", "location": "Place", "notes": "Notes",
    "track": "Track",
}


@router.get("/settings/data/applied/sample")
async def applied_sample():
    return Response(_SAMPLE_APPLIED, media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="kestrel-sample-applications.csv"'})


def _imp_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_import.html", ctx,
                                        headers=headers or {})


def _imp_base(**extra: Any) -> dict:
    return {"step": 0, "file_name": "", "text": "", "error": "", "cols": [], "n_rows": 0,
            "review": None, "result": None, "labels": APPLIED_LABELS, **extra}


async def _file_text(request: Request) -> tuple[str, str, str]:
    """(text, file name, error) from the chosen file or the sample. A spreadsheet is read as its
    first sheet; every other file is read as text (UTF-8, a BOM tolerated)."""
    form = await large_form(request, max(svc.MAX_ROWS, cx.MAX_ROWS))
    if form.get("sample"):
        return _SAMPLE_APPLIED, "sample-applications.csv", ""
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read") or not getattr(upload, "filename", ""):
        return "", "", "Choose a file first."
    name = str(upload.filename)[:120]
    try:
        data = await read_upload(upload, limit=MAX_FILE, too_large=UploadTooLarge)
    except UploadTooLarge:
        return "", name, "That file is larger than 25 MB, which is more than a tracker needs."
    if xlsx_text.looks_like_xlsx(data):
        try:
            return await asyncio.to_thread(xlsx_text.to_csv, data), name, ""
        except xlsx_text.SpreadsheetError as exc:
            return "", name, plain_error(exc)
    return data.decode("utf-8-sig", errors="replace"), name, ""


@router.get("/settings/data/applied", response_class=HTMLResponse)
async def applied_card(request: Request):
    return _imp_card(request, _imp_base())


@router.post("/settings/data/applied/read", response_class=HTMLResponse)
async def applied_read(request: Request):
    """Step 1 to 2: read the file and show how each column matches. Nothing is stored."""
    text, name, error = await _file_text(request)
    if error:
        return _imp_card(request, _imp_base(error=error))
    try:
        headers, rows, _fmt = await asyncio.to_thread(svc.parse_table, text)
    except ValueError as exc:
        return _imp_card(request, _imp_base(error=str(exc), file_name=name))
    found = svc.map_columns(headers)
    by_header = {h: c for c, h in found.items()}
    cols = [{"i": i, "header": h or f"Column {i + 1}", "canonical": by_header.get(h, "")}
            for i, h in enumerate(headers)]
    return _imp_card(request, _imp_base(step=1, text=text, file_name=name, cols=cols,
                                        n_rows=len(rows)))


def _normalized(text: str, mapping: dict[int, str]) -> str:
    """The file as CSV with the operator's column choices written as the names the importer
    reads (the first alias of each), skipped columns dropped. One column per field."""
    headers, rows, _ = svc.parse_table(text)
    used: dict[str, int] = {}
    for i, canon in mapping.items():
        if canon in svc.COLUMN_ALIASES and canon not in used and i < len(headers):
            used[canon] = i
    order = list(used.items())
    out = _io.StringIO()
    w = csv.writer(out)
    w.writerow([svc.COLUMN_ALIASES[c][0] for c, _i in order])
    for r in rows:
        w.writerow([r[i] if i < len(r) else "" for _c, i in order])
    return out.getvalue()


def _mapping(form: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    for k, v in form.items():
        if str(k).startswith("map_") and str(k)[4:].isdigit():
            out[int(str(k)[4:])] = str(v)
    return out


@router.post("/settings/data/applied/review", response_class=HTMLResponse)
async def applied_review(request: Request):
    """Step 2 to 3: apply the column choices and show every row, each untickable."""
    form = await large_form(request, max(svc.MAX_ROWS, cx.MAX_ROWS))
    text = str(form.get("text") or "")
    name = str(form.get("file_name") or "")
    if not text.strip():
        return _imp_card(request, _imp_base(error="Choose a file first."))
    mapping = _mapping(form)
    try:
        norm = await asyncio.to_thread(_normalized, text, mapping)
    except ValueError as exc:
        return _imp_card(request, _imp_base(error=str(exc)))
    async with sessionmaker()() as s:
        rev = await svc.review(s, norm)
    if rev.errors or not rev.rows:
        headers, rows, _ = await asyncio.to_thread(svc.parse_table, text)
        cols = [{"i": i, "header": h or f"Column {i + 1}", "canonical": mapping.get(i, "")}
                for i, h in enumerate(headers)]
        msg = rev.errors[0] if rev.errors else "There are no rows to add."
        if rev.missing:
            msg = ("Match at least Company, Role, Date applied and Stage. Still missing: "
                   + ", ".join(APPLIED_LABELS[c] for c in rev.missing) + ".")
        return _imp_card(request, _imp_base(step=1, text=text, file_name=name, cols=cols,
                                            n_rows=len(rows), error=msg))
    return _imp_card(request, _imp_base(step=2, text=norm, file_name=name, review=rev,
                                        n_rows=len(rev.rows),
                                        status_options=svc.status_options("employment")))


def _choices(form: Any, rows: list[svc.ImportRow]) -> dict[int, svc.RowChoice]:
    return {
        r.index: svc.RowChoice(
            include="on" in form.getlist(f"include_{r.index}"),
            status=str(form.get(f"status_{r.index}") or "") or None,
            allow_reposts="on" in form.getlist(f"reposts_{r.index}"),
        )
        for r in rows
    }


@router.post("/settings/data/applied/add", response_class=HTMLResponse)
async def applied_add(request: Request):
    """Step 3 to 4: write only the ticked rows. Nothing is added before this press. The Undo
    token carries the pursuits and hidden roles this created and the cards it changed, and
    removes only those."""
    form = await large_form(request, max(svc.MAX_ROWS, cx.MAX_ROWS))
    text = str(form.get("text") or "")
    name = str(form.get("file_name") or "")
    if not text.strip():
        return _imp_card(request, _imp_base(error="Nothing to add: choose a file first."))
    async with sessionmaker()() as s:
        rev = await svc.review(s, text)
        if rev.errors or not rev.rows:
            return _imp_card(
                request, _imp_base(error="That file could not be read again. Choose it again.")
            )
        choices = _choices(form, rev.rows)
        before: list[dict] = []
        for r in rev.rows:
            ch = choices.get(r.index)
            if ch and ch.include and r.existing_pursuit_id is not None:
                p = await s.get(Pursuit, r.existing_pursuit_id)
                if p is not None:
                    snap = await pursuit_undo.snapshot(s, p)
                    last = (
                        await s.execute(
                            select(func.max(StatusEvent.id)).where(StatusEvent.pursuit_id == p.id)
                        )
                    ).scalar_one()
                    before.append(
                        {"pursuit_id": p.id, "fields": snap["fields"], "last_event": last or 0}
                    )
        try:
            result = await svc.commit(s, rev, choices, commit_transaction=False)
        except (SQLAlchemyError, ValueError) as exc:
            await s.rollback()
            log.warning("applied_import_failed", error_type=type(exc).__name__)
            return _imp_card(
                request,
                _imp_base(
                    step=2,
                    text=text,
                    file_name=name,
                    review=rev,
                    n_rows=len(rev.rows),
                    status_options=svc.status_options("employment"),
                    error=f"Nothing was added: the write failed ({type(exc).__name__}). "
                    "Your review is still here.",
                ),
            )
        for ch in before:
            p = await s.get(Pursuit, ch["pursuit_id"])
            ch["after"] = {
                k: getattr(p, k) for k in ch["fields"] if getattr(p, k) != ch["fields"][k]
            }
            ch["events_after"] = await _import_events(s, p.id)
            ch["event_ids"] = [e["id"] for e in ch["events_after"] if e["id"] > ch["last_event"]]
        created = []
        for pid in result.created_pursuit_ids:
            p = await s.get(Pursuit, pid)
            created.append(
                {
                    "row": pursuit_undo.row_dict(p),
                    "dependents": await pursuit_undo.dependents(s, pid),
                }
            )
        token = secrets.token_urlsafe(9)
        await _save_undo(
            s,
            token,
            {
                "created_pursuits": result.created_pursuit_ids,
                "created_opps": [
                    pursuit_undo.row_dict(await s.get(Opportunity, oid))
                    for oid in result.created_opp_ids
                ],
                "changed": before,
                "created": created,
            },
        )
        await s.commit()
    n = result.imported + result.updated
    msg = f"{n} application{'' if n == 1 else 's'} added."
    return _imp_card(
        request,
        _imp_base(step=3, result=result, n_rows=n, file_name=name),
        trigger(
            msg,
            undo=f"/settings/data/applied/undo/{token}",
            refresh={"refresh": "/settings/data/applied", "target": "#st-imp"},
        ),
    )


async def _save_undo(session: Any, token: str, data: dict) -> None:
    cutoff = datetime.fromtimestamp(time.time() - UNDO_KEEP_S, UTC).isoformat()
    await session.execute(
        delete(Setting).where(Setting.key.like(UNDO_APPLIED + "%"), Setting.updated_at < cutoff)
    )
    session.add(Setting(key=UNDO_APPLIED + token, value=data, updated_at=utcnow_iso()))


async def _import_events(session: Any, pid: int) -> list[dict]:
    rows = (
        await session.execute(
            select(StatusEvent).where(StatusEvent.pursuit_id == pid).order_by(StatusEvent.id)
        )
    ).scalars()
    return [pursuit_undo.row_dict(e) for e in rows]


@router.post("/settings/data/applied/undo/{token}")
async def applied_undo(token: str):
    """Remove just the rows that Add created, and put back the cards it changed. Used up on the
    first press; a card the operator has moved on since keeps their later changes."""
    async with sessionmaker()() as s:
        row = await s.get(Setting, UNDO_APPLIED + token)
        if row is None or not isinstance(row.value, dict):
            return io.refuse(io.NO_UNDO)
        data = dict(row.value)
        await s.delete(row)
        cutoff = datetime.fromtimestamp(time.time() - UNDO_KEEP_S, UTC).isoformat()
        if row.updated_at < cutoff or "created" not in data:
            await s.commit()
            return io.refuse(io.NO_UNDO)
        # Validate the complete import before touching any application or history.
        for ch in data.get("changed", []):
            p = await s.get(Pursuit, ch["pursuit_id"])
            if (
                p is None
                or "after" not in ch
                or any(getattr(p, k) != v for k, v in ch["after"].items())
                or await _import_events(s, p.id) != ch["events_after"]
            ):
                await s.commit()
                return io.refuse(io.NO_UNDO)
        for saved in data["created"]:
            p = await s.get(Pursuit, saved["row"]["id"])
            if (
                p is None
                or pursuit_undo.row_dict(p) != saved["row"]
                or await pursuit_undo.dependents(s, p.id) != saved["dependents"]
            ):
                await s.commit()
                return io.refuse(io.NO_UNDO)
        for saved in data.get("created_opps", []):
            opp = await s.get(Opportunity, saved["id"])
            if opp is None or pursuit_undo.row_dict(opp) != saved:
                await s.commit()
                return io.refuse(io.NO_UNDO)
        removed = 0
        for saved in data["created"]:
            p = await s.get(Pursuit, saved["row"]["id"])
            await s.execute(delete(StatusEvent).where(StatusEvent.pursuit_id == p.id))
            await s.delete(p)
            removed += 1
        await s.flush()
        for saved in data.get("created_opps") or []:
            opp = await s.get(Opportunity, saved["id"])
            if opp is None:
                continue
            has = (
                await s.execute(
                    select(func.count())
                    .select_from(Pursuit)
                    .where(Pursuit.opportunity_id == opp.id)
                )
            ).scalar_one()
            if not has:
                try:
                    async with s.begin_nested():
                        await s.delete(opp)
                except SQLAlchemyError:
                    pass  # it is referenced elsewhere: it stays hidden, which is harmless
        for ch in data.get("changed") or []:
            p = await s.get(Pursuit, int(ch["pursuit_id"]))
            if p is None:
                continue
            for k in ch["after"]:
                setattr(p, k, ch["fields"][k])
            await s.execute(
                delete(StatusEvent).where(
                    StatusEvent.pursuit_id == p.id, StatusEvent.id.in_(ch["event_ids"])
                )
            )
        await s.commit()
    return io.reply(f"Removed the {removed} application{'' if removed == 1 else 's'} just added.")


# ------------------------------------------------------------------------- Your contacts
_SAMPLE_CONTACTS = (
    "Notes:\n"
    '"When exporting your connection data, you may notice that some of the email addresses are '
    'missing."\n\n'
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Sam,Ortiz,https://example.com/in/sam,sam@example.com,Larkspur Logistics,Ops Manager,"
    "12 Aug 2024\n"
    "Priya,Nair,https://example.com/in/priya,,Northgate Supply Co.,Planner,03 Mar 2023\n"
    "Marcus,Bell,https://example.com/in/marcus,,Alder Works,Engineer,19 Nov 2022\n"
    "Elena,Ruiz,https://example.com/in/elena,elena@example.com,Birchwood Freight,Analyst,"
    "07 Jan 2025\n"
    "Dana,Whitfield,https://example.com/in/dana,,Cedar Ridge,Director,25 Jun 2021\n"
    "Tom,Alvarez,https://example.com/in/tom,,,Consultant,14 Feb 2020\n"
    "Wen,Zhao,https://example.com/in/wen,,Harbor & Pine LLC,Senior Planner,30 Sep 2024\n"
    "Lee,Okafor,https://example.com/in/lee,,Meadowbrook Foods,Director,11 May 2023\n"
)
HOW_STEPS = (
    "Sign in to LinkedIn in your browser and open Settings.",
    "Choose Data privacy, then Get a copy of your data.",
    "Choose Connections and request the file.",
    "Download the file when LinkedIn emails you.",
)


@router.get("/settings/data/contacts/sample")
async def contacts_sample():
    return Response(_SAMPLE_CONTACTS, media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="kestrel-sample-connections.csv"'})


@router.get("/settings/data/contacts/how", response_class=HTMLResponse)
async def contacts_how(request: Request):
    return templates().TemplateResponse(request, "partials/st_ct_how.html", {"steps": HOW_STEPS})


async def contacts_state(request: Request) -> dict:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        n = await cx.imported_contact_count(s)
        last = (await s.execute(
            select(ContactImport).order_by(ContactImport.id.desc()).limit(1)
        )).scalars().first()
        in_pipe = 0
        if n:
            live_orgs = select(Opportunity.org_id).where(
                Opportunity.org_id.is_not(None), Opportunity.archived_at.is_(None),
                Opportunity.stage != "t0_reject",
            ).distinct()
            in_pipe = (await s.execute(
                select(func.count()).select_from(Contact).where(
                    Contact.source == cx.SOURCE, Contact.org_id.in_(live_orgs))
            )).scalar_one()
    when = ""
    if last is not None:
        try:
            from kestrel.date_display import format_display

            when = format_display(
                last.imported_at, "region", date_format=settings.region.date_format
            )
        except ValueError:
            when = ""
    return {"ct_n": n, "ct_when": when, "ct_pipe": in_pipe, "ct_first": last is None}


def _ct_base(**extra: Any) -> dict:
    return {"ct_step": "idle", "ct_error": "", "ct_notice": "", "ct_preview": None, **extra}


def _ct_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_contacts.html", ctx,
                                        headers=headers or {})


@router.get("/settings/data/contacts", response_class=HTMLResponse)
async def contacts_card(request: Request):
    return _ct_card(request, {**_ct_base(), **await contacts_state(request)})


def _dump(parsed: cx.ParsedFile) -> str:
    return json.dumps([[r.first_name, r.last_name, r.company, r.position, r.connected_on]
                       for r in parsed.rows], separators=(",", ":"), ensure_ascii=False)


def _load(payload: str, skipped: int = 0) -> cx.ParsedFile:
    out = cx.ParsedFile(skipped_no_name=skipped)
    for row in json.loads(payload):
        first, last, company, position, on = row
        out.rows.append(cx.ParsedConnection(
            first_name=str(first or "")[:cx.MAX_FIELD], last_name=str(last or "")[:cx.MAX_FIELD],
            company=(str(company)[:cx.MAX_FIELD] if company else None),
            position=(str(position)[:cx.MAX_FIELD] if position else None),
            connected_on=(str(on)[:10] if on else None)))
    if len(out.rows) > cx.MAX_ROWS:
        raise ValueError("Too many rows.")
    return out


async def _similar(session: Any, name: str, org_id: int | None) -> list[dict]:
    """Companies in Kestrel whose name starts like this one, for the Change select."""
    toks = cx.company_tokens(name)
    if not toks:
        return []
    rows = (await session.execute(
        select(Org.id, Org.name).where(Org.name.ilike(f"{toks[0]}%")).order_by(Org.name).limit(6)
    )).all()
    return [{"id": int(i), "name": str(n)} for i, n in rows if int(i) != org_id]


@router.post("/settings/data/contacts/read", response_class=HTMLResponse)
async def contacts_read(request: Request):
    """Phase 1: read the file, show the counts, the company matches and (for a newer file) what
    changed. Nothing is stored. The email column is never read."""
    form = await large_form(request, max(svc.MAX_ROWS, cx.MAX_ROWS))
    base = await contacts_state(request)
    if form.get("sample"):
        text, name = _SAMPLE_CONTACTS, "sample-connections.csv"
    else:
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read") or not getattr(upload, "filename", ""):
            return _ct_card(request, {**_ct_base(ct_step="idle", ct_error="Choose a file first."),
                                      **base})
        name = str(upload.filename)[:120]
        try:
            data = await read_upload(upload, limit=MAX_FILE, too_large=UploadTooLarge)
        except UploadTooLarge:
            return _ct_card(request, {**_ct_base(ct_error="That file is larger than 25 MB, which "
                                                 "is more than a connections export."), **base})
        text = data.decode("utf-8-sig", errors="replace")
    try:
        parsed = cx.parse_connections_csv(text)
    except cx.ConnectionsFileError as exc:
        return _ct_card(request, {**_ct_base(ct_error=plain_error(exc)), **base})
    async with sessionmaker()() as s:
        prev = await cx.preview_import(s, parsed)
        sure = [c for c in prev.companies if c.match in (cx.MATCH_SURE, cx.MATCH_MANUAL)]
        possible = [c for c in prev.companies if c.match == cx.MATCH_POSSIBLE]
        poss_rows = []
        for i, c in enumerate(possible):
            poss_rows.append({"i": i, "c": c, "similar": await _similar(s, c.company_name,
                                                                          c.org_id)})
    keys = {cx.company_key(c.company_name) for c in prev.companies
            if c.match != cx.MATCH_NONE}
    n_other = sum(1 for r in parsed.rows
                  if cx.company_key(r.company) and cx.company_key(r.company) not in keys)
    ctx = _ct_base(
        ct_step="review", ct_preview=prev, ct_sure=sure, ct_possible=poss_rows,
        ct_payload=_dump(parsed), ct_skipped=parsed.skipped_no_name, ct_file=name,
        ct_other=n_other, ct_no_company=prev.n_no_company, ct_missing=prev.missing[:3],
        ct_missing_more=max(0, len(prev.missing) - 3),
        ct_changed=prev.changed[:12], ct_changed_more=max(0, len(prev.changed) - 12),
    )
    return _ct_card(request, {**ctx, **base})


@router.post("/settings/data/contacts/apply", response_class=HTMLResponse)
async def contacts_apply(request: Request):
    """Phase 1 and 2 together: store the file as reviewed. Rulings on Possible matches are
    recorded after the import so they attach to the people it created."""
    form = await large_form(request, max(svc.MAX_ROWS, cx.MAX_ROWS))
    try:
        parsed = _load(str(form.get("payload") or "[]"), int(form.get("skipped") or 0))
    except (ValueError, TypeError, KeyError):
        return _ct_card(request, {**_ct_base(ct_error="That review expired. Choose the file "
                                             "again."), **await contacts_state(request)})
    keep = str(form.get("keep_missing") or "1") != "0"
    name = str(form.get("file_name") or "")[:120] or None
    async with sessionmaker()() as s:
        prev = await cx.preview_import(s, parsed)
        try:
            batch = await cx.apply_import(s, parsed, file_name=name, keep_missing=keep)
        except (SQLAlchemyError, ValueError) as exc:
            log.warning("connections_import_failed", error_type=type(exc).__name__)
            return _ct_card(request, {**_ct_base(
                ct_error="Nothing was imported: the write failed. Try again."),
                **await contacts_state(request)})
        # Rulings are keyed by the company name posted with each choice, never by list
        # position: a company added between Read and Apply cannot shift a ruling onto another.
        proposed = {c.company_name: c.org_id for c in prev.companies
                    if c.match == cx.MATCH_POSSIBLE}
        for key in sorted(k for k in form.keys() if k.startswith("match_company_")):
            idx = key[len("match_company_"):]
            company = str(form.get(key) or "")
            pick = str(form.get(f"match_{idx}") or "")
            if not company:
                continue
            if pick == "none":
                await cx.reject_match(s, company)
            elif pick.isdigit() and company in proposed and int(pick) == (proposed[company] or -1):
                await cx.confirm_match(s, company)
            elif pick.isdigit():
                try:
                    await cx.set_match(s, company, int(pick))
                except ValueError:
                    continue
    state = await contacts_state(request)
    msg = (f"{batch.row_count} contacts imported." if prev.first_import else
           f"Contacts updated. {batch.n_new} added, {batch.n_changed} job change"
           f"{'' if batch.n_changed == 1 else 's'} noted.")
    return _ct_card(request, {**_ct_base(ct_step="done"), **state},
                    trigger(msg, refresh={"refresh": "/settings/data/contacts",
                                          "target": "#st-ct"}))


@router.post("/settings/data/contacts/remove", response_class=HTMLResponse)
async def contacts_remove(request: Request):
    """Remove all contacts: deletes only the people the connections import created. Anyone you
    wrote notes or drafts about stays and is no longer marked as imported; the card says how
    many."""
    async with sessionmaker()() as s:
        try:
            res = await cx.remove_all_imported(s)
        except SQLAlchemyError as exc:
            log.warning("connections_remove_failed", error_type=type(exc).__name__)
            return _ct_card(request, {**_ct_base(ct_error="Nothing was removed: the write "
                                                 "failed. Try again."),
                                      **await contacts_state(request)})
    msg = f"Removed {res.removed} contact{'' if res.removed == 1 else 's'}."
    if res.kept:
        msg += (f" Kept {res.kept} because you wrote notes or drafts about "
                f"{'that contact' if res.kept == 1 else 'them'}.")
    return _ct_card(request, {**_ct_base(ct_notice=msg), **await contacts_state(request)},
                    trigger(msg))


# -------------------------------------------------------------------------------- pane
async def data_pane_ctx(request: Request) -> dict:
    ctx = {**await backup_ctx(), **_imp_base(), **_ct_base(), **await contacts_state(request)}
    ctx["how_steps"] = HOW_STEPS
    return ctx
