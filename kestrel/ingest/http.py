"""Shared async HTTP client for ingestion: a descriptive User-Agent on every request, per-host
rate limiting, backoff on 429/403, and optional robots.txt respect (constraint 7). ATS adapters
share one instance so rate limits are enforced across the whole run, not per adapter."""

from __future__ import annotations

import asyncio
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx


def _safe_url(url: str) -> str:
    """The URL with its query string (and fragment) dropped, for error messages. A query string can
    carry a credential (an API key), and an error string travels into ScrapeRun.error,
    notifications, and logs; nothing this client raises may embed one."""
    return url.split("#", 1)[0].split("?", 1)[0]


def has_contact_address(user_agent: str | None) -> bool:
    """True when a User-Agent carries a real contact address: an `@` and no example.com
    placeholder. SEC EDGAR requires a descriptive UA with a contact (hard rule 7), and the shipped
    default is a placeholder the operator must replace in settings.yml (ingest.user_agent)."""
    ua = (user_agent or "").strip()
    return "@" in ua and "example.com" not in ua.lower()


def _raise_for_status(r: httpx.Response) -> None:
    """raise_for_status with the query string stripped from the message (httpx embeds the full
    URL, which would leak a query-string credential into the error text)."""
    if r.is_success:
        return
    raise httpx.HTTPStatusError(
        f"{r.status_code} {r.reason_phrase} from {_safe_url(str(r.request.url))}",
        request=r.request,
        response=r,
    )


class _HostLimiter:
    """Minimum interval between requests to one host."""

    def __init__(self) -> None:
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str, rate_per_sec: float) -> None:
        if rate_per_sec <= 0:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            earliest = self._next.get(host, 0.0)
            if now < earliest:
                await asyncio.sleep(earliest - now)
            self._next[host] = max(earliest, time.monotonic()) + 1.0 / rate_per_sec


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        default_rate_per_sec: float = 2.0,
        timeout: float = 60.0,
        respect_robots: bool = True,
        max_attempts: int = 3,
    ) -> None:
        self.user_agent = user_agent
        # Attempts per request including the first. The company board lookup sets 1 so its
        # probe count is the request count (review finding 11).
        self.max_attempts = max(1, max_attempts)
        self.default_rate = default_rate_per_sec
        self.respect_robots = respect_robots
        self._limiter = _HostLimiter()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _robots_ok(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = await self._client.get(f"{base}/robots.txt", timeout=10.0)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:
                rp.parse([])  # unreachable robots.txt -> allow (fail open, politely rate-limited)
            self._robots[base] = rp
        rp = self._robots[base]
        return rp is None or rp.can_fetch(self.user_agent, url)

    async def request(
        self,
        method: str,
        url: str,
        *,
        rate_per_sec: float | None = None,
        headers: dict | None = None,
        json: dict | None = None,
        check_robots: bool = False,
        retries: int | None = None,
        follow_redirects: bool | None = None,
    ) -> httpx.Response:
        """One polite request. `follow_redirects=False` returns a 3xx as it is, for a caller that
        must vet each hop itself (a link the operator pasted, where a redirect can land on a login
        page or another host); None keeps the client default (follow)."""
        if check_robots and not await self._robots_ok(url):
            raise PermissionError(f"robots.txt disallows {_safe_url(url)}")
        host = urlparse(url).netloc
        rate = rate_per_sec if rate_per_sec is not None else self.default_rate
        last_exc: Exception | None = None
        for attempt in range(retries if retries is not None else self.max_attempts):
            await self._limiter.wait(host, rate)
            try:
                extra = {} if follow_redirects is None else {"follow_redirects": follow_redirects}
                r = await self._client.request(method, url, headers=headers, json=json, **extra)
                if r.status_code in (429, 403, 503):
                    await asyncio.sleep(min(2**attempt, 8))
                    last_exc = httpx.HTTPStatusError(
                        f"{r.status_code} from {_safe_url(url)}", request=r.request, response=r
                    )
                    continue
                return r
            except httpx.TransportError as exc:
                last_exc = exc
                await asyncio.sleep(min(2**attempt, 8))
        if last_exc:
            raise last_exc
        raise RuntimeError(f"request to {_safe_url(url)} failed without an exception")

    async def get_json(self, url: str, **kw) -> object:
        r = await self.request("GET", url, **kw)
        _raise_for_status(r)
        return r.json()

    async def post_json(self, url: str, *, json: dict, **kw) -> object:
        r = await self.request("POST", url, json=json, **kw)
        _raise_for_status(r)
        return r.json()

    async def get_text(self, url: str, **kw) -> str:
        r = await self.request("GET", url, **kw)
        _raise_for_status(r)
        return r.text
