"""Near titles (D-TTL-1): one normalizer for every place a target title is matched or searched.

"IT Director", "Director of IT" and "Director, Information Technology" are the same job to the
operator. A plain lowercase comparison treats them as three. This module reduces a title to a
set of canonical tokens so word order, punctuation, filler words and common abbreviations stop
mattering for MATCHING, and builds the few other spellings a job board search should also try.

Normalizing, in order:
1. lowercase; punctuation becomes a space (`+` and `#` are kept, so "C++" and "C#" survive);
2. filler words drop out (of, the, and, &, for, a, an; "-" and "," are punctuation already);
3. each abbreviation in ABBREVIATIONS expands to its long form ("vp" to "vice president");
4. a light plural fold ("services" to "service"), applied to both sides alike;
5. multi-word concepts become one token ("information technology", "vice president",
   "registered nurse"), longest first, so "Director of Information Security and Technology"
   never reads as containing "information technology";
6. FAMILIES folds two explicit near-synonyms onto one token (people operations onto human
   resources, software developer onto software engineer);
7. "technology" counts as IT ONLY in a bare level title: "<level> of Technology",
   "<level>, Technology" or "<level> Technology" ("Vice President, Technology" is "VP of IT").
   In any longer title ("Information Security and Technology", "Technology Sales",
   "Educational Technology") it stays its own word. Seniority words are never folded: "Head of
   IT" stays apart from "VP of IT", and Director stays apart from Vice President.

Matching is token-set based: `near_equal` when the sets are the same, `near_contains` when the
term's set is inside the title's. Both are used ONLY for soft signals (Tier 0's
`title_near_equals_any` and `title_near_any`). Every hard blocker, Tier 0's anti_title above
all, and `title_equals_any` keep strict matching: a broader match there would reject more roles,
and a wrong rejection is invisible (hard rule 4).

Search variants (`phrasings`, D-TTL-2) never reorder words: boards handle word order. A variant
only swaps a searchable abbreviation for its other form (IT and Information Technology, VP and
Vice President, RN and Registered Nurse) or one curated family spelling for another
(FAMILY_SPELLINGS), and it is kept only when it normalizes to exactly the title's token set.

The tables are field-agnostic on purpose (CLAUDE.md: any job seeker). Every entry is an
abbreviation that means one thing in a job title in any field; ambiguous ones ("PM" is product
or project, "PR" is public relations or pull request, "UI" is user interface or unemployment
insurance, "Admin" is administrator or administrative, "Acct" is account or accounting, "PT" is
physical therapist or part time) are left out rather than guessed."""

from __future__ import annotations

import re
from functools import lru_cache

FILLER = frozenset({"of", "the", "and", "&", "for", "a", "an"})

# (short, long, searchable). Expanded both ways for matching. `searchable` marks the pairs whose
# both forms are common in real postings, so a search variant may swap one for the other; "Sr"
# or "Mgr" match fine but are not worth a search of their own.
ABBREVIATIONS: tuple[tuple[str, str, bool], ...] = (
    # Levels and general words
    ("VP", "Vice President", True),
    ("SVP", "Senior Vice President", True),
    ("EVP", "Executive Vice President", True),
    ("AVP", "Assistant Vice President", True),
    ("Sr", "Senior", False),
    ("Jr", "Junior", False),
    ("Mgr", "Manager", False),
    ("Dir", "Director", False),
    ("Asst", "Assistant", False),
    ("Assoc", "Associate", False),
    ("Exec", "Executive", False),
    ("Mgmt", "Management", False),
    ("Dept", "Department", False),
    ("Svc", "Service", False),
    ("Svcs", "Services", False),
    ("GM", "General Manager", True),
    # Executive titles
    ("CEO", "Chief Executive Officer", True),
    ("CFO", "Chief Financial Officer", True),
    ("COO", "Chief Operating Officer", True),
    ("CIO", "Chief Information Officer", True),
    ("CTO", "Chief Technology Officer", True),
    ("CMO", "Chief Marketing Officer", True),
    ("CISO", "Chief Information Security Officer", True),
    ("CHRO", "Chief Human Resources Officer", True),
    # Functions
    ("IT", "Information Technology", True),
    ("HR", "Human Resources", True),
    ("Eng", "Engineering", False),
    ("Ops", "Operations", False),
    ("Mktg", "Marketing", False),
    ("QA", "Quality Assurance", True),
    ("BI", "Business Intelligence", True),
    ("UX", "User Experience", True),
    ("SWE", "Software Engineer", False),
    ("SRE", "Site Reliability Engineer", True),
    # Health care
    ("RN", "Registered Nurse", True),
    ("LPN", "Licensed Practical Nurse", True),
    ("NP", "Nurse Practitioner", True),
    ("CNA", "Certified Nursing Assistant", True),
)

# Leading words of a long form that stay their own token, so "Senior Vice President" reads as
# senior + vice president (it contains "VP of IT" the way "Senior Director" contains "Director").
_MODIFIERS = frozenset({"senior", "executive", "assistant", "junior"})

# Explicit near-synonyms (token -> the token it counts as). Small on purpose; never a level.
FAMILIES: dict[str, str] = {
    "people_operation": "human_resource",
    "software_developer": "software_engineer",
}
# "technology" counts as IT only when the whole title is a level plus "Technology" (step 7).
_TECH = "technology"
_IT = "information_technology"
# Normalized tokens that make up a level ("Senior Vice President", "Head", "General Manager").
_LEVEL_TOKENS = frozenset({
    "vice_president", "president", "director", "manager", "head", "supervisor", "chief",
    "general_manager", "senior", "junior", "executive", "assistant", "associate", "principal",
    "deputy", "interim", "lead", "staff", "managing",
})
# Curated family spellings a search variant may swap one for another (both ways). Each pair
# normalizes to the same token where it is used; a swap that does not (Technology in a longer
# title) is dropped by the token-set check in phrasings.
FAMILY_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("Technology", "IT"),
    ("People Operations", "HR"),
    ("Software Developer", "Software Engineer"),
)

_PUNCT = re.compile(r"[^\w+#]+")


def _singular(tok: str) -> str:
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


_DOTTED = re.compile(r"\b((?:[a-z]\.){2,})")  # "i.t." and "h.r." read as "it" and "hr"


def _words(text: str) -> list[str]:
    low = _DOTTED.sub(lambda m: m.group(1).replace(".", ""), text.lower().replace("_", " "))
    return [w for w in _PUNCT.sub(" ", low).split() if w]


_SHORT_TO_LONG: dict[str, list[str]] = {s.lower(): _words(lng) for s, lng, _ in ABBREVIATIONS}


@lru_cache(maxsize=1)
def _compounds() -> tuple[tuple[str, ...], ...]:
    """Every multi-word concept, after the plural fold, longest first."""
    phrases: set[tuple[str, ...]] = set()
    for _s, lng, _ in ABBREVIATIONS:
        words = [_singular(w) for w in _words(lng)]
        while words and words[0] in _MODIFIERS and len(words) > 2:
            words = words[1:]
        if len(words) > 1 and words[0] not in _MODIFIERS:
            phrases.add(tuple(words))
    phrases.update({("people", "operation"), ("software", "developer")})
    return tuple(sorted(phrases, key=len, reverse=True))


@lru_cache(maxsize=4096)
def normalize(title: str | None) -> tuple[str, ...]:
    """A title as canonical tokens, in title order. Empty for None or a blank title."""
    if not isinstance(title, str):
        return ()
    expanded: list[str] = []
    for w in _words(title):
        if w in FILLER:
            continue
        expanded.extend(_SHORT_TO_LONG.get(w, [w]))
    toks = [_singular(w) for w in expanded if w not in FILLER]
    out: list[str] = []
    i = 0
    compounds = _compounds()
    while i < len(toks):
        for phrase in compounds:
            n = len(phrase)
            if tuple(toks[i:i + n]) == phrase:
                out.append("_".join(phrase))
                i += n
                break
        else:
            out.append(toks[i])
            i += 1
    out = [FAMILIES.get(t, t) for t in out]
    if (
        len(out) >= 2 and out[-1] == _TECH and _TECH not in out[:-1]
        and all(t in _LEVEL_TOKENS for t in out[:-1])
    ):
        out[-1] = _IT  # "<level> of Technology", "<level>, Technology", "<level> Technology"
    return tuple(out)


def token_set(title: str | None) -> frozenset[str]:
    return frozenset(normalize(title))


def near_equal(a: str | None, b: str | None) -> bool:
    """The same title, whatever the order, punctuation, filler or abbreviation."""
    sa, sb = token_set(a), token_set(b)
    return bool(sa) and sa == sb


def near_contains(title: str | None, term: str | None) -> bool:
    """Every canonical token of `term` is in `title` ("Director of IT Services" contains
    "IT Director"). Soft signals only; never for excluded titles (module docstring)."""
    st, sterm = token_set(title), token_set(term)
    return bool(sterm) and sterm <= st


def near_equal_any(title: str | None, terms) -> bool:
    return any(near_equal(title, t) for t in terms or [] if isinstance(t, str) and t.strip())


def near_contains_any(title: str | None, terms) -> bool:
    return any(near_contains(title, t) for t in terms or [] if isinstance(t, str) and t.strip())


# ------------------------------------------------------------------------------- search variants
_SEARCH_PAIRS: dict[str, str] = {}
for _short, _long, _ok in ABBREVIATIONS:
    if _ok:
        _SEARCH_PAIRS[_short.casefold()] = _long
        _SEARCH_PAIRS[_long.casefold()] = _short


def _alternation(keys) -> re.Pattern[str]:
    # Longest first, whole words only, so a swapped-in long form is never swapped again
    # ("CHRO" becomes "Chief Human Resources Officer", not "Chief HR Officer").
    return re.compile(
        r"(?<![\w&])("
        + "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
        + r")(?![\w&])",
        re.IGNORECASE,
    )


_SWAP_RE = _alternation(_SEARCH_PAIRS)
_FAMILY_PAIRS: dict[str, str] = {}
for _a, _b in FAMILY_SPELLINGS:
    _FAMILY_PAIRS[_a.casefold()] = _b
    _FAMILY_PAIRS[_b.casefold()] = _a
_FAMILY_RE = _alternation(_FAMILY_PAIRS)


def _swaps(text: str, pattern: re.Pattern[str], pairs: dict[str, str]) -> list[str]:
    """`text` with every match swapped at once, then with each match swapped on its own."""
    matches = list(pattern.finditer(text))
    if not matches:
        return []

    def swap_at(chosen) -> str:
        out, last = [], 0
        for m in matches:
            if m in chosen:
                out += [text[last:m.start()], pairs[m.group(1).casefold()]]
                last = m.end()
        out.append(text[last:])
        return "".join(out)

    found = [swap_at(matches)]
    if len(matches) > 1:
        found += [swap_at([m]) for m in matches]
    return found


def phrasings(title: str, limit: int = 3) -> list[str]:
    """Up to `limit` other spellings postings commonly use for `title` (D-TTL-2), most complete
    first: every searchable abbreviation swapped for its other form, then each one on its own,
    then a curated family spelling swapped. Word order is never changed (boards handle it).
    Every variant normalizes to exactly the title's token set; one that does not is dropped.
    Never the title itself, never a duplicate (case-insensitive)."""
    title = " ".join(str(title or "").split())
    if not title or limit <= 0:
        return []
    want = token_set(title)
    candidates = _swaps(title, _SWAP_RE, _SEARCH_PAIRS) + _swaps(title, _FAMILY_RE, _FAMILY_PAIRS)
    out: list[str] = []
    seen = {title.casefold()}
    for c in candidates:
        c = " ".join(c.split())
        if not c or c.casefold() in seen or token_set(c) != want:
            continue
        seen.add(c.casefold())
        out.append(c)
        if len(out) >= limit:
            break
    return out
