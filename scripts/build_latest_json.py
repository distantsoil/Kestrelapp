"""Build the `latest.json` update feed the website serves and the app polls.

Usage: python scripts/build_latest_json.py --version 0.4.0 --sums SHA256SUMS.txt \
    --signing SIGNING.txt --assets-base https://github.com/OWNER/REPO/releases/download/v0.4.0

Inputs are the release job's own files: SHA256SUMS.txt (`<hex>  <filename>` lines) and
SIGNING.txt (`<platform>: signed` or `<platform>: unsigned: <how to sign>` lines). The output is
validated by round-tripping through `LatestFeed`. Refuses prerelease versions and versions with
no changelog section.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from changelog_section import extract_section, list_sections  # noqa: E402

from kestrel.services.updater import (  # noqa: E402
    FEED_SCHEMA,
    MAX_RELEASES_IN_FEED,
    Download,
    LatestFeed,
    ReleaseNote,
)

PLATFORM_PATTERNS = {
    "windows": "Kestrel-Setup-*.exe",
    "macos": "Kestrel-*.dmg",
    "linux": "Kestrel-*.AppImage",
}
_SUM_LINE = re.compile(r"^(?P<sha>[0-9a-fA-F]{64})\s+\*?(?P<file>\S.*?)\s*$")
_SIGNING_LINE = re.compile(r"^(?P<platform>\w+):\s*(?P<state>.*?)\s*$")


class FeedError(Exception):
    """A reason the feed cannot be built; main() prints it and exits 1."""


def parse_sums(text: str) -> dict[str, str]:
    """filename -> lowercase sha256 from `sha256sum` output."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = _SUM_LINE.match(line.strip())
        if match:
            sums[Path(match["file"]).name] = match["sha"].lower()
    return sums


def parse_signing(text: str) -> dict[str, bool]:
    """platform -> signed, from lines like `windows: signed` / `linux: unsigned: <reason>`."""
    signed: dict[str, bool] = {}
    for line in text.splitlines():
        match = _SIGNING_LINE.match(line.strip())
        if match:
            signed[match["platform"].lower()] = match["state"].lower() == "signed"
    return signed


def build_feed(
    *,
    version: str,
    changelog: str,
    sums: str,
    signing: str,
    assets_base: str,
    download_page: str,
    published_at: str,
) -> LatestFeed:
    version = version.removeprefix("v")
    if "-" in version:
        raise FeedError(f"{version} is a prerelease; prereleases are never published to the feed")
    if not extract_section(changelog, version):
        raise FeedError(f"CHANGELOG has no non-empty section for {version}")
    checksums, signed = parse_sums(sums), parse_signing(signing)
    base = assets_base.rstrip("/")
    downloads: dict[str, Download] = {}
    for platform, pattern in PLATFORM_PATTERNS.items():
        names = sorted(n for n in checksums if fnmatch.fnmatch(n, pattern))
        if platform == "linux":
            names = [n for n in names if n.endswith("-x86_64.AppImage")] or names
        if not names:
            raise FeedError(f"SHA256SUMS has no {platform} file matching {pattern}")
        name = names[0]
        downloads[platform] = Download(
            file=name,
            url=f"{base}/{name}",
            sha256=checksums[name],
            signed=signed.get(platform, False),
        )
    releases = [
        ReleaseNote(version=v, date=d, notes_md=body)
        for v, d, body in list_sections(changelog)
        if "-" not in v and body
    ][:MAX_RELEASES_IN_FEED]
    feed = LatestFeed(
        schema=FEED_SCHEMA,
        version=version,
        published_at=published_at,
        download_page=download_page,
        downloads=downloads,
        releases=releases,
    )
    # Round trip: what is written must be what the app will parse.
    return LatestFeed.model_validate_json(feed.model_dump_json(by_alias=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--sums", default="SHA256SUMS.txt")
    parser.add_argument("--signing", default="SIGNING.txt")
    parser.add_argument("--assets-base", required=True)
    parser.add_argument("--download-page", default="https://kestrelapp.co/download")
    parser.add_argument("--published-at", default=None)
    parser.add_argument("--out", default="latest.json")
    args = parser.parse_args(argv)
    published = args.published_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        feed = build_feed(
            version=args.version,
            changelog=Path(args.changelog).read_text(encoding="utf-8"),
            sums=Path(args.sums).read_text(encoding="utf-8"),
            signing=Path(args.signing).read_text(encoding="utf-8"),
            assets_base=args.assets_base,
            download_page=args.download_page,
            published_at=published,
        )
    except OSError as exc:
        print(f"cannot read input: {exc}", file=sys.stderr)
        return 1
    except (FeedError, ValueError) as exc:
        print(f"latest.json not built: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(
        feed.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}: {feed.version}, {len(feed.releases)} release note(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
