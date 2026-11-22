"""Render the Homebrew cask for a release from that release's latest.json (#201).

    python3 scripts/homebrew_cask.py --latest latest.json --tag v1.2.3 \
        --repo owner/name --license LICENSE.md --out tap-update

Standard library only: the release workflow runs it with the runner's own python3, in a job that
installs nothing. Every value that reaches the cask is checked first: the version must be the
tag's, the URL must be this release's exact download path, the SHA-256 must be 64 hex digits.
When the macOS download is not signed and notarized the cask is not written and the run says
"skipped: macOS build not notarized". The license is copied byte for byte; a missing license
fails the run. Writes `publish=true|false` to $GITHUB_OUTPUT when that is set.

Exit codes: 0 written or skipped, 1 refused (the reason is printed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "packaging" / "macos" / "kestrel-cask.rb.in"
_STABLE_TAG = re.compile(r"v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)")
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?/[A-Za-z0-9_.-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
SKIPPED = "skipped: macOS build not notarized"


class CaskError(Exception):
    """A reason the cask must not be written."""


def render(feed: object, *, tag: str, repo: str) -> str | None:
    """The cask text, or None when the macOS download is not signed. Raises CaskError."""
    tag_match = _STABLE_TAG.fullmatch(tag)
    if not tag_match:
        raise CaskError(f"{tag!r} is not a stable version tag such as v1.2.3")
    version = tag_match["version"]
    if not _REPO.fullmatch(repo) or ".." in repo:
        raise CaskError(f"{repo!r} is not an owner/name repository")
    if not isinstance(feed, dict):
        raise CaskError("latest.json is not an object")
    if feed.get("version") != version:
        raise CaskError(f"latest.json is for {feed.get('version')!r}, not the tag's {version}")
    downloads = feed.get("downloads")
    mac = downloads.get("macos") if isinstance(downloads, dict) else None
    if not isinstance(mac, dict):
        raise CaskError("latest.json has no macOS download")
    if mac.get("signed") is not True:
        return None
    name = f"Kestrel-{version}.dmg"
    url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
    if mac.get("url") != url:
        raise CaskError(f"the macOS URL is {mac.get('url')!r}, expected {url!r}")
    if mac.get("file") != name:
        raise CaskError(f"the macOS file is {mac.get('file')!r}, expected {name!r}")
    sha256 = mac.get("sha256")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise CaskError("the macOS SHA-256 is not 64 lowercase hex digits")
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in (("@VERSION@", version), ("@SHA256@", sha256), ("@URL@", url)):
        text = text.replace(key, value)
    return text


def _output(publish: bool | None) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"publish={'true' if publish is True else 'false'}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--latest", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--license", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    published = False
    try:
        published = _write(args)
    finally:
        _output(published)  # once, whatever happened
    return 0 if published is not None else 1


def _write(args: argparse.Namespace) -> bool | None:
    """True when the cask was written, False when skipped, None when refused."""
    if not args.license.is_file():
        print(f"refused: the license file {args.license} is missing", file=sys.stderr)
        return None
    try:
        feed = json.loads(args.latest.read_text(encoding="utf-8"))
        cask = render(feed, tag=args.tag, repo=args.repo)
    except (OSError, ValueError) as exc:
        print(f"refused: cannot read {args.latest}: {exc}", file=sys.stderr)
        return None
    except CaskError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return None
    if cask is None:
        print(SKIPPED)
        return False
    (args.out / "Casks").mkdir(parents=True, exist_ok=True)
    (args.out / "Casks" / "kestrel.rb").write_text(cask, encoding="utf-8")
    (args.out / "LICENSE").write_bytes(args.license.read_bytes())
    print(f"wrote {args.out / 'Casks' / 'kestrel.rb'} for {args.tag}")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
