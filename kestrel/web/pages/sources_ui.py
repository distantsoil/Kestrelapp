"""Sources page interactions (D-UI-102): the details drawer, Check now and Check all now, How
often, Remove with Undo, Suggested companies, and the Add a source sheet with its checks.

  GET  /sources/add                     the sheet: a tile per type
  GET  /sources/add/{type}              the steps for one type (boards, ats, name, feeds, gov, sig)
  POST /sources/add/{type}              validate and add; a refusal re-renders the sheet
  POST /sources/check/link              company career page link: hiring system and open roles
  POST /sources/check/company           company by name: the boards found
  POST /sources/check/feed              a feed link: reads it and counts the items
  POST /sources/check-all               queue a check of every source that is on
  POST /sources/suggestions/add         add a suggested company
  POST /sources/suggestions/dismiss     dismiss one; /undismiss takes it back
  GET  /sources/{id}/drawer             the details drawer
  POST /sources/{id}/check              Check now, on the usual models
  POST /sources/{id}/cadence            How often
  POST /sources/{id}/remove             Remove (hidden, never deleted); /restore is its Undo

Nothing here sends anything for the operator: every check is a polite read of a public page, and
a source added here is only ever read from. A removed source stays in the database, hidden by a
marker in its config, so the roles it found keep their source; Undo clears the marker.

Fixed paths are declared before the per-row ones, and this module is included before
`sources`, whose `/sources/{source_id}` would otherwise swallow `/sources/check-all`."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from kestrel.db.models import Setting, Source
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.adapters.adzuna import KEY_SIGNUP_URL as ADZUNA_SIGNUP_URL
from kestrel.ingest.adapters.usajobs import KEY_SIGNUP_URL
from kestrel.ingest.base import AdapterError, SourceConfigError
from kestrel.services import pursuit_undo, source_suggestions
from kestrel.web import source_view as sv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.pages import sources as pg

log = structlog.get_logger()
router = APIRouter()

CADENCES = ((6, "Every 6 hours"), (12, "Twice a day"), (24, "Daily"), (168, "Weekly"))
TYPES = {  # the tile per type, in order: (key, title, plain line)
    "employment": [
        ("boards", "Job board search", "Kestrel searches public job boards for a title and place."),
        ("ats", "Company career page",
         "Paste any careers page link. Kestrel reads most of them."),
        ("name", "Company by name", "Type a company name. Kestrel looks for its job board."),
        ("feeds", "Job feed", "Any board's RSS or Atom link."),
    ],
    "consulting": [
        ("boards", "Job board search", "Contract and freelance searches."),
        ("gov", "Public bids", "Government requests for proposals on SAM.gov."),
        ("sig", "Watch a company", "Public SEC filings for a company you would like to work with."),
        ("feeds", "Company news", "A company newsroom's RSS or Atom link."),
    ],
}
SUPPORTED = ("Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Rippling, ADP, Paylocity, "
             "Workable, Recruitee, Teamtailor, Personio and Breezy HR")
# Boards the sheet offers: (jobspy site, label, why it is limited or "")
BOARDS = (
    ("indeed", "Indeed", ""), ("glassdoor", "Glassdoor", ""),
    ("zip_recruiter", "ZipRecruiter", "ZipRecruiter blocks automated searches, so this always "
                                      "finds 0."),
    ("google", "Google Jobs", "Google Jobs returns nothing right now. Kestrel keeps the search "
                              "in case that changes."),
)
# The board the sheet offers beside the JobSpy ones: its own adapter, a key of the user's own.
USAJOBS = ("usajobs", "USAJobs", "US government jobs")
USAJOBS_SENT = (
    "Kestrel sends USAJobs your search words and place, your key and your email address. "
    "Nothing else leaves this computer."
)
ADZUNA = ("adzuna", "Adzuna", "")
ADZUNA_SENT = (
    "Kestrel sends Adzuna your search words and place and your app ID and app key. "
    "Nothing else leaves this computer."
)
# Remote job boards that need no key: (form name, label, small note under the chip).
REMOTE_BOARDS = (
    ("himalayas", "Himalayas", "Remote jobs open to the US"),
    ("weworkremotely", "We Work Remotely", "Whole feed"),
)
# Every board the sheet adds as its own source, beside the JobSpy ones.
EXTRA_BOARDS = ("usajobs", "adzuna", "himalayas", "weworkremotely")
EXTRAS_ONLY = "extras"  # a sheet add whose boards all have a source of their own
DICE = ("Dice", "Dice has no public feed Kestrel can read yet, so it cannot be added.")
INDEED_FILTERS = ("hours", "type", "remote", "easy", "none")
MAX_FIELD = 200


# ------------------------------------------------------------------------------------ helpers
def _hx(request: Request) -> bool:
    return bool(request.headers.get("HX-Request"))


async def _track(request: Request, session) -> str:
    from kestrel.services import tracks as tracks_svc

    return pg._page_track(request, await tracks_svc.load_enabled(session))


def _text(form, key: str) -> str:
    return str(form.get(key) or "").strip()[:MAX_FIELD]


def _sheet(request: Request, kind: str, ctx: dict, status: int = 200) -> HTMLResponse:
    return templates().TemplateResponse(
        request, "partials/source_sheet.html", {"kind": kind, **ctx}, status_code=status
    )


def _types(track: str) -> list[tuple[str, str, str]]:
    return TYPES.get(track, TYPES["employment"])


def _sheet_base(track: str) -> dict:
    return {
        "track": track, "types": _types(track), "supported": SUPPORTED, "boards": BOARDS,
        "usajobs_board": USAJOBS, "usajobs_sent": USAJOBS_SENT,
        "usajobs_signup": KEY_SIGNUP_URL, "adzuna_board": ADZUNA, "adzuna_sent": ADZUNA_SENT,
        "adzuna_signup": ADZUNA_SIGNUP_URL, "remote_boards": REMOTE_BOARDS,
        "dice": DICE, "paste_label": "Leads" if track == "consulting" else "Applications",
        "paste_href": f"/pursuits?track={track}",
    }


async def _added(
    request: Request, session, message: str, source_ids: list[int]
) -> HTMLResponse:
    """The sheet closes, the new rows arrive, a toast offers Undo. One add can create several
    sources (a JobSpy search and a board of its own); the Undo token remembers all of them, so
    Undo removes everything that add created."""
    first = await session.get(Source, source_ids[0])
    snapshot = {"kind": "source_added", "source": pursuit_undo.row_dict(first)}
    others = [await session.get(Source, sid) for sid in source_ids[1:]]
    if others:
        snapshot["also"] = [pursuit_undo.row_dict(o) for o in others if o is not None]
    token = await pursuit_undo.save(session, snapshot)
    await session.commit()
    ctx = await pg._panel_ctx(request, session)
    headers = pg.toast_header(
        message,
        f"/sources/{source_ids[0]}/remove?token={token}",
        **{"src-sheet-close": {}, "src-arrived": {"id": source_ids[0]}},
    )
    headers["HX-Retarget"] = "#src-list"
    headers["HX-Reswap"] = "outerHTML"
    return pg.list_response(request, ctx, headers)


# ------------------------------------------------------------------------------- the sheet
@router.get("/sources/add", response_class=HTMLResponse)
async def add_choose(request: Request):
    async with sessionmaker()() as s:
        track = await _track(request, s)
    return _sheet(request, "", _sheet_base(track))


@router.get("/sources/add/{kind}", response_class=HTMLResponse)
async def add_form(request: Request, kind: str):
    async with sessionmaker()() as s:
        track = await _track(request, s)
        usajobs = await pg.usajobs_status(s)
        adzuna = await pg.adzuna_status(s)
    if kind not in {k for k, _t, _d in _types(track)}:
        return _sheet(request, "", _sheet_base(track), 404)
    return _sheet(request, kind, {**_sheet_base(track), "values": {}, "usajobs": usajobs,
                                  "adzuna": adzuna})


def _indeed_config(form) -> dict:
    """The one extra filter Indeed allows. A radio group, so more than one is not possible
    from the sheet; `_validate_source_config` still refuses a conflict from any other caller."""
    choice = _text(form, "indeed_filter") or "none"
    if choice == "hours":
        days = _text(form, "hours_days")
        return {"hours_old": {"1": 24, "3": 72, "7": 168}.get(days, 72)}
    if choice == "type":
        picked = _text(form, "job_type")
        return {"job_type": picked if picked in ("fulltime", "parttime", "contract") else
                "fulltime"}
    if choice == "remote":
        return {"is_remote": True}
    if choice == "easy":
        return {"easy_apply": True}
    return {}


def _boards_config(form) -> tuple[dict, str | None]:
    title, place = _text(form, "title"), _text(form, "place")
    sites = [site for site, _l, _w in BOARDS if form.get(f"board_{site}")]
    if not title:
        return {}, "Type a job title or keywords first."
    if not sites and not any(form.get(f"board_{b}") for b in EXTRA_BOARDS):
        return {}, "Pick at least one job board."
    if not sites:  # only boards with their own source: built in `_extra_specs`
        return {"search_term": title, **({"location": place} if place else {})}, None
    config: dict = {"search_term": title, "site_name": sites}
    if place:
        config["location"] = place
    if "google" in sites:
        config["google_search_term"] = f"{title} jobs near {place}" if place else f"{title} jobs"
    if "indeed" in sites:
        config.update(_indeed_config(form))
    return config, None


@router.post("/sources/add/{kind}", response_class=HTMLResponse)
async def add_source(request: Request, kind: str):
    form = await request.form()
    async with sessionmaker()() as s:
        track = await _track(request, s)
        base = _sheet_base(track)
        values = {k: str(v) for k, v in form.items() if isinstance(v, str)}
        spec, error = await _build_source(kind, form, track)
        only_extras = spec is not None and spec["kind"] == EXTRAS_ONLY  # built below
        if error is None and spec is not None and not only_extras:
            error = pg._validate_source_config(spec["kind"], spec["config"])
        if error is not None or spec is None:
            return _sheet(request, kind, {**base, "values": values, "error": error}, 200)
        dupe = None if only_extras else await _existing(s, spec)
        if dupe is not None:
            return _sheet(request, kind, {**base, "values": values,
                                          "error": f"{dupe.name} is already one of your sources."})
        specs = [] if only_extras else [spec]
        message = "Added. Kestrel checks it on the next run."
        if kind == "boards":
            # Keys first: a refusal (a bad email) re-renders the sheet and adds nothing.
            error = await _save_board_keys(s, form)
            base = {**base, "usajobs": await pg.usajobs_status(s),
                    "adzuna": await pg.adzuna_status(s)}
            extras: list[dict] = []
            if error is None:
                extras, error = await _extra_specs(s, form, track, base)
            if error is not None:
                return _sheet(request, kind, {**base, "values": values, "error": error})
            specs += extras
            needs = [label for name, label in (("usajobs", "USAJobs"), ("adzuna", "Adzuna"))
                     if form.get(f"board_{name}") and not base[name]["ready"]]
            if needs:
                what = {"USAJobs": "API key and email", "Adzuna": "app ID and app key"}
                message = ("Added. " + " ".join(
                    f"{b} needs your {what[b]} before it can search;" for b in needs
                ) + " add them under Sources that need your own key.")
        added: list[Source] = []
        for sp in specs:
            src = Source(
                track=sp["track"], kind=sp["kind"], name=sp["name"], config=sp["config"],
                enabled=True, cadence_hours=sp.get("cadence", pg._DEFAULT_CADENCE),
                origin=pg.CUSTOM,
            )
            s.add(src)
            added.append(src)
        await s.commit()
        return await _added(request, s, message, [src.id for src in added])


async def _save_board_keys(session, form) -> str | None:
    """Save the keys typed into the panels of the boards that are ticked; a refusal is a plain
    sentence and nothing is added."""
    if form.get("board_usajobs"):
        error = await pg.save_usajobs_credentials(
            session, key=str(form.get("usajobs_key") or "").strip(),
            email=str(form.get("usajobs_email") or "").strip(),
        )
        if error is not None:
            return error
    if form.get("board_adzuna"):
        return await pg.save_adzuna_credentials(
            session, app_id=str(form.get("adzuna_id") or "").strip(),
            app_key=str(form.get("adzuna_key") or "").strip(),
        )
    return None


async def _extra_specs(session, form, track: str, base: dict) -> tuple[list[dict], str | None]:
    """The sources for the ticked boards that are not JobSpy ones, each checked for a repeat."""
    out: list[dict] = []
    title, place = _text(form, "title"), _text(form, "place")
    for name in EXTRA_BOARDS:
        if not form.get(f"board_{name}"):
            continue
        spec = _extra_spec(name, track, title, place, base)
        error = pg._validate_source_config(spec["kind"], spec["config"])
        dupe = await _existing(session, spec)
        if error is None and dupe is not None:
            error = f"{dupe.name} is already one of your sources."
        if error is not None:
            return [], error
        out.append(spec)
    return out, None


async def _existing(session, spec: dict) -> Source | None:
    """A source with the same kind and the same board, so a company is never listed twice."""
    from kestrel.ingest.board_discovery import _KEY

    if spec["kind"] == "jobspy":
        cfg = spec["config"]
        sig = (str(cfg.get("search_term") or "").lower(), str(cfg.get("location") or "").lower(),
               sorted(cfg.get("site_name") or []))
        for row in (await session.execute(
                select(Source).where(Source.kind == "jobspy"))).scalars():
            c = row.config or {}
            if sv.is_removed(row):
                continue
            if sig == (str(c.get("search_term") or "").lower(),
                       str(c.get("location") or "").lower(), sorted(c.get("site_name") or [])):
                return row
        return None
    if spec["kind"] in ("usajobs", "adzuna", "himalayas", "weworkremotely"):
        cfg = spec["config"]
        want = (str(cfg.get("keyword") or "").lower(), str(cfg.get("location") or "").lower())
        for row in (await session.execute(
                select(Source).where(Source.kind == spec["kind"]))).scalars():
            c = row.config or {}
            if not sv.is_removed(row) and want == (
                    str(c.get("keyword") or "").lower(), str(c.get("location") or "").lower()):
                return row
        return None
    key = _KEY.get(spec["kind"]) or ("url" if spec["kind"] == "careers_page" else None)
    if key is None:
        return None
    want = str(spec["config"].get(key) or "").lower()
    rows = (await session.execute(select(Source).where(Source.kind == spec["kind"]))).scalars()
    for row in rows:
        if sv.is_removed(row):
            continue
        if str((row.config or {}).get(key) or "").lower() == want and want:
            return row
    return None


async def _build_source(kind: str, form, track: str) -> tuple[dict | None, str | None]:
    """(spec, refusal) for one sheet type. The spec is what `Source(...)` needs."""
    if kind == "boards":
        config, error = _boards_config(form)
        if error:
            return None, error
        place = config.get("location")
        name = f"{config['search_term']} in {place}" if place else config["search_term"]
        if "site_name" not in config:  # boards with their own source only; the caller adds them
            return {"track": track, "kind": EXTRAS_ONLY, "name": name[:MAX_FIELD],
                    "config": {}}, None
        return {"track": track, "kind": "jobspy", "name": name[:MAX_FIELD], "config": config}, None
    if kind in ("ats", "name"):
        parsed = _pick(str(form.get("pick") or "").strip())
        if parsed is None:
            return None, ("Check the link first, or find the company by name, and Add turns on.")
        platform_kind, config, company = parsed
        return {"track": "employment", "kind": platform_kind,
                "name": _text(form, "company") or company, "config": config}, None
    if kind == "feeds":
        url = _text(form, "url")
        if not url.lower().startswith(("http://", "https://")):
            return None, "Paste a feed link that starts with https://."
        name = _text(form, "name") or url.split("//", 1)[-1].split("/", 1)[0]
        return {"track": track, "kind": "rss", "name": name,
                "config": {"feeds": [url]}}, None
    if kind == "gov":
        naics = [c for c in _text(form, "naics").replace(",", " ").split() if c.isdigit()]
        return {"track": "consulting", "kind": "sam_gov",
                "name": _text(form, "name") or "Federal opportunities",
                "config": {"api_key_env": pg.SAM_ENV, "naics": naics or pg.SAM_NAICS}}, None
    if kind == "sig":
        company = _text(form, "company")
        if not company:
            return None, "Type a company name first."
        return {"track": "consulting", "kind": "sec_edgar", "name": company,
                "config": {"queries": [f'"{company}"']}}, None
    return None, "That type of source is not available."


def _extra_spec(name: str, track: str, title: str, place: str, base: dict) -> dict:
    """The source one ticked board gets from the sheet's title and place. The USAJobs email rides
    in the config (it is not a secret); no key ever does. The remote boards take no place."""
    if name == "usajobs":
        config: dict = {"keyword": title, "contact_email": base["usajobs"]["email"]}
    else:
        config = {"keyword": title}
    if place and name in ("usajobs", "adzuna"):
        config["location"] = place
    label = {"usajobs": "USAJobs", "adzuna": "Adzuna", "himalayas": "Himalayas",
             "weworkremotely": "We Work Remotely"}[name]
    if name == "weworkremotely":  # the whole feed: there is no search to name
        return {"track": track, "kind": name, "name": label, "config": {}}
    shown = f"{title} on {label}" + (f" in {place}" if place and "location" in config else "")
    return {"track": track, "kind": name, "name": shown[:MAX_FIELD], "config": config}


def _pick(raw: str) -> tuple[str, dict, str] | None:
    """The board a check found, as the sheet carries it: `pick:<kind>|<json config>|<name>`.
    Only a check produces one, so Add stays off until a link or a name has been read."""
    if not raw.startswith("pick:"):
        return None
    try:
        kind, cfg, company = raw[5:].split("|", 2)
        config = json.loads(cfg)
    except (ValueError, json.JSONDecodeError):
        return None
    return (kind, config, company) if isinstance(config, dict) and get_adapter(kind) else None


# ------------------------------------------------------------------------------ the checks
def _msg(
    request: Request, tone: str, text: str, pick: str = "", extra: str = "", name: str = ""
) -> HTMLResponse:
    return templates().TemplateResponse(
        request, "partials/source_check.html",
        {"tone": tone, "text": text, "pick": pick, "extra": extra, "name": name},
    )


def _domain_name(domain: object) -> str | None:
    """A career site address as a company word: `career.example.org` is `example`, and
    `harbor-point.teamtailor.com` is `harbor-point`."""
    if not isinstance(domain, str) or "." not in domain:
        return None
    labels = domain.lower().split(".")
    if domain.lower().endswith(".teamtailor.com"):
        return labels[0]
    while len(labels) > 2 and labels[0] in ("www", "career", "careers", "jobs", "join", "work"):
        labels = labels[1:]
    return labels[-2] if len(labels) >= 2 else labels[0]


def _pretty(slug: str) -> str:
    """A board address as a name: `harbor-point` becomes `Harbor Point`; a mixed-case address is
    already written as the company writes it."""
    words = slug.replace("_", "-").split("-")
    return " ".join(w if w != w.lower() else w.capitalize() for w in words if w)


@router.post("/sources/check/link", response_class=HTMLResponse)
async def check_link(request: Request):
    """Read one careers page link: which hiring system it is, and how many roles it lists now.
    One polite request. Nothing is saved; Add is only enabled by a good answer."""
    from kestrel.ingest.board_discovery import BoardRef, board_from_url

    url = _text(await request.form(), "url")
    if not url.lower().startswith(("http://", "https://")):
        return _msg(request, "bad", "Nothing found at that link. Check the address and try again.")
    ref = board_from_url(url)
    html = None
    if ref is None:  # a page on the employer's own domain: read once, for a hiring system first
        html = await _page_text(url)
        ref = _board_from_markup(html, url)
    if ref is None:
        ref = BoardRef("careers_page", {"url": url})  # the generic reader (schema.org JobPosting)
    adapter = get_adapter(ref.kind)
    page = ref.kind == "careers_page"
    platform = sv.PLATFORMS.get(ref.kind, "Careers page" if page else ref.kind)
    ident = str(ref.config.get("token") or ref.config.get("company") or ref.config.get("slug")
                or ref.config.get("tenant") or _domain_name(ref.config.get("domain"))
                or _domain_name(urlsplit(url).hostname if page else None) or platform)
    name = _pretty(ident)
    try:
        async with await pg._http_client() as http:
            if page and html is not None:  # the page is already in hand: do not ask for it twice
                postings = await adapter.read_html(html, ref.config, http)
            else:
                postings = await adapter.fetch(ref.config, http) if adapter else []
    except AdapterError as exc:
        if page:  # the reader's own sentence says why (robots.txt, not found, blocked)
            return _msg(request, "bad", str(exc))
        log.info("source_link_check_failed", kind=ref.kind, error=type(exc).__name__)
        return _msg(request, "bad", "Nothing found at that link. Check the address and try again.")
    except (httpx.HTTPError, SourceConfigError, ValueError, PermissionError,
            RuntimeError) as exc:
        log.info("source_link_check_failed", kind=ref.kind, error=type(exc).__name__)
        return _msg(request, "bad", "Nothing found at that link. Check the address and try again.")
    stopped = getattr(postings, "partial_error", None)
    if not postings and stopped:  # a feed that is off or a rate limit: say so, offer no Add
        if page:  # a page Kestrel could not read jobs from: say why, and the way round it
            return _msg(request, "warn", f"{stopped} To track a role from this page, copy the "
                                         "posting text and add it with One posting.")
        return _msg(request, "warn", f"This is a {platform} job board. {stopped}")
    if not postings:
        return _msg(request, "warn",
                    f"This is a {platform} job board, but it lists no open roles right now. "
                    "You can still add it; Kestrel checks it daily.",
                    pick=_encode(ref.kind, ref.config, name), name=name)
    if (ref.config.get("domain") or page) and postings[0].org_name:
        name = postings[0].org_name  # the career site names its employer
    config = {**ref.config, "company_name": name} if page else ref.config
    n = len(postings)
    what = "careers page" if page else f"{platform} job board"
    cap = f" {stopped}" if page and stopped else ""
    return _msg(request, "good", f"Found it. {what[0].upper() + what[1:]}. {n} open role"
                f"{'' if n == 1 else 's'}.{cap}", pick=_encode(ref.kind, config, name),
                name=name)


async def _page_text(url: str) -> str | None:
    """A careers page on the employer's own domain, read once. Any failure is None (the careers
    page reader then reports the reason itself). The link is the operator's, so the fetch is the
    guarded one: public addresses only, robots.txt checked, one request, 2 MB at most."""
    from kestrel.ingest.http import UnsafeURLError

    try:
        async with await pg._http_client() as http:
            return await http.get_text(
                url, public_only=True, check_robots=True, retries=1, rate_per_sec=1.0
            )
    except (httpx.HTTPError, UnsafeURLError, PermissionError, ValueError) as exc:
        log.info("source_link_page_unreadable", error=type(exc).__name__)
        return None


def _board_from_markup(html: str | None, url: str):
    """The hiring system a page's own markup names (Recruitee, Teamtailor), or None."""
    from kestrel.ingest.board_discovery import board_from_markup

    return board_from_markup(html, url)


def _encode(kind: str, config: dict, company: str) -> str:
    return "pick:" + f"{kind}|{json.dumps(config, separators=(',', ':'))}|{company}"


@router.post("/sources/check/company", response_class=HTMLResponse)
async def check_company(request: Request):
    """Company by name: the usual job board addresses, a few polite requests, and the match."""
    from kestrel.ingest.board_lookup import find_company_board

    company = _text(await request.form(), "q")
    if not company:
        return _msg(request, "warn", "Type a company name first.")
    try:
        async with await pg._http_client() as http:
            result = await find_company_board(company, http)
    except Exception as exc:  # a dead network is a message, never a 500
        log.info("source_name_check_failed", error=type(exc).__name__)
        return _msg(request, "warn", "The lookup could not run. Check the network and try again.")
    m = result.match
    if m is None:
        return _msg(request, "warn", f"No board found for {company}. Paste their careers page "
                                     "link instead.")
    async with sessionmaker()() as s:
        have = await _existing(s, {"kind": m.kind, "config": m.config})
    if have is not None:
        return _msg(request, "info", f"{have.name} is already one of your sources. "
                                     "Nothing to add.")
    jobs = f" · {m.jobs} open role{'s' if m.jobs != 1 else ''}" if m.jobs else ""
    return _msg(request, "match", result.company, pick=_encode(m.kind, m.config, result.company),
                extra=f"{m.platform} job board{jobs}", name=result.company)


@router.post("/sources/check/feed", response_class=HTMLResponse)
async def check_feed(request: Request):
    """Read a feed link once and count its items (the same reader a check uses)."""
    from xml.etree import ElementTree

    from kestrel.ingest.http import UnsafeURLError

    url = _text(await request.form(), "url")
    if not url.lower().startswith(("http://", "https://")):
        return _msg(request, "bad", "Nothing found at that link. Check the address and try again.")
    try:
        async with await pg._http_client() as http:
            text = await http.get_text(url, public_only=True)
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("refused")
        root = ElementTree.fromstring(text)
    except UnsafeURLError as exc:  # a link into this computer or a private network, or too big
        log.info("source_feed_check_refused", error=type(exc).__name__)
        return _msg(request, "bad", f"{exc}.")
    except (httpx.HTTPError, ValueError, ElementTree.ParseError, PermissionError,
            RuntimeError) as exc:
        log.info("source_feed_check_failed", error=type(exc).__name__)
        return _msg(request, "bad", "Nothing found at that link. Check the address and try again.")
    n = len(root.findall(".//item")) + len(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
    return _msg(request, "good", f"Found it. A feed with {n} item{'' if n == 1 else 's'}.",
                pick="feed")


# -------------------------------------------------------------------------- check all, drawer
@router.get("/sources/check-all/choose", response_class=HTMLResponse)
async def check_all_choose(request: Request):
    from kestrel.services.online_consent import RUN_TIERS, approval_for, online_route
    from kestrel.services.setting_overrides import load_settings_with_db

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        approval = approval_for(settings, RUN_TIERS)
        routes = [await online_route(s, settings, tier) for tier in approval]
    return templates().TemplateResponse(request, "partials/sources_check_all.html", {
        "approval": approval, "routes": routes,
    })


@router.post("/sources/check-all", response_class=HTMLResponse)
async def check_all(request: Request):
    """Queue a check of every source that is on, due or not. Only enqueues; the worker reads
    the pages, one polite request at a time, and each row updates when its check finishes."""
    from kestrel.ingest.scheduler import enqueue_all_scrapes
    from kestrel.queue.queue import TaskQueue

    sm = sessionmaker()
    async with sm() as s:
        from kestrel.services.online_consent import approval_problem
        from kestrel.services.setting_overrides import load_settings_with_db
        settings = await load_settings_with_db(s)
        form = await request.form()
        problem = await approval_problem(s, settings, ("tier1", "tier2", "trigger"), form)
        if problem:
            return _msg(request, "bad", "Nothing was sent. Confirm the online scoring first.")
        n = await enqueue_all_scrapes(
            s, TaskQueue(sm), online_approval=form.get("online_approval"), reconfirm_held=True,
        )
        ctx = await pg._panel_ctx(request, s)
    text = (f"Checking {n} source{'' if n == 1 else 's'}. Each row updates when its check "
            "finishes." if n else "Nothing to check: every source that is on is already being "
            "checked, or none is on.")
    return pg.list_response(request, ctx, pg.toast_header(text))


async def _drawer_ctx(request: Request, session, source_id: int, **extra) -> dict | None:
    ctx = await pg._panel_ctx(request, session)
    src = await session.get(Source, source_id)
    if src is None or sv.is_removed(src):
        return None
    view = next((v for v in ctx["views"] if v.id == source_id), None)
    if view is None:
        recent = await pg._recent_runs(session, [src])
        view = sv.build_view(src, recent.get(src.id, []))
    cad = int(src.cadence_hours or pg._DEFAULT_CADENCE)
    options = list(CADENCES)
    if cad not in {h for h, _l in options}:
        options.append((cad, f"Every {cad} hours"))
    ctx.update(
        view=view, rows=_facts(src, view), cadences=sorted(options), cadence=cad,
        details=_details(src, view), robots=_robots(src),
        profile_href="/profile?tab=" + ("consulting" if src.track == "consulting"
                                        else "employment"),
        **extra,
    )
    return ctx


def _facts(src: Source, view: sv.SourceView) -> list[tuple[str, str]]:
    cfg = src.config or {}
    rows: list[tuple[str, str]] = [("Came from", view.origin_label)]
    if src.kind == "jobspy":
        term = str(cfg.get("search_term") or "").strip('"')
        if term:
            rows.append(("Searching for", term))
        if cfg.get("location"):
            rows.append(("Where", str(cfg["location"])))
        rows.append(("Boards", sv.type_label(src)))
        rows.append(("Filter", _filter_words(cfg)))
    elif src.kind in sv.PLATFORMS:
        ident = cfg.get("token") or cfg.get("company") or cfg.get("slug") or cfg.get("tenant")
        rows.append(("Hiring system", sv.PLATFORMS[src.kind]))
        if ident:
            rows.append(("Company page", str(ident)))
    elif src.kind == "careers_page":
        rows.append(("Hiring system", "Careers page"))
        rows.append(("Page", str(cfg.get("url") or "")))
    elif src.kind == "sec_edgar":
        rows.append(("Watching for", ", ".join(str(q) for q in (cfg.get("queries") or [])[:3])))
    elif src.kind == "rss":
        rows.append(("Feed", ", ".join(str(f) for f in (cfg.get("feeds") or [])[:2])))
    elif src.kind == "sam_gov":
        rows.append(("Industry codes", ", ".join(str(c) for c in (cfg.get("naics") or []))))
    if view.open_n is None:
        rows.append(("Open now", "Not checked yet"))
    else:
        new = f" ({view.new_n} new since last check)" if view.new_n else ""
        rows.append(("Open now", f"{view.open_n}{new}"))
    return rows


def _filter_words(cfg: dict) -> str:
    if cfg.get("hours_old") is not None:
        days = max(1, round(int(cfg["hours_old"]) / 24))
        return f"Posted in the last {days} day{'' if days == 1 else 's'}"
    if cfg.get("easy_apply"):
        return "Easy apply only"
    parts = []
    if cfg.get("job_type"):
        parts.append(f"Job type: {cfg['job_type']}")
    if cfg.get("is_remote"):
        parts.append("Remote only")
    return ", ".join(parts) or "None"


def _details(src: Source, view: sv.SourceView) -> str:
    """What Show details prints: the kind, the saved settings and the raw last status."""
    cfg = {k: v for k, v in (src.config or {}).items() if k != sv.REMOVED_KEY}
    lines = [f"kind: {src.kind}", f"how often: every {src.cadence_hours} hours"]
    lines += [f"{k}: {json.dumps(v) if not isinstance(v, str) else v}" for k, v in cfg.items()]
    lines.append(f"last status: {view.raw or 'no problem recorded'}")
    return "\n".join(lines)


def _robots(src: Source) -> str:
    if src.kind in ("jobspy", "sam_gov", "sec_edgar") or src.kind in sv.PLATFORMS:
        return ("Kestrel waits between requests, names itself in each one and never signs in. "
                "Company pages are read from the public job list the hiring system offers.")
    return "Kestrel follows this site's robots.txt and waits between requests. It never signs in."


@router.get("/sources/{source_id}/drawer", response_class=HTMLResponse)
async def drawer(request: Request, source_id: int):
    async with sessionmaker()() as s:
        ctx = await _drawer_ctx(request, s, source_id)
    if ctx is None:
        return HTMLResponse("source not found", status_code=404)
    return templates().TemplateResponse(request, "partials/source_drawer.html", ctx)


def drawer_with_list(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    """The drawer, with the groups and health tiles refreshed out of band behind it."""
    return templates().TemplateResponse(
        request, "partials/source_drawer.html", {**ctx, "refresh_list": True},
        headers=headers or {},
    )


@router.post("/sources/{source_id}/check", response_class=HTMLResponse)
async def check_now(request: Request, source_id: int):
    """Check now on the usual models: the real runner, inline, wrapped so a dead network or a
    down model is a line in the drawer, never a 500 (the same call Run now makes)."""
    async with sessionmaker()() as s:
        src = await s.get(Source, source_id)
        if src is None or sv.is_removed(src):
            return HTMLResponse("source not found", status_code=404)
        kind = src.kind
        from kestrel.pipeline import lane
        from kestrel.services.setting_overrides import load_settings_with_db

        online = lane.scores_on_hosted(await load_settings_with_db(s))
    if online and kind in pg.SIGNAL_SOURCE_KINDS:
        # This kind reads with the model. Online, a click would send text to the service with no
        # per-send confirmation, so it waits for its schedule (Hosted mode's standing consent).
        async with sessionmaker()() as s:
            ctx = await _drawer_ctx(request, s, source_id)
        if ctx is None:
            return HTMLResponse("source not found", status_code=404)
        return drawer_with_list(request, {**ctx, "note": {"tone": "warn", "text": (
            "Nothing was sent. This source is read with your online model, so it is checked "
            "on its schedule, not by a click.")}})
    text = await pg._trigger_run(source_id)
    async with sessionmaker()() as s:
        ctx = await _drawer_ctx(request, s, source_id)
    if ctx is None:
        return HTMLResponse("source not found", status_code=404)
    v = ctx["view"]
    if text.startswith("queued, but the run failed"):
        note = {"tone": "bad", "text": text}
    elif v.health in ("w", "b") and v.reason:
        note = {"tone": "warn" if v.health == "w" else "bad", "text": v.reason}
    else:
        got = f"{v.open_n} open" if v.open_n is not None else "done"
        note = {"tone": "good", "text": f"{v.name} checked: {got}."}
    return drawer_with_list(request, {**ctx, "note": note})


@router.post("/sources/{source_id}/cadence", response_class=HTMLResponse)
async def set_cadence(request: Request, source_id: int):
    form = await request.form()
    try:
        hours = int(str(form.get("cadence_hours") or ""))
        if not 1 <= hours <= 24 * 30:
            raise ValueError
    except ValueError:
        return HTMLResponse("how often must be 1 to 720 hours", status_code=400)
    async with sessionmaker()() as s:
        src = await s.get(Source, source_id)
        if src is None or sv.is_removed(src):
            return HTMLResponse("source not found", status_code=404)
        src.cadence_hours = hours
        await s.commit()
        label = dict(CADENCES).get(hours, f"every {hours} hours").lower()
        name = src.name
    return HTMLResponse("", headers=pg.toast_header(f"{name} is now checked {label}."))


@router.post("/sources/{source_id}/remove", response_class=HTMLResponse)
async def remove_source(request: Request, source_id: int):
    """Remove a source you added. It is hidden and switched off, never deleted: the roles it
    found keep their source, and Undo brings it back as it was."""
    async with sessionmaker()() as s:
        src = await s.get(Source, source_id)
        token = request.query_params.get("token")
        siblings: list[Source] = []  # the other sources the same add created (Undo takes them too)
        if token is not None:
            row = await s.get(Setting, pursuit_undo.PREFIX + token)
            if (
                row is None
                or row.updated_at < (datetime.now(UTC) - pursuit_undo.KEEP).isoformat()
                or row.value.get("kind") != "source_added"
                or src is None
                or row.value.get("source") != pursuit_undo.row_dict(src)
            ):
                return HTMLResponse(
                    '<span data-undo-msg data-ok="0">This can no longer be undone.</span>'
                )
            for snap in row.value.get("also") or []:
                other = await s.get(Source, snap.get("id"))
                if other is None or snap != pursuit_undo.row_dict(other):
                    return HTMLResponse(  # one of them changed since: undo none of them
                        '<span data-undo-msg data-ok="0">This can no longer be undone.</span>'
                    )
                siblings.append(other)
            await s.delete(row)
        if src is None or sv.is_removed(src):
            return HTMLResponse("source not found", status_code=404)
        if src.origin not in ("custom", "suggested"):
            return HTMLResponse(
                "only a source you added can be removed; turn it off instead", status_code=400
            )
        stamp = secrets.token_urlsafe(9)
        for gone in (src, *siblings):  # one stamp: Restore brings back all of them
            gone.config = {
                **(gone.config or {}),
                sv.REMOVED_KEY: {"enabled": bool(gone.enabled), "at": stamp},
            }
            gone.enabled = False
        await s.commit()
        name = src.name
        ctx = await pg._panel_ctx(request, s)
    return pg.list_response(
        request,
        ctx,
        pg.toast_header(
            f"Removed {name}. Roles already found stay.",
            f"/sources/{source_id}/restore?at={stamp}",
            **{"src-drawer-close": {}},
        ),
    )


@router.post("/sources/{source_id}/restore", response_class=HTMLResponse)
async def restore_source(request: Request, source_id: int):
    async with sessionmaker()() as s:
        src = await s.get(Source, source_id)
        if src is None:
            return HTMLResponse("source not found", status_code=404)
        marker = (src.config or {}).get(sv.REMOVED_KEY)
        if (
            not isinstance(marker, dict)
            or not request.query_params.get("at")
            or marker.get("at") != request.query_params["at"]
            or src.enabled
        ):
            return HTMLResponse(
                '<span data-undo-msg data-ok="0">This can no longer be undone.</span>'
            )
        if marker:
            # Everything removed together (same stamp) comes back together.
            stamp = request.query_params["at"]
            for row in (await s.execute(select(Source))).scalars():
                mk = (row.config or {}).get(sv.REMOVED_KEY)
                if row.id != src.id and not (isinstance(mk, dict) and mk.get("at") == stamp):
                    continue
                if not isinstance(mk, dict):
                    continue
                row.config = {k: v for k, v in (row.config or {}).items()
                              if k != sv.REMOVED_KEY}
                row.enabled = bool(mk.get("enabled"))
            await s.commit()
        ctx = await pg._panel_ctx(request, s)
    return pg.list_response(request, ctx)


# ----------------------------------------------------------------------------- suggestions
@router.post("/sources/suggestions/add", response_class=HTMLResponse)
async def suggestion_add(request: Request):
    """Add a suggested company. On Employment Kestrel looks for its public job board (a few
    polite requests) and adds what it finds; on Consulting it watches its public SEC filings.
    The operator pressed Add for this named company, so it starts on, and Undo removes it."""
    from kestrel.ingest.board_lookup import find_company_board

    name = _text(await request.form(), "name")
    async with sessionmaker()() as s:
        track = await _track(request, s)
        ctx = await pg._panel_ctx(request, s)
    if not name:
        return pg.list_response(request, ctx)
    if track == "consulting":
        spec = {"kind": "sec_edgar", "config": {"queries": [f'"{name}"']}, "name": name}
        text = f"Added {name}. Kestrel watches its public filings."
    else:
        try:
            async with await pg._http_client() as http:
                result = await find_company_board(name, http)
        except Exception as exc:  # a dead network is a message, never a 500
            log.info("source_suggestion_lookup_failed", error=type(exc).__name__)
            result = None
        m = result.match if result is not None else None
        if m is None:
            return pg.list_response(
                request,
                ctx,
                pg.toast_header(
                    f"No public job board found for {name}. Use Add a source with its careers page "
                    "link."
                ),
            )
        spec = {"kind": m.kind, "config": m.config, "name": result.company}
        jobs = f", {m.jobs} open" if m.jobs else ""
        text = f"Added {result.company} ({m.platform}{jobs}). Check it is the right company."
    async with sessionmaker()() as s:
        dupe = await _existing(s, spec)
        if dupe is not None:
            await source_suggestions.dismiss(s, track, name)
            await s.commit()
            ctx = await pg._panel_ctx(request, s)
            return pg.list_response(
                request, ctx, pg.toast_header(f"{dupe.name} is already one of your sources.")
            )
        src = Source(
            track=track,
            kind=spec["kind"],
            name=spec["name"],
            config=spec["config"],
            enabled=True,
            cadence_hours=pg._DEFAULT_CADENCE,
            origin="suggested",
        )
        s.add(src)
        await s.commit()
        ctx = await pg._panel_ctx(request, s)
        sid = src.id
        token = await pursuit_undo.save(
            s, {"kind": "source_added", "source": pursuit_undo.row_dict(src)}
        )
        await s.commit()
    return pg.list_response(
        request,
        ctx,
        pg.toast_header(
            text, f"/sources/{sid}/remove?token={token}", **{"src-arrived": {"id": sid}}
        ),
    )


@router.post("/sources/suggestions/dismiss", response_class=HTMLResponse)
async def suggestion_dismiss(request: Request):
    name = _text(await request.form(), "name")
    async with sessionmaker()() as s:
        track = await _track(request, s)
        changed = await source_suggestions.dismiss(s, track, name)
        token = await pursuit_undo.save(s, {"kind": "suggestion_dismiss", "track": track,
                                           "name": name}) if changed else None
        await s.commit()
        ctx = await pg._panel_ctx(request, s)
    undo = f"/sources/suggestions/undismiss?token={token}" if token else None
    return pg.list_response(
        request, ctx, pg.toast_header(f"Dismissed {name}. Kestrel won't suggest it again.", undo)
    )


@router.post("/sources/suggestions/undismiss", response_class=HTMLResponse)
async def suggestion_undismiss(request: Request):
    async with sessionmaker()() as s:
        row = await s.get(Setting, pursuit_undo.PREFIX + request.query_params.get("token", ""))
        if (row is None or row.value.get("kind") != "suggestion_dismiss"
                or row.updated_at < (datetime.now(UTC) - pursuit_undo.KEEP).isoformat()):
            return HTMLResponse(
                '<span data-undo-msg data-ok="0">This can no longer be undone.</span>')
        await source_suggestions.undismiss(s, row.value["track"], row.value["name"])
        await s.delete(row)
        await s.commit()
        ctx = await pg._panel_ctx(request, s)
    return pg.list_response(request, ctx)
