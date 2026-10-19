"""The online service by its own name, for text a person reads."""

from __future__ import annotations

from urllib.parse import urlsplit


def service_name(base_url: str | None) -> str:
    """The host of the service URL without a leading "api." or "www." (for example
    "openrouter.ai"). Never the internal backend key ("frontier"), which means nothing to the
    person reading the confirmation (A3)."""
    host = (urlsplit(base_url or "").hostname or "").lower()
    for prefix in ("api.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host or "your online model service"
