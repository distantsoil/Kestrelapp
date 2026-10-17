"""Organization resolution and attribute inference (SPEC.md 5.4). Orgs are deduplicated by
normalized name so signals accumulate across every opportunity. size_band/sector/ownership are
inferred from the payload and description and PERSISTED, because four consulting Tier 0 rules key
off them; unknown is left as None rather than guessed."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Org

_SUFFIX_RE = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|gmbh|plc|s\.a|ag|pty|holdings|group)\b\.?",
    re.IGNORECASE,
)
_NONWORD_RE = re.compile(r"[^a-z0-9]+")

_OWNERSHIP = [
    (
        "pe_backed",
        re.compile(r"private[- ]equity|pe[- ]backed|portfolio company|carve[- ]out", re.I),
    ),
    ("public", re.compile(r"publicly[- ]traded|\b(nyse|nasdaq|lse)\b|public company", re.I)),
    ("vc_backed", re.compile(r"venture[- ]backed|series\s+[a-e]\b|vc[- ]backed", re.I)),
    ("nonprofit", re.compile(r"non[- ]?profit|501\(c\)|not[- ]for[- ]profit", re.I)),
    ("government", re.compile(r"\bgovernment\b|public sector|municipal|federal agency", re.I)),
]
_SECTOR = [
    (
        "healthcare",
        re.compile(r"health\s?care|hospital|clinical|patient|hipaa|hitrust|payer|provider", re.I),
    ),
    (
        "financial_services",
        re.compile(r"financial services|fintech|banking|insurance|payments|capital markets", re.I),
    ),
    ("manufacturing", re.compile(r"manufactur|industrial|factory|plant floor|supply chain", re.I)),
    (
        "professional_services",
        re.compile(r"professional services|advisory|consultanc|staffing", re.I),
    ),
    ("technology", re.compile(r"\bsaas\b|software company|platform company|cloud[- ]native", re.I)),
    ("retail", re.compile(r"\bretail\b|e-?commerce|consumer goods|omnichannel", re.I)),
]


def normalized_name(name: str | None) -> str:
    if not name:
        return ""
    s = _SUFFIX_RE.sub(" ", name.lower())
    s = _NONWORD_RE.sub(" ", s).strip()
    return re.sub(r"\s+", " ", s)


def infer_org_attrs(raw, description: str | None) -> dict[str, str]:
    """Prefer the adapter's structured hints; fall back to conservative keyword inference over the
    org name + title + description. Only high-signal patterns; otherwise leave the key absent."""
    out: dict[str, str] = {}
    for key in ("size_band", "sector", "ownership"):
        val = getattr(raw, key, None)
        if val:
            out[key] = val
    blob = " ".join(filter(None, [raw.org_name, raw.title, (description or "")[:2000]]))
    if "ownership" not in out:
        for label, pat in _OWNERSHIP:
            if pat.search(blob):
                out["ownership"] = label
                break
    if "sector" not in out:
        for label, pat in _SECTOR:
            if pat.search(blob):
                out["sector"] = label
                break
    return out


async def resolve_org(
    session: AsyncSession,
    *,
    name: str | None,
    domain: str | None = None,
    attrs: dict[str, str] | None = None,
) -> Org:
    """Get-or-create an org by normalized name. Fills missing attributes but never overwrites a
    known value with a later guess."""
    norm = normalized_name(name) or (name or "unknown").lower()
    org = (
        await session.execute(select(Org).where(Org.normalized_name == norm))
    ).scalar_one_or_none()
    now = utcnow_iso()
    attrs = attrs or {}
    if org is None:
        org = Org(
            name=name or norm,
            normalized_name=norm,
            domain=domain,
            size_band=attrs.get("size_band"),
            sector=attrs.get("sector"),
            ownership=attrs.get("ownership"),
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(org)
        await session.flush()
        return org
    for key in ("size_band", "sector", "ownership"):
        if getattr(org, key) is None and attrs.get(key):
            setattr(org, key, attrs[key])
    if domain and not org.domain:
        org.domain = domain
    org.last_seen_at = now
    return org
