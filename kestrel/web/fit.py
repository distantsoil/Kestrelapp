"""The operator's vocabulary for a verdict (D-UI-45).

The pipeline stores six verdict words across two tiers (`keep`/`kill` at Tier 1, `apply`/
`pursue`/`consider`/`pass` at Tier 2) and a stage string for rows no model saw. None of them
answers the question the operator is asking, which is "how well does this fit me?", and `pass`
reads as the opposite of what it means. This module is the ONLY place that translation lives:
every page, badge, group header, override choice and Today line goes through it, so the words
are the same everywhere and the stored enums never change.

Fit words, in order: strong fit, good fit, maybe, not a fit. Plus "your call" when the model and
the weighted score disagree (`needs_review`), and "new" for anything not yet scored. Tier 1's
keep/kill is a triage step the operator never needs to see: a kept row is "new" until Tier 2
scores it, a killed row is "not a fit" with the rule named.

Groups on the Pipeline: WORTH ("Worth a look"), MAYBE, NEW ("Being scored"), FAILED ("Scoring
failed": parked with no live scoring task, D-UI-56), NOT_FIT (collapsed). The operator's own
decision (a pursuit, or the latest label) outranks the model for the group (`operator_group`,
D-UI-51); the word and tone stay the model's. The override control offers the four fit words;
`verdict_for_fit` maps a chosen word back to the stored verdict and a representative score so
the learning loop keeps receiving the (verdict, score) pair it trains on (the 1 to 5 slider is
gone, D-UI-45).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kestrel.db.enums import APPLIED_OR_LATER

STRONG = "strong fit"
GOOD = "good fit"
MAYBE = "maybe"
NOT_FIT = "not a fit"
YOUR_CALL = "your call"
NEW = "new"

FIT_WORDS: tuple[str, ...] = (STRONG, GOOD, MAYBE, NOT_FIT)

GROUP_WORTH = "worth"
GROUP_MAYBE = "maybe"
GROUP_APPLIED = "applied"
GROUP_NOT_FIT = "not_fit"
GROUP_NEW = "new"
GROUP_FAILED = "failed"
GROUP_LABELS = {
    GROUP_WORTH: "Worth a look",
    GROUP_MAYBE: "Maybe",
    GROUP_APPLIED: "Applied",
    GROUP_NOT_FIT: "Not a fit",
    GROUP_NEW: "Being scored",
    GROUP_FAILED: "Scoring failed",
}
# The one-line explanation under the Scoring failed header (D-UI-56).
FAILED_NOTE = "The model could not finish these. Score them again, or try another model."
FAILED_WHY = "scoring failed"

# A quarantine error in plain words (D-UI-56), checked in this order: a loop's message also
# names the max_tokens it ran into, and the loop is the truer reason.
_FAILURE_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("generation loop", "degenerate", "repeated"), "the model repeated itself"),
    (("finish_reason: length", "finish_reason length", "max_tokens", "hit the cap"),
     "the reply ran out of room"),
    (("http 4",), "the provider refused the request"),
    (("schema", "validation", "invalid json", "json", "field required", "not valid"),
     "the reply was not in the expected shape"),
)
FAILURE_FALLBACK = "scoring did not finish"


def failure_words(error: str | None) -> str:
    """A quarantine row's error as the operator would say it (D-UI-56). Never empty."""
    low = (error or "").lower()
    for needles, words in _FAILURE_WORDS:
        if any(n in low for n in needles):
            return words
    return FAILURE_FALLBACK

# The scoring thresholds are apply at 4.0 and consider at 3.0 (kestrel/pipeline/scoring.py), so
# "good fit" is an apply at the threshold and "strong fit" is well above it.
STRONG_FLOOR = 4.5
# The consider and apply thresholds (Settings > Scoring; config.Thresholds). A disagreement that
# stays on one side of both is not a call the operator has to make. load_settings_with_db keeps
# these in step with the operator's settings; the shipped defaults stand until then.
LINES = {"consider": 3.0, "apply": 4.0}


def set_lines(consider: float, apply: float) -> None:
    LINES["consider"], LINES["apply"] = float(consider), float(apply)


def score_pair(evaluation: Any) -> tuple[float, float] | None:
    """(model score, weighted score), one standing in for the other when it is missing (the
    lists' SQL does the same), or None when neither exists."""
    model, weighted = _get(evaluation, "score"), _get(evaluation, "weighted_score")
    if model is None and weighted is None:
        return None
    return (model if model is not None else weighted,
            weighted if weighted is not None else model)

# What an operator's fit word means to the learning loop: the stored verdict and the score the
# reranker trains on. `pursue` is the consulting-track spelling of `apply`.
_FIT_TO_VERDICT = {
    STRONG: ("apply", 4.75),
    GOOD: ("apply", 4.0),
    MAYBE: ("consider", 3.0),
    NOT_FIT: ("pass", 1.5),
}

# Tier 0 rule names the operator sees instead of the rule id: exact ids only (both tracks'
# shipped rules, filters.yml, plus older spellings). Never a substring: an operator's own rule
# "compliance_check" must not read as "pay" because it contains "comp". Unknown ids read "rule".
_RULE_IDS = {
    "comp_floor": "pay",
    "sponsorship": "sponsorship",
    "clearance": "clearance",
    "anti_title": "title",
    "seniority_floor": "seniority",
    "geo": "location",
    "geography": "location",
    "stale": "posting age",
    "rate_floor": "rate",
    "too_short": "engagement length",
    "excluded_sector": "sector",
    "stale_trigger": "event age",
    "title": "title",
}

# Why a score was adjusted (evaluation.forced_by), in the operator's words. forced_by is stored
# as "; "-joined reasons: "blocker:<kind>, <kind>", "legitimacy_reject", "legitimacy_suspect_cap"
# (kestrel.pipeline.scoring), or one of the single-reason ids below (Tier 1, the reranker).
_FORCED_SENTENCES = {
    "low_confidence_forced_keep": "The quick screen was unsure, so it went to a full read.",
    "t1_quarantine_promoted": "The quick screen failed, so it went to a full read.",
    "t1_hosted_refused": "The hosted quick screen refused it, so it went to a full read.",
    "reranker": "Adjusted by your ranker.",
}
_BLOCKER_WORDS = {
    "work_authorization": "the posting does not offer the work authorization you need",
    "clearance": "needs a security clearance",
    "location": "the location does not work for you",
    "comp": "the pay is below what you need",
    "employment_type": "the employment type is not one you want",
    "licensure": "needs a license you do not hold",
    "other": "the model found a dealbreaker",
    # Consulting track (P2CBlocker).
    "rate": "the rate is below what you need",
    "duration": "the engagement length does not suit you",
    "sector": "the sector is one you do not take on",
    "geography": "the location does not work for you",
    "conflict_of_interest": "it may conflict with your other work",
    "capacity": "you do not have the capacity for it",
}
_LEGIT_WORDS = {
    "legitimacy_reject": "Capped because the posting may not be a real opening.",
    "legitimacy_suspect_cap": "Capped because the posting looks suspect.",
}


def _humanize(raw: str) -> str:
    return " ".join(raw.replace("_", " ").replace("-", " ").split()).lower()


def forced_words(forced_by: str | None) -> str:
    """A stored `forced_by` as one operator sentence (never the raw ids, never repeated clauses).

    Known blocker kinds and legitimacy caps get plain phrases; an unknown id is humanized (its
    `blocker:` prefix dropped, underscores to spaces) instead of shown raw. Empty in, empty out.
    The stored value is untouched."""
    text = (forced_by or "").strip()
    if not text:
        return ""
    if text in _FORCED_SENTENCES:
        return _FORCED_SENTENCES[text]
    phrases: list[str] = []
    legit: list[str] = []
    known = False

    def add(phrase: str) -> None:
        if phrase and phrase not in phrases:
            phrases.append(phrase)

    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.lower().startswith("blocker:"):
            known = True
            for kind in part[len("blocker:"):].split(","):
                key = kind.strip().lower()
                if key:
                    add(_BLOCKER_WORDS.get(key) or _humanize(key))
        elif part in _LEGIT_WORDS:
            if _LEGIT_WORDS[part] not in legit:
                legit.append(_LEGIT_WORDS[part])
        elif part in _FORCED_SENTENCES:
            add(_FORCED_SENTENCES[part].rstrip("."))
        else:
            add(_humanize(part))
    out: list[str] = []
    if phrases:
        lead = "Capped by your rules" if known else "Adjusted by a rule"
        out.append(f"{lead}: {'; '.join(phrases)}.")
    # The posting-looks-suspect caps come from the legitimacy check, not from the operator's
    # rules, so they are worded apart from them.
    out.extend(("Also c" + w[1:]) if out else w for w in legit)
    return " ".join(out)


# Stripe/pill color per fit word: never green next to red in shade (the old keep/kill pills).
_TONE = {STRONG: "good", GOOD: "good", MAYBE: "warn", YOUR_CALL: "warn", NOT_FIT: "bad",
         NEW: "idle"}


@dataclass(frozen=True)
class Fit:
    word: str  # one of STRONG, GOOD, MAYBE, NOT_FIT, YOUR_CALL, NEW
    group: str  # one of GROUP_*
    tone: str  # good | warn | bad | idle
    score: float | None  # the one fit number shown on a row, or None
    why: str  # one short clause: "model and score disagree", "title rule", ""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def rule_word(rules: Any) -> str:
    """The first Tier 0 rule name as the operator would say it ("title", "pay")."""
    if isinstance(rules, str):
        rules = [rules]
    for r in rules or []:
        # Tier 0 stores stage_reason as "blocker:<rule>: detail". Only the rule-name segment is
        # read (review finding 13): the detail quotes the posting, and a word in it ("title",
        # "stale") must not name the rule.
        text = str(r).strip().lower()
        if text.startswith("blocker:"):
            text = text[len("blocker:"):]
        key = text.split(":", 1)[0].strip()
        if not key:
            continue
        if key in _RULE_IDS:
            return _RULE_IDS[key]
    return "rule"


def fit_for(
    evaluation: Any, stage: str | None, *, t0_rules: Any = None, live: bool = False
) -> Fit:
    """Translate the latest evaluation plus the opportunity stage into the operator's word.

    `evaluation` may be None, an Evaluation row or a dict with verdict / score / weighted_score /
    needs_review / tier. `stage` is the opportunity's stage string. `live` is whether the role
    has a pending or leased scoring task: a parked role queued to score again is Being scored,
    one with nothing queued is Scoring failed (D-UI-56)."""
    verdict = _get(evaluation, "verdict")
    tier = _get(evaluation, "tier")
    score = _get(evaluation, "score")
    if score is None:
        score = _get(evaluation, "weighted_score")
    needs_review = bool(_get(evaluation, "needs_review", False))

    if stage in ("quarantined", "error"):
        # Scoring stopped without a new score: Scoring failed (or Being scored again once a task
        # is queued), whatever an older evaluation (or the operator, see
        # kestrel.web.queries.row_fit) said. Hard rule 4: never shown as a no.
        if live:
            return Fit(NEW, GROUP_NEW, _TONE[NEW], None, "")
        return Fit(NEW, GROUP_FAILED, _TONE[NEW], None, FAILED_WHY)
    if stage == "t0_reject":
        return Fit(NOT_FIT, GROUP_NOT_FIT, _TONE[NOT_FIT], None, f"{rule_word(t0_rules)} rule")
    if verdict == "kill" or stage == "t1_reject":
        return Fit(NOT_FIT, GROUP_NOT_FIT, _TONE[NOT_FIT], None, "quick screen")
    if verdict in ("apply", "pursue", "consider", "pass") and tier != 1:
        if needs_review:
            # "Your call" only when the two scores land on different sides of a line that matters
            # (owner round 14: a 1.0 against a 1.8 read as a maybe). Both below the consider line
            # is not a fit; both at the apply line or above is a good or strong fit.
            # A verdict forced to pass (a blocker, a likely fake posting) never reads as a fit,
            # whatever the scores (round 14 review).
            pair = score_pair(evaluation)
            if pair is not None and max(pair) < LINES["consider"]:
                return Fit(NOT_FIT, GROUP_NOT_FIT, _TONE[NOT_FIT], score, "")
            if (pair is not None and min(pair) >= LINES["apply"]
                    and verdict in ("apply", "pursue")):
                word = STRONG if min(pair) >= STRONG_FLOOR else GOOD
                return Fit(word, GROUP_WORTH, _TONE[word], score, "")
            return Fit(YOUR_CALL, GROUP_WORTH, _TONE[YOUR_CALL], score, "model and score disagree")
        if verdict in ("apply", "pursue"):
            word = STRONG if (score is not None and score >= STRONG_FLOOR) else GOOD
            return Fit(word, GROUP_WORTH, _TONE[word], score, "")
        if verdict == "consider":
            return Fit(MAYBE, GROUP_MAYBE, _TONE[MAYBE], score, "")
        return Fit(NOT_FIT, GROUP_NOT_FIT, _TONE[NOT_FIT], score, "")
    # Tier 1 keep, new, t0_pass: not scored yet. Hard rule 4: never shown as a no.
    return Fit(NEW, GROUP_NEW, _TONE[NEW], None, "")


def operator_group(
    operator_verdict: str | None, *, pursued: bool = False, pursuit_status: str | None = None
) -> str | None:
    """The group the operator's own decision puts a role in, or None to let the model decide
    (D-UI-51). Applies only to a role already scored: one still being scored or failed (fit_for's
    GROUP_NEW or GROUP_FAILED: unscored, quarantined, waiting on a tier) stays there, so callers
    check that before this (kestrel.web.queries.row_fit). A role on the shortlist (an open
    pursuit; a closed one has left the shortlist) is Worth a look while it is only shortlisted
    and Applied once its status is applied or later (db.enums.APPLIED_OR_LATER, D-UI-51 amended);
    otherwise the operator's latest label outranks the model: apply/pursue to Worth a look,
    consider to Maybe, pass to Not a fit. kestrel.web.queries._group_expr is the same rule in
    SQL."""
    if pursued:
        return GROUP_APPLIED if pursuit_status in APPLIED_OR_LATER else GROUP_WORTH
    if operator_verdict in ("apply", "pursue"):
        return GROUP_WORTH
    if operator_verdict == "consider":
        return GROUP_MAYBE
    if operator_verdict == "pass":
        return GROUP_NOT_FIT
    return None


def verdict_for_fit(word: str, track: str | None = None) -> tuple[str, float]:
    """The stored verdict and training score for an operator's fit word. Raises ValueError for
    anything outside FIT_WORDS: the form offers only those four, so anything else is tampering."""
    if word not in _FIT_TO_VERDICT:
        raise ValueError(f"not a fit word: {word!r}")
    verdict, score = _FIT_TO_VERDICT[word]
    if verdict == "apply" and track == "consulting":
        verdict = "pursue"
    return verdict, score


def fit_word_for_verdict(verdict: str | None, score: float | None = None) -> str | None:
    """The reverse map for stored operator labels (an override row's operator_verdict)."""
    if verdict in ("apply", "pursue"):
        return STRONG if (score is not None and score >= STRONG_FLOOR) else GOOD
    if verdict == "consider":
        return MAYBE
    if verdict == "pass":
        return NOT_FIT
    return None


# ------------------------------------------------------------------ why a Tier 0 rule set it aside
# D-UI-73: the rule, the concrete detail and where the operator changes it, in one sentence.
# Tier 0 stores only "blocker:<rule>: <reason>"; the detail (the title's level word, the floor,
# the pay, the place) is computed here at render time from the opportunity and the profile, so
# nothing is migrated and the sentence follows the operator's current profile.

PLACES: dict[str, tuple[str, str]] = {
    "employment": ("Profile > Employment", "/profile?tab=employment&track=employment"),
    "consulting": ("Profile > Consulting", "/profile?tab=consulting&track=consulting"),
    "about": ("Profile > About you", "/profile?tab=about&track={track}"),
    "location": ("Profile > Location", "/profile?tab=location&track={track}"),
    "core": ("Settings > Core", "/settings#core"),
    "filters": ("Settings > Filters", "/settings"),
}

# rule id -> (where it is changed, whose rule it is). "the" for rules that ship with Kestrel and
# no profile field sets (clearance), "your" for everything the operator's profile or settings set.
_RULE_HOME: dict[str, tuple[str, str]] = {
    "comp_floor": ("employment", "your"),
    "sponsorship": ("about", "your"),
    "clearance": ("filters", "the"),
    "anti_title": ("employment", "your"),
    "seniority_floor": ("employment", "your"),
    "geo": ("location", "your"),
    "stale": ("core", "your"),
    "rate_floor": ("consulting", "your"),
    "too_short": ("consulting", "your"),
    "excluded_sector": ("consulting", "your"),
    "stale_trigger": ("core", "your"),
}
# The rule's name inside the sentence where the short word would read oddly.
_SENTENCE_WORD = {"anti_title": "excluded titles", "clearance": "security clearance"}

# seniority_rank's ladder (SPEC.md 6.1) as level names.
LEVEL_LABELS = {
    0: "Individual contributor", 1: "Lead", 2: "Manager", 3: "Senior Manager", 4: "Director",
    5: "Senior Director", 6: "Vice President", 7: "Senior Vice President", 8: "C-level",
}


@dataclass(frozen=True)
class RuleWhy:
    rule: str  # the rule id ("seniority_floor"), or "" when the stored reason has none
    word: str  # rule_word ("seniority")
    text: str  # "Set aside by your seniority rule: the title reads as Director, and ..."
    verb: str  # "Change it in" or "See it in"
    place: str  # "Profile > Employment"
    href: str  # "/profile?tab=employment&track=employment"

    @property
    def sentence(self) -> str:
        """The whole thing as plain text (a note that cannot carry a link)."""
        return f"{self.text} {self.verb} {self.place}."

    @property
    def override_note(self) -> str:
        """What the role says once the operator chose to score it anyway (D-UI-74)."""
        if self.rule not in _RULE_HOME:
            return "You asked to score this despite your rules."
        owner = _RULE_HOME[self.rule][1]
        name = _SENTENCE_WORD.get(self.rule, self.word)
        return f"You asked to score this despite {owner} rule on {name}."


def rule_id(stage_reason: str | None) -> str:
    """The rule id from a stored Tier 0 reason ("blocker:seniority_floor: ..."), or ""."""
    text = (stage_reason or "").strip()
    if text.lower().startswith("blocker:"):
        text = text[len("blocker:"):]
    return text.split(":", 1)[0].strip().lower()


def _money(n: Any) -> str:
    try:
        return f"${float(n):,.0f}"
    except (TypeError, ValueError):
        return str(n)


def _days(value: Any) -> int | None:
    from kestrel.rules.helpers import AGE_NONE_SENTINEL, age_days

    d = age_days(value)
    return None if d >= AGE_NONE_SENTINEL else d


def _detail(rule: str, opp: Any, profile: Any, settings: Any, org: Any) -> str:
    """The concrete clause for `rule`, or a plain generic one when the facts are not there."""
    from kestrel.rules import helpers

    eterms = _get(profile, "employment_terms") or {}
    cterms = _get(profile, "consulting_terms") or {}
    if rule == "seniority_floor":
        rank, _words = helpers.seniority_match(_get(opp, "title"))
        floor = helpers.seniority_rank(_get(eterms, "seniority_floor"))
        if rank >= 0 and floor >= 0:
            return (f"the title reads as {LEVEL_LABELS[rank]}, and your lowest level is "
                    f"{LEVEL_LABELS[floor]}")
        return "the title reads as below the lowest level you set"
    if rule == "comp_floor":
        cap, floor = _get(opp, "comp_max"), _get(eterms, "comp_floor_usd")
        if cap is not None and floor is not None:
            return (f"the top of the posted pay is {_money(cap)}, below your floor of "
                    f"{_money(floor)}")
        return "the posted pay is below your floor"
    if rule == "rate_floor":
        cap, floor = _get(opp, "comp_max"), _get(cterms, "day_rate_floor")
        if cap is not None and floor is not None:
            return (f"the top of the posted rate is {_money(cap)}, below your day rate floor "
                    f"of {_money(floor)}")
        return "the posted rate is below your day rate floor"
    if rule == "anti_title":
        title = str(_get(opp, "title") or "").lower()
        for term in _get(eterms, "anti_titles") or []:
            if isinstance(term, str) and term.strip() and term.lower() in title:
                return f'the title contains "{term.strip()}", one of your excluded titles'
        return "the title contains one of your excluded titles"
    if rule == "sponsorship":
        return ("the posting says it will not sponsor a visa, and your profile says you need "
                "sponsorship")
    if rule == "clearance":
        return "the posting asks for an active security clearance"
    if rule == "geo":
        remote = _get(opp, "remote_type")
        if remote == "remote":
            country = _get(opp, "country")
            where = f" in {country}" if country else ""
            return f"it is remote{where}, outside the countries you listed"
        place = _get(opp, "location_raw") or ", ".join(
            str(x) for x in (_get(opp, "city"), _get(opp, "state")) if x
        )
        kind = {"onsite": "on site", "hybrid": "hybrid"}.get(str(remote or ""), "")
        if place:
            lead = f"it is {kind} in {place}" if kind else f"it is in {place}"
            return f"{lead}, outside the places you listed"
        return "its location is outside the places you listed"
    if rule == "stale":
        days, limit = _days(_get(opp, "date_posted")), _get(settings, "max_posting_age_days")
        if days is not None and limit is not None:
            return f"it was posted {days} days ago, and your limit is {limit} days"
        return "it was posted longer ago than the oldest posting you keep"
    if rule == "stale_trigger":
        days = _days(_get(opp, "event_date"))
        limit = _get(_get(settings, "consulting"), "max_trigger_age_days")
        if days is not None and limit is not None:
            return (f"the event behind it was {days} days ago, past your {limit}-day window")
        return "the event behind it is older than your window"
    if rule == "too_short":
        data = _get(opp, "trigger_data") or {}
        d, low = _get(data, "engagement_days"), _get(cterms, "min_engagement_days")
        if d is not None and low is not None:
            return f"the engagement is {d} days, shorter than your minimum of {low}"
        return "the engagement is shorter than your minimum"
    if rule == "excluded_sector":
        sector = _get(org, "sector")
        if sector:
            return f"the company's sector, {sector}, is one you excluded"
        return "the company's sector is one you excluded"
    return ""


def explain_rule(
    stage_reason: str | None,
    *,
    opp: Any = None,
    profile: Any = None,
    settings: Any = None,
    org: Any = None,
) -> RuleWhy:
    """Why Tier 0 set a role aside, in the operator's words, with where to change the rule
    (D-UI-73). Never raises; an unknown rule falls back to Settings > Filters."""
    rule = rule_id(stage_reason)
    word = rule_word(stage_reason) if stage_reason else "rule"
    track = str(_get(opp, "track") or "employment")
    home, owner = _RULE_HOME.get(rule, ("filters", "the"))
    place, href = PLACES[home]
    href = href.format(track=track if track in ("employment", "consulting") else "employment")
    try:
        detail = _detail(rule, opp, profile, settings, org)
    except Exception:  # a display sentence never breaks a page; the generic one stands in
        detail = ""
    if rule in _RULE_HOME:
        name = _SENTENCE_WORD.get(rule, word)
        text = f"Set aside by {owner} {name} rule" + (f": {detail}." if detail else ".")
        verb = "See it in" if owner == "the" else "Change it in"
    else:
        text = "Set aside by one of your rules" + (
            f" ({_humanize(rule)})." if rule else "."
        )
        verb = "See your rules in"
    return RuleWhy(rule=rule, word=word, text=text, verb=verb, place=place, href=href)
