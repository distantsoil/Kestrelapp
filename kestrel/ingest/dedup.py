"""Deduplication (SPEC.md 5.5): exact content hash, near-exact title-token Jaccard within an org,
and canonical-source priority (company ATS > company page > Indeed > Google > ZipRecruiter >
LinkedIn > Glassdoor). There is no semantic (embedding) dedup pass: dedup is the
content hash of org, title and description plus the title-token Jaccard grouping."""

from __future__ import annotations

import hashlib
import re

_URL_RE = re.compile(r"https?://\S+")
_TRACK_RE = re.compile(r"[?&](utm_[^=]+|gh_src|src|ref)=[^&\s]*", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")
# Only true stop words. Seniority markers (senior/sr/jr/lead/staff/principal) are KEPT so
# "Senior Software Engineer" does not collapse onto "Software Engineer" and hide a distinct req.
_STOP = {"the", "a", "an", "of", "and", "for", "to", "in", "at", "with", "on"}

# Lower rank = more canonical. ATS platforms tie at 0; ties break by lowest opportunity id.
_PRIORITY = {
    "greenhouse": 0,
    "lever": 0,
    "ashby": 0,
    "workday": 0,
    "smartrecruiters": 0,
    "rippling": 0,
    "page": 1,
    "indeed": 2,
    "jobspy": 2,
    "google": 3,
    "zip_recruiter": 4,
    "ziprecruiter": 4,
    "linkedin": 5,
    "glassdoor": 6,
    "manual": 7,
    "import": 7,
    "rss": 7,
    "sec_edgar": 8,
    "sam_gov": 8,
}


def source_rank(kind: str | None) -> int:
    # An unmapped kind is LEAST canonical (9), never outranking a known board.
    return _PRIORITY.get(kind or "", 9)


def normalize_desc(desc: str | None) -> str:
    if not desc:
        return ""
    s = _TRACK_RE.sub("", desc.lower())
    s = _URL_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip()


def content_hash(org: str | None, title: str | None, description: str | None) -> str:
    o = (org or "").strip().lower()
    t = (title or "").strip().lower()
    key = f"{o}|{t}|{normalize_desc(description)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def title_tokens(title: str | None) -> set[str]:
    return {t for t in _TOKEN_RE.split((title or "").lower()) if t and t not in _STOP}


def significant_title_tokens(title: str | None, n: int = 4) -> tuple[str, ...]:
    toks = [t for t in _TOKEN_RE.split((title or "").lower()) if t and t not in _STOP]
    return tuple(sorted(toks)[:n])


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
