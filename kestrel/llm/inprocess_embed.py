"""In-process CPU embeddings (SPEC.md 8.1.1, D-EMBED-1).

The embed tier's second engine, for installs with no Ollama (the hosted_only profile, BYOK): a
bundled ONNX sentence-embedding model run on onnxruntime's CPU provider, tokenized by `tokenizers`.
No torch, no network, and nothing leaves the machine, which is the whole point: embeddings carry
the operator's text and have no hosted path.

The model is NOT downloaded at run time. `scripts/fetch_embed_model.py` fetches the pinned archive
at release-build time, verifies every file against the SHA-256 values below, and writes the files
into `kestrel/resources/embed_model/`, which ships as package data and is resolved through
`paths.packaged_embed_model()` (importlib.resources). When the files are absent the engine raises
`EmbedModelMissing` with the command that fixes it; it never reaches for the network.

Inference is synchronous CPU work. `embed_async` runs it in a worker thread so the event loop (and
with it the web UI) stays responsive, and a lock serializes runs: one inference at a time on a
bounded thread pool beats four concurrent ones fighting over every core.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from kestrel import paths

log = structlog.get_logger()


@dataclass(frozen=True)
class BundledModel:
    """What the build bundles and the runtime expects, pinned in one place. The fetch script and
    the runtime both read this, so they cannot disagree about which model or which bytes."""

    model_id: str
    dims: int
    max_tokens: int
    # sentence-transformers' recipe for this model: mean over non-padding tokens, then L2 norm.
    pooling: str
    archive_url: str
    archive_sha256: str
    # The directory inside the archive that holds the files below.
    archive_root: str
    # file name -> sha256. Only these files are extracted; nothing else in the archive is read.
    files: dict[str, str] = field(default_factory=dict)
    license: str = ""
    attribution: str = ""
    # Prefix conditioning. all-MiniLM-L6-v2 is symmetric (no instruction on either side), unlike
    # nomic-embed-text's search_document: / search_query:.
    document_prefix: str = ""
    query_prefix: str = ""
    # Per-file download URLs (file name -> URL). When set, the build fetches each file directly
    # (Hugging Face, pinned to one commit) instead of the archive above; `files` still pins every
    # byte. Empty means archive mode (D-EMBED-6).
    file_urls: dict[str, str] = field(default_factory=dict)


# sentence-transformers/all-MiniLM-L6-v2 as ONNX, from its publisher at a fixed commit. Every
# file is pinned by SHA-256, so the download location is a transport, not a trust anchor.
BUNDLED = BundledModel(
    model_id="all-MiniLM-L6-v2",
    dims=384,
    max_tokens=256,
    pooling="mean",
    # File mode (D-EMBED-6): fetched from the publisher, Hugging Face, at one fixed commit, each
    # file verified against Hugging Face's published hash when pinned (`fetch_embed_model.py
    # --pin`) and against the SHA-256 below on every build and every load.
    archive_url="",
    archive_sha256="",
    archive_root="",
    files={
        "model.onnx": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
        "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
        "config.json": "953f9c0d463486b10a6871cc2fd59f223b2c70184f49815e7efbcab5d8908b41",
    },
    file_urls={
        "model.onnx": "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/onnx/model.onnx",
        "tokenizer.json": "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/tokenizer.json",
        "config.json": "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/config.json",
    },
    license="Apache-2.0",
    attribution=(
        "all-MiniLM-L6-v2 by the sentence-transformers project (Nils Reimers and contributors, "
        "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2, commit "
        "1110a243fdf4), fine-tuned from Microsoft's MiniLM-L6-H384-uncased."
    ),
)

NOTICE_FILENAME = "NOTICE.txt"
_BATCH = 32
_FETCH_HINT = (
    "run `uv run python scripts/fetch_embed_model.py` (a source checkout), or reinstall Kestrel; "
    "the in-process embedding model ships inside the installer"
)


class EmbedModelMissing(RuntimeError):
    """The bundled model files are absent or do not match their pinned hashes."""


class EmbedModelMismatch(RuntimeError):
    """llm.tiers.embed.model names a model other than the one this build bundles."""


def model_dir() -> Path:
    """Where the bundled files live (package data; a real directory in the one-folder build)."""
    return Path(str(paths.packaged_embed_model()))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def missing_files(directory: Path | None = None, *, verify: bool = False) -> list[str]:
    """The bundled files that are absent (or, with verify, whose SHA-256 does not match). Empty
    means the model is usable. Cheap without verify, so doctor can call it on every run."""
    d = directory or model_dir()
    bad: list[str] = []
    for name, digest in BUNDLED.files.items():
        p = d / name
        if not p.is_file():
            bad.append(name)
        elif verify and sha256_file(p) != digest:
            bad.append(f"{name} (sha256 mismatch)")
    return bad


def check_model_id(model: str) -> None:
    if model != BUNDLED.model_id:
        raise EmbedModelMismatch(
            f"llm.tiers.embed.model is {model!r} but the in-process engine bundles "
            f"{BUNDLED.model_id!r}; set llm.tiers.embed.model to {BUNDLED.model_id!r}"
        )


class InProcessEmbedder:
    """One loaded ONNX session plus its tokenizer. Construct through `get_embedder()`."""

    def __init__(self, directory: Path) -> None:
        bad = missing_files(directory, verify=True)
        if bad:
            raise EmbedModelMissing(
                f"in-process embedding model not usable in {directory}: "
                f"{', '.join(bad)}; {_FETCH_HINT}"
            )
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tok = Tokenizer.from_file(str(directory / "tokenizer.json"))
        # The exported tokenizer.json pads every input to a fixed 128; sentence-transformers runs
        # this model at max_seq_length 256 with dynamic padding, so both are set explicitly.
        tok.enable_truncation(max_length=BUNDLED.max_tokens)
        tok.enable_padding(pad_id=0, pad_token="[PAD]")
        self._tok = tok
        opts = ort.SessionOptions()
        # Half the cores at most: embedding shares the machine with the web UI and the worker.
        opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(directory / "model.onnx"), opts, providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._session.get_inputs()}
        # By name, not position: exports differ in which outputs they add (D-EMBED-6). Token
        # states are mean-pooled below; an export that already pools is used as is.
        names = [o.name for o in self._session.get_outputs()]
        self._output = "last_hidden_state" if "last_hidden_state" in names else names[0]
        self._run_lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Synchronous. L2-normalized mean-pooled vectors, one per input, BUNDLED.dims wide."""
        import numpy as np

        out: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            batch = texts[start : start + _BATCH]
            enc = self._tok.encode_batch(batch)
            ids = np.asarray([e.ids for e in enc], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
            feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            with self._run_lock:
                hidden = self._session.run([self._output], feed)[0]
            if hidden.ndim == 2:
                pooled = hidden.astype(np.float32)
            else:
                m = mask[..., None].astype(np.float32)
                pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            pooled = pooled / np.clip(norms, 1e-12, None)
            out.extend(row.astype(np.float32).tolist() for row in pooled)
        return out


_cache: dict[str, InProcessEmbedder] = {}
_load_lock = threading.Lock()


def get_embedder() -> InProcessEmbedder:
    """The process-wide embedder, loaded (and hash-verified) once on first use."""
    d = model_dir()
    key = str(d)
    with _load_lock:
        emb = _cache.get(key)
        if emb is None:
            emb = InProcessEmbedder(d)
            _cache[key] = emb
            log.info("inprocess_embed_loaded", model=BUNDLED.model_id, dims=BUNDLED.dims)
        return emb


def embed_sync(texts: list[str], *, model: str) -> list[list[float]]:
    check_model_id(model)
    if not texts:
        return []
    return get_embedder().embed(texts)


async def embed_async(texts: list[str], *, model: str) -> list[list[float]]:
    """Off the event loop: loading the session and running it are both CPU-bound."""
    return await asyncio.to_thread(embed_sync, texts, model=model)
