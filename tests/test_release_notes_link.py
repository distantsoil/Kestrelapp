"""#209: the Release notes link opens the running version's GitHub release (D-UPD-11); the public
CHANGELOG never shows an empty [Unreleased]; a version's CHANGELOG date equals the feed's
published_at date. Invented text only; nothing here touches the network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import kestrel
from kestrel.services import updater

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_latest_json as blj  # noqa: E402
import changelog_section as cs  # noqa: E402

REPO = "https://github.com/distantsoil/Kestrelapp"
CHANGELOG_URL = f"{REPO}/blob/main/CHANGELOG.md"


# ----------------------------------------------------------------------- the link (item 1)
@pytest.mark.parametrize("version", ["0.7.6", "1.20.3"])
def test_release_notes_url_targets_the_running_version(version):
    assert updater.release_notes_url(version, published="99.0.0") == (
        f"{REPO}/releases/tag/v{version}"
    )


@pytest.mark.parametrize(
    "version",
    [
        "", "dev", "0.0.0+unknown", "0.7.7.dev3", "0.7", "v0.7.6", " 0.7.6", "0.7.6\n",
        "0.7.6/../x", "0.7.6?a=b", "0.7.6#x", "\u0660.7.6", "0.7.6-", "0.7.6-rc 1", "0.7.6-rc/1",
    ],
)
def test_release_notes_url_falls_back_to_the_changelog(version):
    assert updater.release_notes_url(version, published="99.0.0") == CHANGELOG_URL


def test_release_notes_url_has_no_query_fragment_or_userinfo():
    url = updater.release_notes_url("0.7.6", published="0.7.6")
    assert url.startswith("https://github.com/distantsoil/Kestrelapp/")
    assert not any(ch in url for ch in "?#@")


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_about_links_the_running_release_and_the_full_changelog(monkeypatch):
    import asyncio

    from tests.test_updates_ui import _feed, _prepare

    monkeypatch.setattr(kestrel, "__version__", "0.7.6")
    asyncio.run(_prepare(cached_feed=_feed("0.7.6")))
    with _client() as c:
        body = c.get("/about").text
    notes = body.split('data-testid="release-notes"')[0].rsplit("<a", 1)[1]
    assert f'href="{REPO}/releases/tag/v0.7.6"' in notes
    every = body.split('data-testid="see-every-change"')[0].rsplit("<a", 1)[1]
    assert f'href="{CHANGELOG_URL}"' in every


def test_about_for_a_dev_version_falls_back_to_the_changelog(monkeypatch):
    monkeypatch.setattr(kestrel, "__version__", "0.0.0+unknown")
    with _client() as c:
        body = c.get("/about").text
    notes = body.split('data-testid="release-notes"')[0].rsplit("<a", 1)[1]
    assert f'href="{CHANGELOG_URL}"' in notes


# --------------------------------------------------- empty [Unreleased] (item 2, D-UPD-12)
EMPTY = "# Changelog\n\n## [Unreleased]\n\n## [0.7.6] - 2026-11-12\n\n### Added\n\n- A thing.\n"
HEADINGS_ONLY = EMPTY.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Added\n\n### Fixed\n")
FILLED = EMPTY.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- Soon.\n")


def test_an_empty_unreleased_heading_is_dropped():
    out = cs.drop_empty_unreleased(EMPTY)
    assert "Unreleased" not in out
    assert out.startswith("# Changelog\n\n## [0.7.6] - 2026-11-12")


def test_unreleased_with_only_subheadings_is_dropped():
    out = cs.drop_empty_unreleased(HEADINGS_ONLY)
    assert "Unreleased" not in out and "### Added\n\n### Fixed" not in out
    assert "- A thing." in out


def test_unreleased_with_entries_is_kept_byte_for_byte():
    assert cs.drop_empty_unreleased(FILLED) == FILLED


def test_a_changelog_without_unreleased_is_unchanged():
    text = EMPTY.replace("## [Unreleased]\n\n", "")
    assert cs.drop_empty_unreleased(text) == text


def test_the_released_sections_survive_the_drop():
    out = cs.drop_empty_unreleased(HEADINGS_ONLY)
    assert cs.extract_section(out, "0.7.6") == "### Added\n\n- A thing."


# ship.tidy_public_changelog is tested in tests/test_ship_changelog.py: scripts/ship.py is private
# and not exported, so a test that imports it must be private too.


# -------------------------------------------------------------------- date check (item 3)
SUMS = (ROOT / "tests/fixtures/release/SHA256SUMS.txt").read_text(encoding="utf-8")
SIGNING = (ROOT / "tests/fixtures/release/SIGNING.txt").read_text(encoding="utf-8")


def _feed(changelog: str, published_at: str):
    return blj.build_feed(
        version="0.7.6", changelog=changelog, sums=SUMS, signing=SIGNING,
        assets_base="https://github.com/example/app/releases/download/v0.7.6",
        download_page="https://kestrelapp.co/download", published_at=published_at,
    )


def test_matching_utc_date_passes():
    assert cs.date_problem(EMPTY, "0.7.6", "2026-11-12T08:00:00Z") is None


def test_a_date_mismatch_is_reported():
    msg = cs.date_problem(EMPTY, "0.7.6", "2026-11-06T08:00:00Z")
    assert msg and "2026-11-12" in msg and "2026-11-06" in msg


def test_a_missing_changelog_date_is_reported():
    undated = EMPTY.replace("## [0.7.6] - 2026-11-12", "## [0.7.6]")
    assert "no date" in (cs.date_problem(undated, "0.7.6", "2026-11-12T08:00:00Z") or "")


def test_an_offset_timestamp_compares_on_the_utc_day():
    # 01:20 on the 13th at +02:00 is 23:20 UTC on the 12th.
    assert cs.date_problem(EMPTY, "0.7.6", "2026-11-13T01:20:00+02:00") is None
    # 23:20 on the 12th at -05:00 is 04:20 UTC on the 13th.
    assert cs.date_problem(EMPTY, "0.7.6", "2026-11-12T23:20:00-05:00")


@pytest.mark.parametrize("bad", ["", "t", "2026-13-40", "yesterday"])
def test_an_unreadable_timestamp_is_reported(bad):
    assert "timestamp" in (cs.date_problem(EMPTY, "0.7.6", bad) or "")


def test_the_cli_check_exits_nonzero_on_a_mismatch_and_zero_on_a_match(tmp_path, capsys):
    f = tmp_path / "CHANGELOG.md"
    f.write_text(EMPTY, encoding="utf-8")
    base = ["0.7.6", "--file", str(f), "--check-date"]
    assert cs.main([*base, "2026-11-13T00:00:00Z"]) == 1
    assert "2026-11-12" in capsys.readouterr().err
    assert cs.main([*base, "2026-11-12T23:20:00Z"]) == 0


def test_the_feed_build_never_fails_on_a_date_it_only_notes(capsys):
    """The website job runs after the release is published, so it cannot fail on a date."""
    feed = _feed(EMPTY, "2026-11-13T00:05:00Z")
    assert feed.version == "0.7.6"


def test_a_tag_at_2320_utc_with_the_website_after_midnight_still_builds(tmp_path):
    for name in ("SHA256SUMS.txt", "SIGNING.txt"):
        (tmp_path / name).write_text((ROOT / "tests/fixtures/release" / name).read_text("utf-8"),
                                     encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(EMPTY, encoding="utf-8")
    argv = ["--version", "0.7.6", "--changelog", str(tmp_path / "CHANGELOG.md"),
            "--sums", str(tmp_path / "SHA256SUMS.txt"), "--signing", str(tmp_path / "SIGNING.txt"),
            "--assets-base", "https://github.com/example/app/releases/download/v0.7.6",
            "--published-at", "2026-11-12T23:20:00Z", "--out", str(tmp_path / "latest.json")]
    assert blj.main(argv) == 0
    assert '"published_at": "2026-11-12T23:20:00Z"' in (tmp_path / "latest.json").read_text("utf-8")


# ------------------------------------------------------- release.yml: the check is in preflight
def _workflow():
    import yaml

    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))


def test_preflight_checks_the_changelog_date_against_the_tag_date():
    wf = _workflow()
    steps = wf["jobs"]["preflight"]["steps"]
    step = next(s for s in steps if s.get("name") == "Changelog date matches the tag date")
    run = step["run"]
    assert "--check-date" in run and "scripts/changelog_section.py" in run
    assert "taggerdate" in run and "date -u" in run
    names = [s.get("name") for s in steps]
    assert names.index(step["name"]) > names.index("Changelog section exists")
    assert "released_at" in wf["jobs"]["preflight"]["outputs"]


def test_the_website_job_takes_published_at_from_preflight_and_never_checks_after_publish():
    wf = _workflow()
    web = wf["jobs"]["website"]
    build = next(s for s in web["steps"] if s.get("name") == "Build latest.json")
    assert "--published-at" in build["run"]
    assert "needs.preflight.outputs.released_at" in str(build.get("env", {}))
    assert "--check-date" not in str(web)


# ------------------------------------------------- prerelease and ahead-of-feed builds (round 2)
def test_a_build_ahead_of_the_newest_published_release_opens_the_changelog():
    # 0.7.7 is not published yet (a prerelease build reports the base version): no tag to open.
    assert updater.release_notes_url("0.7.7", published="0.7.6") == CHANGELOG_URL


@pytest.mark.parametrize("published", ["0.7.7", "0.8.0"])
def test_a_build_the_feed_confirms_opens_its_release(published):
    assert updater.release_notes_url("0.7.7", published=published) == f"{REPO}/releases/tag/v0.7.7"


@pytest.mark.parametrize("published", [None, "", "junk"])
def test_with_no_confirmation_the_changelog_opens_not_a_tag_that_may_404(published):
    assert updater.release_notes_url("0.7.7", published=published) == CHANGELOG_URL


@pytest.mark.parametrize("published", [None, "", "0.7.6", "0.8.0", "0.7.7"])
def test_a_prerelease_version_opens_the_changelog_unless_the_feed_names_that_tag(published):
    assert updater.release_notes_url("0.7.7-rc.1", published=published) == CHANGELOG_URL


def test_a_prerelease_tag_the_feed_names_exactly_is_linked():
    assert updater.release_notes_url("0.7.7-rc.1", published="0.7.7-rc.1") == (
        f"{REPO}/releases/tag/v0.7.7-rc.1"
    )


def test_about_for_a_build_ahead_of_the_cached_feed_opens_the_changelog(monkeypatch):
    import asyncio

    from tests.test_updates_ui import _newer_state, _prepare

    monkeypatch.setattr(kestrel, "__version__", "0.0.1")
    asyncio.run(_prepare(**_newer_state()))
    with _client() as c:
        body = c.get("/about").text
    notes = body.split('data-testid="release-notes"')[0].rsplit("<a", 1)[1]
    assert "/releases/tag/" in notes  # the cached feed is newer than 0.0.1: its tag exists


def test_about_with_no_feed_yet_opens_the_changelog(monkeypatch):
    import asyncio

    from tests.test_updates_ui import _prepare

    monkeypatch.setattr(kestrel, "__version__", "0.7.6")
    asyncio.run(_prepare())
    with _client() as c:
        body = c.get("/about").text
    notes = body.split('data-testid="release-notes"')[0].rsplit("<a", 1)[1]
    assert f'href="{CHANGELOG_URL}"' in notes


def test_whats_new_text_matches_the_target_on_a_dev_build(monkeypatch):
    import asyncio

    from tests.test_updates_ui import _prepare

    monkeypatch.setattr(kestrel, "__version__", "0.0.0+unknown")
    asyncio.run(_prepare(upgraded_from="0.0.0"))
    with _client() as c:
        body = c.get("/updates/whats-new-card").text
    link = body.split('data-testid="whats-new-release-page"')[0].rsplit("<a", 1)[1]
    assert f'href="{CHANGELOG_URL}"' in link
    assert "GitHub release page" not in body and "full changelog</a>" in body


def test_the_real_changelog_dates_every_release_in_iso_form():
    import re

    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    stable = [(v, d) for v, d, _ in cs.list_sections(text) if "-" not in v]
    assert stable
    bad = [(v, d) for v, d in stable if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", d)]
    assert bad == []


def test_the_real_changelog_dates_0_7_6_as_published():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert dict((v, d) for v, d, _ in cs.list_sections(text))["0.7.6"] == "2026-11-12"


def _run_reldate(tmp_path, tag_stamp: str | None, changelog_date: str, *, tag: str = "v0.7.6",
                 local_lightweight: bool = False, event: str = "push"):
    """Run preflight's own date step in bash. `origin` is a bare repo holding the tag (annotated,
    dated `tag_stamp`; lightweight when None); the working repo is what checkout leaves, which
    with `local_lightweight` already holds the tag as a lightweight ref (actions/checkout#290)."""
    import os
    import shutil
    import subprocess

    bash = shutil.which("bash")
    assert bash, "bash is needed to run the preflight script"
    step = next(
        s for s in _workflow()["jobs"]["preflight"]["steps"]
        if s.get("name") == "Changelog date matches the tag date"
    )
    work, origin = tmp_path / "work", tmp_path / "origin.git"
    work.mkdir()
    (work / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/changelog_section.py", work / "scripts")
    (work / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [0.7.6] - {changelog_date}\n\n- A thing.\n", encoding="utf-8")
    stamp = tag_stamp or "2026-11-12T23:58:00+00:00"
    env = {**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp,
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    who = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]

    def git(cwd, *cmd):
        subprocess.run(["git", *cmd], cwd=cwd, env=env, check=True, capture_output=True)

    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, *who, "commit", "-qm", "x")
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(work, "remote", "add", "origin", str(origin))
    if tag_stamp is None:
        git(work, "tag", tag)
    else:
        git(work, *who, "tag", "-a", tag, "-m", "x")
    git(work, "push", "-q", "origin", f"refs/tags/{tag}")
    if local_lightweight:
        git(work, "tag", "-d", tag)
        git(work, "tag", tag)  # a lightweight ref of the same name, as checkout can leave it
    out = tmp_path / "out.txt"
    run_env = {**env, "GITHUB_EVENT_NAME": event, "GITHUB_REF_NAME": tag,
               "GITHUB_OUTPUT": str(out)}
    proc = subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                          cwd=work, env=run_env, capture_output=True, text=True, check=False)
    return proc, out.read_text(encoding="utf-8") if out.exists() else ""


def test_preflight_passes_when_the_heading_has_the_tag_utc_date(tmp_path):
    proc, written = _run_reldate(tmp_path, "2026-11-12T23:20:00+00:00", "2026-11-12")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "released_at=2026-11-12T23:20:00Z" in written


def test_preflight_reads_the_tag_date_on_the_utc_day_not_the_local_one(tmp_path):
    proc, written = _run_reldate(tmp_path, "2026-11-13T01:20:00+02:00", "2026-11-12")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "released_at=2026-11-12T23:20:00Z" in written


def test_a_tag_at_2358_utc_passes_when_the_run_starts_after_midnight(tmp_path):
    """The tag's own date decides, never the run's clock: this test runs on some later day."""
    proc, written = _run_reldate(tmp_path, "2026-11-12T23:58:00+00:00", "2026-11-12")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "released_at=2026-11-12T23:58:00Z" in written


def test_preflight_stops_the_run_on_a_date_mismatch(tmp_path):
    proc, _ = _run_reldate(tmp_path, "2026-11-12T23:20:00+00:00", "2026-11-06")
    assert proc.returncode == 1
    assert "2026-11-06" in proc.stderr and "::error::" in proc.stdout


def test_a_lightweight_tag_stops_the_run_and_says_to_annotate_it(tmp_path):
    proc, written = _run_reldate(tmp_path, None, "2026-11-12")
    assert proc.returncode == 1
    assert "git tag -a v0.7.6" in proc.stdout and "not an annotated tag" in proc.stdout
    assert "released_at" not in written


def test_a_tag_checkout_left_lightweight_is_refetched_as_the_annotated_tag(tmp_path):
    proc, written = _run_reldate(
        tmp_path, "2026-11-12T23:58:00+00:00", "2026-11-12", local_lightweight=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "released_at=2026-11-12T23:58:00Z" in written


def test_the_date_step_never_reads_the_run_clock():
    import re

    run = next(
        s for s in _workflow()["jobs"]["preflight"]["steps"]
        if s.get("name") == "Changelog date matches the tag date"
    )["run"]
    assert not re.search(r"date -u \+", run), "only `date -u -d <tag stamp>` may be used"
    assert 'git fetch --force --no-tags origin "refs/tags/${GITHUB_REF_NAME}' in run
    assert "${{" not in run, "no expression interpolation in run:"


@pytest.mark.parametrize("event,tag", [("push", "v0.7.7-rc.1"), ("workflow_dispatch", "main")])
def test_a_prerelease_tag_and_a_dry_run_are_not_date_checked(tmp_path, event, tag):
    proc, written = _run_reldate(tmp_path, None, "1999-01-01", tag=tag, event=event)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "released_at" not in written
