"""Adapter registry, keyed by source.kind."""

from __future__ import annotations

from kestrel.ingest.adapters.adp import AdpAdapter
from kestrel.ingest.adapters.ashby import AshbyAdapter
from kestrel.ingest.adapters.greenhouse import GreenhouseAdapter
from kestrel.ingest.adapters.jobspy import JobSpyAdapter
from kestrel.ingest.adapters.lever import LeverAdapter
from kestrel.ingest.adapters.paylocity import PaylocityAdapter
from kestrel.ingest.adapters.remoterocketship import RemoteRocketshipAdapter
from kestrel.ingest.adapters.rippling import RipplingAdapter
from kestrel.ingest.adapters.smartrecruiters import SmartRecruitersAdapter
from kestrel.ingest.adapters.workable import WorkableAdapter
from kestrel.ingest.adapters.workday import WorkdayAdapter
from kestrel.ingest.base import SourceAdapter

_ADAPTERS: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> None:
    _ADAPTERS[adapter.kind] = adapter


def get_adapter(kind: str) -> SourceAdapter | None:
    return _ADAPTERS.get(kind)


def known_kinds() -> list[str]:
    return sorted(_ADAPTERS)


for _adapter in (
    GreenhouseAdapter(),
    LeverAdapter(),
    AshbyAdapter(),
    WorkdayAdapter(),
    SmartRecruitersAdapter(),
    RipplingAdapter(),
    AdpAdapter(),
    PaylocityAdapter(),
    WorkableAdapter(),
    RemoteRocketshipAdapter(),
    JobSpyAdapter(),
):
    register(_adapter)
