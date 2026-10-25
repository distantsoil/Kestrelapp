"""Assemble the shared context blocks (PROMPTS.md "Shared context blocks") and render a prompt's
USER template for one opportunity.

Rendering is deterministic string substitution, never a model call and never `eval`: a mapping of
the exact brace-delimited placeholder text to its resolved value, plus two structural passes for the
consulting templates' `{if trigger_event:}` / `{if posted:}` section markers and the P2C
`{for each org signal: ...}` line. The `{X or "default"}` placeholders are resolved in Python and
mapped under both their bare and their with-default spellings, so a wording nuance in the template
never leaks an unresolved brace to the model.
"""

from __future__ import annotations

import re
from statistics import median
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import CompObservation, Opportunity, Org
from kestrel.ingest.tokens import count_tokens, truncate_head, truncate_middle
from kestrel.llm.fewshot import FewShotExample

_INTERVAL_LABEL = {"year": "year", "hour": "hour", "day": "day", "week": "week", "month": "month"}
_MIN_COMP_CORPUS = 5  # hard rule 6: fewer than five observations is "insufficient", never rendered

# Per-tier JD budgets (docs/PROMPT_BUDGETS.md). Tier 1 decides keep/kill from the structured fields
# plus the HEAD of the description (SPEC.md 6.2: first 1200 tokens) inside a 4096 num_ctx window;
# Tier 2 gets settings.pipeline.max_jd_tokens with the middle elided (SPEC.md 6.3). Tier 1 must
# always see a smaller slice than Tier 2; tests/test_prompt_budgets.py guards both.
TIER1_HEAD_TOKENS = 1200


# ---------------------------------------------------------------------------- small helpers
def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _joined(value: Any, limit: int | None = None) -> str:
    if not value:
        return "(none)"
    items = list(value)[:limit] if limit else list(value)
    return ", ".join(str(x) for x in items)


def comp_display(opp: Any) -> str:
    """The posting's stated compensation as a human string, or "" (falsy, so the template's
    `or "not stated"` fallback fires). Never fabricates: only what ingestion parsed/structured."""
    lo, hi = _get(opp, "comp_min"), _get(opp, "comp_max")
    if lo is None and hi is None:
        return ""
    currency = _get(opp, "comp_currency") or "USD"
    interval = _INTERVAL_LABEL.get(_get(opp, "comp_interval") or "year", "year")
    sym = {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency, currency + " ")
    if lo is not None and hi is not None and lo != hi:
        return f"{sym}{lo:,.0f} - {sym}{hi:,.0f} / {interval}"
    one = hi if hi is not None else lo
    return f"{sym}{one:,.0f} / {interval}"


def _geo_summary(profile: Any) -> str:
    geo = _get(profile, "geo_rules")
    if geo is None:
        return "unspecified"
    states = _joined(_get(geo, "onsite_states"))
    metros = [_get(m, "name") for m in (_get(geo, "onsite_metros") or [])]
    return f"onsite {states}; metros {_joined(metros)}"


# --------------------------------------------------------------------------- PROFILE blocks
def _money(value: Any, unit: str = "") -> str:
    if value is None or value == "":
        return "unspecified"
    try:
        return f"${float(value):,.0f}{unit}"
    except (TypeError, ValueError):
        return f"{value}{unit}"


def profile_compact(profile: Any) -> str:
    """PROFILE_COMPACT (PROMPTS.md), under ~400 tokens, used by Tier 1 and as the head of
    PROFILE_FULL. Built per track from whichever `*_terms` block the loaded profile carries: the
    employment block renders level, target titles, anti-titles and a base-salary floor; the
    consulting block renders minimum engagement, service lines, excluded sectors, a day-rate floor,
    and the target org size and sectors that the P1C/P2C size-range rules depend on."""
    if profile is None:
        return "CANDIDATE\n(profile unavailable)"
    emp = _get(profile, "employment_terms")
    con = _get(profile, "consulting_terms")
    geo = _get(profile, "geo_rules")
    headline = _get(profile, "headline") or "n/a"
    lines = ["CANDIDATE"]
    if emp is not None or con is None:
        terms = emp or SimpleNamespace()
        lines += [
            f"Level: {_get(terms, 'seniority_floor') or 'unspecified'} and above. "
            f"Currently operating at {headline}.",
            f"Targets: {_joined(_get(terms, 'target_titles'))}",
            f"Hard no: {_joined(_get(terms, 'anti_titles'))}",
            f"Comp floor: {_money(_get(terms, 'comp_floor_usd'))} base. "
            f"Target: {_money(_get(terms, 'comp_target_usd'))}.",
        ]
        lines += _work_modes_line(terms)
    else:
        lines += [
            f"Practice: {headline}. Minimum engagement: "
            f"{_get(con, 'min_engagement_days') or 'unspecified'} days.",
            f"Service lines: {_joined(_get(con, 'service_lines'))}",
            f"Target orgs: size {_joined(_get(con, 'target_org_size'))}; "
            f"sectors {_joined(_get(con, 'target_sectors'))}",
            f"Excluded sectors: {_joined(_get(con, 'excluded_sectors'))}",
            f"Day rate floor: {_money(_get(con, 'day_rate_floor'), '/day')}. "
            f"Target: {_money(_get(con, 'day_rate_target'), '/day')}.",
        ]
        lines += _work_modes_line(con)
    lines += [
        f"Location: {_geo_summary(profile)}. Remote OK: {_get(geo, 'remote_ok')}. "
        f"Relocation: {_get(geo, 'relocation_acceptable')}.",
        f"Work authorization: {_get(profile, 'work_auth') or 'unspecified'}",
        f"Must have: {_joined(_get(profile, 'must_have'), 4)}",
        f"Must avoid: {_joined(_get(profile, 'must_avoid'), 4)}",
    ]
    return "\n".join(lines)


def _work_modes_line(terms: Any) -> list[str]:
    """One line naming the work modes the person chose (Profile > Employment or Consulting).
    Nothing when none is chosen, so a profile that never set it reads exactly as before."""
    modes = [str(m) for m in (_get(terms, "work_modes") or []) if m]
    return [f"Work modes wanted: {', '.join(modes)}."] if modes else []


def profile_full(profile: Any) -> str:
    """PROFILE_FULL (PROMPTS.md): PROFILE_COMPACT plus narrative, proof points, and experience
    shape. Proof points are the ONLY achievements the model may cite (hard rule 2)."""
    if profile is None:
        return profile_compact(profile)
    proofs = _get(profile, "proof_points") or []
    proof_lines = [
        f"- {_get(p, 'claim')} | evidence: {_get(p, 'evidence')} "
        f"| metric: {_get(p, 'metric') or 'n/a'}"
        for p in proofs
    ]
    parts = [
        profile_compact(profile),
        "",
        "NARRATIVE",
        (_get(profile, "narrative") or "").strip(),
        "",
        "PROOF POINTS (the only achievements you may cite; do not invent others)",
        "\n".join(proof_lines) if proof_lines else "(none provided)",
    ]
    return "\n".join(parts)


# ------------------------------------------------------------------------- few-shot rendering
def few_shot_block(examples: list[FewShotExample]) -> str:
    """The PRIOR EVALUATIONS block, or "" when zero-shot (below the label threshold). Diversity and
    the label gate are handled by fewshot.select_few_shot; this only renders what it returns."""
    if not examples:
        return ""
    out = [
        "PRIOR EVALUATIONS BY THIS CANDIDATE'S OWN JUDGMENT",
        "(These are past decisions the candidate confirmed or corrected. Calibrate to them.)",
    ]
    for ex in examples:
        summary = " ".join((ex.summary or "").split()) or "(opportunity summary unavailable)"
        why = " ".join((ex.rationale or "").split())
        out.append(f"- {summary}")
        out.append(f"  Holistic: {ex.holistic}  Verdict: {ex.verdict}")
        # The caller may derive both lines from the same label text; never send it twice.
        if why and why not in summary and summary not in why:
            out.append(f"  Why: {why}")
    return "\n".join(out)


# ---------------------------------------------------------------------- description preprocessing
_WS_RUN_RE = re.compile(r"[ \t ]{2,}")
_TRAILING_WS_RE = re.compile(r"[ \t ]+$", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_TIER1_TRUNCATION_MARKER = (
    "\n[... description truncated for triage; it continues beyond this point ...]"
)


def normalize_description(text: str | None) -> str:
    """Whitespace-only normalization of a description before it is budgeted: non-breaking spaces
    and runs of spaces or tabs collapse to one space, trailing whitespace is dropped, and runs of
    blank lines collapse to one. No word is removed or reordered; EEO and benefits paragraphs are
    deliberately kept because they carry work-authorization and comp statements."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RUN_RE.sub(" ", text)
    text = _TRAILING_WS_RE.sub("", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


def tier1_description(text: str | None, n_tokens: int = TIER1_HEAD_TOKENS) -> str:
    """The Tier 1 JD slice, never more than n_tokens in total: the head of the description, with
    an explicit marker appended ONLY when the description was actually cut (its tokens are reserved
    out of the budget), so the model knows requirement text may follow the slice."""
    text = normalize_description(text)
    if count_tokens(text) <= n_tokens:
        return text
    budget = max(0, n_tokens - count_tokens(_TIER1_TRUNCATION_MARKER))
    return truncate_head(text, budget).rstrip() + _TIER1_TRUNCATION_MARKER


def _date_only(value: str | None) -> str:
    return (value or "")[:10] or "unknown"


# ------------------------------------------------------------------------- comp corpus intel
async def comp_intel_stats(session: AsyncSession, opp: Opportunity) -> dict[str, Any] | None:
    """The numbers behind comp_intel_summary: {"n", "lo", "hi", "median", "p25", "p75", "scope"}
    for the observed midpoints of this title (same geography when it has five, else all), or None
    when fewer than five observations exist (hard rule 6: the caller then says "too few" and
    draws no bracket). `n_seen` is how many same-title observations were looked at either way."""
    title = (opp.title or "").lower().strip()
    if not title:
        return None
    geo_bucket = opp.state or opp.country or opp.city or "unknown"
    rows = (
        (
            await session.execute(
                select(CompObservation).where(CompObservation.normalized_title == title)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    same_geo = [r for r in rows if (r.geo_bucket or "unknown") == geo_bucket]
    chosen, scope = (
        (same_geo, f"{geo_bucket}")
        if len(same_geo) >= _MIN_COMP_CORPUS
        else (rows, "all geographies")
    )
    if len(chosen) < _MIN_COMP_CORPUS:
        return None
    mids = [
        (r.min + r.max) / 2 if r.min is not None and r.max is not None else (r.max or r.min)
        for r in chosen
        if r.min is not None or r.max is not None
    ]
    mids = sorted(m for m in mids if m is not None)
    if len(mids) < _MIN_COMP_CORPUS:
        return None
    n = len(mids)

    def pct(q: float) -> float:
        pos = (n - 1) * q
        lo_i = int(pos)
        hi_i = min(lo_i + 1, n - 1)
        return mids[lo_i] + (mids[hi_i] - mids[lo_i]) * (pos - lo_i)

    return {
        "n": n, "lo": mids[0], "hi": mids[-1], "median": median(mids), "p25": pct(0.25),
        "p75": pct(0.75), "scope": scope,
    }


async def comp_intel_count(session: AsyncSession, opp: Opportunity) -> int:
    """How many same-title observations exist at all, for the "Too few (n)" line."""
    title = (opp.title or "").lower().strip()
    if not title:
        return 0
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(CompObservation)
                .where(CompObservation.normalized_title == title)
            )
        ).scalar_one()
    )


async def comp_intel_summary(session: AsyncSession, opp: Opportunity) -> str | None:
    """The operator's own accumulated comp corpus for this title and geography (SPEC-MODULES 6.1),
    or None when fewer than five observations exist (hard rule 6). Prefers same-geo observations,
    falling back to title-only if same-geo is too thin; states the sample size either way."""
    stats = await comp_intel_stats(session, opp)
    if stats is None:
        return None
    return (
        f"median ${stats['median']:,.0f} (n={stats['n']}, {stats['scope']}; "
        f"range ${stats['lo']:,.0f}-${stats['hi']:,.0f}), "
        "from the operator's own observed corpus"
    )


# ---------------------------------------------------------------------------- org signals text
def org_signals_lines(org: Org | None) -> str:
    signals = _get(org, "signals") or []
    if not signals:
        return "(no accumulated signals on this organization yet)"
    lines = []
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        cat = sig.get("category") or sig.get("event_type") or "signal"
        stmt = sig.get("statement") or sig.get("evidence_quote") or sig.get("implied_need") or ""
        when = sig.get("event_date") or sig.get("date") or sig.get("observed_at") or "undated"
        url = sig.get("url") or sig.get("source_url") or "n/a"
        lines.append(f"- [{cat}] {stmt} ({when}) — source {url}")
    return "\n".join(lines) if lines else "(no accumulated signals on this organization yet)"


def org_signal_summary(org: Org | None) -> str:
    signals = _get(org, "signals") or []
    kinds = [s.get("category") or s.get("event_type") for s in signals if isinstance(s, dict)]
    kinds = [k for k in kinds if k]
    return _joined(kinds) if kinds else "none"


# ------------------------------------------------------------------------------ rendering
_IF_RE = re.compile(r"^\s*\{if (\w+):\}\s*$")
_FOR_ORG_SIG_RE = re.compile(r"\{for each org signal:.*\}")


def _apply_conditionals(template: str, *, trigger_event: bool, posted: bool) -> str:
    """Drop `{if COND:}` marker lines and keep each following section only when COND is true. The
    preamble before the first marker is always kept; markers partition the rest sequentially."""
    flags = {"trigger_event": trigger_event, "posted": posted}
    out: list[str] = []
    keep = True
    for line in template.split("\n"):
        m = _IF_RE.match(line)
        if m:
            keep = flags.get(m.group(1), True)
            continue
        if keep:
            out.append(line)
    return "\n".join(out)


def render_user(
    template: str,
    values: dict[str, str],
    *,
    trigger_event: bool = False,
    posted: bool = True,
    org_signals: str = "",
) -> str:
    """Render a prompt USER template: conditionals, the for-each-signal line, then flat placeholder
    substitution. `values` keys are the exact text between braces."""
    text = _apply_conditionals(template, trigger_event=trigger_event, posted=posted)
    text = _FOR_ORG_SIG_RE.sub(lambda _: org_signals, text)
    for key, val in values.items():
        text = text.replace("{" + key + "}", val)
    return text


def _dual(base_key: str, value: str, default: str) -> dict[str, str]:
    """Map both `{key}` and `{key or "default"}` to the resolved value (which is `value` when
    truthy, else `default`), so either template spelling renders correctly."""
    resolved = value if value else default
    return {base_key: resolved, f'{base_key} or "{default}"': resolved}


def build_tier1_values(
    *,
    opp: Opportunity,
    org: Org | None,
    profile: Any,
    source_name: str,
    n_tokens: int = TIER1_HEAD_TOKENS,
) -> dict[str, str]:
    """Placeholder map for P1 (employment) and P1C (consulting) USER templates. The description is
    the HEAD slice (tier1_description), always smaller than Tier 2's max_jd_tokens window."""
    desc = tier1_description(opp.description_md, n_tokens)
    values: dict[str, str] = {
        "PROFILE_COMPACT": profile_compact(profile),
        "job.title": opp.title or "(untitled)",
        "job.company": (org.name if org else None) or opp.org_name or "(unknown)",
        "job.location_raw": opp.location_raw or "(unspecified)",
        "job.remote_type": opp.remote_type or "unknown",
        "job.employment_type": opp.employment_type or "unspecified",
        "opp.opp_type": opp.opp_type or "unknown",
        "opp.title": opp.title or "(untitled)",
        "opp.location_raw": opp.location_raw or "(unspecified)",
        "opp.remote_type": opp.remote_type or "unknown",
        "opp.event_date": opp.event_date or "unknown",
        "org.name": (org.name if org else None) or opp.org_name or "(unknown)",
        "org_signal_count": str(len((org.signals if org else None) or [])),
        "signal_summary": org_signal_summary(org),
        "n": str(n_tokens),
        "description_truncated": desc or "(no description)",
        "age_days": _age_days_str(opp.event_date),
    }
    td = opp.trigger_data or {}
    values.update(
        {
            "trigger_data.event_type": str(td.get("event_type", "unknown")),
            "trigger_data.evidence_quote": str(td.get("evidence_quote", "")),
            "trigger_data.implied_need": str(td.get("implied_need", "")),
        }
    )
    values.update(_dual("comp_display", comp_display(opp), "not stated"))
    values.update(_dual("job.date_posted", opp.date_posted or "", "unknown"))
    values.update(_dual("opp.date_posted", opp.date_posted or "", "unknown"))
    values.update(_dual("org.sector", (org.sector if org else "") or "", "unknown"))
    values.update(_dual("org.size_band", (org.size_band if org else "") or "", "unknown"))
    values.update(_dual("org.ownership", (org.ownership if org else "") or "", "unknown"))
    return values


def build_tier2_values(
    *,
    opp: Opportunity,
    org: Org | None,
    profile: Any,
    source_name: str,
    max_jd_tokens: int,
    few_shot: str,
    comp_intel: str | None,
) -> dict[str, str]:
    """Placeholder map for P2 (employment) and P2C (consulting) USER templates."""
    desc = truncate_middle(normalize_description(opp.description_md), max_jd_tokens)
    values: dict[str, str] = {
        "PROFILE_FULL": profile_full(profile),
        "FEW_SHOT_EXAMPLES": few_shot,
        "opp.seen_count": str(opp.seen_count),
        # Dates only: the time of day carries nothing the ghost/repost read needs.
        "opp.first_seen_at": _date_only(opp.first_seen_at),
        "opp.last_seen_at": _date_only(opp.last_seen_at),
        "opp.version": str(opp.version),
        "opp.ghost_score": f"{opp.ghost_score:.2f}",
        "opp.opp_type": opp.opp_type or "unknown",
        "opp.location_raw": opp.location_raw or "(unspecified)",
        "opp.event_date": opp.event_date or "unknown",
        "job.title": opp.title or "(untitled)",
        "job.company": (org.name if org else None) or opp.org_name or "(unknown)",
        "job.location_raw": opp.location_raw or "(unspecified)",
        "job.remote_type": opp.remote_type or "unknown",
        "job.employment_type": opp.employment_type or "unspecified",
        "job.url": opp.url or "(no url)",
        "org.name": (org.name if org else None) or opp.org_name or "(unknown)",
        "org.sector": (org.sector if org else None) or "unknown",
        "org.size_band": (org.size_band if org else None) or "unknown",
        "org.ownership": (org.ownership if org else None) or "unknown",
        "source.name": source_name,
        "n": str(max_jd_tokens),
        "description_truncated": desc or "(no description)",
        "description_truncated_to_max_jd_tokens": desc or "(no description)",
        "age_days": _age_days_str(opp.event_date),
    }
    td = opp.trigger_data or {}
    values.update(
        {
            "trigger_data.event_type": str(td.get("event_type", "unknown")),
            "trigger_data.evidence_quote": str(td.get("evidence_quote", "")),
            "trigger_data.source_url": str(td.get("source_url", "n/a")),
        }
    )
    values.update(_dual("comp_display", comp_display(opp), "not stated"))
    values.update(_dual("job.date_posted", opp.date_posted or "", "unknown"))
    values.update(_dual("opp.date_posted", opp.date_posted or "", "unknown"))
    # Two spellings appear across P2 / P2C.
    for spelling in (
        'comp_intel_summary or "insufficient data (n<5)"',
        'comp_intel_summary or "insufficient data"',
    ):
        values[spelling] = comp_intel if comp_intel else spelling.split(' or "', 1)[1].rstrip('"')
    values["comp_intel_summary"] = comp_intel or "insufficient data (n<5)"
    return values


def _age_days_str(iso_date: str | None) -> str:
    if not iso_date:
        return "unknown"
    from datetime import date

    try:
        d = date.fromisoformat(iso_date[:10])
    except ValueError:
        return "unknown"
    return str((date.today() - d).days)
