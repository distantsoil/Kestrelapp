from __future__ import annotations

from urllib.parse import urlsplit


def safe_web_url(value: object) -> str | None:
    """Allow only absolute HTTP(S) addresses in external links."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            return value
    except ValueError:
        return None
    return None
