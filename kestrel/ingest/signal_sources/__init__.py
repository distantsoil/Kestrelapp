"""Signal-source registry, keyed by source.kind. The parent's SEC EDGAR reference registers
unconditionally; the fan-out siblings (SAM.gov, PE portfolio, RSS) register themselves once their
modules land, so the foundation commit is importable with EDGAR alone."""

from __future__ import annotations

from kestrel.ingest.signal_base import SignalSource

# The consulting signal-source kinds, static so the runner ROUTES a source here even before its
# adapter module lands (an unregistered kind then yields a clean partial, never a misroute).
SIGNAL_SOURCE_KINDS = frozenset({"sec_edgar", "sam_gov", "pe_portfolio", "rss"})

_SIGNAL_SOURCES: dict[str, SignalSource] = {}


def register(source: SignalSource) -> None:
    _SIGNAL_SOURCES[source.kind] = source


def get_signal_source(kind: str) -> SignalSource | None:
    return _SIGNAL_SOURCES.get(kind)


def known_signal_kinds() -> list[str]:
    return sorted(_SIGNAL_SOURCES)


def _register_all() -> None:
    from kestrel.ingest.signal_sources.sec_edgar import SecEdgarSignalSource

    register(SecEdgarSignalSource())

    # Fan-out siblings: present after Phase 4 step 2. Import defensively so the foundation commit
    # (EDGAR only) still imports, and each self-contained adapter registers when its module exists.
    for module_name, class_name in (
        ("sam_gov", "SamGovSignalSource"),
        ("pe_portfolio", "PePortfolioSignalSource"),
        ("rss", "RssSignalSource"),
    ):
        try:
            mod = __import__(f"kestrel.ingest.signal_sources.{module_name}", fromlist=[class_name])
        except ImportError:
            continue
        register(getattr(mod, class_name)())


_register_all()
