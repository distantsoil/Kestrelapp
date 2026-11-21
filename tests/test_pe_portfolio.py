"""PE portfolio crawler signal source: extract plausible portfolio-company names from a firm's
public portfolio page, excluding nav/boilerplate links, emit one trigger RawSignal per company
with a stable external_id, and never skip the robots.txt check (constraint 7)."""

from __future__ import annotations

import pytest

from kestrel.ingest.signal_base import SourceConfigError
from kestrel.ingest.signal_sources.pe_portfolio import (
    PePortfolioSignalSource,
    _extract_companies,
)

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


FIRM = [{"name": "Example Capital", "url": "https://example-capital.test/portfolio"}]


@pytest.mark.parametrize(
    "selector",
    [
        "div:has(a ~ p)",  # a combinator inside :has(), refused by soupsieve 4 (D-SEC-63)
        "a::before",  # a pseudo-element: soupsieve raises NotImplementedError
        ",".join(["a"] * 9000),  # past soupsieve's selector limit: ValueError
        "ul >",  # incomplete
        "",
        "   ",
        42,
    ],
)
def test_a_selector_the_parser_refuses_is_a_config_error_not_a_fetch_crash(selector):
    """D-SEC-63: the Sources page and the runner stop on SourceConfigError before a fetch, so
    a refused selector must be caught here rather than raise from soup.select mid-run."""
    with pytest.raises(SourceConfigError, match="css_selector"):
        PePortfolioSignalSource().validate_config({"firms": FIRM, "css_selector": selector})


@pytest.mark.parametrize("selector", ["div a", "div > a", "div a[href]", "div:has(> a) > a"])
def test_ordinary_selectors_still_validate_and_find_the_company(selector):
    PePortfolioSignalSource().validate_config({"firms": FIRM, "css_selector": selector})
    html = '<div><a href="/co/example-widgets">Example Widgets</a></div>'
    found = _extract_companies(html, FIRM[0]["url"], selector)
    assert [name for name, _link in found] == ["Example Widgets"]


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


@pytest.mark.parametrize("selector", ["div:has(a ~ p)", "a::before", ",".join(["a"] * 9000)])
def test_saving_a_refused_selector_shows_a_message_not_a_server_error(selector):
    """D-SEC-63: the Sources save path turns the refusal into the operator-facing message."""
    from kestrel.web.pages.sources import _validate_source_config

    message = _validate_source_config("pe_portfolio", {"firms": FIRM, "css_selector": selector})
    assert message is not None
    assert "css_selector" in message or "selector" in message
