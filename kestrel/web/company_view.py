"""What the Company page shows (D-UI-123, SPEC-INTERFACES 2.1 `/orgs/{id}`), as plain dicts so the
templates stay dumb renderers. Everything is read from rows Kestrel already holds; nothing here
fetches a page or calls a model (hard rule 7: public data only, and the summary is queued, never
run, by the page).

Hard rule 6 applies to every figure: pay and the reply rate say "too few to tell" under five."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    Contact,
    ContactImport,
    Opportunity,
    Org,
    OutreachDraft,
    Pursuit,
)
from kestrel.research import org_summary as osum
from kestrel.research.connections import KEPT_SOURCE, SOURCE, connection_paths
from kestrel.services.pursuit_board import stage_name
from kestrel.urls import safe_web_url
from kestrel.web.fit import fit_for
from kestrel.web.queries import latest_evaluation
from kestrel.web.redact import redact_text

MIN_SAMPLE = 5  # hard rule 6
STALE_DAYS = 45  # listed this long: often filled or forgotten, and Kestrel cannot tell which
LINKEDIN_LINE = (
    "Because you imported these connections from LinkedIn, their details may not be public. "
    "Kestrel keeps them on this computer only and never shares them."
)
SOURCE_KINDS = {
    "website": "Company website",
    "careers": "Company careers page",
    "ats_board": "Job board listing",
    "news": "Company news page",
    "filing": "SEC filing (EDGAR)",
    "search": "Web search result",
    "pasted": "Text you pasted",
}
# Pursuit statuses that mean the company answered (a rejection is an answer). Applied, ghosted,
# withdrawn and the pre-application steps are not.
_REPLIED = frozenset(
    {"screening", "interviewing", "final", "offer", "accepted", "rejected",
     "conversation", "scoping", "proposal_sent", "negotiating", "won", "lost"}
)
_GOOD = frozenset({"offer", "accepted", "won", "interviewing", "final", "conversation", "scoping"})
_BAD = frozenset({"rejected", "lost"})


# ------------------------------------------------------------------------------ small helpers
def parse_iso(stamp: object) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def short_date(stamp: object, *, now: datetime | None = None) -> str:
    """"Oct 9", or "Today" for the current day. Empty when there is no usable date."""
    dt = parse_iso(stamp)
    if dt is None:
        return ""
    now = now or datetime.now(UTC)
    if dt.date() == now.date():
        return "Today"
    from kestrel.date_display import format_display

    return format_display(dt, "short")


def safe_url(url: str | None) -> str | None:
    """Only web addresses become links; anything else (a stored `javascript:` value) is dropped."""
    return safe_web_url(url)


def host_of(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url if "//" in url else f"//{url}").netloc
    return host.removeprefix("www.")


def _age_days(stamp: object, now: datetime) -> int | None:
    dt = parse_iso(stamp)
    return None if dt is None else max(0, (now - dt).days)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def money(n: float, currency: str | None) -> str:
    symbol = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "$", "AUD": "$"}.get(
        currency or "USD", ""
    )
    prefix = symbol or ""
    suffix = "" if symbol else f" {currency}"
    return f"{prefix}{round(n):,}{suffix}"


# ---------------------------------------------------------------------------- the open roles
def liveness_chip(opp: Opportunity, now: datetime) -> dict[str, str] | None:
    """The chip on a role row. Only a role the company's own page still lists gets a chip that
    says so; a role that was never checked shows none (nothing is claimed)."""
    if opp.liveness != "live":
        return None
    when = short_date(opp.liveness_checked_at, now=now)
    where = "today" if when == "Today" else f"on {when}" if when else "recently"
    age = _age_days(opp.date_posted or opp.first_seen_at, now)
    if age is not None and age >= STALE_DAYS:
        return {
            "state": "stale",
            "text": f"Possibly stale: posted {age} days ago, still listed",
            "tip": "Roles listed for this long are often filled or forgotten. Kestrel cannot "
            "tell which. A quick look at the posting, or a note to the recruiter, settles it.",
        }
    return {
        "state": "ok",
        "text": "Looks active",
        "tip": f"Kestrel checked the company's careers page {where} and this role is still "
        "listed.",
    }


def _place(opp: Opportunity) -> str:
    place = opp.location_raw or ""
    if opp.remote_type in ("remote", "hybrid") or opp.is_remote:
        word = "Hybrid" if opp.remote_type == "hybrid" else "Remote"
        place = f"{place} · {word}" if place and word.lower() not in place.lower() else (
            place or word
        )
    return place or "Place not listed"


async def open_roles(
    session: AsyncSession, opps: list[Opportunity], pursuits: dict[int, Pursuit], now: datetime
) -> list[dict[str, Any]]:
    out = []
    for o in opps:
        if o.archived_at is not None or o.liveness == "closed":
            continue
        pursuit = pursuits.get(o.id)
        if pursuit is not None and pursuit.closed_at is not None:
            continue
        fit = fit_for(await latest_evaluation(session, o.id), o.stage)
        out.append(
            {
                "id": o.id,
                "title": o.title or "Untitled role",
                "place": _place(o),
                "stage": _cap(stage_name(pursuit.status, pursuit.track) if pursuit else fit.word),
                "tone": fit.tone if pursuit is None else "info",
                "score": fit.score,
                "chip": liveness_chip(o, now),
            }
        )
    out.sort(key=lambda r: (-(r["score"] or 0), r["title"]))
    return out


# ------------------------------------------------------------------------------- the history
def reply_line(applications: list[Pursuit]) -> str:
    """Hard rule 6 in the history card: no rate under five applications."""
    n = len(applications)
    if n < MIN_SAMPLE:
        return (
            f"Reply rate: Too few to tell ({n}). A rate needs at least {MIN_SAMPLE} applications."
        )
    replied = sum(1 for p in applications if p.status in _REPLIED)
    return f"Reply rate: {replied} of {n} applications heard back ({round(100 * replied / n)}%)."


def history_rows(
    applications: list[Pursuit], titles: dict[int, str], now: datetime
) -> list[dict[str, Any]]:
    rows = []
    for p in sorted(applications, key=lambda p: p.applied_at or "", reverse=True):
        tone = "good" if p.status in _GOOD else "bad" if p.status in _BAD else "info"
        note = p.outcome_reason or p.next_action or ""
        waited = _age_days(p.applied_at, now)
        if not note and p.status == "applied" and waited is not None:
            note = f"No reply yet, {waited} days"
        rows.append(
            {
                "title": titles.get(p.opportunity_id, "Role"),
                "applied": f"Applied {short_date(p.applied_at, now=now)}".strip(),
                "status": _cap(stage_name(p.status, p.track)),
                "tone": tone,
                "note": note,
            }
        )
    return rows


# ------------------------------------------------------------------------------------ pay
def pay_view(opps: list[Opportunity]) -> dict[str, Any]:
    """The company's posted pay, built only from its own postings that list a yearly range, and
    only in one currency. Under five it says so and gives no range (hard rule 6)."""
    listed = [
        o for o in opps
        if o.comp_interval == "year" and (o.comp_min is not None or o.comp_max is not None)
        and getattr(o, "comp_source", None) != "inferred"  # a job-board estimate is not posted pay
    ]
    # One role reposted is one observation: count each (title, range) once.
    seen: set[tuple] = set()
    unique = []
    for o in listed:
        key = ((getattr(o, "title", None) or "").lower(), o.comp_min, o.comp_max, o.comp_currency)
        if key not in seen:
            seen.add(key)
            unique.append(o)
    listed = unique
    currencies = {o.comp_currency or "USD" for o in listed}
    if len(currencies) > 1:
        listed = [o for o in listed if (o.comp_currency or "USD") == "USD"]
        currencies = {"USD"}
    n = len(listed)
    if n < MIN_SAMPLE:
        return {"n": n, "enough": False}
    currency = next(iter(currencies)) if currencies else "USD"
    mids = sorted(
        statistics.fmean([v for v in (o.comp_min, o.comp_max) if v is not None]) for o in listed
    )
    lows = [o.comp_min if o.comp_min is not None else o.comp_max for o in listed]
    highs = [o.comp_max if o.comp_max is not None else o.comp_min for o in listed]
    q1, med, q3 = statistics.quantiles(mids, n=4, method="inclusive")
    lo, hi = min(lows), max(highs)
    span = (hi - lo) or 1.0
    pad = span * 0.1
    a, b = lo - pad, hi + pad

    def pct(v: float) -> float:
        return round((v - a) / (b - a) * 100, 1)

    return {
        "n": n,
        "enough": True,
        "typical": money(med, currency),
        "middle": f"{money(q1, currency)} to {money(q3, currency)}",
        "spread": f"{money(lo, currency)} to {money(hi, currency)}",
        "scale": (money(a, currency), money(b, currency)),
        "bar": {
            "typical": pct(med),
            "q1": pct(q1),
            "q3w": round(pct(q3) - pct(q1), 1),
            "lo": pct(lo),
            "spanw": round(pct(hi) - pct(lo), 1),
        },
    }


# ---------------------------------------------------------------------------------- summary
def _marker(n: int, sources: dict[int, dict]) -> dict[str, Any]:
    src = sources.get(n) or {}
    kind = SOURCE_KINDS.get(src.get("kind") or "", "Public page")
    return {"n": n, "tip": f"{kind}: {src.get('title') or host_of(src.get('url'))}".strip(": ")}


def summary_view(
    record: dict[str, Any] | None,
    *,
    live: bool,
    online_model: str | None,
    redact: Any = None,
) -> dict[str, Any]:
    """The summary card's state. `online_model` is the online model's name when Tier 2 runs on
    one, else None (then the AI on this computer wrote it). `redact` maps free text (confidential
    mode); it never changes what was stored."""
    clean = redact or (lambda t: t)
    # With Tier 2 online, name the model that wrote the stored text (an older local one is named
    # as itself, never claimed by the online service); otherwise it was the AI on this computer.
    # A stored text names the model that wrote it, whatever Tier 2 is now: it may have been an
    # online model since switched back to local, and "on this computer" would then be untrue.
    stored = (record or {}).get("model")
    written_by = stored or online_model
    label = (f"Written by {written_by}" if written_by
             else "Written by the AI on this computer")
    base: dict[str, Any] = {"label": label, "live": live, "online": bool(online_model)}
    if record is None:
        return {**base, "state": "pending" if live else "none", "sources": []}
    sources = {
        int(s.get("index", i + 1)): {**s, "title": clean(s.get("title") or "")}
        for i, s in enumerate(record.get("sources") or [])
    }
    base.update(
        source_count=int(record.get("source_count") or len(sources)),
        updated=short_date(record.get("generated_at")),
        sources=list(sources.values()),
    )
    status = record.get("status")
    if status == "insufficient":
        return {**base, "state": "pending" if live else "insufficient",
                "text": osum.INSUFFICIENT_TEXT.format(n=base["source_count"])}
    if status != "ok":
        return {**base, "state": "pending" if live else "quarantined"}
    sentences = [
        {
            "text": clean(s.get("text") or ""),
            "markers": [_marker(int(i), sources) for i in s.get("source_indexes") or []],
        }
        for s in record.get("sentences") or []
    ]
    tags = {
        t.get("key"): {
            "text": clean(t.get("text") or ""),
            "markers": [_marker(int(i), sources) for i in t.get("source_indexes") or []],
        }
        for t in record.get("tags") or []
    }
    return {**base, "state": "ok", "refreshing": live, "sentences": sentences, "tags": tags}


def knows_view(
    record: dict[str, Any] | None, signals: list[Any], now: datetime
) -> dict[str, Any]:
    """"What Kestrel knows": the public pages the summary rests on (each with the date Kestrel
    saw it), then recent signals the research passes recorded, newest first."""
    facts = []
    for i, s in enumerate((record or {}).get("sources") or []):
        facts.append(
            {
                "n": int(s.get("index", i + 1)),
                "what": s.get("title") or host_of(s.get("url")) or "Public page",
                "kind": SOURCE_KINDS.get(s.get("kind") or "", "Public page"),
                "url": safe_url(s.get("url")),
                "seen": short_date(s.get("fetched_at"), now=now),
            }
        )
    sigs = []
    for s in sorted(
        (x for x in signals if isinstance(x, dict) and x.get("statement")),
        key=lambda x: x.get("observed_at") or "",
        reverse=True,
    )[:12]:
        sigs.append(
            {
                "what": s["statement"],
                "source": host_of(s.get("source_url")) or "Company research",
                "url": safe_url(s.get("source_url")),
                "seen": short_date(s.get("observed_at"), now=now),
            }
        )
    return {"facts": facts, "signals": sigs}


# ------------------------------------------------------------------------------------ people
async def people_you_know(session: AsyncSession, org_id: int) -> dict[str, Any]:
    """Only from the operator's own imported connections file (D-UI-131)."""
    imports = (
        await session.execute(select(func.count()).select_from(ContactImport))
    ).scalar_one()
    paths = await connection_paths(session, org_id) if imports else []
    drafts = await _drafts_for(session, [p.contact_id for p in paths])
    stamps = [p.imported_at for p in paths if p.imported_at]
    people = [
        {
            "name": p.name,
            "title": p.title,
            "now": p.kind == "works_here",
            "how": (
                ("Works here now" if p.kind == "works_here" else "Used to work here, as your "
                 "earlier import shows")
                + (f". Connected {short_date(p.connected_on)}" if p.connected_on else "")
                + ("" if p.confirmed else ". Possible match, not confirmed")
            ),
            "imported": short_date(p.imported_at),
            "drafts": drafts.get(p.contact_id, []),
            "id": p.contact_id,
        }
        for p in paths
    ]
    return {
        "imported": bool(imports),
        "people": people,
        "when": short_date(max(stamps)) if stamps else "",
        "line": LINKEDIN_LINE if people else "",
    }


async def _drafts_for(session: AsyncSession, contact_ids: list[int]) -> dict[int, list[Any]]:
    if not contact_ids:
        return {}
    rows = (
        await session.execute(
            select(OutreachDraft)
            .where(OutreachDraft.contact_id.in_(contact_ids))
            .order_by(OutreachDraft.created_at.desc())
        )
    ).scalars()
    out: dict[int, list[Any]] = {}
    for d in rows:
        out.setdefault(d.contact_id, []).append(d)
    return out


async def public_contacts(session: AsyncSession, org_id: int) -> list[dict[str, Any]]:
    """Business contacts the company made public: never an imported connection."""
    rows = (
        await session.execute(
            select(Contact)
            .where(
                Contact.org_id == org_id,
                func.coalesce(Contact.source, "").not_in((SOURCE, KEPT_SOURCE)),
            )
            .order_by(Contact.name)
        )
    ).scalars().all()
    drafts = await _drafts_for(session, [c.id for c in rows])
    return [
        {
            "id": c.id,
            "name": c.name,
            "title": c.title,
            "url": safe_url(c.source_url),
            "page": host_of(c.source_url) or "No page recorded",
            "seen": short_date(c.created_at),
            "details": " · ".join(x for x in (c.name, c.title, c.email, c.phone) if x),
            "drafts": drafts.get(c.id, []),
        }
        for c in rows
    ]


# ------------------------------------------------------------------------------------ gather
async def gather(session: AsyncSession, org: Org) -> dict[str, Any]:
    now = datetime.now(UTC)
    opps = list(
        (
            await session.execute(
                select(Opportunity)
                .where(Opportunity.org_id == org.id)
                .order_by(Opportunity.first_seen_at.desc(), Opportunity.id.desc())
            )
        ).scalars()
    )
    pursuits = {
        p.opportunity_id: p
        for p in (
            await session.execute(
                select(Pursuit).where(Pursuit.opportunity_id.in_([o.id for o in opps] or [0]))
            )
        ).scalars()
    }
    applications = [p for p in pursuits.values() if p.applied_at]
    roles = await open_roles(session, opps, pursuits, now)
    record = await osum.load_record(session, org.id)
    return {
        "roles": roles,
        "record": record,
        "knows": knows_view(record, org.signals or [], now),
        "pay": pay_view(opps),
        "know": await people_you_know(session, org.id),
        "contacts": await public_contacts(session, org.id),
        "history": history_rows(applications, {o.id: o.title or "Role" for o in opps}, now),
        "reply_line": reply_line(applications),
        "applications": len(applications),
        "hidden": org.hidden_at is not None,
        "stamp": utcnow_iso(),
    }


def redactor(mapping: Any, enabled: bool):
    """A text mapper for confidential mode, or None when it is off."""
    if not enabled:
        return None
    return lambda text: redact_text(text, mapping, enabled=True) or ""


def redact_tree(value: Any, fn: Any) -> Any:
    """`fn` over every string in plain data (dicts, lists). Drafts are ORM rows, left as they are:
    the page drops them when the company is the employer."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: (v if k == "drafts" else redact_tree(v, fn)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_tree(v, fn) for v in value]
    return value


def redact_research(research: Any, fn: Any) -> dict[str, Any]:
    return {
        "researched_at": research.researched_at,
        "insufficient_sources": research.insufficient_sources,
        "summary_md": fn(research.summary_md or ""),
        "angle": fn(research.angle or ""),
        "unknowns": [
            fn(str(u.get("statement") if isinstance(u, dict) else u))
            for u in research.unknowns or []
        ],
    }


def strip_urls(value: Any) -> Any:
    """Plain data with every `url` emptied: an employer's own addresses name it."""
    if isinstance(value, dict):
        return {k: (None if k == "url" else (v if k == "drafts" else strip_urls(v)))
                for k, v in value.items()}
    if isinstance(value, list):
        return [strip_urls(v) for v in value]
    return value
