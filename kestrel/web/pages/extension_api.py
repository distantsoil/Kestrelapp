"""The browser extension's routes (issue #186, SPEC-INTERFACES 5.3, D-EXT-740 to D-EXT-769).

Every route here needs the shared key in X-Kestrel-Key, whatever the method and whatever the
Origin. The app-wide guard (`app._extension_refusal`) covers only unsafe methods from an extension
origin, so the read routes below would otherwise answer any local client; `require_key` closes
that for this router. The key is the one Settings > Browser extension copies.

The extension reads only the page the person has open, and only when they click; nothing here
fetches, applies, fills or submits anything anywhere (hard rule 1). Page text is hostile input: the
body is parsed into the models below (an allowlist of fields and types, D-SEC-20) and the text is
only ever stored as the description of the one role named in the path, through the same
ingest.normalize.attach_description the app's paste form uses. No route here creates a role.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from kestrel import __version__
from kestrel.db.models import DocRender, Opportunity, Pursuit, Setting, Task
from kestrel.services import extension as ext
from kestrel.web.auth import current_key, key_is_valid
from kestrel.web.deps import sessionmaker

log = structlog.get_logger(__name__)

WAITING_LIMIT_DEFAULT = 5
WAITING_LIMIT_MAX = 20


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


class Refused(Exception):
    """A refused key: the app's handler (refused_response) turns it into the JSON answer."""

    def __init__(self, response: JSONResponse) -> None:
        super().__init__(response.status_code)
        self.response = response


async def refused_response(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, Refused):
        return exc.response
    return _error("refused", "Kestrel refused the request.", 403)


async def require_key(request: Request) -> None:
    """Refuse unless X-Kestrel-Key carries the shared key. Header only: a cookie is the ambient
    credential another extension or page could ride on. Records the last-seen note on success."""
    expected = await current_key()
    if not expected:
        raise Refused(_error(
            "not_set_up",
            "Kestrel has no connection key yet. In Kestrel: Settings, Browser extension, Copy key.",
            403,
        ))
    if not key_is_valid(request.headers.get("X-Kestrel-Key"), expected):
        raise Refused(_error(
            "wrong_key",
            "Kestrel didn't accept that key. Copy it again from Settings, Browser extension.",
            403,
        ))
    try:
        async with sessionmaker()() as s:
            if await ext.note_seen(s, request.headers.get("user-agent")):
                await s.commit()
                ext.seen_saved()
    except SQLAlchemyError as exc:  # the note is best effort; the request still answers
        log.warning("extension_last_seen_failed", error=str(exc))


router = APIRouter(prefix="/api/extension", dependencies=[Depends(require_key)])


# ---------------------------------------------------------------------------- bodies (allowlists)
class DescriptionBody(BaseModel):
    """The only fields a Read may carry. Anything else in the body is ignored, never stored."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)
    text: str
    page_url: str | None = None
    page_title: str | None = None
    dry_run: bool = True


class FeaturesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    badges: bool | None = None
    waiting: bool | None = None
    toolbar_count: bool | None = None


async def _body(request: Request, model: type[BaseModel]) -> BaseModel | JSONResponse:
    try:
        raw = await request.json()
    except (ValueError, TypeError):
        return _error("bad_request", "The request was not JSON.", 400)
    if not isinstance(raw, dict):
        return _error("bad_request", "The request was not a JSON object.", 400)
    try:
        return model.model_validate(raw, strict=True)
    except ValidationError:
        return _error("bad_request", "The request had a field Kestrel doesn't take.", 400)


# ---------------------------------------------------------------------------- 1. config
@router.get("/config")
async def config():
    """The switches and the hosts with waiting roles, for the toolbar count. The extension
    matches the tab's host itself, so no browsing data leaves the browser."""
    async with sessionmaker()() as s:
        feats = await ext.features(s)
        urls = (await s.execute(select(Opportunity.url).where(ext.waiting_clause()))).scalars()
        counts: dict[str, int] = {}
        for url in urls:
            host = ext.host_of(url)
            if host:
                counts[host] = counts.get(host, 0) + 1
    hosts = [{"host": h, "count": n} for h, n in sorted(counts.items())]
    return {"version": __version__, "features": feats, "waiting_hosts": hosts}


# ---------------------------------------------------------------------------- 2. waiting
@router.get("/waiting")
async def waiting(
    host: str = Query(default="", max_length=253),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=WAITING_LIMIT_DEFAULT, ge=1, le=WAITING_LIMIT_MAX),
):
    """The roles waiting for a description: this host's first, then newest first."""
    tab_host = ext.host_of(f"http://{host.strip().lower()}/") if host.strip() else ""
    needle = " ".join(q.lower().split())
    from kestrel.web.redact import confidential, redact_name, redact_text

    async with sessionmaker()() as s:
        conf, mapping = await confidential(s)
        rows = (
            await s.execute(
                select(Opportunity)
                .where(ext.waiting_clause())
                .order_by(Opportunity.first_seen_at.desc(), Opportunity.id.desc())
            )
        ).scalars().all()
    items = []
    for opp in rows:
        # Confidential mode: the list shows, and the search matches, only the redacted names.
        title = redact_text(opp.title, mapping, enabled=conf) or "Untitled role"
        company = redact_name(opp.org_name, mapping, enabled=conf) or ""
        if needle and needle not in f"{title} {company}".lower():
            continue
        role_host = ext.host_of(opp.url)
        items.append({
            "id": opp.id,
            "title": title,
            "company": company,
            "found": (opp.first_seen_at or "")[:10],
            "source_label": ext.source_label(role_host) if role_host else "",
            "host_match": ext.same_site(role_host, tab_host),
        })
    # Stable sort: host matches first, each half still newest first.
    items.sort(key=lambda it: not it["host_match"])
    return {"total": len(items), "items": items[:limit]}


# ---------------------------------------------------------------------------- 3. Read
@router.post("/roles/{opp_id}/description")
async def read_description(request: Request, opp_id: int):
    """Preview (dry_run) or save the page text as this role's description. Never creates a role
    and never replaces one that has a description; the check-page rule is enforced here even if
    the popup was bypassed."""
    body = await _body(request, DescriptionBody)
    if not isinstance(body, DescriptionBody):
        return body
    text = body.text[: ext.MAX_CHARS].strip()
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return _error("not_found", "That role is not in Kestrel any more.", 404)
        if not ext.is_waiting(opp):
            return _error("has_description",
                          "That role already has a description, so nothing was changed.", 409)
        if ext.is_linkedin(body.page_url):
            return _error("linkedin", "Kestrel doesn't read LinkedIn pages. Paste the posting "
                          "in Kestrel instead.", 422)
        problem = ext.page_problem(text)
        if body.dry_run:
            return {
                "words": ext.word_count(text),
                "first_lines": ext.first_lines(text),
                "looks_like_check_page": problem == "check_page",
                "too_short": problem == "too_short",
                "title_match": ext.titles_match(opp.title, body.page_title),
            }
        if problem == "check_page":
            return _error("check_page", "This looks like a check page, not a job posting.", 422)
        if problem == "too_short":
            return _error("too_short", "That page has too little text to be a job posting.", 422)
        # Claim the role first, so two saves racing for it cannot both write: only the one
        # that still finds it waiting goes on; the other gets has_description.
        from sqlalchemy import update

        from kestrel.ingest.normalize import attach_description
        from kestrel.services import run_model

        claimed = (
            await s.execute(
                update(Opportunity)
                .where(Opportunity.id == opp_id, ext.waiting_clause())
                .values(stage="new", stage_reason=None)
                .returning(Opportunity.id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            await s.rollback()
            return _error("has_description",
                          "That role already has a description, so nothing was changed.", 409)
        await s.refresh(opp)
        await attach_description(s, opp, text)
        await run_model.discard(s, opp_id)
        s.add(Task(type="embed", payload={"opportunity_id": opp_id}))
        await s.commit()
    return {"saved": True, "stage": "new"}


# ---------------------------------------------------------------------------- 4. summary
_SCALARS = (str, int, float, bool, type(None))


def _pick(d: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    """Only these keys, only plain values: the popup never receives an ORM object or markup."""
    if not isinstance(d, dict):
        return {}
    return {k: d[k] for k in keys if k in d and isinstance(d[k], _SCALARS)}


async def _documents(s, opp_id: int) -> dict[str, dict[str, Any]]:
    from kestrel.docs import letter_store
    from kestrel.docs.letters import LETTER_TEMPLATES

    renders = (
        await s.execute(
            select(DocRender)
            .where(DocRender.opportunity_id == opp_id)
            .order_by(DocRender.created_at.desc(), DocRender.id.desc())
        )
    ).scalars().all()
    out: dict[str, dict[str, Any]] = {
        "resume": {"available": False, "updated": None, "render_id": None, "pdf_path": None},
        "cover_letter": {"available": False, "updated": None, "render_id": None, "pdf_path": None},
    }
    from kestrel.db.models import DocMaster

    for r in renders:
        if r.template in LETTER_TEMPLATES:
            kind = "cover_letter"
        else:
            master = await s.get(DocMaster, r.doc_master_id)
            if master is None or master.doc_kind != "resume":
                continue  # a capability statement is not the role's resume
            kind = "resume"
        if out[kind]["available"]:
            continue
        if kind == "cover_letter" and await letter_store.letter_for(s, r.id) is None:
            continue
        pdf = (f"/opportunities/{opp_id}/role/cover-letter/{r.id}/download"
               if kind == "cover_letter" else f"/renders/{r.id}/download")
        out[kind] = {"available": True, "updated": (r.created_at or "")[:10],
                     "render_id": r.id, "pdf_path": pdf}
    return out


@router.get("/roles/{opp_id}/summary")
async def summary(opp_id: int):
    """Everything the popup's role summary shows, built from the Pipeline panel's own context so
    the words match the app (scope 4.2)."""
    from kestrel.web.pages.pipeline import panel_context

    async with sessionmaker()() as s:
        ctx = await panel_context(s, opp_id)
        if ctx is None:
            return _error("not_found", "That role is not in Kestrel any more.", 404)
        opp = ctx["opp"]
        fit = ctx["fit"]
        pursuit = (
            await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
        ).scalar_one_or_none()
        docs = await _documents(s, opp_id)
    dims = [_pick(d, ("key", "name", "score", "pct")) for d in ctx.get("dims") or []]
    dims.sort(key=lambda d: -(d.get("score") or 0))
    return {
        "id": opp_id,
        "kind": ctx.get("kind"),
        "title": ctx.get("title") or "",
        "company": ctx.get("org_name") or "",
        "place": ctx.get("place") or "",
        "score": getattr(fit, "score", None),
        "verdict": {"word": getattr(fit, "word", None), "tone": ctx.get("tone")},
        "waiting": ext.is_waiting(opp),
        "dimensions": dims[:3],
        "dimension_count": len(dims),
        "pay": _pick(ctx.get("pay"), ("label", "text", "note")),
        "level": _pick(ctx.get("level"), ("label", "text", "note")),
        "fits": [_pick(r, ("mark", "word", "text", "tone")) for r in ctx.get("reasons") or []][:3],
        "gaps": [_pick(g, ("text", "how")) for g in (ctx.get("gaps") or [])][:2],
        "documents": docs,
        "applied_at": pursuit.applied_at if pursuit is not None else None,
        "open_path": f"/opportunities/{opp_id}",
    }


# ---------------------------------------------------------------------------- 5. document text
@router.get("/roles/{opp_id}/documents/{kind}.txt")
async def document_text(opp_id: int, kind: Literal["resume", "cover_letter"]):
    """The plain text of this role's latest resume or cover letter, for Copy text. Every resume
    line is the operator's own text from the catalog (hard rule 2)."""
    from kestrel.docs import letter_store
    from kestrel.docs.catalog import load_catalog, resolve
    from kestrel.web.redact import confidential, redact_text

    async with sessionmaker()() as s:
        conf, mapping = await confidential(s)
        docs = await _documents(s, opp_id)
        rid = docs[kind]["render_id"]
        if rid is None:
            return PlainTextResponse("No document yet.", status_code=404)
        render = await s.get(DocRender, rid)
        if render is None:
            return PlainTextResponse("No document yet.", status_code=404)
        if kind == "cover_letter":
            letter = await letter_store.letter_for(s, rid)
            text = (letter.body_md if letter is not None else "") or ""
        else:
            catalog = await load_catalog(s, render.doc_master_id)
            if catalog is None:
                return PlainTextResponse("No document yet.", status_code=404)
            # The same view the preview and the PDF use: confidential mode swaps the employer
            # (redact_orgs, as pages/renders.py does), and a pick that no longer resolves is
            # never copied as its default stand-in (hard rule 2).
            config = dict(render.config or {})
            config["redact_orgs"] = bool(config.get("redact_orgs") or conf)
            doc = resolve(catalog, config)
            from kestrel.web.pages.renders import _needs

            # The download's own rule (renders._needs: Pick again's needs_pick and any saved key
            # the catalog lost), less what the person already chose to leave as it is.
            if set(_needs(config, doc)) - set(config.get("left_unresolved") or []):
                return PlainTextResponse(
                    "Some lines still need your pick. Open the resume in Kestrel to choose.",
                    status_code=409)
            text = resume_text(doc)
    text = redact_text(text, mapping, enabled=conf) or ""
    return PlainTextResponse(text, headers={"Cache-Control": "no-store"})


def resume_text(doc: Any) -> str:
    """A resolved resume as plain text: headings, roles and bullets, in order."""
    lines: list[str] = []
    header = doc.header if isinstance(doc.header, dict) else {}
    for key in ("name", "headline"):
        if isinstance(header.get(key), str) and header[key].strip():
            lines.append(header[key].strip())
    for block in doc.blocks:
        lines.append("")
        if block.heading:
            lines.append(block.heading)
        if block.text:
            lines.append(block.text)
        for role in block.roles:
            head = " · ".join(x for x in (role.title, role.org, role.dates) if x)
            lines.append(head)
            for b in role.bullets:
                lines.append(f"- {b.text}")
        for group in block.skills:
            if isinstance(group, dict):
                name = group.get("name") or group.get("group") or ""
                items = group.get("items") or group.get("skills") or []
                if isinstance(items, list):
                    joined = ", ".join(str(i) for i in items if isinstance(i, str))
                    lines.append(f"{name}: {joined}" if name else joined)
        for entry in block.entries:
            if isinstance(entry, str):
                lines.append(entry)
            elif isinstance(entry, dict):
                lines.append(" · ".join(str(v) for v in entry.values() if isinstance(v, str)))
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------- 7. applied
@router.post("/roles/{opp_id}/applied")
async def applied(opp_id: int):
    """Record that the person applied, by hand, the same Applications verb the app's I applied
    runs. Records a status in Kestrel only; nothing is sent anywhere. A second call changes
    nothing."""
    from kestrel.services import pursuit_board as board_svc
    from kestrel.services import pursuit_undo as undo
    from kestrel.web.pages.opportunity_actions import add_to_list

    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return _error("not_found", "That role is not in Kestrel any more.", 404)
        p = (
            await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
        ).scalar_one_or_none()
        if p is not None and p.applied_at:
            return {"stage": p.status, "applied_at": p.applied_at, "changed": False}
        if opp.archived_at:
            return _error("hidden", "Put the role back in Kestrel before marking it applied.", 409)
        try:
            if p is None:
                p, text = await add_to_list(s, opp)
                if p is None:
                    return _error("not_available", text, 409)
                token = (s.info.get("added_undo") or {}).get(opp_id)
                leftover = await s.get(Setting, undo.PREFIX + token) if token else None
                if leftover is not None:
                    await s.delete(leftover)
            err = await board_svc.apply_verb(s, p, "applied", {})
            if err:
                await s.rollback()
                return _error("not_available", err, 409)
            await s.commit()
        except IntegrityError:
            # Another I've applied made the pursuit first. Answer with what it recorded, or
            # finish the verb on that pursuit if it has not got there yet.
            await s.rollback()
            p = (
                await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
            ).scalar_one_or_none()
            if p is None:
                return _error("not_saved", "Kestrel could not record that. Try again in the app.",
                              500)
            if not p.applied_at:
                err = await board_svc.apply_verb(s, p, "applied", {})
                if err:
                    await s.rollback()
                    return _error("not_available", err, 409)
                await s.commit()
            return {"stage": p.status, "applied_at": p.applied_at, "changed": False}
        except SQLAlchemyError as exc:
            await s.rollback()
            log.warning("extension_applied_failed", opp_id=opp_id, error=str(exc))
            return _error("not_saved", "Kestrel could not record that. Try again in the app.", 500)
        return {"stage": p.status, "applied_at": p.applied_at, "changed": True}


# ---------------------------------------------------------------------------- 8. features
@router.put("/features")
async def put_features(request: Request):
    body = await _body(request, FeaturesBody)
    if not isinstance(body, FeaturesBody):
        return body
    if any(getattr(body, name) is None for name in body.model_fields_set):
        return _error("bad_request", "Each switch is true or false.", 400)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    async with sessionmaker()() as s:
        out = await ext.set_features(s, changes)
        await s.commit()
    return {"features": out}


async def waiting_count(s) -> int:
    return int(
        (await s.execute(select(func.count(Opportunity.id)).where(ext.waiting_clause())))
        .scalar_one()
    )
