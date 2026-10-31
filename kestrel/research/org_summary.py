"""The sourced company summary (P16, D-LLM-9, SPEC-MODULES 2.1 Company summary).

Three to five sentences at the top of the company page, written ONLY from public excerpts Kestrel
already fetched (the P5 sources). It does not browse, does not use the model's memory of the
company and never describes the operator.

* Fewer than two excerpts: **no model call**. The record is stored `insufficient` with the source
  count and the page shows the not-enough-information state.
* The model selects, it never invents (hard rule 2): the per-call schema makes `source_indexes` an
  enum of the supplied excerpt indexes, and the app checks every index again on any backend. An
  unresolved index is a validation failure, never silently dropped.
* Every figure (digit sequence) in a sentence or tag must appear in an excerpt that sentence cites
  (D-LLM-9; thousands separators and a trailing "+" ignored). The check is part of the per-call
  pydantic model, so a failure reaches the guard as an ordinary validation error: one repair call
  that quotes it, then a quarantine row (`tier` 16, raw reply kept). The previous good summary
  stays on the page. The task never crashes (hard rule 3).
* It runs as a queued `org_summary` task (hard rule 5): resumable, lease renewed while it runs. The
  Refresh summary button calls `refresh_summary`. When Tier 2 is a hosted model no automatic pass
  runs; the click is the consent (SPEC.md 6.4).

Storage. One row per organization in the `org_summary` table (SPEC-MODULES 2.1, migration
0023), behind `load_record` and `save_record`. The time of the latest attempt lives in `setting`
under `org_summary_attempt.<org_id>`."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, Field, create_model, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Org, OrgSummary, Setting, Task
from kestrel.llm.schemas import (
    MAX_SUMMARY_SENTENCES,
    MAX_SUMMARY_TAGS,
    P16OrgSummary,
    P16Sentence,
    P16Tag,
    _cap,
    _dedupe_cap,
    _max_items,
)

log = structlog.get_logger()

TASK_TYPE = "org_summary"
PROMPT = "org_summary/v1"
QUARANTINE_TIER = 16
MAX_ATTEMPTS = 3
MIN_SOURCES = 2
REFRESH_DAYS = 7
KINDS = ("website", "careers", "ats_board", "news", "filing", "search", "pasted")
# The excerpts must fit the tier's num_ctx (SPEC.md 8.2): a per-excerpt cap, and a total budget
# the per-excerpt cap shrinks to when there are many.
PER_EXCERPT_CHARS = 2500
TOTAL_EXCERPT_CHARS = 16000
MAX_EXCERPTS = 8
_LIVE = ("pending", "leased")
_ATTEMPT_KEY = "org_summary_attempt.{org_id}"
_HOLD_KEY = "org_summary_hold.{org_id}"
MAX_WORDS = 30
MIN_SENTENCES = 3
_CHECK_EVERY_S = 600.0
_WEEKLY_BATCH = 10
_next_check = 0.0

INSUFFICIENT_TEXT = (
    "Not enough public information to write a summary yet. Kestrel found {n} sources."
)


# ================================================================================== excerpts
@dataclass
class SummaryExcerpt:
    index: int
    kind: str
    url: str | None
    title: str
    fetched_at: str
    text: str

    def source(self) -> dict[str, Any]:
        """The stored form the page's numbered markers resolve to."""
        return {
            "index": self.index,
            "url": self.url,
            "title": self.title,
            "kind": self.kind,
            "fetched_at": self.fetched_at,
        }


_KIND_FROM_ACQUIRED = {
    "company_page": "website",
    "ats": "ats_board",
    "edgar": "filing",
    "searx": "search",
    "operator_paste": "pasted",
}


def from_excerpt_set(excerpt_set: Any, *, fetched_at: str | None = None) -> list[SummaryExcerpt]:
    """The P5 acquisition result as P16 excerpts. No new fetching: this only relabels what
    `acquire_excerpts` returned (a careers page is a company page whose address says so)."""
    seen = fetched_at or utcnow_iso()
    out: list[SummaryExcerpt] = []
    for e in excerpt_set.excerpts:
        kind = _KIND_FROM_ACQUIRED.get(e.kind, "website")
        if kind == "website" and e.url and "career" in e.url.lower():
            kind = "careers"
        title = re.sub(r"^https?://(www\.)?", "", e.url or "").rstrip("/") or kind
        out.append(
            SummaryExcerpt(
                index=len(out) + 1,
                kind=kind,
                url=e.url,
                title=title[:120],
                fetched_at=seen,
                text=e.text,
            )
        )
    return out


def fit_excerpts(excerpts: list[SummaryExcerpt]) -> list[SummaryExcerpt]:
    """Keep at most MAX_EXCERPTS, each trimmed so the set fits the tier's context budget. Indexes
    are kept as supplied."""
    kept = excerpts[:MAX_EXCERPTS]
    if not kept:
        return []
    each = max(400, min(PER_EXCERPT_CHARS, TOTAL_EXCERPT_CHARS // len(kept)))
    return [replace(e, text=(e.text or "").strip()[:each]) for e in kept]


def render_excerpts(excerpts: list[SummaryExcerpt]) -> str:
    return "\n\n".join(
        f"[{e.index}] ({e.kind}) {e.title} - {e.url or 'no address'} - seen {e.fetched_at[:10]}"
        f"\n{e.text}"
        for e in excerpts
    )


# ============================================================================== figure check
_FIGURE_RE = re.compile(r"\d+(?:[.,]\d+)*")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _norm_digits(text: str) -> str:
    return _THOUSANDS_RE.sub("", text)


def figures(text: str) -> list[str]:
    """The digit sequences in `text`, thousands separators removed. A trailing "+" is never part
    of a match, so "500+" is the figure 500."""
    return [_norm_digits(m).rstrip(".,") for m in _FIGURE_RE.findall(text)]


def figure_in(figure: str, source_text: str) -> bool:
    """Whether `figure` appears in `source_text` as a whole number (400 is not found in 3,400.5)."""
    pattern = r"(?<![\d.])" + re.escape(figure) + r"(?!\d|\.\d)"
    return re.search(pattern, _norm_digits(source_text)) is not None


def unsupported_figures(text: str, cited_texts: list[str]) -> list[str]:
    found = dict.fromkeys(figures(text))
    return [f for f in found if not any(figure_in(f, t) for t in cited_texts)]


# ============================================================================ per-call model
def summary_model_for(excerpts: list[SummaryExcerpt]) -> type[BaseModel]:
    """P16 with every `source_indexes` an enum of the supplied indexes and every figure checked
    against the excerpts its sentence or tag cites. A sentence with no index is a failure."""
    by_index = {e.index: e.text for e in excerpts}
    index_type: Any = Literal[tuple(by_index)]  # type: ignore[valid-type]

    def check(label: str):
        def validator(self):
            cited = [by_index[i] for i in self.source_indexes if i in by_index]
            bad = unsupported_figures(self.text, cited)
            if bad:
                shown = ", ".join(bad)
                raise ValueError(
                    f"{label} {self.text!r} states {shown}, which no excerpt it cites "
                    "contains. Copy figures exactly from a cited excerpt, or leave them out."
                )
            return self

        return model_validator(mode="after")(validator)

    sentence = create_model(
        "P16SentenceForOrg",
        __base__=P16Sentence,
        __validators__={"figures": check("The sentence")},
        source_indexes=(
            Annotated[list[index_type], _dedupe_cap(4), _max_items(4), Field(min_length=1)],
            ...,
        ),
    )
    tag = create_model(
        "P16TagForOrg",
        __base__=P16Tag,
        __validators__={"figures": check("The tag")},
        source_indexes=(
            Annotated[list[index_type], _dedupe_cap(3), _max_items(3), Field(min_length=1)],
            ...,
        ),
    )
    return create_model(
        "P16OrgSummaryForOrg",
        __base__=P16OrgSummary,
        sentences=(
            Annotated[
                list[sentence], _cap(MAX_SUMMARY_SENTENCES), _max_items(MAX_SUMMARY_SENTENCES)
            ],
            ...,
        ),
        tags=(Annotated[list[tag], _cap(MAX_SUMMARY_TAGS), _max_items(MAX_SUMMARY_TAGS)], ...),
    )


def summary_schema(model_cls: type[BaseModel]) -> dict:
    from kestrel.docs.assess import assess_schema

    return assess_schema(model_cls)


def display_text(text: str, *, max_words: int | None = None) -> str:
    """Em dashes become a comma (display only; PROMPTS.md P16 validation). A sentence over the
    word limit is cut, never rejected (the schema-subset rule: bounds are advisory)."""
    out = re.sub(r"\s*\u2014\s*", ", ", text).strip()
    if max_words is not None:
        words = out.split()
        if len(words) > max_words:
            out = " ".join(words[:max_words]).rstrip(",;:.") + "\u2026"
    return out


# ================================================================================== storage
_RECORD_FIELDS = (
    "sentences",
    "tags",
    "sources",
    "model",
    "prompt_version",
    "generated_at",
    "insufficient_sources",
    "source_count",
    "status",
)


async def _summary_row(session: AsyncSession, org_id: int) -> OrgSummary | None:
    return (
        await session.execute(select(OrgSummary).where(OrgSummary.org_id == org_id))
    ).scalar_one_or_none()


async def load_record(session: AsyncSession, org_id: int) -> dict[str, Any] | None:
    row = await _summary_row(session, org_id)
    if row is None:
        return None
    return {name: getattr(row, name) for name in _RECORD_FIELDS}


async def save_record(session: AsyncSession, org_id: int, record: dict[str, Any]) -> None:
    """Stage the record in the org_summary table (the caller commits)."""
    row = await _summary_row(session, org_id)
    if row is None:
        row = OrgSummary(org_id=org_id)
        session.add(row)
    for name in _RECORD_FIELDS:
        if name in record:
            setattr(row, name, record[name])


async def note_attempt(session: AsyncSession, org_id: int, stamp: str) -> None:
    """Stage the time of the latest attempt, whatever came of it. Automatic triggers wait out the
    refresh window from this, so a quarantined or dead-lettered run is not re-queued forever; the
    operator's Refresh ignores it. The caller commits."""
    key = _ATTEMPT_KEY.format(org_id=org_id)
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value={"at": stamp}))
    else:
        row.value = {"at": stamp}
        row.updated_at = utcnow_iso()


def _model_name(client: Any) -> str:
    try:
        return str(client.settings.llm.tiers["tier2"].model)
    except (AttributeError, KeyError):
        return ""


# ================================================================================ generating
@dataclass
class Outcome:
    status: str  # ok | insufficient | quarantined | skipped | error
    error: str | None = None
    out_of_credit: bool = False
    model_called: bool = False


async def summarize(
    session: AsyncSession,
    client: Any,
    org: Org,
    excerpts: list[SummaryExcerpt],
    *,
    now: str | None = None,
) -> Outcome:
    """Write (or refuse to write) the summary for `org` from `excerpts`. Commits. A reply that
    fails validation is repaired once, then quarantined; the previous good summary is kept."""
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline._shared import make_quarantine_hook, run_prompt_hosted

    stamp = now or utcnow_iso()
    excerpts = fit_excerpts([e for e in excerpts if (e.text or "").strip()])
    previous = await load_record(session, org.id)
    base = {
        "sources": [e.source() for e in excerpts],
        "source_count": len(excerpts),
        "prompt_version": PROMPT,
        "generated_at": stamp,
    }

    if len(excerpts) < MIN_SOURCES:
        # No model call: nothing to summarize, and the page says so plainly.
        await save_record(
            session,
            org.id,
            {
                **base,
                "sentences": [],
                "tags": [],
                "model": None,
                "insufficient_sources": True,
                "status": "insufficient",
            },
        )
        await note_attempt(session, org.id, stamp)
        await session.commit()
        return Outcome("insufficient")

    spec = load_prompt(PROMPT)
    model_cls = summary_model_for(excerpts)
    user = spec.user.replace("{org_name}", org.name)
    user = user.replace("{EXCERPTS}", render_excerpts(excerpts))
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    result = await run_prompt_hosted(
        session,
        client,
        replace(spec, model_cls=model_cls),
        messages,
        quarantine_tier=QUARANTINE_TIER,
        on_quarantine=make_quarantine_hook(session, tier=QUARANTINE_TIER, opportunity_id=None),
        schema=summary_schema(model_cls),
    )
    if result.quarantined:
        # The quarantine row (full prompt, raw reply) is staged by the hook. A previous good
        # summary stays as it was (the attempt time is kept by note_attempt); otherwise the page
        # shows the not-enough state with status quarantined.
        if previous is None or previous.get("status") != "ok":
            await save_record(
                session,
                org.id,
                {
                    **base,
                    "sentences": [],
                    "tags": [],
                    "model": _model_name(client),
                    "insufficient_sources": False,
                    "status": "quarantined",
                },
            )
        await note_attempt(session, org.id, stamp)
        await session.commit()
        log.warning("org_summary_quarantined", org=org.id, error=result.error)
        return Outcome("quarantined", error=result.error, model_called=True)
    if not result.ok or result.value is None:
        await session.rollback()
        return Outcome(
            "error",
            error=result.error or "no reply",
            out_of_credit=result.out_of_credit,
            model_called=True,
        )

    value = result.value
    sentences = [
        {
            "text": display_text(s.text, max_words=MAX_WORDS),
            "source_indexes": list(s.source_indexes),
        }
        for s in value.sentences[:MAX_SUMMARY_SENTENCES]
        if s.text.strip()
    ]
    if 0 < len(sentences) < MIN_SENTENCES:
        # PROMPTS.md P16 only makes fewer than two sentences "insufficient"; two sourced
        # sentences are accepted and noted.
        log.info("org_summary_short", org=org.id, sentences=len(sentences))
    if value.insufficient or not sentences:
        await save_record(
            session,
            org.id,
            {
                **base,
                "sentences": [],
                "tags": [],
                "model": _model_name(client),
                "insufficient_sources": True,
                "status": "insufficient",
            },
        )
        await note_attempt(session, org.id, stamp)
        await session.commit()
        return Outcome("insufficient", model_called=True)
    tags = [
        {"key": t.key, "text": display_text(t.text), "source_indexes": list(t.source_indexes)}
        for t in value.tags
        if t.text.strip()
    ]
    await save_record(
        session,
        org.id,
        {
            **base,
            "sentences": sentences,
            "tags": tags,
            "model": _model_name(client),
            "insufficient_sources": False,
            "status": "ok",
        },
    )
    await note_attempt(session, org.id, stamp)
    await session.commit()
    return Outcome("ok", model_called=True)


# =================================================================================== queueing
async def _live_for(session: AsyncSession, org_id: int) -> bool:
    n = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.type == TASK_TYPE,
                Task.status.in_(_LIVE),
                func.json_extract(Task.payload, "$.org_id") == org_id,
            )
        )
    ).scalar_one()
    return bool(n)


async def _runs_online(session: AsyncSession) -> bool:
    from kestrel.services.title_suggest import _runs_online as online

    return await online(session, PROMPT)


async def _configured_dest(session: AsyncSession) -> str:
    """The destination (backend, base URL, model) the summary tier points at now."""
    from kestrel.llm.prompts import load_prompt
    from kestrel.services.online_consent import destination_id
    from kestrel.services.setting_overrides import load_settings_with_db

    return destination_id(await load_settings_with_db(session), load_prompt(PROMPT).tier)


async def _configured_model(session: AsyncSession) -> str:
    """The model the summary tier is configured with now."""
    from kestrel.llm.prompts import load_prompt
    from kestrel.services.online_consent import configured_model
    from kestrel.services.setting_overrides import load_settings_with_db

    return configured_model(await load_settings_with_db(session), load_prompt(PROMPT).tier)


async def request_org_summary(
    session: AsyncSession, org_id: int, reason: str, *, automatic: bool = False
) -> bool:
    """Queue one summary task for the company unless one is already waiting or running. Flushes,
    never commits (the caller commits). An automatic request (page open, follow, weekly) is
    skipped when Tier 2 is an online model: only the operator's Refresh click is consent."""
    if await session.get(Org, org_id) is None or await _live_for(session, org_id):
        return False
    online = await _runs_online(session)
    if automatic and online:
        return False
    await clear_hold(session, org_id)
    session.add(
        Task(
            type=TASK_TYPE,
            payload={
                "org_id": org_id,
                "reason": reason,
                "automatic": automatic,
                # The operator's Refresh click, made while Tier 2 was online, is the consent for
                # the online send. A click made on a local model is not (checked again at drain).
                "confirmed_online": online and not automatic,
                # The confirmation is for THIS model (the one the dialog showed). A drain that
                # finds another one configured refuses to send.
                "confirmed_model": await _configured_model(session) if online else None,
                "confirmed_dest": await _configured_dest(session) if online else None,
                "online_approval": {
                    "tier2": [await _configured_dest(session)]
                } if online else {},
                "requested_at": utcnow_iso(),
            },
            max_attempts=MAX_ATTEMPTS,
        )
    )
    await session.flush()
    return True


async def hold_for_confirmation(session: AsyncSession, org_id: int) -> None:
    """Remember that a manual Refresh was held because Tier 2 went online after the click. The
    caller commits."""
    key = _HOLD_KEY.format(org_id=org_id)
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value={"at": utcnow_iso()}))
    else:
        row.value = {"at": utcnow_iso()}
        row.updated_at = utcnow_iso()


async def clear_hold(session: AsyncSession, org_id: int) -> None:
    row = await session.get(Setting, _HOLD_KEY.format(org_id=org_id))
    if row is not None:
        await session.delete(row)


async def is_held(session: AsyncSession, org_id: int) -> bool:
    """A Refresh is waiting for the operator to confirm the online send."""
    return await session.get(Setting, _HOLD_KEY.format(org_id=org_id)) is not None


async def refresh_summary(session: AsyncSession, org_id: int) -> bool:
    """The Refresh summary entry point (a button click, so consent for an online model)."""
    return await request_org_summary(session, org_id, "refresh")


async def is_queued(session: AsyncSession, org_id: int) -> bool:
    """A summary task for the company is waiting or running."""
    return await _live_for(session, org_id)


async def runs_online(session: AsyncSession) -> bool:
    """Tier 2, which writes the summary, would go to an online model right now."""
    return await _runs_online(session)


def _parse(stamp: object) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def needs_summary(session: AsyncSession, org_id: int) -> bool:
    """True when the company has no summary, or the last attempt (a success, or a try that was
    quarantined, insufficient or dead-lettered) is over a week old."""
    rec = await load_record(session, org_id)
    attempt = await session.get(Setting, _ATTEMPT_KEY.format(org_id=org_id))
    stamps = [
        _parse(x)
        for x in (
            (rec or {}).get("generated_at"),
            attempt.value.get("at")
            if attempt is not None and isinstance(attempt.value, dict)
            else None,
        )
        if x
    ]
    latest = max((t for t in stamps if t is not None), default=None)
    if latest is None:
        return True
    return datetime.now(UTC) - latest >= timedelta(days=REFRESH_DAYS)


async def request_if_needed(session: AsyncSession, org_id: int, reason: str) -> bool:
    """Queue an automatic pass when the page is opened or the company followed."""
    if not await needs_summary(session, org_id):
        return False
    return await request_org_summary(session, org_id, reason, automatic=True)


async def request_weekly(sm: async_sessionmaker[AsyncSession], *, force: bool = False) -> int:
    """The worker's weekly pass: queue an automatic refresh for every company that already has a
    summary (or an attempt) older than a week. At most every ten minutes and a handful at a time;
    never raises into the cycle. An online Tier 2 queues nothing (the click is the consent)."""
    global _next_check
    now = time.monotonic()
    if not force and now < _next_check:
        return 0
    _next_check = now + _CHECK_EVERY_S
    queued = 0
    try:
        async with sm() as s:
            org_ids = (await s.execute(select(OrgSummary.org_id))).scalars().all()
            for org_id in org_ids:
                if queued >= _WEEKLY_BATCH:
                    break
                if await needs_summary(s, org_id) and await request_org_summary(
                    s, org_id, "weekly", automatic=True
                ):
                    queued += 1
            await s.commit()
    except Exception as exc:  # noqa: BLE001 - the cadence is a convenience; try next check
        log.warning("org_summary_weekly_failed", error_type=type(exc).__name__, error=str(exc))
    return queued


# ==================================================================================== draining
Acquire = Callable[[AsyncSession, Org, Settings], Awaitable[list[SummaryExcerpt]]]


async def acquire_public_excerpts(
    session: AsyncSession, org: Org, settings: Settings
) -> list[SummaryExcerpt]:
    """The same public sources as P5 (company pages, the ATS board, EDGAR, optional SearXNG),
    politely and with robots.txt respected. Operator-pasted text is not available to a queued
    task and is left out. No new fetch behavior."""
    from kestrel.ingest.http import HttpClient
    from kestrel.research.excerpts import acquire_excerpts

    async with HttpClient(
        user_agent=settings.ingest.user_agent,
        default_rate_per_sec=settings.ingest.default_rate_per_sec,
        timeout=settings.ingest.request_timeout_s,
        respect_robots=settings.ingest.respect_robots,
    ) as http:
        found = await acquire_excerpts(session, http, org, settings=settings)
    return from_excerpt_set(found)


async def _note_dead(sm: async_sessionmaker[AsyncSession], org_id: int) -> None:
    """A task out of attempts counts as an attempt, so automatic triggers wait a week."""
    if not org_id:
        return
    try:
        async with sm() as s:
            await note_attempt(s, org_id, utcnow_iso())
            await s.commit()
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never break the drain
        log.warning("org_summary_note_failed", error_type=type(exc).__name__, error=str(exc))


async def drain_org_summaries(
    sm: async_sessionmaker[AsyncSession],
    client: Any,
    settings: Settings,
    *,
    stop: Any = None,
    acquire: Acquire | None = None,
) -> int:
    """Run every queued summary task, one at a time, on the Tier 2 model. A transport error or
    unexpected failure goes back to the queue while attempts remain; a quarantined reply completes
    the task (the raw reply is kept; retrying would loop). Never raises into the worker cycle."""
    from kestrel.interview.story_task import LEASE_S, _renew
    from kestrel.queue.queue import TaskQueue

    get = acquire or acquire_public_excerpts
    queue = TaskQueue(sm)
    wid = "pipeline-org-summary"
    done = 0
    while stop is None or not stop.is_set():
        task = await queue.lease(wid, types=[TASK_TYPE], lease_seconds=LEASE_S)
        if task is None:
            break
        beat = asyncio.create_task(_renew(queue, task.id, wid))
        try:
            payload = task.payload or {}
            org_id = int(payload.get("org_id") or 0)
            async with sm() as s:
                org = await s.get(Org, org_id)
                from kestrel.services.online_consent import (
                    destination_id,
                    held_for_approval,
                    tier_goes_online,
                )
                from kestrel.services.setting_overrides import load_settings_with_db

                live_settings = await load_settings_with_db(s)
                live_dest = destination_id(live_settings, "tier2")
                client_dest = destination_id(getattr(client, "settings", settings), "tier2")
                online_now = await _runs_online(s) and not (
                    payload.get("confirmed_online")
                    and payload.get("confirmed_model") == await _configured_model(s)
                    and payload.get("confirmed_dest") == await _configured_dest(s)
                    and payload.get("confirmed_dest") == client_dest
                    and not held_for_approval(live_settings, ("tier2",), payload)
                )
                if online_now and payload.get("automatic"):
                    # Tier 2 became an online model after this was queued: only the operator's
                    # Refresh may send anything there, so the automatic task is dropped.
                    outcome = Outcome("skipped", error="tier 2 is online now")
                elif online_now:
                    # A manual Refresh queued on a local model: nothing is sent. The page asks
                    # the operator to Refresh again, which shows what would be sent.
                    await hold_for_confirmation(s, org_id)
                    await s.commit()
                    outcome = Outcome(
                        "skipped", error="waiting for the online send to be confirmed"
                    )
                elif org is None:
                    outcome = Outcome("skipped", error="the company no longer exists")
                else:
                    excerpts = await get(s, org, live_settings)
                    async with sm() as current:
                        latest = await load_settings_with_db(current)
                        latest_dest = destination_id(latest, "tier2")
                    online_send = (tier_goes_online(live_settings, "tier2")
                                   or tier_goes_online(latest, "tier2"))
                    if online_send and (
                        latest_dest != live_dest
                        or not latest.llm.hosted_mode.active
                        or held_for_approval(latest, ("tier2",), payload)
                    ):
                        await hold_for_confirmation(s, org_id)
                        await s.commit()
                        outcome = Outcome("skipped", error="online consent changed")
                    else:
                        from kestrel.pipeline._shared import client_for

                        approved_client = client_for(client, latest)
                        outcome = await summarize(s, approved_client, org, excerpts)
        except Exception as exc:  # noqa: BLE001 - a failed task is retried, never fatal
            beat.cancel()
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("org_summary_failed", task=task.id, error=reason)
            if await queue.fail(task.id, reason, worker_id=wid, retry=True) == "dead":
                await _note_dead(sm, int((task.payload or {}).get("org_id") or 0))
            continue
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        if outcome.status == "error" and outcome.out_of_credit:
            await queue.defer(task.id, outcome.error or "", worker_id=wid)
            break
        if outcome.status == "error":
            status = await queue.fail(task.id, outcome.error or "error", worker_id=wid, retry=True)
            if status == "dead":
                await _note_dead(sm, int((task.payload or {}).get("org_id") or 0))
            continue
        await queue.complete(task.id, worker_id=wid)
        done += 1
    return done
