"""Embedding storage helpers. Vectors are stored as float32 bytes (numpy.tobytes) in
opportunity_embedding.vector (SPEC.md 4.3), produced by LLMClient.embed() on either engine
(Ollama nomic-embed-text, or the in-process model, SPEC.md 8.1.1).

Vectors from two models are not comparable (different spaces, usually different widths), so every
row records its `model` and every reader compares only rows whose model is the CURRENT embed
model (`current_model`). A switch of engine or model therefore never mixes spaces: the stale rows
are ignored until the embed sweep (pipeline/embed.py enqueue_stale_embeddings) re-embeds them.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# nomic-embed-text is prefix conditioned (see pipeline/embed.py and intelligence/search.py).
NOMIC_DOCUMENT_PREFIX = "search_document: "
NOMIC_QUERY_PREFIX = "search_query: "


def current_model(settings: Any) -> str:
    """The model the embed tier runs now; the key stored vectors must match to be compared."""
    tcfg = settings.llm.tiers.get("embed")
    return tcfg.model if tcfg is not None else ""


def is_inprocess(settings: Any) -> bool:
    tcfg = settings.llm.tiers.get("embed")
    if tcfg is None:
        return False
    backend = settings.llm.backends.get(tcfg.backend)
    return backend is not None and backend.engine == "inprocess"


def prefixes(settings: Any) -> tuple[str, str]:
    """(document prefix, query prefix) for the current embed model. The in-process model is
    symmetric and takes none; the Ollama path keeps nomic-embed-text's conditioning."""
    if is_inprocess(settings):
        from kestrel.llm.inprocess_embed import BUNDLED

        return BUNDLED.document_prefix, BUNDLED.query_prefix
    return NOMIC_DOCUMENT_PREFIX, NOMIC_QUERY_PREFIX


def to_bytes(vector: list[float] | np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_bytes(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def cosine(a: list[float] | np.ndarray, b: list[float] | np.ndarray) -> float:
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))
