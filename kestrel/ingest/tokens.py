"""Portable token counting via the VENDORED cl100k_base encoding. TIKTOKEN_CACHE_DIR is set to the
packaged cache before first use so the app never downloads the encoding (platform trap)."""

from __future__ import annotations

import os

from kestrel import paths

_encoder = None


def _enc():
    global _encoder
    if _encoder is None:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", paths.tiktoken_cache_dir())
        import tiktoken

        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str | None) -> int:
    if not text:
        return 0
    return len(_enc().encode(text))


def truncate_head(text: str | None, n_tokens: int) -> str:
    """The first n_tokens of text, decoded back to a string (SPEC.md 6.2: Tier 1 sees the first
    1200 tokens). Returns "" for empty input; returns text unchanged when it already fits."""
    if not text:
        return ""
    enc = _enc()
    ids = enc.encode(text)
    if len(ids) <= n_tokens:
        return text
    return enc.decode(ids[:n_tokens])


def truncate_middle(text: str | None, n_tokens: int) -> str:
    """Keep the head and tail, eliding the middle, so a long description fits n_tokens without
    losing the opening framing or the closing requirements (SPEC.md 6.3: middle elided if over
    max_jd_tokens). Returns text unchanged when it already fits."""
    if not text:
        return ""
    enc = _enc()
    ids = enc.encode(text)
    if len(ids) <= n_tokens:
        return text
    marker = "\n\n[... middle of description elided to fit context ...]\n\n"
    marker_len = len(enc.encode(marker))
    budget = max(0, n_tokens - marker_len)
    head = budget * 2 // 3
    tail = budget - head
    return enc.decode(ids[:head]) + marker + (enc.decode(ids[-tail:]) if tail else "")
