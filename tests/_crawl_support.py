"""Shared pieces of the route crawler (tests/test_route_crawl.py), kept apart so the crawler's own
guard tests can drive them with a deliberately broken app.

Nothing here reaches the network or a model. All rows and text are invented.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup
from sqlalchemy import select

from kestrel import paths
from kestrel.db.engine import get_sessionmaker, reset_engine
from kestrel.db.models import (
    Calibration,
    DocRender,
    Label,
    Letter,
    MockSession,
    Notification,
    Offer,
    Opportunity,
    PrepPack,
    Pursuit,
    Quarantine,
    Run,
    SavedView,
    ScrapeRun,
    Source,
    StatusEvent,
    Task,
)
from tests import clock_seed, role_seed
from tests.test_contracts import MODEL, TIERS, _walk  # reuse: the route table and online route

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "kestrel" / "web" / "templates"

# The only requests a page view (GET) may make that leave this computer: (method, host, path) ->
# the docs/OUTBOUND-CALLS.md row that documents it. Anything else a GET sends is a bug. An
# allowed request must also carry no body and no query string, so no profile, resume or posting
# text can ride along (page_view_requests asserts that).
PAGE_VIEW_EGRESS: dict[tuple[str, str, str], str] = {
    ("GET", "openrouter.ai", "/api/v1/key"): "docs/OUTBOUND-CALLS.md, AI providers table, row "
    "'OpenRouter balance': the key only, about every 5 minutes, only in Hosted mode with "
    "OpenRouter",
}


def page_view_requests(rec) -> list[str]:
    """The requests made during a GET that are not documented page-view calls (see
    PAGE_VIEW_EGRESS). A documented call that carries a body or a query string is reported too."""
    out = []
    for req in rec.http:
        host = req.url.host
        if host in ("127.0.0.1", "localhost", "::1"):
            continue
        key = (req.method, host, req.url.path.rstrip("/"))
        if key not in PAGE_VIEW_EGRESS:
            if req.method == "GET" and not req.content and req.url.path.rstrip("/").endswith(
                "/models"
            ):
                continue  # a provider's model list: the saved key only (as Recorder.sent allows)
            out.append(f"{req.method} {req.url}")
        elif req.content or req.url.query:
            out.append(f"{req.method} {req.url} carries a body or query (must carry the key only)")
    return out


# Routes the crawler does not call. Every entry carries its reason; a route not listed here is
# crawled, so a new route is covered the day it is added.
ALLOWLIST: dict[str, str] = {
    "POST /api/quit": "asks the desktop shell to quit the whole app; the one route whose "
    "success ends the process the test runs in",
    "POST /api/focus": "raises the desktop window (a second launch posts it); a no-op headless "
    "and not a page, so there is no visible result to check",
}

# Routes that show the data folder on purpose, because finding their files is what the page is
# for. The rule bans the path everywhere else. (A path inside a "Show details" block is allowed
# on every page: CLAUDE.md puts raw terminal commands behind that toggle.)
SHOWS_DATA_FOLDER: dict[str, str] = {
    "GET /about": "its Your data row names the folder next to an Open folder button",
    "GET /backup": "the Backup page says which folder is backed up",
    "POST /about/diagnostics/save": "tells the person where the diagnostics zip was saved",
    "GET /settings/learning": "the Learning details page lists the database and ml folder so a "
    "technical reader can inspect them",
    "POST /settings/learning/reset": "answers with the same Learning details page",
}

# Field names a form can carry as consent. The crawler never posts them: a post that never
# consents must never send anything online (test_contracts owns the consent assertions).
CONSENT_NAMES = {"confirmed", "confirm", "ack", "consent"}

# Values for path parameters that are names or words, not rows.
WORD_PARAMS = {
    "tier": "tier2",
    "track": "employment",
    "pane": "models",
    "name": "ollama",
    "action": "read",
    "verb": "applied",
    "decision": "accept",
    "do": "accept",
    "key": "summary",
    "kind": "greenhouse",
    "section": "about",
    "group": "recent",
    "token": "invented-token",
}

VISIBLE_ERROR_NAME = re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception)\b")
HTML_TYPES = ("text/html", "application/xhtml")
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


@dataclass
class Case:
    method: str
    path: str  # the route pattern, e.g. /opportunities/{opp_id}
    route: object = field(repr=False, default=None)

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


def enumerate_cases(app, allowlist: dict[str, str] | None = None) -> list[Case]:
    """Every method of every route (HEAD and OPTIONS are the framework's echoes of GET)."""
    allow = ALLOWLIST if allowlist is None else allowlist
    cases, seen = [], set()
    for path, route in _walk(app.routes):
        for method in sorted(getattr(route, "methods", None) or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            case = Case(method, path, route)
            # A second handler on the same method and path never runs (the first one answers).
            if case.key not in allow and case.key not in seen:
                seen.add(case.key)
                cases.append(case)
    return cases


def shadowed_routes(app) -> list[str]:
    """Method and path pairs registered more than once; only the first can ever answer."""
    seen, dup = set(), []
    for path, route in _walk(app.routes):
        for method in getattr(route, "methods", None) or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            key = f"{method} {path}"
            if key in seen:
                dup.append(key)
            seen.add(key)
    return sorted(set(dup))


def stale_allowlist(app, allow: dict[str, str] | None = None) -> list[str]:
    seen = {f"{m} {p}" for p, r in _walk(app.routes) for m in (getattr(r, "methods", None) or ())}
    return sorted(set(ALLOWLIST if allow is None else allow) - seen)


# --------------------------------------------------------------------------------- the world
async def build_world() -> dict[str, int]:
    """One rich invented world in the current (throwaway) data home: roles at every stage, a
    pursuit in each status, a cover letter draft, a prep pack, offers, a source of every adapter
    kind, notifications, runs, tasks, a quarantined role and a calibration proposal."""
    from kestrel.ingest.adapters import known_kinds

    ids = await role_seed.seed_async()
    docs = paths.documents_dir()
    docs.mkdir(parents=True, exist_ok=True)
    paths.prep_packs_dir().mkdir(parents=True, exist_ok=True)
    pdf = docs / "invented-render.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    pack_pdf = paths.prep_packs_dir() / "invented-pack.pdf"
    pack_pdf.write_bytes(pdf.read_bytes())
    now = datetime.now(UTC)

    def ago(**kw) -> str:
        return clock_seed.ago(now, **kw).isoformat(timespec="seconds")

    async with get_sessionmaker()() as s:
        # An opportunity in each status: every stage and the hidden/archived states.
        base = (await s.execute(select(Opportunity).limit(1))).scalars().first()
        extra = {}
        for stage, kw in (
            ("new", {}),
            ("t0_rejected", {}),
            ("t1_rejected", {}),
            ("t2_scored", {}),
            ("t3_done", {}),
            ("archived", {"archived_at": ago(days=2), "archive_reason": "later"}),
        ):
            o = Opportunity(
                track="employment",
                opp_type="job",
                org_id=base.org_id,
                source_id=base.source_id,
                title=f"Invented {stage} role",
                org_name="Larkspur Logistics",
                url=f"https://jobs.invalid/{stage}",
                external_id=f"crawl-{stage}",
                description_md="An invented posting body for the crawl.",
                stage=stage,
                **kw,
            )
            s.add(o)
            extra[stage] = o
        await s.flush()

        pursuits = {}
        for status, opp in zip(
            ("applied", "interview", "offer", "rejected", "withdrawn"),
            [
                ids["strong"],
                ids["second"],
                ids["maybe"],
                extra["t2_scored"].id,
                extra["t3_done"].id,
            ],
            strict=False,
        ):
            p = Pursuit(
                opportunity_id=opp,
                track="employment",
                status=status,
                applied_at=ago(days=9),
                last_activity_at=ago(days=3),
                next_action="Send a thank-you note",
                next_action_at=ago(days=-2),
                notes_md="Invented note.",
            )
            s.add(p)
            pursuits[status] = p
        await s.flush()
        for p in pursuits.values():
            s.add(StatusEvent(pursuit_id=p.id, from_status=None, to_status=p.status))
        s.add_all(
            [
                Offer(pursuit_id=pursuits["offer"].id, base=120000, status="offer"),
                Offer(pursuit_id=pursuits["interview"].id, base=95000, status="offer"),
            ]
        )

        letter = DocRender(
            opportunity_id=ids["strong"],
            doc_master_id=ids["master"],
            template="cover_letter",
            config={"kind": "cover_letter"},
            pdf_path=str(pdf),
            filename="letter.pdf",
            page_count=1,
        )
        pack = PrepPack(
            opportunity_id=ids["strong"],
            content_md="Invented prep pack.",
            pdf_path=str(pack_pdf),
            questions_to_ask=["What does success look like?"],
        )
        mock = MockSession(
            opportunity_id=ids["strong"],
            mode="practice",
            question_count=1,
            transcript=[
                {
                    "question": "Tell me about a project.",
                    "answer": None,
                    "critique": None,
                    "scores": None,
                    "unsupported_claims": None,
                }
            ],
        )
        quarantined = Quarantine(
            opportunity_id=extra["t1_rejected"].id,
            tier=1,
            model="m",
            raw_response="x",
            error="invalid",
            attempts=2,
        )
        cal = Calibration(
            track="employment",
            n_samples=12,
            status="proposed",
            previous_weights={"role_fit": 0.3},
            proposed_weights={"role_fit": 0.4},
        )
        s.add_all([letter, pack, mock, quarantined, cal])
        s.add(Label(opportunity_id=ids["strong"], track="employment", score=4.0, verdict="keep"))
        s.add(SavedView(name="Invented view", track="employment", filters={}, sort={}))

        # A source of each adapter kind (the Sources page lists them all).
        for kind in known_kinds():
            s.add(
                Source(
                    track="employment",
                    kind=kind,
                    name=f"Invented {kind}",
                    enabled=False,
                    config={},
                    origin="custom",
                )
            )
        await s.flush()
        # The letter's text lives in the letter table, attached to its render (D-LETTER-1).
        s.add(Letter(render_id=letter.id, opportunity_id=ids["strong"], kind="cover_letter",
                     body_md="Dear Hiring Team,\n\nAn invented draft.", word_count=6))
        src = (await s.execute(select(Source).limit(1))).scalars().first()
        s.add(
            ScrapeRun(source_id=src.id, status="done", counts={"new": 3}, finished_at=ago(hours=1))
        )

        runs = [
            Run(
                name="scan",
                track="employment",
                status="done",
                started_at=ago(hours=26),
                finished_at=ago(hours=25),
                counts={},
                mode="local",
            ),
            Run(
                name="scan",
                track="employment",
                status="partial",
                started_at=ago(hours=3),
                finished_at=ago(hours=2),
                counts={},
                mode="local",
            ),
            Run(
                name="scan",
                track="employment",
                status="running",
                started_at=ago(minutes=2),
                counts={},
                mode="local",
            ),
        ]
        s.add_all(runs)
        s.add_all(
            [
                Notification(
                    kind="new_match",
                    title="Invented match",
                    body="Body.",
                    opportunity_id=ids["strong"],
                ),
                Notification(
                    kind="follow_up",
                    severity="warn",
                    title="Invented follow-up",
                    pursuit_id=pursuits["applied"].id,
                    read_at=ago(hours=1),
                ),
            ]
        )
        s.add_all(
            [
                Task(type="t1", payload={"opportunity_id": extra["new"].id}, status="pending"),
                Task(
                    type="t2",
                    payload={"opportunity_id": extra["t2_scored"].id},
                    status="failed",
                    last_error="invented failure",
                ),
                Task(type="research", payload={"opportunity_id": ids["strong"]}, status="done"),
            ]
        )
        await s.flush()
        task = (await s.execute(select(Task).limit(1))).scalars().first()
        await s.commit()
        await s.refresh(letter)
        ids.update(
            {
                "opp_id": ids["strong"],
                "opportunity_id": ids["strong"],
                "org_id": ids["org"],
                "master_id": ids["master"],
                "render_id": ids["render"],
                "letter_id": letter.id,
                "prep_pack_id": pack.id,
                "mock_session_id": mock.id,
                "pursuit_id": pursuits["applied"].id,
                "quarantine_id": quarantined.id,
                "cal_id": cal.id,
                "calibration_id": cal.id,
                "source_id": src.id,
                "run_id": runs[0].id,
                "task_id": task.id,
                "view_id": 1,
                "label_id": 1,
                "note_id": 1,
                "offer_id": 1,
                "override_id": 1,
                "suggestion_id": 1,
                "announcement_id": 1,
                "story_id": ids.get("story", 1),
            }
        )
    await reset_engine()
    return ids


def apply_mode(mode: str) -> None:
    """Local mode is the world as seeded. Hosted mode adds the saved disclosure and an online
    route for every tier, written straight into the throwaway database."""
    if mode != "hosted":
        return
    from kestrel.config import HostedMode

    rows = {
        "llm.hosted_mode.enabled": True,
        "llm.hosted_mode.acknowledged_at": "2026-10-15T00:00:00Z",
        "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
    }
    for t in TIERS:
        rows[f"llm.tiers.{t}.backend"] = "frontier"
        rows[f"llm.tiers.{t}.model"] = MODEL
    for t in ("tier1", "tier2", "docsel"):
        rows[f"llm.hosted_tiers.{t}.backend"] = "frontier"
        rows[f"llm.hosted_tiers.{t}.model"] = MODEL
    con = sqlite3.connect(paths.db_path())
    try:
        for k, v in rows.items():
            con.execute(
                "INSERT OR REPLACE INTO setting(key, value, updated_at) VALUES (?, ?, ?)",
                (k, json.dumps(v), "2026-11-04T00:00:00+00:00"),
            )
        con.commit()
    finally:
        con.close()


def repoint_files(old_home: Path, new_home: Path) -> None:
    """The world's stored file paths name the template home; point them at this test's copy."""
    con = sqlite3.connect(paths.db_path())
    try:
        for table in ("doc_render", "prep_pack"):
            con.execute(
                f"UPDATE {table} SET pdf_path = replace(pdf_path, ?, ?)",  # noqa: S608
                (str(old_home), str(new_home)),
            )
        con.commit()
    finally:
        con.close()


def fill(path: str, ids: dict[str, int]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in WORD_PARAMS:
            return WORD_PARAMS[name]
        return str(ids.get(name, 1))

    return re.sub(r"\{(\w+)\}", sub, path)


# ------------------------------------------------------------------------------ form scraping
def _norm(path: str) -> str:
    path = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "\x00", path.split("?")[0], flags=re.S)
    return re.sub(r"\{[^}]+\}", "\x00", path)


def scrape_forms(templates: Path = TEMPLATES) -> dict[str, list[dict[str, str]]]:
    """Normalized POST url -> the field sets of every form in a template that posts there.
    A form is a <form action=...> or any element with hx-post; a checkbox is left unticked and a
    consent field is never carried."""
    out: dict[str, list[dict[str, str]]] = {}

    def add(url: str, fields: dict[str, str]) -> None:
        if not url.startswith("/"):
            return
        sets = out.setdefault(_norm(url), [])
        if fields not in sets and len(sets) < 3:
            sets.append(fields)

    for p in sorted(templates.rglob("*.html")):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"<form\b([^>]*)>(.*?)</form>", text, re.S | re.I):
            attrs, body = m.group(1), m.group(2)
            url = re.search(r'\b(?:action|hx-post)="([^"]+)"', attrs)
            if url:
                add(url.group(1), _fields(body))
        for m in re.finditer(r'\bhx-post="([^"]+)"', text):
            if _norm(m.group(1)) not in out:  # a button outside any form posts no fields
                add(m.group(1), {})
    return out


def _value(tag: str, default: str = "1") -> str:
    v = re.search(r'\bvalue="([^"]*)"', tag)
    if not v or "{{" in v.group(1) or "{%" in v.group(1):
        return default
    return html_lib.unescape(v.group(1))


def _fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in re.finditer(r"<input\b[^>]*>", body, re.I):
        tag = m.group(0)
        name = re.search(r'\bname="([^"]+)"', tag)
        if not name or "{{" in name.group(1) or name.group(1) in CONSENT_NAMES:
            continue
        kind = (re.search(r'\btype="([^"]+)"', tag) or [None, "text"])[1].lower()
        if kind in ("checkbox", "file", "submit", "button", "image", "radio"):
            continue
        fields[name.group(1)] = _value(tag, "5" if kind == "number" else "Invented text")
    for m in re.finditer(r"<textarea\b[^>]*\bname=\"([^\"]+)\"", body, re.I):
        if m.group(1) not in CONSENT_NAMES and "{{" not in m.group(1):
            fields[m.group(1)] = "Invented text"
    select = r"<select\b[^>]*\bname=\"([^\"]+)\"[^>]*>(.*?)</select>"
    for m in re.finditer(select, body, re.S | re.I):
        if m.group(1) in CONSENT_NAMES or "{{" in m.group(1):
            continue
        opt = re.search(r"<option\b[^>]*>", m.group(2))
        fields[m.group(1)] = _value(opt.group(0), "1") if opt else "1"
    return fields


# ------------------------------------------------------------------------------ body checks
def body_problems(r, data_home: Path, *, shows_path: bool = False) -> list[str]:
    """What is wrong with a response, in words naming the rule and the fix."""
    problems: list[str] = []
    # 503 with a plain message is the designed "this computer cannot do that now" answer (the same
    # reading test_contracts uses for a missing PDF renderer); every other 5xx is a crash.
    refused = r.status_code == 503 and bool(r.text.strip())
    if r.status_code >= 500 and not refused:
        problems.append(
            f"status {r.status_code}: the route crashed or refused; a request must never get a "
            "5xx. Catch the failure in the handler and answer with a plain message"
        )
    ctype = r.headers.get("content-type", "").lower()
    if not ctype.startswith(TEXT_TYPES):
        return problems
    text = r.text
    if ctype.startswith(HTML_TYPES):
        soup = BeautifulSoup(text, "html.parser")
        for t in soup(["script", "style", "template"]):
            t.decompose()
        visible = soup.get_text(" ")
    else:
        visible = text
    for needle, why in (
        ("Traceback", "a Python traceback reached the page; catch the error and show a message"),
        ("coroutine", "a coroutine object reached the page; an async call is missing await"),
        ("object has no attribute", "an AttributeError text reached the page; fix the handler"),
    ):
        if needle in text:
            problems.append(f"body contains {needle!r}: {why}")
    outside_details = re.sub(r"<details\b.*?</details>", "", text, flags=re.S | re.I)
    for home in {str(data_home), str(data_home.resolve()), data_home.as_posix()}:
        if home and home in outside_details and not shows_path:
            problems.append(
                "body contains the absolute data folder path; show a name or a relative label, "
                "never a path on the person's disk (use the Open folder button)"
            )
            break
    # Exception names are allowed under Show details (owner, 2026-11-05), like the data folder.
    shown = (BeautifulSoup(outside_details, "html.parser").get_text(" ")
             if ctype.startswith(HTML_TYPES) else visible)
    leaked = sorted(set(VISIBLE_ERROR_NAME.findall(shown)))
    if leaked:
        problems.append(
            f"visible text names exception classes {leaked}; say what went wrong in plain words"
        )
    return problems
