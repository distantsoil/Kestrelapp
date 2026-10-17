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


def _words(name: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", name.replace("'", ""))
    while words and words[-1].lower() in SUFFIXES:
        words.pop()
    return words


def candidate_slugs(name: str) -> dict[str, list[str]]:
    """Likely identifiers per platform, most likely first. Empty when the name has no letters
    or digits left once the suffixes are gone."""
    words = _words(name)
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


def _probes(name: str) -> list[tuple[str, dict]]:
    """(kind, config) pairs in the order they are tried: the most likely identifier on every
    platform first, then the variants, so a common case answers in four requests or fewer."""
    slugs = candidate_slugs(name)
    key = {"greenhouse": "token", "lever": "company", "ashby": "token",
           "smartrecruiters": "company"}
    out: list[tuple[str, dict]] = []
    depth = max((len(v) for v in slugs.values()), default=0)
    for i in range(depth):
        for kind in ("greenhouse", "lever", "ashby", "smartrecruiters"):
            options = slugs.get(kind) or []
            if i < len(options):
                config: dict[str, Any] = {key[kind]: options[i]}
                if kind == "smartrecruiters":
                    config["max_results"] = 1
                out.append((kind, config))
    return out[:MAX_PROBES]


async def find_company_board(name: str, http: Any) -> LookupResult:
    """Try each candidate until a board answers with at least one job. A 404, a refused request,
    a non-JSON body or an empty board is simply "not this one"; nothing here raises for them."""
    company = " ".join(_words(name)) or name.strip()
    tried: list[str] = []
    for kind, config in _probes(name):
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
            continue
        if postings:
            saved = {k: v for k, v in config.items() if k != "max_results"}
            if kind == "ashby":
                saved["company_name"] = company
            jobs = None if kind == "smartrecruiters" else len(postings)
            log.info("board_lookup_found", kind=kind, ident=ident, jobs=jobs)
            return LookupResult(company, BoardMatch(kind, saved, jobs, PLATFORM_NAMES[kind]),
                                tried)
    log.info("board_lookup_none", company=company, tried=len(tried))
    return LookupResult(company, None, tried)
