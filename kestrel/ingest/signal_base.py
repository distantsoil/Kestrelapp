"""The signal-source contract (SPEC.md 5.3). A signal source maps ONE consulting source's payload
into canonical RawSignal objects. Like SourceAdapter it never touches the database or the shared
canonicalization; the signal runner does that. This is the seam that lets SAM.gov, the PE portfolio
crawler, and the RSS adapter be written independently against the SEC EDGAR worked example.

Two output kinds share one protocol so all four sources implement the SAME contract:
  - signal_kind == "trigger": a public fact to classify via P10 (EDGAR, RSS, PE portfolio). The
    runner runs trigger classification, accumulates the signal on the org, and promotes per the
    three-weak-signals rule.
  - signal_kind == "posting": a ready consulting opportunity, e.g. a SAM.gov RFP. The runner builds
    a RawPosting from it and runs the same normalize_and_store path the employment adapters use, on
    the consulting track. No model is involved for a posting.
The reused SourceConfigError / AdapterError keep the failure semantics identical to Phase 3."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from kestrel.ingest.base import AdapterError, SourceConfigError  # re-exported for signal sources

__all__ = ["RawSignal", "SignalSource", "SourceConfigError", "AdapterError"]


@dataclass
class RawSignal:
    """Canonical intermediate from a consulting signal source. Adapters fill what their payload
    provides; None means unknown.

    Trigger fields (signal_kind='trigger'): `text` is the excerpt P10 classifies; `source_url` is
    the public filing/page; `external_id` (e.g. an EDGAR accession:filename) makes re-observation
    idempotent. Posting fields (signal_kind='posting'): title/org/url/description_md plus optional
    comp/location, used to build a RawPosting."""

    signal_kind: Literal["trigger", "posting"] = "trigger"
    org_name: str | None = None
    org_url: str | None = None
    source_url: str | None = None
    external_id: str | None = None
    title: str | None = None
    # The raw signal text to classify (trigger) or the description (posting).
    text: str | None = None
    published_date: str | None = None  # ISO date/datetime string
    form: str | None = None  # e.g. "8-K"
    item: str | None = None  # e.g. "5.02" | "1.05"
    # Posting-only extras (SAM.gov RFPs); ignored for triggers.
    location_raw: str | None = None
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    comp_interval: str | None = None
    naics: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SignalSource(Protocol):
    """kind matches source.kind. validate_config is PURE (structural) and raises SourceConfigError
    (a missing SAM.gov key surfaces here so the runner can DISABLE the source cleanly rather than
    fail the batch). fetch does the network I/O via the shared HttpClient and returns signals."""

    kind: str

    def validate_config(self, config: dict) -> None: ...

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]: ...
