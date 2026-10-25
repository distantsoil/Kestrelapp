"""The complete whitelist of Tier 0 helper functions (SPEC.md 6.1) plus the simpleeval evaluator
and the config-load guard.

Every helper is NULL-SAFE: a None input returns the safe answer (False for predicates, the large
sentinel 999999 for age_days) rather than raising. There is no `any`/`all`; every list operation is
one of these helpers. Do not add generator expressions or comprehensions to filters.yml: the load
guard rejects them at the AST level (check_filters), independent of simpleeval's version. (Note:
EvalWithCompoundTypes in simpleeval 1.0.7 WOULD evaluate a genexpr, so the ban is enforced here, not
by relying on simpleeval to raise.)
"""

from __future__ import annotations

import ast
import operator
import re
from datetime import date, datetime
from typing import Any

import yaml
from simpleeval import EvalWithCompoundTypes

from kestrel.rules.margin import comp_max_near_floor, margin_floor

AGE_NONE_SENTINEL = 999999  # SPEC.md 6.1 helper table: age_days(None)

# seniority ladder (SPEC.md 6.1). Unknown -> -1; the seniority_floor rule guards on `>= 0` so an
# ambiguous title passes (hard rule 4), honoring the helper's "-1 passes every floor check".
_SENIORITY = [
    (
        8,
        r"\b(c[a-z]o|chief\s+\w+\s+officer)\b",
    ),  # CEO/CFO/CIO/CTO/COO/CMO/CRO/... + "chief X officer"
    (7, r"\b(svp|senior\s+vice\s+president)\b"),
    (6, r"\b(vp|vice\s+president)\b"),
    (5, r"\b(senior|sr\.?)\s+director\b"),
    (4, r"\b(director|head\s+of)\b"),
    (3, r"\bsenior\s+manager\b"),
    (2, r"\bmanager\b"),
    (1, r"\blead\b"),
    (0, r"\b(engineer|analyst|administrator|specialist|coordinator|technician|help\s*desk)\b"),
]


# ---------------------------------------------------------------------------------- primitives
def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def _lower(value: Any) -> str:
    return value.lower() if isinstance(value, str) else ""


# --------------------------------------------------------------------------- whitelisted helpers
def regex(text: Any, pattern: str) -> bool:
    if not isinstance(text, str):
        return False
    return re.search(pattern, text) is not None


def age_days(value: Any) -> int:
    if value is None:
        return AGE_NONE_SENTINEL
    d = _to_date(value)
    if d is None:
        return AGE_NONE_SENTINEL
    return (date.today() - d).days


def seniority_match(title: Any) -> tuple[int, str | None]:
    """(rank, the words in the title that gave it) for explaining a seniority_floor rejection
    (kestrel.web.fit). (-1, None) for an unknown title. Not a filters.yml helper."""
    if not isinstance(title, str) or not title.strip():
        return -1, None
    low = title.lower()
    for rank, pattern in _SENIORITY:
        m = re.search(pattern, low)
        if m:
            return rank, title[m.start():m.end()]
    return -1, None


def seniority_rank(title: Any) -> int:
    return seniority_match(title)[0]


def title_contains_any(opp: Any, terms: Any) -> bool:
    """STRICT substring match, lowercased (SPEC.md 6.1). Tier 0's anti_title hard blocker uses
    this in every filters.yml, shipped or seeded, so it never widens: a broader excluded-title
    match would reject more roles (hard rule 4, D-TTL-1). Near matching is title_near_any."""
    title = _lower(getattr(opp, "title", None))
    if not title or not terms:
        return False
    return any_substr(title, terms)


def title_near_any(opp: Any, terms: Any) -> bool:
    """A target title appears in the posting's title, near or exact (D-TTL-1): the strict
    substring match, or every normalized token of a term inside the title's ("Director of IT
    Services" for "IT Director"). Soft signals only."""
    from kestrel.rules import titles

    raw = getattr(opp, "title", None)
    if not _lower(raw) or not terms:
        return False
    return title_contains_any(opp, terms) or titles.near_contains_any(raw, terms)


def title_equals_any(opp: Any, terms: Any) -> bool:
    """STRICT exact match, lowercased (SPEC.md 6.1), as it always was. A rule an operator wrote
    with it, a hard blocker above all, reads exactly as written: "Sr Analyst" never equals
    "Senior Analyst" here. Near equality is title_near_equals_any (D-TTL-1)."""
    title = _lower(getattr(opp, "title", None))
    if not title or not terms:
        return False
    return any(
        title == _lower(t) for t in terms
    )  # exact match over a real list, not a genexpr in YAML


def title_near_equals_any(opp: Any, terms: Any) -> bool:
    """The posting's title is one of `terms`, exactly or as a near title (D-TTL-1): the same
    normalized tokens in any order ("IT Director" equals "Director of IT"). The shipped soft
    signals use it, so the wider match can only raise a score; never use it in a hard blocker."""
    from kestrel.rules import titles

    raw = getattr(opp, "title", None)
    if not _lower(raw) or not terms:
        return False
    return title_equals_any(opp, terms) or titles.near_equal_any(raw, terms)


def desc_contains_any(opp: Any, terms: Any) -> bool:
    desc = _lower(getattr(opp, "description_md", None))
    if not desc or not terms:
        return False
    return any_substr(desc, terms)


def any_substr(haystack: str, terms: Any) -> bool:
    return any(_lower(t) in haystack for t in terms if isinstance(t, str) and t)


def value_in(value: Any, choices: Any) -> bool:
    if value is None or not choices:
        return False
    if isinstance(value, str):
        low = value.lower()
        return any(isinstance(c, str) and c.lower() == low for c in choices)
    return value in choices


def source_kind_in(source: Any, kinds: Any) -> bool:
    kind = getattr(source, "kind", None)
    return value_in(kind, kinds)


def comp_max_below(opp: Any, n: Any) -> bool:
    cap = getattr(opp, "comp_max", None)
    if cap is None or n is None:
        return False
    return cap < n


def comp_max_at_least(opp: Any, n: Any) -> bool:
    cap = getattr(opp, "comp_max", None)
    if cap is None or n is None:
        return False
    return cap >= n


def trigger_int_below(opp: Any, key: str, n: Any) -> bool:
    data = getattr(opp, "trigger_data", None)
    if not isinstance(data, dict) or n is None:
        return False
    val = data.get(key)
    if not isinstance(val, (int, float)):
        return False
    return val < n


def org_signal_count(org: Any, days: int) -> int:
    signals = getattr(org, "signals", None)
    if not signals:
        return 0
    count = 0
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        when = sig.get("event_date") or sig.get("date") or sig.get("observed_at")
        if when is None:
            count += 1  # undated signal still counts (bias to keep)
        elif age_days(when) <= days:
            count += 1
    return count


# ------------------------------------------------------------------------------------ evaluator
class FilterConfigError(ValueError):
    """A rule references a symbol outside the whitelist, or uses an unsupported construct."""


def geo_evaluate(opp: Any, geo_rules: Any) -> tuple[bool, bool]:
    """Name matching, never distance (SPEC.md 6.1). Returns (allowed, geo_uncertain). Order matters.
    `geo_uncertain` is True in exactly the SPEC 6.1 step-5 case: an unknown/unparsed remote type
    that nothing confidently matched but which is allowed anyway (bias to false positives). A
    confident match at steps 1-4 is NOT uncertain."""
    if geo_rules is None:
        return True, False
    remote_type = getattr(opp, "remote_type", None)
    country = getattr(opp, "country", None)
    state = getattr(opp, "state", None)
    city = getattr(opp, "city", None)
    location_raw = getattr(opp, "location_raw", None)

    allowlist = _get(geo_rules, "remote_country_allowlist") or []
    onsite_states = _get(geo_rules, "onsite_states") or []
    onsite_metros = _get(geo_rules, "onsite_metros") or []

    # 1. remote in an allowed (or unparsed) country -> allow. An unknown country on a remote role
    #    must escalate, not reject (hard rule 4); ingestion often leaves country None on "Remote".
    if remote_type == "remote" and (
        not allowlist or country in (None, "") or value_in(country, allowlist)
    ):
        return True, False
    # 1b. no on-site states and no metros listed: the operator gave no on-site location, so an
    #     on-site or hybrid posting is not filtered by location (release round review, finding
    #     1). An empty list must never mean "nowhere is acceptable". Only a remote posting in a
    #     country outside a list the operator typed can still be rejected.
    if remote_type != "remote" and not onsite_states and not onsite_metros:
        return True, False
    # 2. parsed state in the onsite set
    if value_in(state, onsite_states):
        return True, False
    # 3. parsed city matches any metro alias
    aliases = [a for metro in onsite_metros for a in (_get(metro, "aliases") or [])]
    if value_in(city, aliases):
        return True, False
    # 4. location_raw contains any alias as a substring
    if isinstance(location_raw, str) and any(_lower(a) in location_raw.lower() for a in aliases):
        return True, False
    # 5. unknown remote type, nothing matched -> allow (bias to false positives), and flag it
    if remote_type in (None, "unknown"):
        return True, True
    return False, False


def _geo_allows(opp: Any, geo_rules: Any) -> bool:
    return geo_evaluate(opp, geo_rules)[0]


def _get(obj: Any, key: str) -> Any:
    """Attribute-or-key access, so geo_rules works as a dict (YAML) or an object (ORM)."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def build_evaluator(
    *, opp: Any, org: Any, profile: Any, source: Any, settings: Any
) -> EvalWithCompoundTypes:
    geo_rules = _get(profile, "geo_rules") if profile is not None else None
    functions = {
        "regex": regex,
        "age_days": age_days,
        "seniority_rank": seniority_rank,
        "geo_allows": lambda o: _geo_allows(o, geo_rules),
        "title_contains_any": title_contains_any,
        "title_near_any": title_near_any,
        "title_equals_any": title_equals_any,
        "title_near_equals_any": title_near_equals_any,
        "desc_contains_any": desc_contains_any,
        "value_in": value_in,
        "source_kind_in": source_kind_in,
        "comp_max_below": comp_max_below,
        "margin_floor": margin_floor,
        "comp_max_near_floor": comp_max_near_floor,
        "comp_max_at_least": comp_max_at_least,
        "trigger_int_below": trigger_int_below,
        "org_signal_count": org_signal_count,
        "len": len,
        "min": min,
        "max": max,
        "abs": abs,
    }
    names = {"opp": opp, "org": org, "profile": profile, "source": source, "settings": settings}
    # EvalWithCompoundTypes (not plain SimpleEval) so list literals like ['greenhouse', ...] in
    # source_kind_in are allowed. It also evaluates comprehensions/genexprs, which we forbid at the
    # AST level in check_filters (see _assert_no_comprehensions).
    ev = EvalWithCompoundTypes(functions=functions, names=names)
    # `is` / `is not` are already in simpleeval's default operators; set them explicitly so the
    # `x is None` rules keep working even if a future version drops them.
    ev.operators.setdefault(ast.Is, operator.is_)
    ev.operators.setdefault(ast.IsNot, operator.is_not)
    return ev


# A seeded filters.yml (paths.config_file: the operator's copy wins) keeps the soft signals it was
# seeded with. These exact, untouched lines from before D-TTL-1 are read as their near-title
# successors, so an existing install gets near titles too. Only these two soft signals, only the
# exact text: an edited rule, and every hard blocker (anti_title above all), is read as written.
_SOFT_UPGRADES = {
    ("title_exact", "title_equals_any(opp, profile.employment_terms.target_titles)"):
        "title_near_equals_any(opp, profile.employment_terms.target_titles)",
    ("title_contains", "title_contains_any(opp, profile.employment_terms.target_titles)"):
        "title_near_any(opp, profile.employment_terms.target_titles)",
}


# The two pay blockers as shipped before the near-floor margin (D-RULES-2). Only these exact
# untouched lines are read as their margin successors, so an existing install keeps near-floor
# roles too; a blocker the operator edited is read as written. The matching near_floor soft signal
# is appended when a track has the upgraded blocker and no near_floor signal of its own.
_BLOCKER_UPGRADES = {
    ("comp_floor", "comp_max_below(opp, profile.employment_terms.comp_floor_usd)"): (
        "comp_max_below(opp, margin_floor(profile.employment_terms.comp_floor_usd, "
        "profile.employment_terms.comp_floor_margin_pct))",
        "comp_max_near_floor(opp, profile.employment_terms.comp_floor_usd, "
        "profile.employment_terms.comp_floor_margin_pct)",
    ),
    ("rate_floor", "comp_max_below(opp, profile.consulting_terms.day_rate_floor)"): (
        "comp_max_below(opp, margin_floor(profile.consulting_terms.day_rate_floor, "
        "profile.consulting_terms.day_rate_floor_margin_pct))",
        "comp_max_near_floor(opp, profile.consulting_terms.day_rate_floor, "
        "profile.consulting_terms.day_rate_floor_margin_pct)",
    ),
}


def _upgrade_shipped_blockers(data: dict) -> None:
    for groups in data.values():
        if not isinstance(groups, dict):
            continue
        soft = groups.get("soft_signals")
        for rule in groups.get("hard_blockers", []) or []:
            if not isinstance(rule, dict):
                continue
            hit = _BLOCKER_UPGRADES.get((rule.get("id"), rule.get("when")))
            if hit is None:
                continue
            rule["when"] = hit[0]
            if not isinstance(soft, list):
                soft = groups["soft_signals"] = []
            if not any(isinstance(r, dict) and r.get("id") == "near_floor" for r in soft):
                soft.append({"id": "near_floor", "when": hit[1], "weight": 0.0})


def _upgrade_shipped_soft(data: dict) -> None:
    for groups in data.values():
        if not isinstance(groups, dict):
            continue
        for rule in groups.get("soft_signals", []) or []:
            if not isinstance(rule, dict):
                continue
            new = _SOFT_UPGRADES.get((rule.get("id"), rule.get("when")))
            if new is not None:
                rule["when"] = new


def load_filters(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise FilterConfigError(f"filters file {path} is not a mapping")
    _upgrade_shipped_soft(data)
    _upgrade_shipped_blockers(data)
    return data


def iter_rules(filters: dict):
    """Yield (track, kind, rule) for every hard_blocker and soft_signal in the filters config."""
    for track, groups in filters.items():
        if not isinstance(groups, dict):
            continue
        for kind in ("hard_blockers", "soft_signals"):
            for rule in groups.get(kind, []) or []:
                yield track, kind, rule


# Constructs banned from filters.yml so rules stay restricted to whitelisted helpers. List/dict
# LITERALS (ast.List/ast.Dict) are allowed; comprehensions and generator expressions are not.
_BANNED_NODES = (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp, ast.Lambda)


def _assert_no_comprehensions(expr: str, where: str) -> None:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FilterConfigError(f"{where}: syntax error: {exc}  ::  {expr}") from exc
    for node in ast.walk(tree):
        if isinstance(node, _BANNED_NODES):
            raise FilterConfigError(
                f"{where}: {type(node).__name__} is not allowed; use a whitelisted helper "
                f"(SPEC.md 6.1)  ::  {expr}"
            )


def check_filters(filters: dict, *, opp, org, profile, source, settings) -> None:
    """Evaluate every rule's `when:` expression against the given context. Raises FilterConfigError
    naming the offending rule if any rule references a symbol outside the whitelist or uses a banned
    construct (comprehension / generator expression / lambda). This is the guard that keeps a
    dead-on-arrival filters.yml from shipping."""
    ev = build_evaluator(opp=opp, org=org, profile=profile, source=source, settings=settings)
    for track, kind, rule in iter_rules(filters):
        rid = rule.get("id", "<no id>")
        where = f"{track}/{kind}/{rid}"
        expr = rule.get("when")
        if not isinstance(expr, str):
            raise FilterConfigError(f"{where}: missing string `when:`")
        _assert_no_comprehensions(expr, where)
        try:
            ev.eval(expr)
        except FilterConfigError:
            raise
        except Exception as exc:  # simpleeval raises NameNotDefined, FunctionNotDefined, etc.
            raise FilterConfigError(
                f"{track}/{kind}/{rid}: {type(exc).__name__}: {exc}  ::  {expr}"
            ) from exc
