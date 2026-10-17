"""Fetch, verify and install the bundled in-process embedding model (D-EMBED-2, SPEC.md 8.1.1).

Run at release-build time (release.yml, before PyInstaller) and once in a source checkout that
wants the in-process embed engine. The model is ~90 MB and is never committed.

    uv run python scripts/fetch_embed_model.py                    # download, verify, install
    uv run python scripts/fetch_embed_model.py --archive onnx.tar.gz   # offline: a local archive
    uv run python scripts/fetch_embed_model.py --check            # verify what is installed
    uv run python scripts/fetch_embed_model.py --pin              # print Hugging Face pins

Two sources (D-EMBED-6). Archive mode reads one pinned tarball. File mode (BUNDLED.file_urls)
fetches each file from Hugging Face at one fixed commit. `--pin` resolves the model repo's current
commit, downloads the files at that commit, cross-checks each against the SHA-256 Hugging Face
publishes for it, and prints the BUNDLED fields to commit: a one-time, human-reviewed step, after
which every build verifies the same bytes.

Everything is pinned in kestrel/llm/inprocess_embed.py (BUNDLED): the archive URL, the archive's
SHA-256, and the SHA-256 of every file taken from it. A mismatch anywhere aborts with nothing
installed. Only the named files are read out of the archive (no extractall, so a hostile archive
cannot write outside the destination). The destination defaults to kestrel/resources/embed_model,
which PyInstaller collects as package data.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import re
import sys
import tarfile
import tempfile
from pathlib import Path

from kestrel.llm import inprocess_embed as ipe

_APACHE_MARK = "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION"
_APACHE_HEAD = re.compile(r"Apache License\s+Version 2\.0, January 2004")


def _sha256(path: Path) -> str:
    return ipe.sha256_file(path)


def _download(url: str, dest: Path) -> None:
    import httpx

    print(f"downloading {url}", flush=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as r:
        r.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)


def apache_license_text() -> str | None:
    """The full Apache-2.0 text, taken from an installed distribution that ships it (tokenizers
    pulls in huggingface-hub, which does), so the model's NOTICE carries the license it needs."""
    for dist in md.distributions():
        for f in dist.files or []:
            name = f.name.upper()
            if not name.startswith(("LICENSE", "COPYING")):
                continue
            try:
                body = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            m = _APACHE_HEAD.search(body or "")
            if m and _APACHE_MARK in body:
                return body[m.start() :].strip()
    return None


def notice_text(bundled: ipe.BundledModel) -> str:
    parts = [
        f"Bundled embedding model: {bundled.model_id}",
        f"License: {bundled.license}",
        "Source: " + (", ".join(bundled.file_urls.values()) or bundled.archive_url),
        bundled.attribution,
        "",
    ]
    text = apache_license_text() if bundled.license == "Apache-2.0" else None
    if text:
        parts += ["--- Apache License 2.0 ---", text, ""]
    else:
        parts += ["Full license text: https://www.apache.org/licenses/LICENSE-2.0", ""]
    return "\n".join(parts)


HF_REPO = "sentence-transformers/all-MiniLM-L6-v2"
# Hugging Face path -> the name the runtime expects in the install directory.
HF_FILES = {"onnx/model.onnx": "model.onnx", "tokenizer.json": "tokenizer.json",
            "config.json": "config.json"}


def install_files(dest: Path, bundled: ipe.BundledModel) -> None:
    """File mode: download each pinned URL to a staging file in `dest`, verify its SHA-256, and
    only when every file matches move them all into place. Nothing is installed on a mismatch."""
    dest.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for name, digest in bundled.files.items():
            url = bundled.file_urls.get(name)
            if not url:
                raise SystemExit(f"no download URL pinned for {name}; nothing installed")
            tmp = dest / f".{name}.part"
            staged.append((tmp, dest / name))
            _download(url, tmp)
            got = _sha256(tmp)
            if got != digest:
                raise SystemExit(f"{name} sha256 mismatch: got {got}, pinned {digest}; "
                                 "nothing installed")
        for tmp, final in staged:
            tmp.replace(final)
        staged.clear()
    finally:
        for tmp, _final in staged:
            tmp.unlink(missing_ok=True)
    (dest / ipe.NOTICE_FILENAME).write_text(notice_text(bundled), encoding="utf-8")


def pin_from_huggingface(repo: str = HF_REPO) -> str:
    """Resolve `repo`'s current commit, download HF_FILES at that commit, check each against the
    SHA-256 Hugging Face publishes for its LFS object (or its git blob id for a small file), and
    return the BUNDLED fields to paste. Two independent checks: Hugging Face's own index, and the
    bytes received."""
    import httpx

    api = f"https://huggingface.co/api/models/{repo}/revision/main?blobs=true"
    meta = httpx.get(api, timeout=60.0, follow_redirects=True).raise_for_status().json()
    commit = meta["sha"]
    index = {s["rfilename"]: s for s in meta.get("siblings", [])}
    pins: dict[str, str] = {}
    urls: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        for path, name in HF_FILES.items():
            entry = index.get(path)
            if entry is None:
                raise SystemExit(f"{path} is not in {repo} at {commit}")
            url = f"https://huggingface.co/{repo}/resolve/{commit}/{path}"
            tmp = Path(tmpdir) / name
            _download(url, tmp)
            got = _sha256(tmp)
            lfs = (entry.get("lfs") or {}).get("sha256")
            if lfs:
                if lfs != got:
                    raise SystemExit(f"{path}: downloaded {got}, Hugging Face lists {lfs}")
            else:
                data = tmp.read_bytes()
                blob = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
                if entry.get("blobId") and entry["blobId"] != blob:
                    raise SystemExit(f"{path}: git blob {blob}, Hugging Face lists "
                                     f"{entry['blobId']}")
            pins[name], urls[name] = got, url
    files = "".join(f'        "{n}": "{h}",\n' for n, h in pins.items())
    file_urls = "".join(f'        "{n}": "{u}",\n' for n, u in urls.items())
    return (f"# {repo} at commit {commit}, verified against Hugging Face's published hashes\n"
            f"    files={{\n{files}    }},\n    file_urls={{\n{file_urls}    }},\n")


def install(archive: Path, dest: Path, bundled: ipe.BundledModel) -> None:
    """Verify `archive`, then write each pinned file into `dest` only if its hash matches."""
    got = _sha256(archive)
    if got != bundled.archive_sha256:
        raise SystemExit(
            f"archive sha256 mismatch: got {got}, pinned {bundled.archive_sha256}; "
            "nothing installed"
        )
    dest.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for name, digest in bundled.files.items():
                member_name = f"{bundled.archive_root}/{name}"
                try:
                    member = tar.getmember(member_name)
                except KeyError:
                    raise SystemExit(
                        f"{member_name} not in the archive; nothing installed"
                    ) from None
                if not member.isfile():
                    raise SystemExit(f"{member_name} is not a regular file; nothing installed")
                src = tar.extractfile(member)
                if src is None:
                    raise SystemExit(f"{member_name} could not be read; nothing installed")
                tmp = dest / f".{name}.part"
                staged.append((tmp, dest / name))  # before the write, so a failed read is cleaned
                h = hashlib.sha256()
                with src, tmp.open("wb") as out:
                    while chunk := src.read(1 << 20):
                        h.update(chunk)
                        out.write(chunk)
                if h.hexdigest() != digest:
                    raise SystemExit(
                        f"{name} sha256 mismatch: got {h.hexdigest()}, pinned {digest}; "
                        "nothing installed"
                    )
        for tmp, final in staged:
            tmp.replace(final)
        staged.clear()
    finally:
        for tmp, _final in staged:
            tmp.unlink(missing_ok=True)
    (dest / ipe.NOTICE_FILENAME).write_text(notice_text(bundled), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=None, help="install directory")
    parser.add_argument("--archive", type=Path, default=None, help="use this local archive")
    parser.add_argument("--check", action="store_true", help="verify the installed files only")
    parser.add_argument("--pin", action="store_true",
                        help="resolve, download and verify from Hugging Face; print the pins")
    args = parser.parse_args(argv)
    if args.pin:
        print(pin_from_huggingface())
        return 0
    bundled = ipe.BUNDLED
    dest = args.dest or ipe.model_dir()

    if args.check:
        bad = ipe.missing_files(dest, verify=True)
        if bad:
            print(f"embedding model in {dest} is not usable: {', '.join(bad)}")
            return 1
        print(f"embedding model {bundled.model_id} in {dest}: all files verified")
        return 0

    if not ipe.missing_files(dest, verify=True) and (dest / ipe.NOTICE_FILENAME).is_file():
        print(f"embedding model {bundled.model_id} already installed and verified in {dest}")
        return 0
    if args.archive is not None:
        install(args.archive, dest, bundled)
    elif bundled.file_urls:
        install_files(dest, bundled)
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "model.tar.gz"
            _download(bundled.archive_url, archive)
            install(archive, dest, bundled)
    print(f"embedding model {bundled.model_id} installed and verified in {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
