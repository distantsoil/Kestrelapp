"""The release scripts: the changelog extractor and the latest.json builder, against fixtures in
tests/fixtures/release/ (never the real CHANGELOG.md)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kestrel.services.updater import MAX_RELEASES_IN_FEED, LatestFeed

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures" / "release"
sys.path.insert(0, str(ROOT / "scripts"))

import build_latest_json as blj  # noqa: E402
import changelog_section as cs  # noqa: E402

CHANGELOG = (FIX / "CHANGELOG.md").read_text(encoding="utf-8")
BASE = "https://github.com/example/app/releases/download/v0.4.0"


def _build(changelog: str = CHANGELOG, version: str = "0.4.0") -> LatestFeed:
    return blj.build_feed(
        version=version,
        changelog=changelog,
        sums=(FIX / "SHA256SUMS.txt").read_text(encoding="utf-8"),
        signing=(FIX / "SIGNING.txt").read_text(encoding="utf-8"),
        assets_base=BASE,
        download_page="https://kestrelapp.co/download",
        published_at="2026-09-20T12:00:00Z",
    )


# ------------------------------------------------------------------------ changelog_section
def test_extract_section_returns_body_without_heading():
    body = cs.extract_section(CHANGELOG, "0.4.0")
    assert body is not None
    assert body.startswith("### Added") and "update checker" in body
    assert "## [" not in body and "Faster scoring" not in body


def test_extract_section_missing_and_empty():
    assert cs.extract_section(CHANGELOG, "9.9.9") is None
    assert cs.extract_section(CHANGELOG, "0.1.0") == ""


def test_list_sections_skips_unreleased_and_keeps_order():
    sections = cs.list_sections(CHANGELOG)
    assert [v for v, _, _ in sections] == [
        "0.5.0-rc.1",
        "0.4.0",
        "0.3.0",
        "0.2.0",
        "0.1.0",
        "0.0.9",
    ]
    assert sections[1][1] == "2026-09-20"


def test_cli_prints_section(capsys):
    assert cs.main(["0.3.0", "--file", str(FIX / "CHANGELOG.md")]) == 0
    assert "Faster scoring" in capsys.readouterr().out


@pytest.mark.parametrize("version", ["9.9.9", "0.1.0"])
def test_cli_fails_on_missing_or_empty(version, capsys):
    assert cs.main([version, "--file", str(FIX / "CHANGELOG.md")]) == 1
    assert version in capsys.readouterr().err


def test_unreleased_fallback_only_for_prereleases_and_only_when_asked():
    ok = cs.extract_section(CHANGELOG, "0.6.0-rc.1", allow_unreleased=True)
    assert ok is not None and "Something not shipped yet" in ok
    assert cs.extract_section(CHANGELOG, "0.6.0-rc.1") is None  # flag not given
    assert cs.extract_section(CHANGELOG, "9.9.9", allow_unreleased=True) is None  # stable
    # an exact section always wins over the fallback
    assert "Something not shipped yet" not in (
        cs.extract_section(CHANGELOG, "0.5.0-rc.1", allow_unreleased=True) or ""
    )


def test_cli_allow_unreleased(capsys):
    path = str(FIX / "CHANGELOG.md")
    assert cs.main(["0.6.0-rc.1", "--file", path]) == 1
    capsys.readouterr()
    assert cs.main(["0.6.0-rc.1", "--file", path, "--allow-unreleased"]) == 0
    assert "Something not shipped yet" in capsys.readouterr().out
    assert cs.main(["9.9.9", "--file", path, "--allow-unreleased"]) == 1


# ----------------------------------------------------------------------- build_latest_json
def test_feed_downloads_and_signing():
    feed = _build()
    assert set(feed.downloads) == {"windows", "macos", "linux"}
    win = feed.downloads["windows"]
    assert win.file == "Kestrel-Setup-0.4.0.exe" and win.url == f"{BASE}/{win.file}"
    assert win.sha256 == "3" * 64 and win.signed is True
    assert feed.downloads["macos"].signed is False
    assert feed.downloads["linux"].file.endswith("-x86_64.AppImage")


def test_feed_releases_exclude_prereleases_and_unreleased():
    feed = _build()
    assert [r.version for r in feed.releases][:2] == ["0.4.0", "0.3.0"]
    assert all("-" not in r.version for r in feed.releases)
    assert all(r.notes_md for r in feed.releases)  # the empty 0.1.0 section is not a note


def test_feed_caps_releases():
    many = "\n".join(f"## [1.{i}.0] - 2026-01-01\n\n- note {i}\n" for i in range(25, 0, -1))
    feed = _build(changelog=many + CHANGELOG, version="0.4.0")
    assert len(feed.releases) == MAX_RELEASES_IN_FEED
    assert feed.releases[0].version == "1.25.0"


def test_prerelease_refused():
    with pytest.raises(blj.FeedError, match="prerelease"):
        _build(version="0.5.0-rc.1")


def test_missing_or_empty_section_refused():
    with pytest.raises(blj.FeedError, match="no non-empty section"):
        _build(version="9.9.9")
    with pytest.raises(blj.FeedError, match="no non-empty section"):
        _build(version="0.1.0")


def test_missing_installer_refused():
    with pytest.raises(blj.FeedError, match="macos"):
        blj.build_feed(
            version="0.4.0",
            changelog=CHANGELOG,
            sums=(FIX / "SHA256SUMS.txt").read_text(encoding="utf-8").replace(".dmg", ".zip"),
            signing="",
            assets_base=BASE,
            download_page="x",
            published_at="t",
        )


def test_cli_writes_a_feed_that_round_trips(tmp_path):
    out = tmp_path / "latest.json"
    code = blj.main(
        [
            "--version",
            "0.4.0",
            "--changelog",
            str(FIX / "CHANGELOG.md"),
            "--sums",
            str(FIX / "SHA256SUMS.txt"),
            "--signing",
            str(FIX / "SIGNING.txt"),
            "--assets-base",
            BASE,
            "--published-at",
            "2026-09-20T12:00:00Z",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["schema"] == 1 and "schema_" not in raw
    feed = LatestFeed.model_validate(raw)
    assert feed.version == "0.4.0" and feed.published_at == "2026-09-20T12:00:00Z"
    assert feed.download_page == "https://kestrelapp.co/download"


def test_cli_exit_1_on_prerelease(tmp_path, capsys):
    code = blj.main(
        [
            "--version",
            "0.5.0-rc.1",
            "--changelog",
            str(FIX / "CHANGELOG.md"),
            "--sums",
            str(FIX / "SHA256SUMS.txt"),
            "--signing",
            str(FIX / "SIGNING.txt"),
            "--assets-base",
            BASE,
            "--out",
            str(tmp_path / "latest.json"),
        ]
    )
    assert code == 1 and "prerelease" in capsys.readouterr().err
    assert not (tmp_path / "latest.json").exists()
