"""Plain-writing check (issue #93, D-UI-232). Pure functions, no model, no database, no network.

`check(text)` lints one string of GENERATED prose and returns the violations it finds. It never
rewrites anything. The flow around it (kestrel/llm/plain_repair.py) is: the model's JSON has
already been parsed and validated by its schema (hard rule 3); this module only reads named string
fields of that validated object, so it never extracts content from a raw model response.

Text that is not the model's is never judged: operator-typed text, `doc_variant.text`, an
`operator_edited` story and `kind="note"` sketches are not passed here at all, and operator text
quoted inside generated output (and any quoted job-ad text) is skipped by span: `check` blanks
every quoted span and every `exempt` string before it looks.

Rules, by what they do:
- blocking (the model is asked once to say it another way): em or en dash used as punctuation,
  tier 1 words, filler phrases, "serves as" style verbs.
- flag only (marked for the person, never re-asked): "not X, but Y" and setup-and-punch contrast,
  a closing line that states a lesson, groups of three single words, and tier 2 words once two or
  more appear in the same piece of writing.
The word lists are in config/writing_words.yml (shipped default, the person's copy wins).

Credit: ideas from the open source avoid-ai-writing skill by Conor Bronsdon (MIT). No text or code
is copied."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import structlog
import yaml

log = structlog.get_logger()

BLOCKING_RULES = frozenset({"dash", "banned_word", "filler", "plain_verb"})
FLAG_ONLY_RULES = frozenset({"contrast", "closer", "triplet", "tier2_cluster"})

RULE_LABELS = {
    "dash": "dash used as punctuation",
    "banned_word": "stock word",
    "filler": "filler phrase",
    "plain_verb": "stock verb",
    "contrast": "contrast framing",
    "closer": "closing line that states a lesson",
    "triplet": "group of three",
    "tier2_cluster": "stock words that cluster",
}

# Operator text shorter than this is not treated as an exempt span (a two letter bullet would
# blank every match of those letters).
_MIN_EXEMPT = 6


@dataclass(frozen=True)
class Violation:
    rule: str
    phrase: str
    start: int
    end: int
    field: str = ""

    @property
    def blocking(self) -> bool:
        return self.rule in BLOCKING_RULES

    def as_flag(self) -> dict[str, str]:
        """The JSON stored on a row and shown as an inline mark."""
        return {"kind": "style", "rule": self.rule, "field": self.field, "phrase": self.phrase}


# ----------------------------------------------------------------------------------- word lists
@lru_cache(maxsize=1)
def _load_words_cached(path_key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from kestrel import paths

    raw: dict[str, Any] = {}
    try:
        p = paths.config_file("writing_words.yml")
        if p is not None:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("writing_words_unreadable", error=str(exc)[:200])
    if not isinstance(raw, dict):
        raw = {}

    def words(*keys: str) -> tuple[str, ...]:
        out: list[str] = []
        for k in keys:
            v = raw.get(k)
            if isinstance(v, list):
                out += [str(w).strip().lower() for w in v if str(w).strip()]
        return tuple(dict.fromkeys(out))

    return words("tier1", "add_tier1"), words("tier2", "add_tier2")


def clear_word_cache() -> None:
    _load_words_cached.cache_clear()
    _compiled.cache_clear()


def _word_pattern(word: str) -> str:
    """A word and its common endings: elevate, elevates, elevated, elevating; synergy, synergies;
    passionate, passionately; drive, drives, driving, driven."""
    w = re.escape(word)
    if " " in word:
        return rf"\b{w}\b"
    stem = re.escape(word[:-1]) if word[-1] in "ey" else w
    return rf"\b{stem}(?:e|es|ed|en|ing|s|ly|ely|y|ies|ness)?\b"


# "leverage" is a stock verb but a plain noun in negotiation ("your leverage in the talk"), so only
# the verb forms are matched: -ed/-ing/-s always, the bare form only after a word that takes a verb.
_LEVERAGE_VERB = re.compile(
    r"\b(?:leverag(?:ed|es|ing)|(?:to|will|would|can|could|should|may|might|i|we|you|they|and|"
    r"then|also)\s+leverage)\b",
    re.IGNORECASE,
)
_DASH = re.compile(r"[\u2014\u2013]|\s--\s")
# The one allowance: an UNSPACED en dash between two range tokens (numbers, money, Q1 to Q4, month
# and day abbreviations as whole tokens), such as a year range or Mon to Fri written with it. A
# spaced dash, an em dash, or a word that only starts like a month or day ("Marketing", "Monday")
# is always a violation.
_RANGE_DASH = "\u2013\u2012"
_RANGE_TOKEN = re.compile(
    r"^(?:\$?\d[\d,.]*(?:[KkMmBb]|%|x)?|Q[1-4]|"
    r"Mon|Tue|Wed|Thu|Fri|Sat|Sun|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)$"
)
_FILLER = re.compile(
    r"\bin order to\b|\bit(?:'s| is) important to note\b|\bit is worth noting\b|\bmoreover\b|"
    r"\bfurthermore\b|\b(?:could|may|might) potentially\b",
    re.IGNORECASE,
)
_PLAIN_VERB = re.compile(
    r"\bserv(?:es|ed|ing) as\b|\bstand(?:s|ing)? as (?:a |an |the )?(?:testament|proof)\b|"
    r"\bstands as\b|\bboast(?:s|ed|ing)?\b",
    re.IGNORECASE,
)
_CONTRAST = re.compile(
    r"\bnot (?:just |only |merely |simply )?[^.;:!?\n]{1,70}?,? but (?!also\b)|"
    r"\b(?:wasn't|weren't|isn't|aren't|was not|is not) (?:about |just |only )?[^.;:!?\n]{1,60}?,"
    r" (?:it|they|this|that) (?:was|were|is|are)\b|"
    r"\bthe (?:easy|easier|simple|simpler|hard|harder|difficult|tough|tougher) part (?:was|is)\b",
    re.IGNORECASE,
)
_CLOSER = re.compile(
    r"\bis what (?:let|made|makes|lets|allowed|allows)\b|\bthe real (?:lesson|win|work|story)\b|"
    r"\bthe lesson (?:was|is)\b|\btaught me that\b|\bat the end of the day\b|"
    r"\bit (?:just )?goes to show\b",
    re.IGNORECASE,
)
_TRIPLET = re.compile(r"\b([A-Za-z][\w'-]*), ([A-Za-z][\w'-]*),? (?:and|or) ([A-Za-z][\w'-]*)\b")
_QUOTED = re.compile(r"\"[^\"\n]{2,}\"|“[^”\n]{2,}”")


@lru_cache(maxsize=1)
def _compiled() -> tuple[re.Pattern[str], re.Pattern[str]]:
    tier1, tier2 = _load_words_cached("")
    t1 = re.compile("|".join(_word_pattern(w) for w in tier1) or r"(?!)", re.IGNORECASE)
    t2 = re.compile("|".join(_word_pattern(w) for w in tier2) or r"(?!)", re.IGNORECASE)
    return t1, t2


def _blank(text: str, exempt: Iterable[str]) -> str:
    """The text with every quoted span and every exempt string replaced by spaces of the same
    length, so offsets still point into the original."""
    out = text
    for m in _QUOTED.finditer(text):
        out = out[: m.start()] + " " * (m.end() - m.start()) + out[m.end():]
    for ex in exempt:
        ex = (ex or "").strip()
        if len(ex) >= _MIN_EXEMPT:
            out = re.sub(re.escape(ex), lambda mm: " " * len(mm.group(0)), out)
    return out


def _is_range_dash(scan: str, m: re.Match[str]) -> bool:
    if m.group(0) not in _RANGE_DASH:  # an em dash or a spaced double hyphen is never a range
        return False
    left = re.search(r"(\S+)$", scan[: m.start()])  # no space between the token and the dash
    right = re.match(r"(\S+)", scan[m.end():])
    if not left or not right:
        return False
    strip = ".,;:)(\"'"
    return bool(
        _RANGE_TOKEN.match(left.group(1).strip(strip))
        and _RANGE_TOKEN.match(right.group(1).strip(strip))
    )


def _last_sentence_start(text: str) -> int:
    body = text.rstrip()
    cut = max(body.rfind(". ", 0, len(body) - 1), body.rfind("\n"))
    return cut + 1 if cut >= 0 else 0


def check(text: str, *, exempt: Iterable[str] = (), field: str = "") -> list[Violation]:
    """Violations in one generated string. Empty for empty text. Never changes the text."""
    if not text or not text.strip():
        return []
    scan = _blank(text, tuple(exempt))
    t1, t2 = _compiled()
    found: list[Violation] = []

    def add(rule: str, pattern: re.Pattern[str]) -> None:
        for m in pattern.finditer(scan):
            phrase = text[m.start():m.end()].strip()
            found.append(Violation(rule, phrase, m.start(), m.end(), field))

    for m in _DASH.finditer(scan):
        if _is_range_dash(scan, m):
            continue
        phrase = text[m.start():m.end()].strip()
        found.append(Violation("dash", phrase, m.start(), m.end(), field))
    add("banned_word", t1)
    add("banned_word", _LEVERAGE_VERB)
    add("filler", _FILLER)
    add("plain_verb", _PLAIN_VERB)
    add("contrast", _CONTRAST)
    add("triplet", _TRIPLET)
    tail = _last_sentence_start(scan)
    for m in _CLOSER.finditer(scan, tail):
        phrase = text[m.start():m.end()].strip()
        found.append(Violation("closer", phrase, m.start(), m.end(), field))
    tier2_hits = list(t2.finditer(scan))
    if len(tier2_hits) >= 2:
        for m in tier2_hits:
            found.append(
                Violation("tier2_cluster", text[m.start():m.end()], m.start(), m.end(), field)
            )
    found.sort(key=lambda v: (v.start, v.rule))
    return found


# ------------------------------------------------------------------------------- prose registry
# prompt version -> the string fields (of the already schema-validated object) that are generated
# prose. A path walks dicts by key; "[]" means every item of a list. Prompts not listed here are
# never checked (labels, selection, import, advisory lists). Operator text is never named here.
PROSE_FIELDS: dict[str, tuple[str, ...]] = {
    "cover/v2": ("body_md", "opening_alternatives[]"),
    "engagement_outline/v2": (
        "body_md", "assumptions[]", "exclusions[]", "commercial_shape.caveat",
    ),
    "story_consolidate/v2": ("situation", "task", "action", "result", "reflection"),
    "mock_critique/v2": (
        "worked_well", "improvements[]", "revised_opening", "next_question_rationale",
    ),
    "negotiate/v2": (
        "situation_read", "scripts[].script", "scripts[].likely_pushback",
        "scripts[].response_to_pushback", "risks[]",
    ),
    "analytics_summary/v2": (
        "summary_md", "headline_finding", "one_thing_to_change", "evidence_for_it",
    ),
    "interview_questions/v2": ("questions[].why_asked",),
    "org_summary/v2": ("sentences[].text",),
    "follow_up/v2": ("body", "checks[]"),
}


def _walk(obj: Any, parts: list[str], trail: str) -> Iterable[tuple[str, str]]:
    if not parts:
        if isinstance(obj, str):
            yield trail, obj
        return
    head, rest = parts[0], parts[1:]
    if head.endswith("[]"):
        key = head[:-2]
        seq = obj.get(key) if isinstance(obj, Mapping) else None
        if isinstance(seq, list):
            for i, item in enumerate(seq):
                yield from _walk(item, rest, f"{trail}{'.' if trail else ''}{key}[{i}]")
        return
    if isinstance(obj, Mapping) and head in obj:
        yield from _walk(obj[head], rest, f"{trail}{'.' if trail else ''}{head}")


def prose_strings(prompt_version: str, data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The (path, text) pairs of generated prose in a validated object, per PROSE_FIELDS."""
    out: list[tuple[str, str]] = []
    for path in PROSE_FIELDS.get(prompt_version, ()):
        out.extend(_walk(data, path.split("."), ""))
    return out


def check_object(
    prompt_version: str, data: Mapping[str, Any], *, exempt: Iterable[str] = ()
) -> list[Violation]:
    """Every violation across the prose fields of a validated object. The tier 2 cluster rule is
    judged per field here, which is where the text is read as one piece of writing."""
    exempt = tuple(exempt)
    found: list[Violation] = []
    for path, text in prose_strings(prompt_version, data):
        found.extend(check(text, exempt=exempt, field=path))
    return found


# ----------------------------------------------------------------------------------- grounding
_MULTIPLIERS = {
    "k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9,
}
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
}
# "1,250" is grouped thousands only when every group after the comma has three digits, so "2,5"
# reads as 2 and 5, never 25.
_NUMBER = re.compile(
    r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(%|percent|thousand|million|billion|[kKmMbB])(?![a-zA-Z]))?",
)
_WORD_NUMBER = re.compile(r"\b(" + "|".join(_WORD_NUMBERS) + r")\b", re.IGNORECASE)
_TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_ONES = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_COMPOUND = re.compile(
    r"\b(" + "|".join(_TENS) + r")[-\s](" + "|".join(_ONES) + r")\b", re.IGNORECASE
)


def _compounds(text: str) -> tuple[list[tuple[str, int]], str]:
    """("twenty-five", 25) pairs found in `text`, and the text with them blanked so the single
    words inside are not read again as 20 and 5."""
    found = [
        (m.group(0), _WORD_NUMBERS[m.group(1).lower()] + _WORD_NUMBERS[m.group(2).lower()])
        for m in _COMPOUND.finditer(text or "")
    ]
    return found, _COMPOUND.sub(lambda m: " " * len(m.group(0)), text or "")


def _num_key(value: float) -> str:
    return f"{value:.6g}"


def _number_keys(text: str, *, words_from: int = 1) -> list[set[str]]:
    """One set of acceptable keys per number in `text`: a value, and for a percentage also the
    `%` form (so "4 percent" in a story is supported by "4%" or a bare 4 in a source).
    Number words count from `words_from` (a story's bare "one" is a pronoun, not a figure)."""
    out: list[set[str]] = []
    for m in _NUMBER.finditer(text or ""):
        value = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        if unit in ("%", "percent"):
            out.append({_num_key(value) + "%", _num_key(value)})
        else:
            out.append({_num_key(value * _MULTIPLIERS.get(unit, 1.0))})
    pairs, rest = _compounds(text or "")
    out += [{_num_key(float(n))} for _tok, n in pairs]
    for m in _WORD_NUMBER.finditer(rest):
        n = _WORD_NUMBERS[m.group(1).lower()]
        if n >= words_from:
            out.append({_num_key(float(n))})
    return out


def ungrounded_details(
    fields: Mapping[str, str], sources: Iterable[str]
) -> list[dict[str, str]]:
    """Numbers in generated story fields that appear in none of the `sources` (the sketches and
    proof points the story was merged from). Returns flags of kind "ungrounded":
    {"kind", "field", "phrase"}. Numbers are compared by normalized value: $500K, 500,000 and
    500000 are one number, "three" is 3, "4 percent" is "4%". Named details are NOT checked
    (D-UI-236 amendment). Deterministic; reads text only."""
    pool: set[str] = set()
    for src in sources:
        for keys in _number_keys(src or ""):
            pool |= keys
    flags: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field, text in fields.items():
        if not text:
            continue
        for m in _NUMBER.finditer(text):
            keys = _number_keys(m.group(0))
            token = m.group(0).strip()
            if keys and not (keys[0] & pool) and (field, token) not in seen:
                seen.add((field, token))
                flags.append({"kind": "ungrounded", "field": field, "phrase": token})
        pairs, rest = _compounds(text)
        words = list(pairs) + [
            (m.group(0), _WORD_NUMBERS[m.group(1).lower()]) for m in _WORD_NUMBER.finditer(rest)
        ]
        for token, n in words:
            key = _num_key(float(n))
            if n >= 2 and key not in pool and (field, token.lower()) not in seen:
                seen.add((field, token.lower()))
                flags.append({"kind": "ungrounded", "field": field, "phrase": token})
    return flags


def operator_spans(text: str) -> list[str]:
    """The sentences and clauses of the person's own text, for the `exempt` argument of `check`:
    when generated output quotes part of what they wrote, that part is theirs, not the model's."""
    whole = (text or "").strip()
    if not whole:
        return []
    parts = [whole]
    parts += [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", whole)]
    parts += [p.strip() for p in re.split(r"[,;:]\s+|(?<=[.!?])\s+|\n+", whole)]
    return list(dict.fromkeys(p for p in parts if len(p) >= _MIN_EXEMPT))


def describe(flag: Mapping[str, Any]) -> str:
    """One plain line for a stored flag, for a title attribute or a repair request."""
    kind = flag.get("kind")
    phrase = str(flag.get("phrase") or "")
    if kind == "ungrounded":
        return f"\"{phrase}\" is not in your sketches or proof points. Is it right?"
    if kind == "style_dropped":
        return f"{phrase} stock phrases are not marked"
    if kind == "unsupported":
        return f"No source for: {phrase}"
    label = RULE_LABELS.get(str(flag.get("rule")), "style")
    return f"{label}: \"{phrase}\""
