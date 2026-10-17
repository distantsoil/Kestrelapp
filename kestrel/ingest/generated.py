"""Job searches generated from the operator's profile (D-SRC-2).

The operator never writes a search query. Their target titles, excluded titles and locations
are already on the Profile page; this module turns them into JobSpy sources and keeps those
sources in step with the profile. `rebuild_generated_sources` runs after every profile save
(silently, the operator's choice) and touches only rows with `origin == "generated"`: a source
the operator added or edited is `custom` and is never changed here.

Shape of what is built, per enabled track:
- employment: one search per on-site metro (titles near that metro) and one remote search
  when remote work is acceptable. Long title lists are split across several searches, because
  job boards reject very long query strings.
- consulting: one search for fractional and interim variants of the employment titles plus
  the service lines, remote or near the first metro; and the SEC EDGAR trigger watch, whose
  phrases are field-neutral events, plus the target titles only when the operator opted in on
  the Sources page (D-SRC-6, review finding 3), and whose
  mandatory User-Agent contact comes from the About you email (no email, no SEC source).

Near titles (D-TTL-2): each employment search also asks for up to VARIANTS_PER_TITLE other
common spellings of each target title ("Director of Information Technology" for "Director of
IT"): abbreviation and curated family swaps only, never a reordering (kestrel.rules.titles
.phrasings), deduplicated across titles and capped at MAX_VARIANTS in all. Variants never add
more than half again the searches the plan has without them, nor push it past MAX_GENERATED
(variant_budget): the last variants are dropped first, before any metro is. The Profile page
lists them, and each one the operator removes is stored (VARIANTS_REMOVED_KEY) and never
searched again. LinkedIn is never among the boards (D-SRC-5); the variants ride on the same
searches and the same rate limits.

Nothing here enables scraping on its own. New generated rows inherit `enabled` from the
generated rows they replace, so an operator who pressed "Start searching" keeps searching after
a profile edit, and one who never did stays off (CLAUDE.md: nothing scrapes unattended until
the operator turns it on). `enable=True` is that "Start searching" press."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Source
from kestrel.ingest.adapters.jobspy import DEFAULT_SITES

log = structlog.get_logger()

ORIGIN = "generated"
MAX_QUERY_CHARS = 220  # Indeed and Google start dropping or rejecting terms beyond this
# A ceiling on generated searches (hard rule 7, review finding 11): titles times metros grows
# fast, and every search is two boards every twelve hours. Beyond this the earliest metros and
# the remote search win, and the Sources page says some were left out.
MAX_GENERATED = 12
# The boards a generated search asks. ZipRecruiter is deliberately absent: it answers every
# JobSpy search with a 403 from Cloudflare's bot check, and JobSpy swallows that (the run shows
# zero errors and zero postings from it), so it only cost a request per search and hid the gap.
# A custom JobSpy source on Settings > Sources can still name `zip_recruiter` in its site_name
# (D-SRC-4).
# LinkedIn is not here and never will be (D-SRC-5): it does not allow automated access.
SITES = list(DEFAULT_SITES)
# Shown beside the generated searches (Sources page, onboarding review). The boards Kestrel
# searches are US-centric, and the Sources page cannot promise more than that.
US_NOTE = (
    "Kestrel's built-in job boards are US-focused; other countries work but with fewer results."
)
CONSULTING_PREFIXES = ("fractional", "interim")
# The SEC EDGAR trigger watch (D-SRC-6, amended by review finding 3). By default only
# field-neutral events that open interim and project work in any function: no operator title,
# no field. SEC sees every query beside the contact email its User-Agent rule requires, so the
# operator's target titles join the queries only when they opt in on the Sources page
# (SEC_TITLES_KEY, off by default). Never a shipped job title: the old list assumed IT.
SEC_NEUTRAL_QUERIES = [
    '"completion of acquisition"',
    '"departure of directors or certain officers"',
]
SEC_TITLE_QUERIES = 3  # at most this many target titles become their own phrase, when opted in
SEC_SOURCE_NAME = "SEC filings that may signal a need for your work"
SEC_TITLES_KEY = "sources.sec_use_titles"
# Near-title search variants (D-TTL-2).
VARIANTS_PER_TITLE = 3
MAX_VARIANTS = 9
VARIANTS_REMOVED_KEY = "sources.title_variants_removed"
SEC_TITLES_LABEL = (
    "Use my target titles in SEC filing searches. SEC sees these searches alongside the "
    "contact email it requires."
)


def sec_queries(
    titles: list[str], service_lines: list[str] | None = None, *, use_titles: bool = False
) -> list[str]:
    """The EDGAR phrases: the neutral events, and only when the operator opted in
    (`use_titles`), up to SEC_TITLE_QUERIES quoted target titles first (the service lines when
    there are no titles). Deduplicated, in order."""
    out: list[str] = []
    if use_titles:
        picked = [t for t in (str(x).strip().replace('"', "") for x in titles) if t]
        if not picked:
            picked = [
                s for s in (str(x).strip().replace('"', "") for x in service_lines or []) if s
            ]
        for t in picked[:SEC_TITLE_QUERIES]:
            q = f'"{t.lower()}"'
            if q not in out:
                out.append(q)
    for q in SEC_NEUTRAL_QUERIES:
        if q not in out:
            out.append(q)
    return out


async def sec_titles_opt_in(session: AsyncSession) -> bool:
    """Whether the operator chose to send their target titles to SEC EDGAR (off by default)."""
    from kestrel.db.models import Setting

    row = await session.get(Setting, SEC_TITLES_KEY)
    return bool(row is not None and row.value is True)


async def save_sec_titles_opt_in(session: AsyncSession, on: bool) -> None:
    """Store the choice. The caller rebuilds and commits."""
    from kestrel.db.base import utcnow_iso
    from kestrel.db.models import Setting

    row = await session.get(Setting, SEC_TITLES_KEY)
    if row is None:
        session.add(Setting(key=SEC_TITLES_KEY, value=bool(on)))
    else:
        row.value = bool(on)
        row.updated_at = utcnow_iso()
    await session.flush()


async def removed_variants(session: AsyncSession) -> list[str]:
    """The near-title variants the operator removed on Profile (D-TTL-2), as typed."""
    from kestrel.db.models import Setting

    row = await session.get(Setting, VARIANTS_REMOVED_KEY)
    value = row.value if row is not None else None
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


async def save_removed_variants(session: AsyncSession, values: list[str]) -> None:
    """Store the removed variants (deduplicated, case-insensitive, at most 200). The caller
    rebuilds the searches and commits."""
    from kestrel.db.base import utcnow_iso
    from kestrel.db.models import Setting

    out: list[str] = []
    for v in values:
        v = " ".join(str(v).split())[:200]
        if v and v.casefold() not in {o.casefold() for o in out}:
            out.append(v)
    out = out[:200]
    row = await session.get(Setting, VARIANTS_REMOVED_KEY)
    if row is None:
        session.add(Setting(key=VARIANTS_REMOVED_KEY, value=out))
    else:
        row.value = out
        row.updated_at = utcnow_iso()
    await session.flush()


@dataclass
class SourceSpec:
    """One source this module wants to exist. `name` is the identity across rebuilds."""

    track: str
    kind: str
    name: str
    config: dict
    cadence_hours: int = 12
    summary: str = ""  # one plain-English line for the onboarding review and the Sources page


@dataclass
class RebuildResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    specs: list[SourceSpec] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.removed)


# ------------------------------------------------------------------------------ query building
def _quote(term: str) -> str:
    term = term.strip().replace('"', "")
    return f'"{term}"' if " " in term else term


def _negations(anti: list[str]) -> str:
    out = []
    for a in anti:
        a = a.strip().replace('"', "")
        if a:
            out.append(f'-"{a}"' if " " in a else f"-{a}")
    return " ".join(out)


def title_groups(titles: list[str], anti: list[str], limit: int = MAX_QUERY_CHARS) -> list[str]:
    """OR-joined title queries, each with the exclusions appended, none longer than `limit`.
    A title list that does not fit one query is split across several, in order.

    The generated searches pass `anti=[]`: job boards apply `-term` to the whole posting, so
    "-Sales" drops an analyst posting that says "partner with Sales" before Tier 0 ever sees it
    (hard rule 4, review finding 3). Excluded titles are Tier 0's anti_title rule, on the title
    only."""
    neg = _negations(anti)
    titles = [t for t in (s.strip() for s in titles) if t]
    groups: list[str] = []
    current: list[str] = []
    for t in titles:
        trial = " OR ".join([*current, _quote(t)])
        if current and len(f"({trial}) {neg}".strip()) > limit:
            groups.append(" OR ".join(current))
            current = [_quote(t)]
        else:
            current.append(_quote(t))
    if current:
        groups.append(" OR ".join(current))
    return [f"({g}) {neg}".strip() if neg else (f"({g})" if " OR " in g else g) for g in groups]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def title_variants(titles: list[str], removed: Any = ()) -> list[str]:
    """The other phrasings searched beside `titles` (D-TTL-2): up to VARIANTS_PER_TITLE each,
    in title order, never a target title itself, never one the operator removed, no duplicate
    (case-insensitive), at most MAX_VARIANTS."""
    from kestrel.rules.titles import phrasings

    targets = [t for t in (" ".join(str(x).split()) for x in titles) if t]
    gone = {str(r).casefold() for r in removed or ()}
    seen = {t.casefold() for t in targets}
    out: list[str] = []
    for t in targets:
        for v in phrasings(t, VARIANTS_PER_TITLE):
            key = v.casefold()
            if key in seen or key in gone:
                continue
            seen.add(key)
            out.append(v)
            if len(out) >= MAX_VARIANTS:
                return out
    return out


def _suffix(i: int, n: int) -> str:
    return f" ({i + 1} of {n})" if n > 1 else ""


def build_specs(
    profiles: dict[str, Any], tracks: tuple[str, ...] | list[str], contact_email: str | None,
    *, sec_use_titles: bool = False, removed_variants: Any = (),
) -> list[SourceSpec]:
    """The sources the profile implies. `profiles` maps track to that track's block (a dict
    from profile.yml or the loader's namespace); a missing block yields nothing for it.
    `sec_use_titles` is the Sources page opt-in (finding 3): off, the SEC watch sends no title.
    `removed_variants` are the near-title phrasings the operator took out (D-TTL-2)."""
    return plan(profiles, tracks, contact_email, sec_use_titles=sec_use_titles,
                removed_variants=removed_variants)[0]


def plan(
    profiles: dict[str, Any], tracks: tuple[str, ...] | list[str], contact_email: str | None,
    *, sec_use_titles: bool = False, removed_variants: Any = (),
) -> tuple[list[SourceSpec], list[str], list[str]]:
    """(specs, the variants searched, the variants offered). Offered variants are dropped from
    the end until the searches fit `variant_budget` without losing a metro (D-TTL-2)."""
    emp = profiles.get("employment")
    titles = list(_get(_get(emp, "employment_terms") or {}, "target_titles") or [])
    offered = title_variants(titles, removed_variants) if "employment" in tracks else []
    used = list(offered)
    base = _build(profiles, tracks, contact_email, sec_use_titles=sec_use_titles, variants=[])
    specs = _build(profiles, tracks, contact_email, sec_use_titles=sec_use_titles,
                   variants=used)
    allowed = variant_budget(len(base))
    while used and len(specs) > allowed:
        used.pop()
        specs = _build(profiles, tracks, contact_email, sec_use_titles=sec_use_titles,
                       variants=used)
    return _cap(specs), used, offered


def variant_budget(base: int) -> int:
    """How many generated searches the plan may have WITH variants, given `base` searches
    without them (D-TTL-2): at most half again as many (rounded down), never past MAX_GENERATED.
    A base already at or over the cap gets no extra search: variants then stay only where they
    fit inside the existing queries."""
    base = max(0, int(base))
    if base >= MAX_GENERATED:
        return base
    return min(base + base // 2, MAX_GENERATED)


def _cap(specs: list[SourceSpec]) -> list[SourceSpec]:
    if len(specs) > MAX_GENERATED:
        log.warning("generated_sources_capped", wanted=len(specs), kept=MAX_GENERATED)
        remote = [s for s in specs if s.name.startswith("Remote")]
        others = [s for s in specs if not s.name.startswith("Remote")]
        specs = (remote + others)[:MAX_GENERATED]
    return specs


def _build(
    profiles: dict[str, Any], tracks: tuple[str, ...] | list[str], contact_email: str | None,
    *, sec_use_titles: bool, variants: list[str],
) -> list[SourceSpec]:
    specs: list[SourceSpec] = []
    emp = profiles.get("employment")
    terms = _get(emp, "employment_terms") or {}
    titles = list(_get(terms, "target_titles") or [])

    if "employment" in tracks and emp is not None and titles:
        geo = _get(emp, "geo_rules") or {}
        queries = title_groups([*titles, *variants], [])
        for metro in _get(geo, "onsite_metros") or []:
            mname = str(_get(metro, "name") or "").strip()
            if not mname:
                continue
            # Never guess the state for a metro (review finding 10): a Seattle metro beside
            # onsite_states [OH, WA] is "Seattle", not "Seattle, OH".
            states = list(_get(geo, "onsite_states") or [])
            state = str(_get(metro, "state") or (states[0] if len(states) == 1 else "")).strip()
            where = f"{mname}, {state}" if state and state not in mname else mname
            for i, q in enumerate(queries):
                specs.append(SourceSpec(
                    track="employment", kind="jobspy",
                    name=f"Jobs near {mname}{_suffix(i, len(queries))}",
                    config={
                        "site_name": SITES, "search_term": q,
                        "google_search_term": f"{titles[0]} jobs near {where} since last week",
                        "location": where, "distance": 50, "results_wanted": 100,
                        "hours_old": 168, "country_indeed": "USA",
                        "description_format": "markdown", "enforce_annual_salary": True,
                    },
                    summary=f"{', '.join(titles[:3])}{'…' if len(titles) > 3 else ''} "
                            f"within 50 miles of {where}, posted in the last week",
                ))
        if _get(geo, "remote_ok"):
            countries = list(_get(geo, "remote_country_allowlist") or ["US"])
            for i, q in enumerate(queries):
                specs.append(SourceSpec(
                    track="employment", kind="jobspy",
                    name=f"Remote jobs{_suffix(i, len(queries))}",
                    config={
                        # Indeed allows hours_old OR is_remote, not both (CLAUDE.md trap).
                        "site_name": SITES, "search_term": q,
                        "google_search_term": f"remote {titles[0]} jobs",
                        "location": "United States" if "US" in countries else countries[0],
                        "is_remote": True, "results_wanted": 100, "country_indeed": "USA",
                        "description_format": "markdown", "enforce_annual_salary": True,
                    },
                    summary=f"Remote {', '.join(titles[:3])}{'…' if len(titles) > 3 else ''} "
                            f"in {', '.join(countries)}",
                ))

    con = profiles.get("consulting")
    if "consulting" in tracks and con is not None:
        cterms = _get(con, "consulting_terms") or {}
        lines = [s for s in (_get(cterms, "service_lines") or []) if str(s).strip()]
        variants = [f"{p} {t}" for t in titles[:3] for p in CONSULTING_PREFIXES]
        variants += [f"{s} consultant" for s in lines[:3]]
        if not variants:
            variants = ["fractional consultant", "interim consultant"]
        geo = _get(con, "geo_rules") or _get(emp, "geo_rules") or {}
        metros = [str(_get(m, "name")) for m in (_get(geo, "onsite_metros") or [])]
        where = metros[0] if metros else "United States"
        cqueries = title_groups(variants, [])
        for i, q in enumerate(cqueries):
            specs.append(SourceSpec(
                track="consulting", kind="jobspy",
                name=f"Fractional and interim roles{_suffix(i, len(cqueries))}",
                config={
                    "site_name": SITES, "search_term": q,
                    "location": where, "results_wanted": 100, "job_type": "contract",
                    "description_format": "markdown",
                },
                cadence_hours=24,
                summary=f"Fractional, interim and consulting roles near {where}",
            ))
        if contact_email and "@" in contact_email:
            specs.append(SourceSpec(
                track="consulting", kind="sec_edgar",
                name=SEC_SOURCE_NAME,
                config={
                    "user_agent": f"Kestrel/1.0 ({contact_email.strip()})",
                    "forms": ["8-K"],
                    "queries": sec_queries(titles, lines, use_titles=sec_use_titles),
                },
                cadence_hours=24,
                summary=(
                    "New 8-K filings about acquisitions, officer departures and the roles "
                    "you target" if sec_use_titles
                    else "New 8-K filings about acquisitions and officer departures"
                ),
            ))
    return specs


# ---------------------------------------------------------------------------------- persistence
def _profile_inputs() -> tuple[dict[str, Any], str | None]:
    """The operator's own blocks (never the shipped example) and the About you email."""
    from kestrel.services.profile_edit import load_state

    state = load_state()
    # load_state() shows unsaved sections from the shipped example as starting values
    # (D-UI-25). Those are not the operator's: a search built from them scrapes the fictional
    # person's titles and metro (review finding 2). Drop every such section here.
    blocks = {}
    for t in state.present:
        block = dict(state.tracks[t])
        for section in state.from_example.get(t, ()):
            block.pop(section, None)
        blocks[t] = block
    email = None
    for t in ("employment", "consulting"):
        e = str((blocks.get(t) or {}).get("email") or "").strip()
        if e:
            email = e
            break
    return blocks, email


async def rebuild_generated_sources(
    session: AsyncSession, *, enable: bool | None = None, dry_run: bool = False
) -> RebuildResult:
    """Make the generated rows match the profile. Matched by name; custom and shipped rows are
    never read or written. `enable` True switches every generated row on (the onboarding
    "Start searching" press), False switches them off, None keeps what they had (a new row
    takes the state its siblings had). `dry_run` computes the result without writing, for the
    onboarding review step. The caller commits."""
    from kestrel.services.tracks import load_enabled

    blocks, email = _profile_inputs()
    tracks = await load_enabled(session)
    specs = build_specs(
        blocks, tracks, email, sec_use_titles=await sec_titles_opt_in(session),
        removed_variants=await removed_variants(session),
    )
    result = RebuildResult(specs=specs)

    existing = {
        s.name: s
        for s in (
            await session.execute(select(Source).where(Source.origin == ORIGIN))
        ).scalars().all()
    }
    was_on = any(s.enabled for s in existing.values())
    # A new row takes its siblings' state; an existing row keeps its own unless `enable` says
    # otherwise, so a search auto-disabled after failures stays off (review finding 9).
    new_on = was_on if enable is None else enable
    # A generated search the operator edited became `custom` under the same name (D-SRC-3). It
    # replaces the generated one, so that name is not built again while the custom row exists.
    taken = set(
        (
            await session.execute(select(Source.name).where(Source.origin != ORIGIN))
        ).scalars().all()
    )
    specs = [sp for sp in specs if sp.name not in taken]
    result.specs = specs
    wanted = {sp.name for sp in specs}

    for sp in specs:
        row = existing.get(sp.name)
        if row is None:
            result.created.append(sp.name)
            if not dry_run:
                session.add(Source(
                    track=sp.track, kind=sp.kind, name=sp.name, config=sp.config,
                    enabled=new_on, cadence_hours=sp.cadence_hours, origin=ORIGIN,
                ))
            continue
        row_on = row.enabled if enable is None else enable
        if (row.config != sp.config or row.track != sp.track or row.kind != sp.kind
                or row.cadence_hours != sp.cadence_hours or row.enabled != row_on):
            result.updated.append(sp.name)
            if not dry_run:
                row.config, row.track, row.kind = sp.config, sp.track, sp.kind
                row.cadence_hours, row.enabled = sp.cadence_hours, row_on
    for name, row in existing.items():
        if name not in wanted:
            result.removed.append(name)
            if not dry_run:
                await _retire(session, row)
    if result.changed and not dry_run:
        log.info("generated_sources_rebuilt", created=len(result.created),
                 updated=len(result.updated), removed=len(result.removed), enabled=new_on)
    return result


RETIRED = "retired"


async def _retire(session: AsyncSession, row: Source) -> None:
    """A generated search the profile no longer implies. Deleted when nothing points at it;
    otherwise switched off and marked retired, because scrape runs and opportunities keep a
    foreign key to it and deleting it fails the whole rebuild (review finding 1). A retired row
    is never scraped or shown as a search; its postings stay in the Pipeline."""
    from kestrel.db.models import Opportunity, ScrapeRun

    used = (
        await session.execute(select(ScrapeRun.id).where(ScrapeRun.source_id == row.id).limit(1))
    ).first() or (
        await session.execute(
            select(Opportunity.id).where(Opportunity.source_id == row.id).limit(1)
        )
    ).first()
    if used:
        row.enabled = False
        row.origin = RETIRED
        row.name = f"{row.name} (retired {row.id})"  # frees the name for a rebuilt search
    else:
        await session.delete(row)


_BOARD_NAMES = {"indeed": "Indeed", "google": "Google"}


async def variants_view(session: AsyncSession) -> dict[str, Any]:
    """What the Profile page lists under the target titles (D-TTL-2): the variants searched,
    the ones left out to stay under the search limit, and the ones the operator removed."""
    from kestrel.services.tracks import load_enabled

    blocks, email = _profile_inputs()
    tracks = await load_enabled(session)
    gone = await removed_variants(session)
    specs, used, offered = plan(
        blocks, tracks, email, sec_use_titles=await sec_titles_opt_in(session),
        removed_variants=gone,
    )
    return {
        "used": used,
        "dropped": [v for v in offered if v not in used],
        "removed": gone,
        "boards": " and ".join(_BOARD_NAMES.get(s, s) for s in SITES),
        "searches": sum(1 for s in specs if s.track == "employment"),
        "employment_on": "employment" in tracks,
    }
