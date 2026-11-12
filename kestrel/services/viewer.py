"""Who is looking at this page: the person at this computer, or a key holder (#155, D-SEC-9).

The web layer sets the flag once per request (kestrel.web.auth, the request middleware) from the
same is_loopback() the route guard uses. A service that would show a balance, a spend line or a
provider's data fetched with the saved key reads it here and answers "nothing" for a remote
viewer, so a page view by a key holder never asks a provider anything and never shows money.
Outside a request (the worker, the CLI) the default is local, which changes nothing there."""

from __future__ import annotations

from contextvars import ContextVar, Token

_remote: ContextVar[bool] = ContextVar("kestrel_remote_viewer", default=False)


def set_remote(value: bool) -> Token[bool]:
    return _remote.set(value)


def reset(token: Token[bool]) -> None:
    _remote.reset(token)


def is_remote() -> bool:
    return _remote.get()
