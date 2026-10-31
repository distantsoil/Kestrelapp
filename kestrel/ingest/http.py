"""Shared async HTTP client for ingestion: a descriptive User-Agent on every request, per-host
rate limiting, backoff on 429/403, and optional robots.txt respect (constraint 7). ATS adapters
share one instance so rate limits are enforced across the whole run, not per adapter."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import httpx

MAX_PUBLIC_BYTES = 2_000_000  # a page or feed the operator pointed at; a posting is far smaller
PUBLIC_TIMEOUT_S = 15.0
MAX_PUBLIC_HOPS = 5


class UnsafeURLError(ValueError):
    """A link the operator supplied that Kestrel will not fetch: it points at this computer or a
    private network (SSRF), uses another scheme, redirects somewhere like that, or is too big.
    The message is plain language and safe to show."""


UNSAFE_MSG = "That link points to this computer or a private network, so Kestrel will not read it"
TOO_BIG_MSG = "That page is larger than 2 MB, so Kestrel will not read it"


async def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to. A seam for tests (they never touch DNS)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public(addr: str) -> bool:
    """True only for a globally routable address. Loopback, private, link-local, multicast,
    reserved, unspecified and carrier-grade NAT all fail; an IPv4-mapped IPv6 address is judged
    as the IPv4 address it wraps."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def assert_public_url(url: str) -> None:
    """Refuse a user-supplied URL unless it is http(s) and EVERY address its host resolves to is
    public. Call it again for each redirect hop. Raises UnsafeURLError (plain message)."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURLError("The link must start with http:// or https://")
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addrs = [host] if _literal_ip(host) else await _resolve(host, port)
    except OSError as exc:
        raise UnsafeURLError("Kestrel could not find that address") from exc
    if not addrs or not all(_is_public(a) for a in addrs):
        raise UnsafeURLError(UNSAFE_MSG)


def _literal_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


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

    async def _robots_ok(self, url: str, *, public_only: bool = False) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                if public_only:  # the same guard as the page: no hop into a private network
                    r = await self._public_request(
                        "GET", f"{base}/robots.txt", headers=None, json=None, follow=True
                    )
                else:
                    r = await self._client.get(f"{base}/robots.txt", timeout=10.0)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except UnsafeURLError:
                raise
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
        public_only: bool = False,
    ) -> httpx.Response:
        """One polite request. `follow_redirects=False` returns a 3xx as it is, for a caller that
        must vet each hop itself (a link the operator pasted, where a redirect can land on a login
        page or another host); None keeps the client default (follow).

        `public_only=True` is for a URL the operator supplied (a pasted posting, a feed link):
        the host must resolve only to public addresses (SSRF guard), checked again on every
        redirect hop, which is followed by hand; the body is capped at 2 MB and the request at
        15 seconds. A refusal raises UnsafeURLError."""
        if public_only:
            await assert_public_url(url)
        if check_robots and not await self._robots_ok(url, public_only=public_only):
            raise PermissionError(f"robots.txt disallows {_safe_url(url)}")
        host = urlparse(url).netloc
        rate = rate_per_sec if rate_per_sec is not None else self.default_rate
        last_exc: Exception | None = None
        for attempt in range(retries if retries is not None else self.max_attempts):
            await self._limiter.wait(host, rate)
            try:
                if public_only:
                    r = await self._public_request(
                        method, url, headers=headers, json=json,
                        follow=follow_redirects is not False,
                    )
                else:
                    extra = (
                        {} if follow_redirects is None else {"follow_redirects": follow_redirects}
                    )
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

    async def _public_request(
        self, method: str, url: str, *, headers: dict | None, json: dict | None, follow: bool
    ) -> httpx.Response:
        """One request to a user-supplied URL: vet the address, send with redirects off, read at
        most MAX_PUBLIC_BYTES, and (when `follow`) vet and follow each redirect by hand."""
        current = url
        for _ in range(MAX_PUBLIC_HOPS + 1):
            await assert_public_url(current)
            async with self._client.stream(
                method, current, headers=headers, json=json, follow_redirects=False,
                timeout=PUBLIC_TIMEOUT_S,
            ) as resp:
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_PUBLIC_BYTES:
                        raise UnsafeURLError(TOO_BIG_MSG)
                # The body is already decoded, so the encoding and length headers no longer apply.
                kept = [
                    (k, v) for k, v in resp.headers.items()
                    if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
                ]
                out = httpx.Response(
                    resp.status_code, headers=kept, content=bytes(body), request=resp.request,
                )
            if follow and out.is_redirect and out.headers.get("location"):
                current = urljoin(current, out.headers["location"])
                method, json = "GET", None
                continue
            return out
        raise UnsafeURLError("That link redirects too many times")

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
