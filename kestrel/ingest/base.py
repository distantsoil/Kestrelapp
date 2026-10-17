"""The source-adapter contract. An adapter maps ONE platform's payload (with its counter-intuitive
field names, SPEC.md Appendix A) into a list of canonical RawPosting objects. It never touches the
database or the shared canonicalization; normalize.py does that. This clean seam is what lets the
four ATS adapters be written independently."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class SourceConfigError(ValueError):
    """A source config is structurally invalid. Raised at config load / source creation, never at
    scan time, so a dead config never silently produces an empty run."""


class AdapterError(Exception):
    """A recoverable fetch failure (HTTP error, bad slug, upstream shape change). The run becomes
    `partial` with this message; it NEVER aborts the batch (hard rule: uncertainty escalates)."""


@dataclass
class RawPosting:
    """Canonical intermediate. Adapters fill what their payload provides; None means unknown.
    Exactly one of description_html / description_md is expected (normalize prefers md, else
    unescapes+markdownifies html)."""

    external_id: str | None = None
    url: str | None = None
    apply_url: str | None = None
    title: str | None = None
    org_name: str | None = None
    org_url: str | None = None
    location_raw: str | None = None
    description_html: str | None = None
    description_md: str | None = None
    employment_type: str | None = None
    is_remote: bool | None = None
    remote_hint: str | None = None  # workplaceType / "Remote"|"Hybrid"|"On-site"
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    comp_interval: str | None = None  # 'year' | 'month' | 'day' | 'hour'
    date_posted: str | None = None  # ISO date/datetime string
    # Org attribute hints inferred from the payload (Greenhouse metadata[], JobSpy company_* labels,
    # etc.). Left None rather than guessed; four consulting Tier 0 rules key off these.
    size_band: str | None = None
    sector: str | None = None
    ownership: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    """kind matches source.kind. validate_config is PURE (structural) and raises SourceConfigError.
    fetch performs the network I/O via the shared client and returns canonical postings."""

    kind: str

    def validate_config(self, config: dict) -> None: ...

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]: ...
