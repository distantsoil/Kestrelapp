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
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from kestrel.services import viewer

log = structlog.get_logger()

COOKIE_NAME = "kestrel_key"
SETTING_KEY = "server.shared_secret"

# /health is exempt so the desktop shell's readiness probe, the extension's discovery probe, and any
# uptime check keep working without the key. It exposes only status, service name, and version.
_EXEMPT_PATHS = frozenset({"/health", "/auth"})

# Loopback is exempt: the operator sitting at the machine is already past every boundary this key
# could enforce, and requiring a key there would break the desktop shell's own webview, the CLI, and
# the MCP server for no gain.
# ONE carve-out (issue #151): a published tunnel (cloudflared, docs/WEBAPP.md) also connects to
# the app from loopback, but its requests come from the internet and carry the public hostname
# as Host. For those, is_loopback is False, so the key still gates them.
# Numeric peers only: request.client.host is always an address, never a hostname. "localhost" is
# deliberately NOT here, because it would be a trust entry that could silently activate under any
# server that put a hostname there.
_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "cf-connecting-ip", "x-real-ip")


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
    if value is None and public_hostnames():
        # A tunnel host is never reachable without a key (#151, D-SEC-15): the key is made here
        # on first need, so a restore or a first request can never leave the host open.
        value = await ensure_tunnel_key()
    if _cache["generation"] != generation_before:
        # A rotation landed while this read was in flight. Its value is the current one; this read
        # is stale and must not overwrite it, or the revoked key would keep working.
        return cached_key()
    set_cached_key(value)
    return value


async def ensure_tunnel_key() -> str | None:
    """Make the shared key when `server.public_hostnames` is set and none exists (#151, D-SEC-15).

    Returns the key in force, or None when no hostname is set or it cannot be stored (the
    middleware then refuses, failing closed). An existing key is never replaced.
    """
    if not public_hostnames():
        return None
    if _cache["loaded"] and cached_key():
        return cached_key()
    try:
        from kestrel.db.engine import get_sessionmaker
        from kestrel.db.models import Setting

        async with get_sessionmaker()() as session:
            row = await session.get(Setting, SETTING_KEY)
            if row is not None and isinstance(row.value, str) and row.value:
                return row.value
            token = generate_key()
            if row is None:
                session.add(Setting(key=SETTING_KEY, value=token))
            else:
                row.value = token
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        log.warning("tunnel_key_create_failed", error=str(exc))
        return None
    set_cached_key(token)
    log.info("tunnel_key_created")
    return token


async def current_key() -> str | None:
    """The key in force right now, loading it on first need. For the /auth route."""
    return await _load_key_once()


def generate_key() -> str:
    """A fresh shared secret. token_urlsafe(32) is 256 bits, which is far beyond what a LAN
    attacker could grind, and it stays copy-pasteable and QR-encodable."""
    return secrets.token_urlsafe(32)


def _host_name(host_header: str) -> str:
    """The hostname from a Host header, lowercased, with any port or IPv6 brackets stripped.

    `Kestrel.Example.com:8443` yields `kestrel.example.com`. server.public_hostnames are exact
    lowercase names (config load refuses anything else), so a match here is an exact match
    against an operator-declared name, never a prefix or wildcard."""
    host = host_header.strip().lower()
    if host.startswith("["):
        close = host.find("]")
        return (host[1:close] if close > 1 else host).rstrip(".")
    # A bare IPv6 literal has colons but no port; every other host may carry one.
    if host.count(":") > 1:
        return host.rstrip(".")
    return host.rsplit(":", 1)[0].rstrip(".") if ":" in host else host.rstrip(".")


def public_hostnames() -> tuple[str, ...]:
    """The exact public hostnames the operator declared (server.public_hostnames), lowercase, read
    from the settings in effect NOW. The one source every guard uses (the web app's key guard,
    its origin and Host checks, and the MCP server's), so a settings change reaches all of them
    the same way (D-SEC-10)."""
    from kestrel.config import get_settings

    return tuple(h.strip().lower() for h in get_settings().server.public_hostnames if h)


def is_loopback(request: Request) -> bool:
    """True only for a request from the person at this computer: a loopback peer, a Host of
    localhost, 127.0.0.1 or ::1, and no proxy header. Everything else is remote (fail closed).

    A tunnel (cloudflared, docs/WEBAPP.md) connects to the app from 127.0.0.1 and forwards the
    public hostname as Host, so such a request has a loopback peer but comes from the internet.
    Exempting it by peer address is the authentication bypass of issue #151. With
    server.public_hostnames set, a request whose Host is one of those names is not loopback and
    falls through to the key check. A plain local request (Host a loopback address or
    localhost) is still exempt, which keeps the desktop shell's webview, the CLI and the MCP
    server working."""
    client = request.client.host if request.client else None
    return peer_is_loopback(client, request.headers, public_hostnames())


def peer_is_loopback(client: str | None, headers: Any, public_hostnames: Any = ()) -> bool:
    """The one loopback decision, on the parts of a request every server layer has (the web app's
    Request and the MCP server's ASGI scope both call it, D-SEC-10): the peer address, the
    headers, and the operator-declared public host names. See `is_loopback` for the rule."""
    if client not in _LOOPBACK_CLIENTS:
        return False
    # Fail closed (#155): a loopback peer is this computer only when the request says so too.
    # Any proxy header means a proxy or tunnel forwarded it; and the Host must be one of the three
    # names the desktop shell, the CLI and the MCP server use. An unlisted name (a quick tunnel,
    # httpHostHeader, ngrok, tailscale serve, a trailing dot) is remote, which is the safe way to
    # be wrong: the person at the keyboard types localhost or an address.
    if any(h in headers for h in _FORWARD_HEADERS):
        return False
    name = _host_name(headers.get("host", ""))
    return name in _LOCAL_HOSTS and name not in (public_hostnames or ())


# ------------------------------------------------------------------ the local-only guard (#155)
#
# The shared key is ONE credential that lets a phone or a tunnel visitor browse and work: read
# roles, score, apply, keep notes. It is NOT the power to take the data away or to point it
# somewhere else. Three tiers, one rule each (SPEC-INTERFACES 2.3, D-SEC-9):
#   * browse and work: open to a key holder.
#   * restore, wipe, reset, and every download of the person's data: this computer only.
#   * change a key, a model route or a destination, or test/spend a saved key: this computer only.
# `require_local` is the ONE dependency that enforces the second and third tiers. A route opts in
# with `dependencies=[LOCAL_ONLY]` (or LOCAL_DOWNLOAD for a file), and the route-table contract
# test (tests/test_local_only_routes.py) fails when a guarded prefix gains a route without it.
LOCAL_ONLY_CHANGE = "Only this computer can change this."
LOCAL_ONLY_DOWNLOAD = "Only this computer can download this."


class LocalOnlyRefused(Exception):
    """Raised by the guard; answered as a plain-sentence 403 that carries no data."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


def local_only(sentence: str = LOCAL_ONLY_CHANGE) -> Callable[[Request], None]:
    """A dependency that lets only this computer through, with `sentence` as the refusal.

    A request counts as local only when is_loopback(request) is True, so a forged Host or
    forwarding header can only ever make a request look remote, never local. The refusal runs
    before the handler, so a blocked action changes nothing and reads nothing."""

    def require_local(request: Request) -> None:
        if not is_loopback(request):
            raise LocalOnlyRefused(sentence)

    require_local.kestrel_local_only = True  # type: ignore[attr-defined]  # the contract test reads it
    return require_local


require_local = local_only(LOCAL_ONLY_CHANGE)
require_local_download = local_only(LOCAL_ONLY_DOWNLOAD)
LOCAL_ONLY = Depends(require_local)
LOCAL_DOWNLOAD = Depends(require_local_download)


async def _local_only_refused(request: Request, exc: Exception) -> Response:
    sentence = exc.sentence if isinstance(exc, LocalOnlyRefused) else LOCAL_ONLY_CHANGE
    return PlainTextResponse(sentence, status_code=403, headers={"Cache-Control": "no-store"})


def refuse_unless_local(request: Request, sentence: str = LOCAL_ONLY_CHANGE) -> None:
    """The guard for a decision made inside a handler (for example a model pick that differs from
    the configured one), where a route-level dependency would also block the ordinary case."""
    if not is_loopback(request):
        raise LocalOnlyRefused(sentence)


def request_is_local(request: Request) -> bool:
    """For templates and page renders: show the controls as disabled when this is False. A page
    rendered with no request at all (outside the web layer) is not a key holder's view."""
    if not isinstance(request, Request):
        return True
    return is_loopback(request)


def release_credit_hold(request: Request) -> bool:
    """The operator asked to score or run now: let sends to an out-of-credit online key resume
    (D-LLM-8). Only from this computer: a key holder never releases a hold (D-SEC-10), whatever
    route it posts. True when the hold was released."""
    if not request_is_local(request):
        return False
    from kestrel.llm import credit

    credit.clear()
    return True


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
    """Require X-Kestrel-Key (or the cookie) on every non-loopback request, which includes every
    request a published tunnel forwards with a public Host (issue #151).

    The key is read from the process cache, refreshed lazily on first need and immediately whenever
    Settings generates or rotates it, so rotation takes effect with no restart.

    Fails CLOSED. If the app is reachable off-loopback and no secret has been generated yet, a
    non-loopback request is refused rather than allowed, because the alternative is an unprotected
    LAN-exposed app that looks protected.
    """

    app.add_exception_handler(LocalOnlyRefused, _local_only_refused)

    @app.middleware("http")
    async def _require_key(request: Request, call_next: Callable) -> Response:
        # Tell the services whether a key holder is looking, for the balance and provider data a
        # page view must not show or fetch for one (kestrel.services.viewer, #155).
        token = viewer.set_remote(not is_loopback(request))
        try:
            return await _serve(request, call_next)
        finally:
            viewer.reset(token)

    async def _serve(request: Request, call_next: Callable) -> Response:
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
    # Dark on white whatever the theme: a scanner needs the contrast, so this is the one color pair
    # that is not a theme token (SPEC-INTERFACES 2.6, Access from other devices).
    # omitsize leaves a viewBox and no fixed width or height, so CSS can scale the whole code to
    # its box (a fixed-size SVG with no viewBox is cropped, not scaled: #133). border=4 is the
    # four-module quiet zone the QR standard asks for.
    segno.make(data, error="m").save(
        buf, kind="svg", scale=1, border=4, xmldecl=False, svgns=True, omitsize=True,
        dark="#000000", light="#ffffff",
    )
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


_VIRTUAL_CONTAINS = (
    "vethernet", "wsl", "hyper-v", "docker", "virtualbox", "vmware", "wireguard", "tailscale",
    "zerotier", "loopback", "isatap", "teredo", "bluetooth", "vpn", "tunnel", "virtual",
    "host-only", "local area connection*",
)
_VIRTUAL_PREFIX = ("veth", "br-", "virbr", "vbox", "vmnet", "tun", "tap", "utun", "wg", "ppp",
                   "zt", "ham", "awdl", "llw", "anpi", "bridge", "docker", "lo")


def _is_virtual_adapter(name: str) -> bool:
    """A virtual, bridge, VPN or tunnel adapter, by its name. A phone on the Wi-Fi cannot reach
    these, so they are never offered as "the address to use"."""
    low = name.lower()
    if low in ("lo", "lo0"):
        return True
    return any(w in low for w in _VIRTUAL_CONTAINS) or any(
        low.startswith(p) for p in _VIRTUAL_PREFIX if p != "lo")


def _adapter_ipv4() -> list[tuple[str, str]] | None:
    """(adapter name, IPv4 address) of every adapter that is up; None when the OS list cannot be
    read (the caller then falls back to the route probe alone)."""
    try:
        import psutil

        stats = psutil.net_if_stats()
        out: list[tuple[str, str]] = []
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            out.extend((name, a.address) for a in addrs
                       if getattr(a.family, "name", "") == "AF_INET")
        return out
    except (ImportError, OSError, AttributeError):
        return None


def _routed_ip() -> str | None:
    """The address the OS would use toward the internet, from a UDP socket to a documentation
    address. No packet is sent and nothing is contacted."""
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


def lan_host_ip() -> str | None:
    """This machine's LAN address, for the pairing QR and the MCP address. The adapter that holds
    the default route wins when it is a real one (Wi-Fi or Ethernet). When the default route runs
    through a VPN or a virtual adapter (WSL, Hyper-V, Docker, VirtualBox, a tunnel), the one real
    adapter is used if there is exactly one. Link-local (169.254) addresses are never offered. If
    it cannot tell, None: the UI says so instead of printing a wrong address into a QR code."""
    routed = _routed_ip()
    adapters = _adapter_ipv4()
    if adapters is None:
        return routed  # the OS adapter list is unreadable: the route probe is all there is
    real = [ip for name, ip in adapters
            if not _is_virtual_adapter(name) and not ip.startswith(("127.", "169.254."))]
    if routed and routed in real:
        return routed
    if len(real) == 1:
        return real[0]
    if routed and not any(ip == routed for _, ip in adapters):
        return routed  # the probe names an address the list does not show: trust the OS route
    return None
