"""PE portfolio crawler signal source: extract plausible portfolio-company names from a firm's
public portfolio page, excluding nav/boilerplate links, emit one trigger RawSignal per company
with a stable external_id, and never skip the robots.txt check (constraint 7)."""

from __future__ import annotations

import pytest

from kestrel.ingest.signal_base import SourceConfigError
from kestrel.ingest.signal_sources.pe_portfolio import PePortfolioSignalSource

_PORTFOLIO_HTML = """
<html><body>
<nav>
  <a href="/">Home</a>
  <a href="/about">About</a>
  <a href="/contact">Contact Us</a>
  <a href="/portfolio">Portfolio</a>
  <a href="/team">Our Team</a>
</nav>
<main>
  <ul>
    <li><a href="/portfolio/acme-robotics">Acme Robotics</a></li>
    <li><a href="/portfolio/globex-systems">Globex Systems</a></li>
    <li><a href="/portfolio/initech-data">Initech Data</a></li>
  </ul>
</main>
<footer>
  <a href="/privacy">Privacy Policy</a>
  <a href="https://linkedin.com/company/example">LinkedIn</a>
</footer>
</body></html>
"""


class _FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def get_text(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return _PORTFOLIO_HTML


def test_validate_config_requires_firms():
    src = PePortfolioSignalSource()
    with pytest.raises(SourceConfigError):
        src.validate_config({})
    with pytest.raises(SourceConfigError):
        src.validate_config({"firms": []})
    with pytest.raises(SourceConfigError):
        src.validate_config({"firms": [{"name": "Example Capital"}]})  # missing url
    src.validate_config(
        {"firms": [{"name": "Example Capital", "url": "https://example-capital.test/portfolio"}]}
    )  # ok


async def test_fetch_returns_trigger_signals_for_companies_and_passes_check_robots():
    src = PePortfolioSignalSource()
    http = _FakeHttp()
    config = {
        "firms": [{"name": "Example Capital", "url": "https://example-capital.test/portfolio"}]
    }
    signals = await src.fetch(config, http)

    assert len(http.calls) == 1
    assert http.calls[0]["check_robots"] is True  # robots.txt MUST be honored (constraint 7)

    names = {s.org_name for s in signals}
    assert names == {"Acme Robotics", "Globex Systems", "Initech Data"}
    for s in signals:
        assert s.signal_kind == "trigger"
        assert s.external_id == f"Example Capital:{s.org_name}"
        assert s.text and s.org_name in s.text and "Example Capital" in s.text
        assert s.raw == {"firm": "Example Capital"}


async def test_nav_and_boilerplate_links_are_excluded():
    src = PePortfolioSignalSource()
    http = _FakeHttp()
    config = {
        "firms": [{"name": "Example Capital", "url": "https://example-capital.test/portfolio"}]
    }
    signals = await src.fetch(config, http)

    names = {s.org_name for s in signals}
    excluded = {"Home", "About", "Contact Us", "Portfolio", "Our Team", "Privacy Policy",
                "LinkedIn"}
    assert names.isdisjoint(excluded)
    assert len(signals) == 3


async def test_one_bad_firm_is_skipped_not_fatal():
    class _FailingHttp:
        async def get_text(self, url, **kw):
            raise PermissionError(f"robots.txt disallows {url}")

    src = PePortfolioSignalSource()
    config = {"firms": [{"name": "Blocked Capital", "url": "https://blocked.test/portfolio"}]}
    signals = await src.fetch(config, _FailingHttp())
    assert signals == []
