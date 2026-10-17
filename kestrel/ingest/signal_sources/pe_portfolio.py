"""PE firm portfolio-page crawler signal source (SPEC.md 5.3). A newly listed portfolio company on
a private-equity firm's public portfolio page is a day-one prospect for fractional IT/CIO
leadership: private-equity acquisitions routinely trigger integration, systems standup, or interim
leadership needs shortly after close. This adapter emits one signal_kind="trigger" RawSignal per
portfolio company found on each configured firm page; P10 classifies the `text` downstream, and
"new addition" detection is left to the pipeline's idempotency (a stable `external_id` means
re-observing the same company re-sees rather than re-promotes it).

LEGAL / POLITE (constraint 7): these are public marketing pages, not an API, so every fetch passes
`check_robots=True` and the shared HttpClient's robots.txt check MUST be honored (a disallow raises
PermissionError, which this adapter treats as "skip this firm", never as "ignore robots.txt").
Requests are rate-limited per firm host via the shared HttpClient. No login is ever attempted and
no page beyond the configured portfolio URL is fetched."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup

from kestrel.ingest.signal_base import RawSignal, SourceConfigError

_DEFAULT_RATE = 1.0
_MIN_NAME_LEN = 2
_MAX_NAME_LEN = 60
_MAX_COMPANIES = 200
_NAV_WORDS = {
    "home",
    "about",
    "about us",
    "contact",
    "contact us",
    "team",
    "our team",
    "news",
    "insights",
    "careers",
    "login",
    "log in",
    "sign in",
    "portfolio",
    "our portfolio",
    "investments",
    "people",
    "press",
    "blog",
    "privacy",
    "privacy policy",
    "terms",
    "terms of use",
    "cookie policy",
    "cookies",
    "sitemap",
    "search",
    "menu",
    "subscribe",
    "linkedin",
    "twitter",
    "facebook",
    "instagram",
    "back to top",
    "skip to content",
    "skip to main content",
}

log = structlog.get_logger()


class PePortfolioSignalSource:
    kind = "pe_portfolio"

    def validate_config(self, config: dict) -> None:
        firms = config.get("firms")
        if not isinstance(firms, list) or not firms:
            raise SourceConfigError(
                "pe_portfolio source requires a non-empty 'firms' list of {name, url} entries"
            )
        for entry in firms:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not entry["name"]
                or not isinstance(entry.get("url"), str)
                or not entry["url"]
            ):
                raise SourceConfigError(
                    "pe_portfolio 'firms' entries must be objects with non-empty 'name' and 'url'"
                )

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:
        self.validate_config(config)
        rate = float(config.get("rate_per_sec", _DEFAULT_RATE))
        css_selector = config.get("css_selector")

        signals: list[RawSignal] = []
        for firm in config["firms"]:
            firm_name = firm["name"]
            firm_url = firm["url"]
            try:
                html = await client.get_text(firm_url, rate_per_sec=rate, check_robots=True)
            except Exception as exc:  # one bad/disallowed firm never sinks the whole source
                log.warning(
                    "pe_portfolio.fetch_failed", firm=firm_name, url=firm_url, error=str(exc)
                )
                continue
            for company, link in _extract_companies(html, firm_url, css_selector):
                signals.append(_to_signal(firm_name, firm_url, company, link))
        return signals


def _extract_companies(
    html: str, firm_url: str, css_selector: str | None
) -> list[tuple[str, str | None]]:
    """Return de-duplicated (company_name, link_url) pairs, capped at _MAX_COMPANIES."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []

    if css_selector:
        elements = soup.select(css_selector)
        for el in elements:
            name = _clean_name(el.get_text())
            if not name or name.casefold() in seen:
                continue
            link = _resolve_link(el, firm_url)
            seen.add(name.casefold())
            out.append((name, link))
            if len(out) >= _MAX_COMPANIES:
                break
        return out

    for a in soup.find_all("a"):
        name = _clean_name(a.get_text())
        if not name or not _plausible_company_name(name) or name.casefold() in seen:
            continue
        link = _resolve_link(a, firm_url)
        # Default heuristic: a portfolio company is a LINKED entry to its own page. Requiring a
        # usable, non-social link drops footer/CTA/heading text that would otherwise become fake
        # orgs (whose org.signals feed a consulting Tier 0 bonus). Operators wanting a text-only
        # list configure `css_selector`, which skips this filter.
        if link is None:
            continue
        seen.add(name.casefold())
        out.append((name, link))
        if len(out) >= _MAX_COMPANIES:
            break
    return out


def _clean_name(text: str | None) -> str:
    return " ".join((text or "").split())


def _plausible_company_name(name: str) -> bool:
    if not (_MIN_NAME_LEN <= len(name) <= _MAX_NAME_LEN):
        return False
    if name.casefold() in _NAV_WORDS:
        return False
    return True


_SOCIAL_HOSTS = ("linkedin.", "twitter.", "x.com", "facebook.", "instagram.", "youtube.")


def _resolve_link(el: Any, firm_url: str) -> str | None:
    href = el.get("href") if hasattr(el, "get") else None
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    absolute = urljoin(firm_url, href)
    low = absolute.lower()
    if any(h in low for h in _SOCIAL_HOSTS):
        return None
    return absolute


def _to_signal(firm_name: str, firm_url: str, company: str, link: str | None) -> RawSignal:
    return RawSignal(
        signal_kind="trigger",
        org_name=company,
        source_url=link or firm_url,
        external_id=f"{firm_name}:{company}",
        title=f"{company} · portfolio of {firm_name}",
        text=(
            f"{company} is listed as a portfolio company of {firm_name} (private equity). "
            "A newly acquired portfolio company implies near-term IT integration, standup, or "
            "leadership needs."
        ),
        raw={"firm": firm_name},
    )
