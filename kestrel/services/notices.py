"""Third-party notices (D-LIC-1): the license text of every library the packaged app bundles.

MIT, BSD, Apache and the like require their copyright and license notice to travel with every
copy, and the installer is a copy. The text is generated from installed package metadata, so it
always matches what was bundled: the build writes it once (packaging/kestrel.spec) and the frozen
app serves that file; a source checkout builds it live. Only the runtime dependency closure of
the `kestrel` distribution is included (dev tools such as pytest never ship), with environment
markers evaluated for the machine doing the build.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

NOTICES_FILENAME = "THIRD-PARTY-NOTICES.txt"
# Written beside the bundled embedding model by scripts/fetch_embed_model.py (D-EMBED-2): the
# model is not a Python distribution, so its license travels through this file instead.
MODEL_NOTICE_FILENAME = "NOTICE.txt"
# A license file by name, with no extension or a text one; never source (openapi_pydantic ships a
# `license.py` model module).
_LICENSE_FILE = re.compile(
    r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)([-_.][^/]*)?(\.(txt|md|rst))?$", re.IGNORECASE
)
_SOURCE_SUFFIXES = (".py", ".pyc", ".pyi", ".so", ".pyd", ".dll", ".c", ".h")
_RULE = "=" * 78


def runtime_closure(root: str = "kestrel") -> list[md.Distribution]:
    """The installed distributions `root` needs at run time, extras followed where requested."""
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name

    seen: dict[str, md.Distribution] = {}
    queue: list[tuple[str, frozenset[str]]] = [(root, frozenset())]
    done: set[tuple[str, frozenset[str]]] = set()
    while queue:
        name, extras = queue.pop()
        key = (canonicalize_name(name), extras)
        if key in done:
            continue
        done.add(key)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue  # a marker-gated or optional dependency not installed here is not bundled
        seen[canonicalize_name(dist.metadata["Name"])] = dist
        for raw in dist.requires or []:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            envs = [{"extra": e} for e in extras] or [{"extra": ""}]
            if req.marker is None or any(req.marker.evaluate(env) for env in envs):
                queue.append((req.name, frozenset(req.extras)))
    seen.pop(canonicalize_name(root), None)  # Kestrel's own license is not a third-party notice
    return sorted(seen.values(), key=lambda d: d.metadata["Name"].lower())


def _license_label(dist: md.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License")
    ]
    if classifiers:
        return ", ".join(classifiers)
    first = (meta.get("License") or "").strip().splitlines()
    if first:
        return first[0][:80]
    # No metadata (python-jobspy): name it from the license file's title line, e.g. "MIT License".
    for _fname, body in _license_texts(dist):
        title = body.strip().splitlines()[0].strip()
        if 0 < len(title) <= 60 and "licen" in title.lower():
            return f"{title} (from the license file)"
    return "not stated in the package metadata"


def _license_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for f in dist.files or []:
        rel = str(f).replace("\\", "/")
        if _LICENSE_FILE.search(rel) and not rel.lower().endswith(_SOURCE_SUFFIXES):
            try:
                body = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if body.strip():
                texts.append((f.name, body.strip()))
    return texts


def _third_party_notices_file(dist: md.Distribution) -> str | None:
    """A package's own ThirdPartyNotices.txt (onnxruntime's covers the libraries compiled into
    its runtime). Matched by exact name, so this never pulls in an arbitrary text file."""
    for f in dist.files or []:
        if f.name == "ThirdPartyNotices.txt":
            try:
                body = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
            return body.strip() or None
    return None


def build_notices(model_notice: Path | None = None, runtime_notice: Path | None = None) -> str:
    """The notices text. `model_notice` is the bundled embedding model's NOTICE.txt, appended as
    its own section when the file exists (the build passes it; a source checkout finds it).
    `runtime_notice` is the AppImage runtime's notice (packaging/linux/RUNTIME-NOTICE.txt), which
    no Python metadata describes; only the Linux build passes it, because only it embeds the
    runtime."""
    from packaging.utils import canonicalize_name

    parts = [
        "Kestrel third-party notices",
        "",
        "Kestrel bundles the open-source software listed below. Each is used under its own",
        "license, reproduced here as its authors require. Kestrel's own code is not covered",
        "by these licenses.",
        "",
    ]
    py_license = Path(sys.base_prefix) / "LICENSE.txt"
    candidates = [
        py_license,
        Path(sys.base_prefix) / "lib" / f"python{sys.version_info[0]}"
        f".{sys.version_info[1]}" / "LICENSE.txt",
    ]
    py_head = [
        _RULE,
        f"Python {sys.version.split()[0]} (the bundled interpreter)",
        "License: PSF-2.0",
    ]
    found = next((p for p in candidates if p.is_file()), None)
    if found is not None:
        # CPython's LICENSE.txt also carries the notices for the native libraries it bundles
        # (OpenSSL, libffi, SQLite, zlib and others on Windows).
        parts += [*py_head, _RULE, found.read_text(encoding="utf-8").strip(), ""]
    else:
        parts += [
            *py_head,
            _RULE,
            "The interpreter's LICENSE.txt was not found at build time. "
            "Full text, including the bundled native libraries: "
            "https://docs.python.org/3/license.html",
            "",
        ]
    for dist in runtime_closure():
        name, version = dist.metadata["Name"], dist.version
        parts += [_RULE, f"{name} {version}", f"License: {_license_label(dist)}"]
        home = dist.metadata.get("Home-page") or next(
            (u.split(",", 1)[-1].strip() for u in dist.metadata.get_all("Project-URL") or []),
            None,
        )
        if home:
            parts.append(f"Source: {home}")
        if canonicalize_name(name) == "pywebview":
            parts.append(
                "pywebview bundles Microsoft WebView2 SDK loader DLLs on Windows, distributed "
                "under Microsoft's WebView2 SDK license: https://aka.ms/webview2sdklicense"
            )
        if canonicalize_name(name) == "playwright":
            parts.append(
                "The bundled Playwright driver carries its own notices for the software it "
                "includes: playwright/driver/package/ThirdPartyNotices.txt in the install folder."
            )
        tpn = (
            _third_party_notices_file(dist)
            if canonicalize_name(name) == "onnxruntime"
            else None
        )
        parts.append(_RULE)
        texts = _license_texts(dist)
        if texts:
            for fname, body in texts:
                parts += [f"--- {fname} ---", body, ""]
        else:
            parts += [
                "The package does not include its license file; the license named above",
                "applies. See the source link for the full text.",
                "",
            ]
        if tpn:
            parts += [f"--- {name} ThirdPartyNotices.txt ---", tpn, ""]
    if model_notice is not None and model_notice.is_file():
        parts += [_RULE, model_notice.read_text(encoding="utf-8").strip(), ""]
    if runtime_notice is not None and runtime_notice.is_file():
        parts += [_RULE, runtime_notice.read_text(encoding="utf-8").strip(), ""]
    return "\n".join(parts) + "\n"


def write_notices(
    path: Path, model_notice: Path | None = None, runtime_notice: Path | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_notices(model_notice, runtime_notice), encoding="utf-8")
    return path


def notices_text() -> str:
    """The file the build bundled (frozen app), else a live build (source checkout)."""
    from importlib.resources import files

    bundled = files("kestrel.resources") / NOTICES_FILENAME
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    model_notice = files("kestrel.resources") / "embed_model" / MODEL_NOTICE_FILENAME
    return build_notices(Path(str(model_notice)) if model_notice.is_file() else None)
