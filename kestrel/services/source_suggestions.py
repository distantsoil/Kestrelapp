"""Suggested companies for the Sources page (D-UI-102, D-SRC-19): companies the operator has a
pursuit at, on the track being viewed, that no source covers yet. Nothing is added until the
operator presses Add; Dismiss is remembered per track and Undo takes it back.

Building the list reads local data only. Whether a suggested company can be added is decided in
the background (D-SRC-19): a queued `suggestion_check` task first reads the board from the links
of the pursued role and its duplicate copies (no request), and only then asks the public
Greenhouse, Lever, Ashby and SmartRecruiters endpoints, politely (shared rate-limited client,
descriptive User-Agent, robots.txt, at most MAX_PROBES requests a company, MAX_PER_PASS companies a
pass). A definite answer is remembered with its date for EXPIRY_DAYS; a lookup that could not get
an answer (network down, timeout, 429, 5xx) is never remembered as "not found"."""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import timedelta
from typing import cast

import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import iso, utcnow, utcnow_iso
from kestrel.db.models import Opportunity, Org, Pursuit, Setting, Task
from kestrel.ingest.orgs import normalized_name

log = structlog.get_logger()
_now = utcnow  # this module's own clock; tests patch it

KEY = "sources.dismissed_suggestions"
BOARD_CACHE_KEY = "sources.suggestion_boards"
ERRORS_KEY = "sources.suggestion_check_errors"
TASK_TYPE = "suggestion_check"
MAX_SHOWN = 6
EXPIRY_DAYS = 30  # a definite "no board" is not asked again sooner (Check again overrides)
ERROR_RETRY_HOURS = 6  # a failed check is retried by the background no sooner than this
MAX_PER_PASS = 3  # companies checked in one drain pass
LEASE_S = 300
_LIVE = ("pending", "leased")
PLATFORM_LABELS = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters", "rippling": "Rippling", "recruitee": "Recruitee",
    "teamtailor": "Teamtailor", "breezy": "Breezy", "jobvite": "Jobvite", "personio": "Personio",
    "workable": "Workable", "paylocity": "Paylocity", "adp": "ADP", "workday": "Workday",
}
_CLOSED = {"rejected", "withdrawn", "ghosted", "declined", "closed", "lost"}


async def _dismissed(session: AsyncSession) -> dict[str, list[str]]:
    row = await session.get(Setting, KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return {str(k): [str(x) for x in v] for k, v in value.items() if isinstance(v, list)}


async def _save(session: AsyncSession, value: dict[str, list[str]]) -> None:
    row = await session.get(Setting, KEY)
    if row is None:
        session.add(Setting(key=KEY, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


# --------------------------------------------------------------------------- board cache
async def board_cache(session: AsyncSession) -> dict:
    """{normalized name: {"found": bool, "checked_at": iso, ...}}. A found entry carries the
    source spec; a not-found entry is {"found": False, "checked_at": ...}. Kept so the row shows
    the outcome up front and a repeat never repeats the lookups (#123)."""
    row = await session.get(Setting, BOARD_CACHE_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


async def _put(session: AsyncSession, key: str, value: dict) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


def fresh(entry: dict | None) -> bool:
    """Whether a cached entry still answers. A found board never expires; a not-found answer
    lasts EXPIRY_DAYS from its date, and one with no date (written before 0.7.6) is stale."""
    if not entry:
        return False
    if entry.get("found"):
        return True
    at = str(entry.get("checked_at") or "")
    return bool(at) and at > iso(_now() - timedelta(days=EXPIRY_DAYS))


async def record_board_lookup(
    session: AsyncSession, name: str, found: bool, *, platform: str | None = None,
    jobs: int = 0, kind: str | None = None, config: dict | None = None,
    company: str | None = None,
) -> None:
    norm = normalized_name(name)
    if not norm:
        return
    value = await board_cache(session)
    entry: dict = {"found": found, "checked_at": iso(_now())}
    if found:
        entry.update(platform=platform, jobs=int(jobs or 0), kind=kind, config=config or {},
                     company=company or name)
    value[norm] = entry
    await _put(session, BOARD_CACHE_KEY, value)
    await clear_error(session, name)


async def check_errors(session: AsyncSession) -> dict[str, str]:
    """{normalized name: when the last check could not get an answer}. Not a cache of "not
    found": it only keeps the row saying "Could not check" and spaces the automatic retries."""
    row = await session.get(Setting, ERRORS_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return {str(k): str(v) for k, v in value.items()}


async def record_check_error(session: AsyncSession, name: str) -> None:
    norm = normalized_name(name)
    if norm:
        value = await check_errors(session)
        value[norm] = iso(_now())
        await _put(session, ERRORS_KEY, value)


async def clear_error(session: AsyncSession, name: str) -> None:
    value = await check_errors(session)
    if value.pop(normalized_name(name), None) is not None:
        await _put(session, ERRORS_KEY, value)


async def dismiss(session: AsyncSession, track: str, name: str) -> bool:
    value = await _dismissed(session)
    norm = normalized_name(name)
    if not norm or norm in value.setdefault(track, []):
        return False
    value[track].append(norm)
    await _save(session, value)
    return True


async def undismiss(session: AsyncSession, track: str, name: str) -> None:
    value = await _dismissed(session)
    norm = normalized_name(name)
    value[track] = [x for x in value.get(track, []) if x != norm]
    await _save(session, value)


_PAREN = re.compile(r"\([^()]*\)?")
_LEGAL = re.compile(
    r"\b(inc|incorporated|llc|llp|lp|ltd|limited|corp|corporation|co|company|gmbh|plc|ag|sa|bv|"
    r"pty|holdings)\b", re.IGNORECASE)


def coverage_key(name: str | None) -> str:
    """A company name as compared with the names of sources: parenthetical text and legal
    suffixes (Inc, LLC, Ltd, Corp, Co, GmbH and similar) dropped, so a company a source already
    covers under a variant name is not suggested (#123 finding 4)."""
    plain = _LEGAL.sub(" ", _PAREN.sub(" ", name or ""))
    return normalized_name(plain)


def _covered(norm: str, covered: list[str]) -> bool:
    return any(norm and (norm in c or c in norm) for c in covered if c)


async def suggestions(
    session: AsyncSession, track: str, source_names: list[str]
) -> list[dict]:
    """[{"name", "reason"}], at most MAX_SHOWN, newest pursuit first. A company already covered
    by a source (its name appears in a source's name) or dismissed is left out."""
    covered = [coverage_key(n) for n in source_names]
    gone = set((await _dismissed(session)).get(track, []))
    rows = (
        await session.execute(
            select(Org.name, Pursuit.status)
            .join(Opportunity, Opportunity.org_id == Org.id)
            .join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
            .where(Pursuit.track == track)
            .order_by(Pursuit.id.desc())
        )
    ).all()
    out: list[dict] = []
    seen: set[str] = set()
    for name, status in rows:
        norm = normalized_name(name)
        if (not norm or norm in seen or norm in gone or str(status) in _CLOSED
                or _covered(coverage_key(name), covered)):
            continue
        seen.add(norm)
        what = "shortlisted a role" if status == "shortlisted" else "an application"
        out.append({
            "name": name,
            "reason": (f"You {what} here" if status == "shortlisted"
                       else f"You have {what} here"),
        })
        if len(out) >= MAX_SHOWN:
            break
    return out


async def _live_checks(session: AsyncSession) -> set[str]:
    rows = (
        await session.execute(
            select(Task.payload).where(Task.type == TASK_TYPE, Task.status.in_(_LIVE))
        )
    ).scalars().all()
    return {normalized_name((p or {}).get("name")) for p in rows}


def _state(norm: str, entry: dict | None, errors: dict[str, str], live: set[str]) -> str:
    """found, none, error or checking. A fresh answer wins; a live task is checking; a recent
    failed check shows "Could not check"; anything else is waiting for its check."""
    if fresh(entry):
        return "found" if entry and entry.get("found") else "none"
    if norm in live:
        return "checking"
    failed = errors.get(norm)
    if failed and failed > iso(_now() - timedelta(hours=ERROR_RETRY_HOURS)):
        return "error"
    return "checking"


async def suggestions_with_boards(
    session: AsyncSession, track: str, source_names: list[str], *, queue_checks: bool = False
) -> list[dict]:
    """suggestions() with the board state on each row: found (Add works), none (add by hand),
    checking (a background check is queued or running) or error (it could not get an answer).
    With `queue_checks` (a page view or a row poll, never the answer to a POST) the rows whose
    check is due get their task queued here: a task row only, never a request.
    Consulting rows carry no board (they watch filings, not a board)."""
    out = await suggestions(session, track, source_names)
    if track == "consulting":
        for sg in out:
            sg["board"] = None
            sg["state"] = "plain"
        return out
    boards = await board_cache(session)
    errors = await check_errors(session)
    live = await _live_checks(session)
    due: list[str] = []
    for sg in out:
        norm = normalized_name(sg["name"])
        entry = boards.get(norm)
        sg["state"] = _state(norm, entry, errors, live)
        sg["board"] = entry if sg["state"] in ("found", "none") else None
        if sg["state"] == "checking" and norm not in live:
            due.append(sg["name"])
    if queue_checks:
        for name in due:
            await enqueue_check(session, name, track)
        if due:
            await session.commit()
    return out


async def enqueue_check(
    session: AsyncSession, name: str, track: str = "employment", *, force: bool = False
) -> bool:
    """Queue one background check for a company unless one is already queued or running.
    `force` (Check again, Try again) forgets the remembered answer first."""
    norm = normalized_name(name)
    if not norm:
        return False
    if norm in await _live_checks(session):
        return False
    if force:
        value = await board_cache(session)
        if value.pop(norm, None) is not None:
            await _put(session, BOARD_CACHE_KEY, value)
        await clear_error(session, name)
    session.add(Task(type=TASK_TYPE, payload={"name": name, "track": track}, status="pending",
                     priority=0, attempts=0, max_attempts=3))
    return True


# ------------------------------------------------------------------------- the check itself
async def http_client():
    """The shared rate-limited client (descriptive User-Agent, robots.txt), one request per
    probe so MAX_PROBES is the ceiling."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.ingest.http import HttpClient
    from kestrel.services.setting_overrides import load_settings_with_db

    async with get_sessionmaker()() as s:
        settings = await load_settings_with_db(s)
    ing = settings.ingest
    return HttpClient(
        user_agent=ing.user_agent,
        default_rate_per_sec=ing.default_rate_per_sec,
        timeout=min(ing.request_timeout_s, 20.0),
        respect_robots=ing.respect_robots,
        max_attempts=1,
    )


async def board_from_pursuit_links(session: AsyncSession, name: str, track: str):
    """The board the pursued role's link, or one of its duplicate copies' links, points at, or
    None. Reads the database only: no request is made."""
    from kestrel.ingest.adapters import get_adapter
    from kestrel.ingest.board_discovery import board_from_url

    norm = normalized_name(name)
    rows = (
        await session.execute(
            select(Org.name, Opportunity.url, Opportunity.dedup_group_id)
            .join(Opportunity, Opportunity.org_id == Org.id)
            .join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
            .where(Pursuit.track == track)
            .order_by(Pursuit.id.desc())
        )
    ).all()
    mine = [(u, g) for n, u, g in rows if normalized_name(n) == norm]
    urls = [u for u, _g in mine if u]
    groups = {g for _u, g in mine if g}
    if groups:
        urls += [
            u for (u,) in (
                await session.execute(
                    select(Opportunity.url).where(Opportunity.dedup_group_id.in_(groups))
                )
            ) if u
        ]
    for url in urls:
        ref = board_from_url(url)
        if ref is not None and get_adapter(ref.kind) is not None:
            return ref
    return None


async def _pending_ids(s: AsyncSession, name: str) -> list[int]:
    return list((await s.scalars(
        select(Task.id).where(Task.type == TASK_TYPE, Task.status == "pending",
                              Task.payload["name"].as_string() == name))).all())


async def _retire_pending(s: AsyncSession, name: str) -> int:
    """Mark the queued copies of this check done. The write is conditional on the row still
    being pending: a worker that leased one between the read and the write keeps it, and that
    is logged, not overwritten."""
    ids = await _pending_ids(s, name)
    if not ids:
        return 0
    res = await s.execute(
        update(Task)
        .where(Task.id.in_(ids), Task.status == "pending")
        .values(status="done", updated_at=utcnow_iso())
    )
    done = cast(CursorResult, res).rowcount or 0
    if done < len(ids):
        log.info("suggestion_check_copy_already_leased", skipped=len(ids) - done)
    return done


async def check_company(name: str, track: str = "employment", *, http=None) -> str:
    """Decide whether a suggested company can be added: "found", "none" or "error". Link first
    (no request), then the name lookup. Only a definite answer is stored; "error" stores only the
    failed-check mark. Never raises for a network problem."""
    import httpx

    from kestrel.db.engine import get_sessionmaker
    from kestrel.ingest.board_lookup import find_company_board, name_variants

    sm = get_sessionmaker()
    async with sm() as s:
        ref = await board_from_pursuit_links(s, name, track)
        if ref is not None:
            label = (name_variants(name) or [name])[0]
            await record_board_lookup(
                s, name, found=True, platform=PLATFORM_LABELS.get(ref.kind, ref.kind.title()),
                jobs=0, kind=ref.kind, config=ref.config, company=label)
            await s.commit()
            return "found"
    try:
        if http is not None:
            result = await find_company_board(name, http)
        else:
            async with await http_client() as client:
                result = await find_company_board(name, client)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        log.info("source_suggestion_lookup_failed", error=type(exc).__name__)
        result = None
    async with sm() as s:
        if result is not None and result.match is not None:
            m = result.match
            await record_board_lookup(
                s, name, found=True, platform=m.platform, jobs=m.jobs or 0, kind=m.kind,
                config=m.config, company=result.company)
            outcome = "found"
        elif result is not None and not result.inconclusive:
            await record_board_lookup(s, name, found=False)
            outcome = "none"
        else:
            # No definite answer (network down, timeout, 429, 5xx): remember nothing but the
            # failure, so the next try asks again instead of "not found" for 30 days.
            log.info("source_suggestion_lookup_inconclusive")
            await record_check_error(s, name)
            outcome = "error"
        await s.commit()
    # This question is answered: a copy still waiting in the queue would only ask it again.
    async with sm() as s:
        await _retire_pending(s, name)
        await s.commit()
    return outcome


async def drain_checks(
    sm: async_sessionmaker[AsyncSession], *, stop=None, cap: int = MAX_PER_PASS, http=None
) -> int:
    """Run queued suggestion checks, at most `cap` companies in this pass, one at a time. A task
    cut short by a quit keeps its row and is leased again after the restart (hard rule 5)."""
    from kestrel.interview.story_task import _renew
    from kestrel.queue.queue import REASON_STOPPED, TaskQueue

    queue = TaskQueue(sm)
    wid = "pipeline-suggestions"
    done = 0
    while done < cap and (stop is None or not stop.is_set()):
        task = await queue.lease(wid, types=[TASK_TYPE], lease_seconds=LEASE_S)
        if task is None:
            break
        payload = task.payload or {}
        beat = asyncio.create_task(_renew(queue, task.id, wid))
        try:
            await check_company(
                str(payload.get("name") or ""), str(payload.get("track") or "employment"),
                http=http)
        except asyncio.CancelledError:
            beat.cancel()
            await queue.defer(
                task.id, "Stopped; resumes at the next start", worker_id=wid,
                reason=REASON_STOPPED,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - one company never takes the cycle with it
            log.warning("suggestion_check_failed", task=task.id, error_type=type(exc).__name__)
            await queue.fail(task.id, "The check could not finish.", worker_id=wid)
        else:
            await queue.complete(task.id, worker_id=wid)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        done += 1
    return done
