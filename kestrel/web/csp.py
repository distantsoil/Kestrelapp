"""The Content-Security-Policy on every app response (D-INST-8, D-SEC-56).

One policy, the same bytes on every response the app sends: pages, htmx partials, JSON, static
files, downloads, redirects, and the error answers Starlette makes itself (a 500 from
ServerErrorMiddleware, a 400 from TrustedHostMiddleware). It is set by an ASGI wrapper around the
whole middleware stack (`KestrelFastAPI.build_middleware_stack`), so no route, middleware or
exception handler can answer without it, and any policy a response carried already is replaced,
never combined (two policies would both apply, and the stricter one would win silently).

No 'unsafe-inline' and no 'unsafe-eval' anywhere: every script and stylesheet is a file under
/static, Alpine is its CSP build, htmx runs with allowEval off, and a value a template has to
compute (a bar's width) is a data-k-style attribute that static/js/k-head.js applies through the
CSSOM, which a policy does not govern (D-SEC-57).
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import Response

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# The order is the issue's (#212); tests compare the whole string.
DIRECTIVES: tuple[tuple[str, str], ...] = (
    ("default-src", "'self'"),
    ("script-src", "'self'"),
    ("style-src", "'self'"),
    ("connect-src", "'self'"),
    ("img-src", "'self' data:"),
    ("form-action", "'self'"),
    ("frame-ancestors", "'none'"),
    ("base-uri", "'none'"),
    ("object-src", "'none'"),
)
POLICY = "; ".join(f"{name} {value}" for name, value in DIRECTIVES)
HEADER = b"content-security-policy"
_POLICY_BYTES = POLICY.encode("ascii")


class SecurityHeaders:
    """Pure ASGI: rewrites the start of every HTTP response to carry exactly POLICY."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_policy(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != HEADER
                ]
                headers.append((HEADER, _POLICY_BYTES))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_policy)


class KestrelFastAPI(FastAPI):
    """FastAPI with SecurityHeaders outside its whole stack, ServerErrorMiddleware included:
    an http middleware or add_middleware sits inside ServerErrorMiddleware, so a 500 it renders
    would leave without the header."""

    def build_middleware_stack(self) -> ASGIApp:  # type: ignore[override]
        return SecurityHeaders(super().build_middleware_stack())


# --------------------------------------------------------------------- print stylesheets in-app
# A document, letter or prep pack is HTML made for print, and it carries its own <style>: the
# PDF path keeps it as it is (Playwright prints it outside the app, with no policy). Shown inside
# the app, in a srcdoc frame (which inherits the page's policy) or in a partial, that <style> is
# inline and the policy refuses it. frame_safe() turns each one into a same-origin stylesheet
# link to the same text, kept here by its own SHA-256 and served by GET /frame-css/<digest>.css
# (D-SEC-59). Only text this process rendered is ever served; an unknown digest is a 404.
FRAME_CSS_LIMIT = 512  # distinct sheets kept; a preview renders its sheet again on every load
_FRAME_CSS: OrderedDict[str, str] = OrderedDict()
_FRAME_LOCK = threading.Lock()
_STYLE_BLOCK = re.compile(r"<style>(.*?)</style>", re.S)
_DIGEST = re.compile(r"[0-9a-f]{64}")

router = APIRouter()


def frame_css_url(css: str) -> str:
    """The address of `css` as a stylesheet of this app (kept until FRAME_CSS_LIMIT newer ones)."""
    digest = hashlib.sha256(css.encode("utf-8")).hexdigest()
    with _FRAME_LOCK:
        _FRAME_CSS[digest] = css
        _FRAME_CSS.move_to_end(digest)
        while len(_FRAME_CSS) > FRAME_CSS_LIMIT:
            _FRAME_CSS.popitem(last=False)
    return f"/frame-css/{digest}.css"


def frame_safe(html: str) -> str:
    """`html` with every <style> element replaced by a link to the same text (see above). Only
    for HTML this app rendered from its own templates, where a person's text is escaped and so
    can never contain a raw <style> tag."""
    return _STYLE_BLOCK.sub(
        lambda m: f'<link rel="stylesheet" href="{frame_css_url(m.group(1))}">', html
    )


def print_safe(html: str) -> str:
    """The reverse, for a page of the app's own templates printed to PDF outside the app (the
    analytics export): data-k-style values become style attributes again, since no k-head.js
    runs there and no policy applies."""
    return html.replace(' data-k-style="', ' style="')


@router.get("/frame-css/{name}")
async def frame_css(name: str) -> Response:
    digest = name.removesuffix(".css")
    with _FRAME_LOCK:
        css = _FRAME_CSS.get(digest) if _DIGEST.fullmatch(digest) else None
    if css is None or not name.endswith(".css"):
        return Response("not found", status_code=404, media_type="text/plain")
    return Response(css, media_type="text/css; charset=utf-8")
