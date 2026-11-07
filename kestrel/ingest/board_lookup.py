"""Find a company's public job board from its name (D-SRC-3).

The operator types "Acme Robotics, Inc." and does not know, or care, which applicant tracking
system Acme uses. This module derives a few likely board identifiers from the name (lowercased,
spaces removed or hyphenated, legal suffixes such as Inc and LLC dropped, and the typed casing for
the two platforms whose identifiers are case sensitive) and asks the public Greenhouse, Lever,
Ashby and SmartRecruiters endpoints, through the same adapters a scan uses, until one answers with
at least one job.

This is an operator-initiated lookup of public endpoints only (hard rule 7): the shared
HttpClient sends the descriptive User-Agent and rate-limits per host, the number of requests is
bounded (MAX_PROBES), and it stops at the first board that answers. SmartRecruiters is asked for
one posting only, because its adapter fetches a detail page per posting. Nothing here writes to the
database; the Sources page decides what to do with a match."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.base import AdapterError, SourceConfigError

log = structlog.get_logger()

# Legal and filler suffixes dropped from the end of a company name before building identifiers.
SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation", "co",
    "company", "plc", "gmbh", "ag", "sa", "bv", "pty", "holdings",
})
PLATFORM_NAMES = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters",
}
MAX_PROBES = 12  # the hard ceiling on requests one lookup may send (plus one SR detail)


@dataclass(frozen=True)
class BoardMatch:
    kind: str
    config: dict
    jobs: int | None  # None when the platform was only asked for one posting
    platform: str


@dataclass
class LookupResult:
    company: str
    match: BoardMatch | None
    tried: list[str]  # "lever:acme", for the plain failure message and the log
    # True when nothing matched but at least one probe could not get a definite answer (the
    # network was down, a timeout, a 429 or 5xx). That is not "no board here" and must never be
    # remembered as one.
    inconclusive: bool = False


_PAREN = re.compile(r"\(([^()]*)\)")
_ALIAS_SPLIT = re.compile(r"[/,;&]")
_ALIAS_LEAD = re.compile(r"^\s*(?:formerly|fka|f/k/a|dba|d/b/a|now|part of|a division of)\s+", re.I)


def name_variants(name: str) -> list[str]:
    """The names to probe, plain name first. "Acme Foods (Northwind Group / Palmex)" is the plain
    "Acme Foods", then each name inside the parentheses. A name with no parentheses is itself
    (#123 finding 3: "Company (Qualifier)" used to become the slug "companyqualifier")."""
    plain = " ".join(_PAREN.sub(" ", name).split()) or name.strip()
    out: list[str] = []
    seen: set[str] = set()
    for item in [plain] + [
        _ALIAS_LEAD.sub("", part)
        for inner in _PAREN.findall(name)
        for part in _ALIAS_SPLIT.split(inner)
    ]:
        key = "".join(w.lower() for w in _words(item))
        if key and key not in seen:
            seen.add(key)
            out.append(" ".join(item.split()))
    return out


def _words(name: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", name.replace("'", ""))
    while words and words[-1].lower() in SUFFIXES:
        words.pop()
    return words


def candidate_slugs(name: str) -> dict[str, list[str]]:
    """Likely identifiers per platform, most likely first. Empty when the name has no letters
    or digits left once the suffixes are gone."""
    variants = name_variants(name)
    words = _words(variants[0] if variants else name)
    if not words:
        return {}
    joined = "".join(w.lower() for w in words)
    hyphen = "-".join(w.lower() for w in words)
    typed = "".join(words)  # Ashby and SmartRecruiters identifiers are case sensitive
    pascal = "".join(w[:1].upper() + w[1:] for w in words)

    def uniq(*items: str) -> list[str]:
        out: list[str] = []
        for i in items:
            if i and i not in out:
                out.append(i)
        return out

    return {
        "greenhouse": uniq(joined, hyphen),
        "lever": uniq(joined, hyphen),
        "ashby": uniq(typed, pascal, joined, hyphen),
        "smartrecruiters": uniq(typed, pascal, joined),
    }


def _probes(name: str) -> list[tuple[str, dict, str]]:
    """(kind, config, variant name) in the order they are tried: the most likely identifier on
    every platform for the plain name, then for each name in parentheses, then the next most
    likely identifiers; MAX_PROBES caps the lot."""
    variants = name_variants(name)
    per_variant = [candidate_slugs(v) for v in variants]
    key = {"greenhouse": "token", "lever": "company", "ashby": "token",
           "smartrecruiters": "company"}
    out: list[tuple[str, dict, str]] = []
    seen: set[tuple] = set()
    depth = max((len(o) for slugs in per_variant for o in slugs.values()), default=0)
    for i in range(depth):
        for variant, slugs in zip(variants, per_variant, strict=True):
            for kind in ("greenhouse", "lever", "ashby", "smartrecruiters"):
                options = slugs.get(kind) or []
                if i < len(options) and (kind, options[i]) not in seen:
                    seen.add((kind, options[i]))
                    config: dict[str, Any] = {key[kind]: options[i]}
                    if kind == "smartrecruiters":
                        config["max_results"] = 1
                    out.append((kind, config, variant))
    return out[:MAX_PROBES]


def _transient(exc: BaseException | None) -> bool:
    """True for a failure that says nothing about whether the board exists: no connection, a
    timeout, or the site asking us to slow down or failing on its side."""
    seen = 0
    while exc is not None and seen < 4:  # an adapter wraps the HTTP error as its __cause__
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            return code in (403, 429) or code >= 500
        exc = exc.__cause__
        seen += 1
    return False


async def find_company_board(name: str, http: Any) -> LookupResult:
    """Try each candidate until a board answers with at least one job. A 404, a refused request,
    a non-JSON body or an empty board is simply "not this one"; nothing here raises for them."""
    variants = name_variants(name)
    company = " ".join(_words(variants[0])) if variants else name.strip()
    tried: list[str] = []
    unanswered = 0
    for kind, config, variant in _probes(name):
        adapter = get_adapter(kind)
        if adapter is None:
            continue
        ident = config.get("token") or config.get("company")
        tried.append(f"{kind}:{ident}")
        try:
            postings = await adapter.fetch(config, http)
        except (httpx.HTTPError, AdapterError, SourceConfigError, ValueError, PermissionError,
                RuntimeError) as exc:
            log.debug("board_lookup_miss", kind=kind, ident=ident, error=type(exc).__name__)
            unanswered += _transient(exc)
            continue
        if not postings and getattr(postings, "partial_error", None):
            unanswered += 1  # the board slowed us down before answering
        if postings:
            saved = {k: v for k, v in config.items() if k != "max_results"}
            company = " ".join(_words(variant)) or company
            if kind == "ashby":
                saved["company_name"] = company
            jobs = None if kind == "smartrecruiters" else len(postings)
            log.info("board_lookup_found", kind=kind, ident=ident, jobs=jobs)
            return LookupResult(company, BoardMatch(kind, saved, jobs, PLATFORM_NAMES[kind]),
                                tried)
    log.info("board_lookup_none", company=company, tried=len(tried))
    return LookupResult(company, None, tried, inconclusive=unanswered > 0)
