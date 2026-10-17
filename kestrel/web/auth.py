"""LAN access control (SPEC-INTERFACES 2.3, AFTERACTION 3.3).

Until now Settings rendered a "Generate shared secret" button whose copy claimed the secret "gates
the non-loopback bind". Nothing read that secret, no middleware existed, and the server bound
127.0.0.1 unconditionally, so the control was decorative. This module makes the claim true.

What this is, stated plainly because the UI has to say it too: ONE shared credential for the whole
application. It is not a login, there are no accounts, and there is no authorization model. Anyone
holding the key has every power the operator has. It exists so a phone on the operator's own LAN or
VPN can reach the app, and it is not sufficient to expose Kestrel to a hostile network.

This is the THIRD control on the request path and subsumes neither of the other two:
  * the cross-origin guard (app.py) stops a third-party PAGE driving the app. The key does not,
    because a hostile page can be made to carry a cookie the browser already holds.
  * TrustedHostMiddleware (app.py) stops DNS rebinding. The key does not, because a rebound
    request arrives on loopback, which is exempt here by design.
  * this key stops an unauthenticated device on the LAN. The other two do not, because such a
    request has a legitimate Host and no Origin at all.

That third point is why the other two must NOT be widened to "any host"/"any origin" when bind_all
is on, which is what the first cut of this feature did. Doing so left all three controls off at
once, since a rebound request also arrives on loopback. Both allowlists are pinned to
local_addresses() instead. See the tests in tests/test_lan_auth.py that pin this.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

log = structlog.get_logger()

COOKIE_NAME = "kestrel_key"
SETTING_KEY = "server.shared_secret"

# /health is exempt so the desktop shell's readiness probe, the extension's discovery probe, and any
# uptime check keep working without the key. It exposes only status, service name, and version.
_EXEMPT_PATHS = frozenset({"/health", "/auth"})

# Loopback is exempt: the operator sitting at the machine is already past every boundary this key
# could enforce, and requiring a key there would break the desktop shell's own webview, the CLI, and
# the MCP server for no gain.
# Numeric peers only: request.client.host is always an address, never a hostname. "localhost" is
# deliberately NOT here, because it would be a trust entry that could silently activate under any
# server that put a hostname there.
_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})


# The key is stored in the `setting` table, but the middleware runs on every request and cannot
# afford a database round trip there. It is cached in the process and refreshed at two points: app
# startup, and whenever Settings generates or rotates it. A cache rather than a snapshot so rotation
# takes effect immediately, with no restart.
# `generation` guards the rotate-during-load race. _load_key_once awaits a database read, and a
# rotation landing inside that await used to be overwritten by the now-stale read: the revoked key
# kept working and the new one did not. Since rotation is the ONLY revocation a single shared
# credential offers, that failure is the one this cache must not have. Any write bumps the counter,
# and a load only stores its result if the counter has not moved.
_cache: dict[str, object] = {"value": None, "loaded": False, "generation": 0}


def cached_key() -> str | None:
    value = _cache["value"]
    return value if isinstance(value, str) else None


def set_cached_key(value: str | None) -> None:
    _cache["value"] = value
    _cache["loaded"] = True
    _cache["generation"] = int(_cache["generation"]) + 1  # type: ignore[arg-type]


def reset_cache() -> None:
    """Forget the loaded key so the next non-loopback request re-reads it. Used after a restore,
    which replaces the whole database underneath us."""
    _cache["value"] = None
    _cache["loaded"] = False
    _cache["generation"] = int(_cache["generation"]) + 1  # type: ignore[arg-type]


async def _load_key_once() -> str | None:
    """Read the stored secret on FIRST NEED, not at startup.

    Deliberately lazy. Loading it in the app lifespan meant every launch opened a database session
    before serving, and the cached engine then held the SQLite WAL file open, which broke restore
    (it replaces the database file, and Windows refuses while a handle is live). A loopback-only
    install now never touches the database for auth at all, because the middleware short-circuits
    on loopback before reaching here.
    """
    if _cache["loaded"]:
        return cached_key()
    generation_before = _cache["generation"]
    try:
        from kestrel.db.engine import get_sessionmaker
        from kestrel.db.models import Setting

        async with get_sessionmaker()() as session:
            row = await session.get(Setting, SETTING_KEY)
        value = row.value if row is not None and isinstance(row.value, str) and row.value else None
    except Exception as exc:
        # Cannot read the key: leave it unloaded so a later request retries, and return None, which
        # the middleware treats as "refuse". Failing closed is the safe direction. Logged because a
        # silent failure here presents as "no shared secret has been generated yet" to every LAN
        # device, which is a lie and gives the operator nothing to diagnose.
        log.warning("lan_key_load_failed", error=str(exc))
        return None
    if _cache["generation"] != generation_before:
        # A rotation landed while this read was in flight. Its value is the current one; this read
        # is stale and must not overwrite it, or the revoked key would keep working.
        return cached_key()
    set_cached_key(value)
    return value


async def current_key() -> str | None:
    """The key in force right now, loading it on first need. For the /auth route."""
    return await _load_key_once()


def generate_key() -> str:
    """A fresh shared secret. token_urlsafe(32) is 256 bits, which is far beyond what a LAN
    attacker could grind, and it stays copy-pasteable and QR-encodable."""
    return secrets.token_urlsafe(32)


def is_loopback(request: Request) -> bool:
    client = request.client.host if request.client else None
    return client in _LOOPBACK_CLIENTS


def _presented_key(request: Request) -> str | None:
    """The key from the header (extension, scripts) or the cookie (a phone that used /auth)."""
    return request.headers.get("X-Kestrel-Key") or request.cookies.get(COOKIE_NAME)


def key_is_valid(presented: str | None, expected: str | None) -> bool:
    """Constant-time comparison. A shared secret checked with `==` leaks its prefix through timing,
    and on a LAN an attacker can take as many samples as they like."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def install_lan_auth(app: FastAPI) -> None:
    """Require X-Kestrel-Key (or the cookie) on every non-loopback request.

    The key is read from the process cache, refreshed lazily on first need and immediately whenever
    Settings generates or rotates it, so rotation takes effect with no restart.

    Fails CLOSED. If the app is reachable off-loopback and no secret has been generated yet, a
    non-loopback request is refused rather than allowed, because the alternative is an unprotected
    LAN-exposed app that looks protected.
    """

    @app.middleware("http")
    async def _require_key(request: Request, call_next: Callable) -> Response:
        if is_loopback(request) or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        expected = await _load_key_once()
        if not key_is_valid(_presented_key(request), expected):
            hint = (
                "no shared secret has been generated yet; open Kestrel on the host machine and "
                "generate one under Settings > LAN"
                if not expected
                else "present it as the X-Kestrel-Key header, or open /auth?key=... once"
            )
            return JSONResponse(
                {"detail": f"Kestrel requires the LAN shared secret: {hint}"}, status_code=401
            )
        return await call_next(request)


def auth_response(key: str | None, expected: str | None, next_path: str = "/") -> Response:
    """Handle `GET /auth?key=...`: on a match, set the cookie and redirect so the phone never has to
    send the key again, and so it never sits in the address bar of every subsequent page.

    The cookie is httponly (JavaScript on any page must not be able to read the operator's key),
    samesite=lax (a cross-site POST must not ride on it, which is what makes this safe to combine
    with the cross-origin guard), and NOT `secure`, because this is plain HTTP on a LAN and a secure
    cookie would simply never be stored.
    """
    if not key_is_valid(key, expected):
        return HTMLResponse(
            "<h1>Kestrel</h1><p>That key is not valid for this instance.</p>", status_code=401
        )
    # Only ever redirect to a path on this app. An open redirect here would let a crafted link
    # bounce the operator somewhere else while looking like a Kestrel URL.
    # Must be a same-site absolute path. Rejects "//evil" and "/\evil", both of which browsers
    # parse as scheme-relative, so neither can bounce the operator off-site from the page that
    # just set their key cookie.
    safe = (
        next_path.startswith("/")
        and not next_path.startswith("//")
        and not next_path.startswith("/\\")
    )
    target = next_path if safe else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        key or "",
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return response


def join_url(host: str, port: int, key: str) -> str:
    """The URL encoded into the QR code: one scan and the phone is in, no typing."""
    from urllib.parse import quote

    return f"http://{host}:{port}/auth?key={quote(key, safe='')}"


def qr_svg(data: str) -> str:
    """An inline SVG QR code. segno is pure Python with no dependencies, so this works in the frozen
    app with no network and no image toolchain. Returns an empty string if segno is unavailable,
    because a missing QR must degrade to "type the URL", never break the Settings page."""
    try:
        import io

        import segno
    except ImportError:
        return ""
    buf = io.BytesIO()  # segno writes bytes even for SVG
    segno.make(data, error="m").save(buf, kind="svg", scale=4, border=2, xmldecl=False, svgns=True)
    return buf.getvalue().decode("utf-8")


def local_addresses() -> list[str]:
    """Every address this machine can legitimately be reached on, for the Host and Origin
    allowlists under bind_all.

    This exists because widening those allowlists to "*"/any-host (the first cut of LAN mode)
    silently disabled BOTH of the other controls: a forged Host passed, so DNS rebinding worked
    again, and any Origin counted as our own, so the cross-origin guard passed too. Since a rebound
    request also arrives on loopback, where the shared key is exempt, turning on LAN access
    disabled every control at once. Enumerating the real addresses keeps the phone working while an
    attacker's own hostname still fails to match.
    """
    import socket

    addresses = {"127.0.0.1", "localhost", "::1"}
    try:
        hostname = socket.gethostname()
        addresses.add(hostname.lower())
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if isinstance(addr, str):
                addresses.add(addr)
    except OSError:
        pass  # no resolvable hostname: loopback plus the route lookup below still apply
    routed = lan_host_ip()
    if routed:
        addresses.add(routed)
    return sorted(addresses)


def lan_host_ip() -> str | None:
    """This machine's LAN address, for the QR target. Uses a UDP socket to a documentation address
    to ask the OS which interface would be used; no packet is actually sent, and nothing is
    contacted. Returns None if there is no usable route, in which case the UI says so instead of
    printing a wrong address into a QR code."""
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None  # created inside the try: socket() itself can fail with no usable stack
    try:
        sock.connect(("192.0.2.1", 9))  # RFC 5737 TEST-NET-1, unroutable and never contacted
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if ip.startswith("127.") or ip == "0.0.0.0":  # noqa: S104 - rejecting it, not binding it
        return None
    return ip
