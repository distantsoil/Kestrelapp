"""Post-processing of a Tier 2 evaluation (SPEC.md 7.2/7.3, PROMPTS.md P2/P2C post-processing).

The model returns dimension scores, a holistic score, its own verdict, blockers, and a legitimacy
object. The system, never the model, then: computes the weighted score, flags divergence for
review, checks each model blocker against the operator's own data (`check_blockers`, D-SCORE-22),
applies the override rules (an upheld blocker or a `reject` legitimacy forces `pass`; `suspect`
caps the displayed score at 3.5), and derives the authoritative verdict from the score thresholds
while preserving the model's own verdict for context. Nothing here calls a model or invents data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kestrel.config import Settings
from kestrel.rules.helpers import geo_evaluate
from kestrel.rules.margin import margin_floor

# Employment uses apply/consider/pass; consulting uses pursue/consider/pass (SPEC.md 6.3/7.3).
_TOP_VERDICT = {"employment": "apply", "consulting": "pursue"}
_SUSPECT_CAP = 3.5


@dataclass
class PostResult:
    weighted: float
    holistic: float
    displayed_score: float
    needs_review: bool
    verdict: str  # threshold-derived, authoritative for filtering
    verdict_model: str  # the model's own verdict, for context
    forced_by: str | None
    blockers: list[dict] = field(default_factory=list)
    legitimacy_status: str = "clean"
    verdict_disagreement: bool = False
    # The legitimacy as stored after `check_legitimacy` (round 7), and its worth-checking lines.
    legitimacy: dict | None = None
    legitimacy_notes: list[str] = field(default_factory=list)


def weights_for(track: str, settings: Settings) -> dict[str, float]:
    block = getattr(settings.scoring.weights, track)
    return block.model_dump()


def weighted_score(dimension_scores: dict[str, float], weights: dict[str, float]) -> float:
    """Σ(dimension.score × weight). Only dimensions with a weight contribute; a missing dimension
    contributes nothing rather than raising."""
    return round(sum(score * weights.get(key, 0.0) for key, score in dimension_scores.items()), 4)


def verdict_from_score(score: float, track: str, settings: Settings) -> str:
    if score >= settings.thresholds.apply:
        return _TOP_VERDICT[track]
    if score >= settings.thresholds.consider:
        return "consider"
    return "pass"


# ------------------------------------------------------------------ the blocker check (D-SCORE-22)
# Hard rule 4: a model blocker forces `pass` only when the operator's own data upholds it. The
# check is code, never a model. Results, stored on each blocker as `check`:
UPHELD = "upheld"  # the operator's data confirms it: it counts, the role is not a fit
CONTRADICTED = "contradicted"  # the operator's data says otherwise: a Worth checking note
NEEDS_CONFIRMING = "needs_confirming"  # nothing to check it against: a Worth checking note
# A kind no stored data can check is never counted on the model's word alone (owner via parent,
# D-SCORE-28): it is stored as needs_confirming with reason not_checkable. Only the upheld count.
COUNTED = frozenset({UPHELD})

_COMP_KINDS = frozenset({"comp", "rate"})  # rate: P2C
_LOCATION_KINDS = frozenset({"location", "geography"})  # geography: P2C
_WORK_AUTH = "work_authorization"
_PER_YEAR = {"year": 1.0, "month": 12.0, "week": 52.0, "day": 260.0, "hour": 2080.0}
_PER_DAY = {"day": 1.0, "hour": 8.0, "week": 1 / 5, "month": 1 / 21.7, "year": 1 / 260}


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_comp(track: str, opp: Any, profile: Any) -> tuple[str, str]:
    """A pay blocker against the posted band and the operator's floor (P2C: day rate)."""
    if track == "consulting":
        terms = _attr(profile, "consulting_terms")
        floor = _number(_attr(terms, "day_rate_floor"))
        pct = _attr(terms, "day_rate_floor_margin_pct")
        scale = _PER_DAY
    else:
        terms = _attr(profile, "employment_terms")
        floor = _number(_attr(terms, "comp_floor_usd"))
        pct = _attr(terms, "comp_floor_margin_pct")
        scale = _PER_YEAR
    if floor is None:
        return NEEDS_CONFIRMING, "no_floor"
    lo, hi = _number(_attr(opp, "comp_min")), _number(_attr(opp, "comp_max"))
    if (lo is None and hi is None) or _attr(opp, "comp_source") == "inferred":
        # A job board's estimate is not pay the employer stated (#66).
        return NEEDS_CONFIRMING, "no_band"
    if _attr(opp, "comp_currency_known") is not True:
        # Assumed, not stated (or a row from before migration 0037): never compared as USD.
        return NEEDS_CONFIRMING, "currency_unknown"
    if hi is None or lo == hi:
        # One figure, not a band: "from $X" is often stored as min = max = X (ingest.comp), so
        # the top is unknown, as Tier 0 treats a missing maximum (margin.posted_comp_max).
        return NEEDS_CONFIRMING, "no_top"
    if (_attr(opp, "comp_currency") or "USD") != "USD":
        return NEEDS_CONFIRMING, "other_currency"
    interval = _attr(opp, "comp_interval")
    if _attr(opp, "comp_interval_known") is not True:
        return NEEDS_CONFIRMING, "no_interval"  # assumed, not stated (round 4)
    if track == "consulting" and interval not in ("day", "hour", "week", "month"):
        # Ingestion stores a rate with no stated interval as "year" (ingest.normalize), so a
        # yearly or missing interval on a rate is unknown, never read as a salary (review).
        return NEEDS_CONFIRMING, "no_interval"
    factor = scale.get(str(interval or "year"))
    if factor is None:
        return NEEDS_CONFIRMING, "no_interval"
    top = max(x for x in (lo, hi) if x is not None) * factor
    if track == "employment" and top < 10_000:
        return NEEDS_CONFIRMING, "implausible"  # a "salary" this low is a misread figure
    if top >= floor:
        return CONTRADICTED, "band_reaches_floor"
    lowest = margin_floor(floor, pct)
    if lowest is not None and top >= lowest:
        return NEEDS_CONFIRMING, "near_floor"
    return UPHELD, "band_below_floor"


def _check_location(opp: Any, profile: Any) -> tuple[str, str]:
    """A location blocker against the operator's remote and on-site choices (`geo_allows`). An
    operator willing to relocate never has one upheld (D-SCORE-28)."""
    check, reason = _location_result(opp, profile)
    if check == UPHELD and _attr(_attr(profile, "geo_rules"), "relocation_acceptable") is True:
        return NEEDS_CONFIRMING, "willing_to_relocate"
    return check, reason


def _location_result(opp: Any, profile: Any) -> tuple[str, str]:
    geo = _attr(profile, "geo_rules")
    remote_type = _attr(opp, "remote_type")
    if geo is None:
        return NEEDS_CONFIRMING, "no_location_rules"
    if remote_type == "remote":
        allow = _attr(geo, "remote_country_allowlist") or []
        country = _attr(opp, "country")
        if allow and not country:
            return NEEDS_CONFIRMING, "remote_country_unknown"
        if allow and not geo_evaluate(opp, geo)[0]:
            return UPHELD, "location_outside_rules"  # a country the operator did not list
        if _attr(geo, "remote_ok") is False:
            # A preference the model reads, not a filter (profile_edit): never a dealbreaker.
            return NEEDS_CONFIRMING, "remote_not_chosen"
    if remote_type in (None, "", "unknown"):
        # A named place with no remote wording is not proof of on-site (D-SCORE-22's wording;
        # the operator may have restored the role from Tier 0).
        return NEEDS_CONFIRMING, "remote_type_unknown"
    if remote_type != "remote" and not any(
        _attr(opp, k) for k in ("state", "city", "location_raw")
    ):
        return NEEDS_CONFIRMING, "location_unknown"
    allowed, uncertain = geo_evaluate(opp, geo)
    if not allowed:
        return UPHELD, "location_outside_rules"
    if uncertain or remote_type in (None, "", "unknown"):
        return NEEDS_CONFIRMING, "remote_type_unknown"
    if remote_type == "remote":
        if not (_attr(geo, "remote_country_allowlist") or []):
            return CONTRADICTED, "remote_anywhere"  # no countries listed: claim none
        return CONTRADICTED, "remote_in_allowed_country"
    if not (_attr(geo, "onsite_states") or []) and not (_attr(geo, "onsite_metros") or []):
        return NEEDS_CONFIRMING, "no_onsite_places"
    return CONTRADICTED, "onsite_place_matches"


def _check_work_auth(work_auth_signal: str | None, requires_sponsorship: bool) -> tuple[str, str]:
    """The explicit-refusal rule (P2 rule 6): only a posting that states no sponsorship, for a
    profile that needs it, rules the candidate out."""
    if work_auth_signal == "citizenship_required":
        # Needing sponsorship says the operator is not a citizen; otherwise it is not stored.
        return (UPHELD, "citizenship_required") if requires_sponsorship else (
            NEEDS_CONFIRMING, "citizenship_required")
    if not requires_sponsorship:
        return CONTRADICTED, "no_sponsorship_needed"
    if work_auth_signal == "explicit_no_sponsorship":
        return UPHELD, "explicit_no_sponsorship"
    return NEEDS_CONFIRMING, "sponsorship_not_stated"


def check_blockers(
    blockers: list[dict],
    *,
    track: str,
    opp: Any,
    profile: Any,
    work_auth_signal: str | None,
    requires_sponsorship: bool,
) -> list[dict]:
    """Each model blocker as the model gave it (`kind`, `detail`, untouched) plus `check` and
    `check_reason`. Deterministic: the same inputs always give the same result. A blocker whose
    check is not upheld or unchecked never forces `pass` (hard rule 4)."""
    out: list[dict] = []
    for b in blockers:
        kind = str(b.get("kind") or "other")
        if kind in _COMP_KINDS:
            check, reason = _check_comp(track, opp, profile)
        elif kind in _LOCATION_KINDS:
            check, reason = _check_location(opp, profile)
        elif kind == _WORK_AUTH:
            check, reason = _check_work_auth(work_auth_signal, requires_sponsorship)
        else:
            # Never on the model's word alone: other, clearance, licensure, employment type, P2C
            # duration, sector, capacity, conflict and any future kind (D-SCORE-28). Closes the
            # relabel bypass (a pay complaint written as "other").
            check, reason = NEEDS_CONFIRMING, "not_checkable"
        out.append({**b, "check": check, "check_reason": reason})
    return out


def counted_blockers(blockers: list | None) -> list[dict]:
    """The blockers that count against the role: upheld, and every blocker stored before the
    check existed (no `check` key), which counted then."""
    return [b for b in blockers or [] if isinstance(b, dict) and b.get("check", UPHELD) in COUNTED]


def noted_blockers(blockers: list | None) -> list[dict]:
    """The blockers the check did not uphold: shown under Worth checking, never counted."""
    return [b for b in blockers or []
            if isinstance(b, dict) and b.get("check", UPHELD) not in COUNTED]


def blocks_with_notes(value: Any, extra: list[str] | None = None) -> dict:
    """The six blocks as the model gave them, plus a v4 read's worth-checking lines (D-SCORE-24)
    and the legitimacy signals the data could not check (round 7) under `worth_checking`, where
    the role page and the Pipeline panel read them."""
    blocks = value.blocks.model_dump()
    notes = [str(n) for n in (getattr(value, "worth_checking", None) or []) if str(n).strip()]
    notes += [n for n in (extra or []) if n not in notes]
    if notes:
        blocks["worth_checking"] = notes
    return blocks


# ------------------------------------------------------------------ legitimacy (round 7, #208)
# The line at which Kestrel's own repost measure says a posting keeps coming back: the same line
# the Pipeline warning uses (kestrel.web.pipeline_view.GHOST_WARN; a test keeps them equal).
GHOST_UPHOLD = 0.6
# Worth-checking lines for signals the data cannot check (plain words, no jargon).
_LEGIT_NOTES = {
    "no_company_detail": "The posting says little about the company. Worth a quick look.",
    "generic_boilerplate": "The model found the posting generic. Worth reading it yourself.",
    "staffing_agency_unnamed_client": "A staffing agency may be posting for an unnamed client.",
    "unlimited_earning_language": "The posting talks about unlimited earnings. Worth checking.",
    "personal_email_contact": "The model saw a personal email address as the contact.",
    "upfront_cost_or_purchase": "The model saw a cost or purchase asked of applicants.",
    "mlm_pattern": "The model saw signs of a multi-level marketing scheme.",
    "unrealistic_requirements_for_comp": "The requirements may be high for the pay offered.",
    "commission_only": "The pay may be commission only. Worth checking.",
    "pipeline_building_not_real_opening": "This may be a pipeline posting, not an open role.",
    "reposted_repeatedly": "The model thought this posting is reposted often.",
    "no_comp_no_location_no_team": "The model found no pay, location or team details.",
}


def _check_signal(signal: str, opp: Any) -> tuple[str, str]:
    """(check, reason) for one legitimacy signal against what ingestion knows about `opp`.
    UPHELD and CONTRADICTED only from data Kestrel holds; anything else is NEEDS_CONFIRMING."""
    if opp is None:
        return NEEDS_CONFIRMING, "no_posting"
    if signal == "reposted_repeatedly":
        ghost = _attr(opp, "ghost_score")
        if not isinstance(ghost, (int, float)) or isinstance(ghost, bool):
            return NEEDS_CONFIRMING, "no_repost_data"
        return ((UPHELD, "ghost_score_high") if ghost >= GHOST_UPHOLD
                else (CONTRADICTED, "ghost_score_low"))
    if signal == "no_comp_no_location_no_team":
        from kestrel.ingest.normalize import mentions_team

        desc = _attr(opp, "description_md")
        has_comp = _attr(opp, "comp_min") is not None or _attr(opp, "comp_max") is not None
        has_place = bool(_attr(opp, "location_raw") or _attr(opp, "city")
                         or _attr(opp, "country") or _attr(opp, "is_remote") is True)
        has_team = mentions_team(desc)
        if has_comp or has_place or has_team:
            return CONTRADICTED, "comp_location_or_team_posted"
        return (UPHELD, "none_posted") if desc else (NEEDS_CONFIRMING, "no_description")
    if signal == "commission_only":
        if (_attr(opp, "comp_min") or 0) > 0 and _attr(opp, "comp_source") in (
                "structured", "parsed"):
            return CONTRADICTED, "base_pay_posted"
        return NEEDS_CONFIRMING, "not_checkable"
    return NEEDS_CONFIRMING, "not_checkable"


def check_legitimacy(legitimacy: dict, opp: Any) -> tuple[dict, list[str]]:
    """Check each legitimacy signal the model gave against ingestion's data (D-SCORE-31, round 7),
    the same way as the blockers. A contradicted signal is dropped; an unchecked one becomes a
    worth-checking line; only an upheld one can keep `suspect` (and its score cap). A `reject`
    stands while any signal is upheld or cannot be checked (hard rule 4 is about uncertainty
    rejecting; here the model's scam signals the data cannot see keep their weight), and drops to
    clean when every signal is contradicted. The status and the kept signals always agree: no
    signal is listed under clean. Returns the stored legitimacy and the worth-checking lines."""
    status = legitimacy.get("status") or "clean"
    checks = []
    for sig in legitimacy.get("signals") or []:
        check, reason = _check_signal(str(sig), opp)
        checks.append({"signal": str(sig), "check": check, "check_reason": reason})
    upheld = [c["signal"] for c in checks if c["check"] == UPHELD]
    unchecked = [c["signal"] for c in checks if c["check"] == NEEDS_CONFIRMING]
    if status == "reject":
        effective = "reject" if (upheld or unchecked or not checks) else "clean"
    elif status == "suspect":
        effective = "suspect" if upheld else "clean"
    else:
        effective = "clean"
    kept = (upheld + unchecked) if effective == "reject" else (
        upheld if effective == "suspect" else [])
    notes = [_LEGIT_NOTES.get(sig, "A legitimacy signal is worth checking.")
             for sig in unchecked if sig not in kept]
    stored = {**legitimacy, "status": effective, "signals": kept, "model_status": status,
              "signal_checks": checks}
    return stored, notes


def post_process(
    *,
    track: str,
    dimension_scores: dict[str, float],
    holistic: float,
    model_verdict: str,
    blockers: list[dict],
    legitimacy_status: str,
    work_auth_signal: str | None,
    requires_sponsorship: bool,
    settings: Settings,
    opp: Any = None,
    profile: Any = None,
    check: bool = True,
    legitimacy: dict | None = None,
) -> PostResult:
    """Apply SPEC.md 7.3 overrides. `blockers` is the model's list of {kind, detail} dicts, each
    checked against `opp` and `profile` (`check_blockers`); only the counted ones force `pass`.
    A work-authorization blocker is added when the posting explicitly refuses sponsorship the
    profile needs. The returned verdict is authoritative; verdict_model is kept for the
    disagreement log. Every blocker, counted or not, is returned with its check result."""
    weights = weights_for(track, settings)
    weighted = weighted_score(dimension_scores, weights)
    needs_review = abs(holistic - weighted) > settings.scoring.divergence_flag

    # check=False is the bench's v3 comparison only: every blocker counts, as before #208, so a
    # v3 run stays comparable with earlier benches (D-SCORE-29). The app always checks.
    effective_blockers = check_blockers(
        list(blockers), track=track, opp=opp, profile=profile,
        work_auth_signal=work_auth_signal, requires_sponsorship=requires_sponsorship,
    ) if check else [{**b, "check": UPHELD, "check_reason": "not_checked_v3_bench"}
                     for b in blockers]
    if work_auth_signal == "explicit_no_sponsorship" and requires_sponsorship and not any(
        b["kind"] == _WORK_AUTH and b["check"] == UPHELD for b in effective_blockers
    ):
        effective_blockers.append(
            {
                "kind": "work_authorization",
                "detail": "Posting states no sponsorship; profile requires it",
                "check": UPHELD,
                "check_reason": "explicit_no_sponsorship",
            }
        )
    counted = counted_blockers(effective_blockers)

    # Each legitimacy signal is checked like a blocker (round 7): the cap and the reject below
    # follow the checked status, never the model's word alone. check=False is the v3 bench only.
    legit_notes: list[str] = []
    if legitimacy is not None and check:
        legitimacy, legit_notes = check_legitimacy(legitimacy, opp)
        legitimacy_status = legitimacy["status"]

    # Legitimacy "suspect" caps the DISPLAYED score before the verdict is derived from it.
    displayed = min(holistic, _SUSPECT_CAP) if legitimacy_status == "suspect" else holistic
    verdict = verdict_from_score(displayed, track, settings)

    reasons: list[str] = []
    if counted:
        verdict = "pass"
        kinds = ", ".join(b.get("kind", "other") for b in counted)
        reasons.append(f"blocker:{kinds}")
    if legitimacy_status == "reject":
        verdict = "pass"
        reasons.append("legitimacy_reject")
    if legitimacy_status == "suspect":
        reasons.append("legitimacy_suspect_cap")

    return PostResult(
        weighted=weighted,
        holistic=holistic,
        displayed_score=displayed,
        needs_review=needs_review,
        verdict=verdict,
        verdict_model=model_verdict,
        forced_by="; ".join(reasons) if reasons else None,
        blockers=effective_blockers,
        legitimacy_status=legitimacy_status,
        verdict_disagreement=(verdict != model_verdict),
        legitimacy=legitimacy,
        legitimacy_notes=legit_notes,
    )
