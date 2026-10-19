"""The packaging contract, checked without running PyInstaller: the one spec keeps the narrowing
Follow-on 4 proved necessary and builds per platform; the frozen entry point dispatches arguments
to the CLI; the release workflow names the three installers; the Inno Setup script installs the
one-folder output under the agreed name; the AppImage and dmg scripts are pinned and forward
arguments. The builds themselves run in CI (docs/RELEASING.md)."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

from kestrel import __version__

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
SPEC = PACKAGING / "kestrel.spec"
ISS = PACKAGING / "kestrel.iss"
WORKFLOWS = ROOT / ".github" / "workflows"


def _code(path: Path) -> str:
    """The file without comment lines, so a comment explaining a rule cannot satisfy its check."""
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


# ------------------------------------------------------------------------------- the spec
def test_spec_parses_as_python():
    ast.parse(SPEC.read_text(encoding="utf-8"), filename=str(SPEC))


def test_spec_keeps_the_follow_on_4_narrowing():
    """collect_submodules on the two string-imported subpackages and NEVER on the whole package
    (Follow-on 4: collecting all of kestrel crashes PyInstaller's isolated import)."""
    src = _code(SPEC)
    assert 'collect_submodules("kestrel.web.pages")' in src
    assert 'collect_submodules("kestrel.ingest.signal_sources")' in src
    calls = re.findall(r'collect_submodules\("([^"]+)"\)', src)
    assert "kestrel" not in calls, "collect_submodules('kestrel') breaks the build; see Follow-on 4"


def test_spec_ships_the_load_bearing_datas_and_hidden_imports():
    src = SPEC.read_text(encoding="utf-8")
    # Alembic versions as DATA under the kestrel/ prefix, resolved by paths.packaged_migrations.
    assert 'ROOT / "kestrel" / "db" / "migrations"' in src
    assert "f.parent.relative_to(ROOT)" in src
    # Package resources (config, prompts, Modelfiles, tiktoken cache, web assets).
    assert '"resources/**/*"' in src and '"web/static/**/*"' in src
    assert '"web/templates/**/*"' in src
    # tiktoken's namespace plugin and starlette's multipart backend.
    assert '"tiktoken_ext", "tiktoken_ext.openai_public"' in src
    assert '"python_multipart", "multipart"' in src
    # The one entry point, and one-folder output.
    assert '"desktop" / "__main__.py"' in src
    assert "COLLECT(" in src
    assert "onefile" not in src.lower()


def test_spec_is_platform_aware_and_windowed_everywhere():
    src = _code(SPEC)
    assert 'sys.platform == "win32"' in src and 'sys.platform == "darwin"' in src
    assert "kestrel.ico" in src and "kestrel.icns" in src
    # The desktop EXE is windowed on every platform; the only console EXE is the Windows CLI twin.
    assert src.count("console=False") == 1
    assert src.count("console=True") == 1
    assert 'name="kestrel-cli"' in src
    # The macOS bundle and its plist.
    assert "BUNDLE(" in src and 'name="Kestrel.app"' in src
    for key in (
        "CFBundleIdentifier",
        "CFBundleShortVersionString",
        "NSHighResolutionCapable",
        "LSMinimumSystemVersion",
    ):
        assert key in src, key
    # clr_loader/pythonnet are win32-only dependencies; collecting them unconditionally would
    # abort the macOS and Linux builds.
    assert 'find_spec("clr_loader")' in src


def test_spec_reads_the_single_source_version():
    src = SPEC.read_text(encoding="utf-8")
    pattern = re.search(r"re\.search\(\s*r'([^']+)'", src).group(1)
    init = (ROOT / "kestrel" / "__init__.py").read_text(encoding="utf-8")
    assert re.search(pattern, init, re.MULTILINE).group(1) == __version__


def test_macos_icon_exists_and_is_icns():
    icns = PACKAGING / "kestrel.icns"
    assert icns.exists(), "run scripts/make_icons.py --icns"
    assert icns.read_bytes()[:4] == b"icns"
    assert (PACKAGING / "kestrel.ico").exists()


# ------------------------------------------------------------------------ the entry point
def test_entry_point_dispatches_arguments_to_the_cli(monkeypatch):
    from kestrel.desktop import __main__ as entry

    calls: list[tuple] = []
    monkeypatch.setattr(entry, "_repair_streams", lambda: None)
    monkeypatch.setattr("kestrel.cli.app", lambda **kw: calls.append(("cli", kw)))
    monkeypatch.setattr(
        "kestrel.desktop.shell.run_desktop", lambda: calls.append(("desktop", {})) or 0
    )

    monkeypatch.setattr(sys, "argv", ["Kestrel", "doctor", "--quick"])
    entry.main()
    assert calls == [("cli", {"prog_name": "kestrel"})]
    assert sys.argv == ["Kestrel", "doctor", "--quick"]


def test_entry_point_with_no_arguments_opens_the_desktop(monkeypatch):
    import pytest

    from kestrel.desktop import __main__ as entry

    calls: list[str] = []
    monkeypatch.setattr(entry, "_repair_streams", lambda: None)
    monkeypatch.setattr("kestrel.cli.app", lambda **kw: calls.append("cli"))
    monkeypatch.setattr("kestrel.desktop.shell.run_desktop", lambda: calls.append("desktop") or 0)

    monkeypatch.setattr(sys, "argv", ["Kestrel"])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 0
    assert calls == ["desktop"]


def test_entry_point_ignores_the_finder_process_serial_number():
    from kestrel.desktop.__main__ import cli_args

    assert cli_args(["Kestrel", "-psn_0_12345"]) == []
    assert cli_args(["Kestrel", "-psn_0_12345", "setup"]) == ["setup"]
    assert cli_args(["Kestrel", "--help"]) == ["--help"]


def test_entry_point_keeps_freeze_support_first():
    src = (ROOT / "kestrel" / "desktop" / "__main__.py").read_text(encoding="utf-8")
    main_block = src.split('if __name__ == "__main__":', 1)[1]
    assert main_block.strip().startswith("multiprocessing.freeze_support()")


# ----------------------------------------------------------------------------- workflows
def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_ci_workflow_runs_the_three_platforms():
    """D-CI-1: all three platforms on every push to main; PRs run Linux and Windows (macOS
    minutes cost 10x); branch pushes are covered by pull_request, never run twice."""
    import json

    wf = _workflow("ci.yml")
    expr = wf["jobs"]["test"]["strategy"]["matrix"]["os"]
    on_push, on_pr = (json.loads(x) for x in re.findall(r"'(\[[^']*\])'", expr))
    assert "github.event_name == 'push'" in expr
    assert set(on_push) == {"ubuntu-latest", "macos-latest", "windows-latest"}
    assert set(on_pr) == {"ubuntu-latest", "windows-latest"}
    triggers = wf.get("on") or wf.get(True)
    assert triggers["push"]["branches"] == ["main"] and "pull_request" in triggers
    runs = "\n".join(step.get("run", "") for step in wf["jobs"]["test"]["steps"])
    assert "uv sync --extra dev" in runs
    assert "ruff check" in runs and "pytest" in runs


def test_release_workflow_names_the_three_artifacts():
    wf = _workflow("release.yml")
    on = wf.get("on", wf.get(True))  # PyYAML reads the bare `on:` key as boolean True
    assert on["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in on

    jobs = wf["jobs"]
    uploads = {}
    for job in ("linux", "macos", "windows"):
        for step in jobs[job]["steps"]:
            if str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                uploads[job] = step["with"]["path"]
    assert "dist/Kestrel-Setup-*.exe" in uploads["windows"].split()
    assert "dist/Kestrel-*.dmg" in uploads["macos"].split()
    assert "dist/Kestrel-*-x86_64.AppImage" in uploads["linux"].split()

    release = jobs["release"]
    assert set(release["needs"]) == {"linux", "macos", "windows"}
    assert "refs/tags/v" in release["if"]
    gh = next(s for s in release["steps"] if str(s.get("uses", "")).startswith("softprops/"))
    assert gh["with"]["generate_release_notes"] is False
    assert "Kestrel-Setup-*.exe" in gh["with"]["files"]
    assert "Kestrel-*.dmg" in gh["with"]["files"]
    assert "Kestrel-*-x86_64.AppImage" in gh["with"]["files"]


def test_release_preflight_refuses_a_tag_that_is_not_the_package_version():
    """D8: a tag push must match kestrel.__version__ (prerelease suffix ignored), or the release
    would publish installers and a feed whose version disagrees with the app they install."""
    wf = _workflow("release.yml")
    step = next(
        s for s in wf["jobs"]["preflight"]["steps"] if s.get("name") == "Changelog section exists"
    )
    run = step["run"]
    push_branch = run.split('if [ "$GITHUB_EVENT_NAME" = "push" ]; then', 1)[1].split("else", 1)[0]
    assert "__version__" in push_branch
    assert "::error::" in run
    assert "mismatch" in run or "does not match" in run
    assert run.index("::error::") > run.index("__version__")


def test_release_workflow_marks_unsigned_artifacts():
    """Every build job writes a SIGNING-<platform>.txt ("signed", or "unsigned: add <secrets> to
    sign") beside its installer and uploads it; the release step attaches the merged SIGNING.txt
    and puts the lines in the release body, so an unsigned download is never silent."""
    wf = _workflow("release.yml")
    jobs = wf["jobs"]
    secrets = {
        "windows": ["WINDOWS_CERT_PFX_BASE64", "WINDOWS_CERT_PASSWORD"],
        "macos": ["APPLE_SIGNING_IDENTITY", "APPLE_CERT_P12_BASE64", "APPLE_CERT_PASSWORD"],
        "linux": [],
    }
    for job, names in secrets.items():
        steps = jobs[job]["steps"]
        marker = next(s for s in steps if s.get("name") == "Signing marker")
        run = marker["run"]
        assert f"SIGNING-{job}.txt" in run
        assert f"{job}: unsigned: " in run
        if names:
            assert f"{job}: signed" in run
            for name in names:
                assert name in run, (job, name)
        upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-"))
        assert f"dist/SIGNING-{job}.txt" in upload["with"]["path"].split()
        # the marker is written after the build and before the upload
        assert steps.index(marker) < steps.index(upload)
    release = jobs["release"]
    name = "Release notes and signing status"
    status = next(s for s in release["steps"] if s.get("name") == name)
    for job in secrets:
        assert f"SIGNING-{job}.txt" in status["run"]
    assert "> SIGNING.txt" in status["run"] and "RELEASE_BODY.md" in status["run"]
    gh = next(s for s in release["steps"] if str(s.get("uses", "")).startswith("softprops/"))
    assert "artifacts/SIGNING.txt" in gh["with"]["files"]
    assert gh["with"]["body_path"] == "artifacts/RELEASE_BODY.md"


# The lowest major of each action that runs on Node 24. GitHub retires a Node runtime about
# once a year and warns on every job first (the Node 20 warning in the release dry run, run
# 38320106428); this fails locally instead. Raise a floor here when the next warning appears,
# and add any new action before using it (D-CI-2).
NODE24_ACTION_FLOORS = {
    "actions/checkout": 5,
    "actions/upload-artifact": 6,
    "actions/download-artifact": 7,
    "astral-sh/setup-uv": 7,
    "softprops/action-gh-release": 3,
}


def test_release_dry_run_can_build_one_platform_but_a_tag_builds_all():
    """D-CI-3: the manual dry run offers all / windows / macos / linux; each build job runs only
    for its own choice or "all"; a tag push builds every platform and only then publishes."""
    wf = _workflow("release.yml")
    triggers = wf.get("on") or wf.get(True)
    choice = triggers["workflow_dispatch"]["inputs"]["platform"]
    assert choice["type"] == "choice" and choice["default"] == "all"
    assert choice["options"] == ["all", "windows", "macos", "linux"]
    jobs = wf["jobs"]
    for job in ("windows", "macos", "linux"):
        assert jobs[job]["if"] == (
            f"github.event_name == 'push' || inputs.platform == 'all' || inputs.platform == '{job}'"
        )
    release = jobs["release"]
    assert set(release["needs"]) == {"windows", "macos", "linux"}
    assert "github.event_name == 'push'" in release["if"]  # a dry run never publishes


def test_ci_skips_only_files_no_test_or_build_reads():
    """D-CI-2: docs-only pushes skip CI, but never a path the app or the tests read (the LLM
    prompts are .md under kestrel/, and tests read docs/SOURCES.md)."""
    triggers = _workflow("ci.yml").get("on") or _workflow("ci.yml").get(True)
    push, pr = triggers["push"]["paths-ignore"], triggers["pull_request"]["paths-ignore"]
    assert push == pr
    for pattern in push:
        assert "*" not in pattern or pattern.startswith("docs/images/"), pattern
        assert not pattern.startswith(("kestrel/", "tests/", "packaging/", ".github/")), pattern
        assert pattern != "docs/SOURCES.md"


def test_every_workflow_action_meets_its_node24_floor():
    for name in ("ci.yml", "release.yml"):
        for job in _workflow(name)["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if not uses:
                    continue
                action, _, ref = uses.partition("@")
                assert action in NODE24_ACTION_FLOORS, (
                    f"{name}: {action} has no Node 24 floor; add one to NODE24_ACTION_FLOORS"
                )
                major = int(re.match(r"v(\d+)", ref).group(1))
                assert major >= NODE24_ACTION_FLOORS[action], (
                    f"{name}: {uses} predates Node 24; use v{NODE24_ACTION_FLOORS[action]} or later"
                )


def test_workflow_actions_are_pinned_to_major_versions():
    for name in ("ci.yml", "release.yml"):
        wf = _workflow(name)
        for job in wf["jobs"].values():
            for step in job["steps"]:
                uses = step.get("uses")
                if uses:
                    assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", uses), f"{name}: {uses}"


def test_release_notes_come_from_the_changelog_and_prereleases_are_marked():
    wf = _workflow("release.yml")
    steps = wf["jobs"]["release"]["steps"]
    run = "\n".join(s.get("run", "") for s in steps)
    assert "scripts/changelog_section.py" in run
    gh = next(s for s in steps if str(s.get("uses", "")).startswith("softprops/"))
    assert gh["with"]["generate_release_notes"] is False
    assert "contains(github.ref_name, '-')" in str(gh["with"]["prerelease"])


def test_website_job_publishes_latest_json_only_for_real_stable_tags():
    wf = _workflow("release.yml")
    job = wf["jobs"]["website"]
    assert job["needs"] == ["release"] or job["needs"] == "release"
    cond = job["if"]
    assert "github.event_name == 'push'" in cond
    assert "refs/tags/v" in cond
    assert "!contains(github.ref_name, '-')" in cond
    assert job["permissions"] == {"contents": "read"}
    run = "\n".join(s.get("run", "") for s in job["steps"])
    assert "scripts/build_latest_json.py" in run
    site = next(s for s in job["steps"] if s.get("with", {}).get("path") == "site")
    assert site["with"]["token"] == "${{ secrets.WEBSITE_TOKEN }}"
    # the push runs inside the site checkout, which persisted that token
    push = next(s for s in job["steps"] if "git push" in s.get("run", ""))
    assert "cd site" in push["run"]
    assert site["with"].get("persist-credentials", True) is True


def test_release_preflight_gates_every_build():
    wf = _workflow("release.yml")
    pre = wf["jobs"]["preflight"]
    assert pre["runs-on"] == "ubuntu-latest"
    for name in ("linux", "macos", "windows"):
        needs = wf["jobs"][name]["needs"]
        assert "preflight" in ([needs] if isinstance(needs, str) else needs), name
    run = "\n".join(s.get("run", "") for s in pre["steps"])
    assert "scripts/changelog_section.py" in run
    assert "--allow-unreleased" in run
    assert "::error::" in run and "HAS_WEBSITE_TOKEN" in run
    token_step = next(s for s in pre["steps"] if "HAS_WEBSITE_TOKEN" in s.get("run", ""))
    assert "refs/tags/v" in token_step["if"]  # a tag push only, never a dry run
    assert "!contains(github.ref_name, '-')" in token_step["if"]  # stable versions only
    # the release body uses the same prerelease logic
    body = "\n".join(s.get("run", "") for s in wf["jobs"]["release"]["steps"])
    assert "--allow-unreleased" in body


def test_website_token_is_only_ever_the_checkout_token_input():
    import re

    wf = _workflow("release.yml")
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*[\w-]+:\s*\$\{\{[^}]*secrets\.WEBSITE_TOKEN[^}]*\}\}", text, re.M)
    allowed = {"HAS_WEBSITE_TOKEN: ${{ secrets.WEBSITE_TOKEN != '' }}",
               "token: ${{ secrets.WEBSITE_TOKEN }}"}
    assert {u.strip() for u in uses} <= allowed and len(uses) == 2, uses
    assert "secrets.WEBSITE_TOKEN }}" not in "".join(
        u for u in uses if u.strip().startswith(("WEBSITE_TOKEN", "GH_TOKEN"))
    )
    for name, job in wf["jobs"].items():
        envs = [job.get("env") or {}] + [st.get("env") or {} for st in job["steps"]]
        for env in envs:
            assert "WEBSITE_TOKEN" not in env, name
            for key, value in env.items():
                if "secrets.WEBSITE_TOKEN" in str(value):
                    assert key == "HAS_WEBSITE_TOKEN" and "!= ''" in str(value), (name, key)
        for st in job["steps"]:
            if "secrets.WEBSITE_TOKEN" in str(st.get("with", {})):
                assert str(st["uses"]).startswith("actions/checkout@")
                assert list(k for k, v in st["with"].items()
                            if "WEBSITE_TOKEN" in str(v)) == ["token"]


def test_real_changelog_has_a_clean_section_for_this_version():
    import pytest

    path = ROOT / "CHANGELOG.md"
    if not path.exists():
        pytest.skip("CHANGELOG.md not present")
    text = path.read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from changelog_section import extract_section
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    body = extract_section(text, __version__)
    assert body, f"CHANGELOG.md needs a non-empty '## [{__version__}]' section"
    assert not re.search(r"#\d+", text), "no issue or PR numbers in the changelog"
    shas = [
        m for m in re.findall(r"\b[0-9a-f]{7,40}\b", text)
        if re.search(r"[a-f]", m) and re.search(r"\d", m)
    ]
    assert not shas, f"no commit SHAs in the changelog: {shas}"
    assert not re.search(r"(?i)distantsoil/kestrel(?!app)", text)


# -------------------------------------------------------------------------------- Windows
def test_iss_has_no_stray_preprocessor_lines():
    """Inno's preprocessor (ISPP) reads any line whose first non-blank character is '#' as a
    directive. A Pascal continuation line starting with #13#10 broke the v0.1.0 release build
    ("Unknown preprocessor directive"); only real directives may start a line with '#'."""
    known = (
        "include",
        "define",
        "if",
        "ifdef",
        "ifndef",
        "else",
        "elif",
        "endif",
        "expr",
        "pragma",
    )
    for n, line in enumerate(ISS.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            word = re.match(r"#\s*(\w*)", stripped).group(1).lower()
            assert word in known, f"kestrel.iss line {n} would be read as a directive: {line!r}"


def test_iss_uninstall_offers_data_cleanup_safely():
    """D-PKG-UNINSTALL: ask after uninstall, default No, never on a silent uninstall, and only
    ever the data home (never {app}, never all of LOCALAPPDATA)."""
    from kestrel import paths

    iss = ISS.read_text(encoding="utf-8")
    code = iss[iss.index("[Code]") :]
    assert "usPostUninstall" in code and "UninstallSilent" in code
    assert "MB_YESNO or MB_DEFBUTTON2" in code and "= IDYES" in code
    target = f"ExpandConstant('{{localappdata}}\\{paths.APP_NAME}')"
    assert target in code
    deltrees = re.findall(r"DelTree\((\w+)", code)
    assert deltrees == ["DataDir"]
    assert "{app}" not in code
    assert "—" not in code


def test_iss_installs_the_one_folder_output_under_the_agreed_name():
    iss = ISS.read_text(encoding="utf-8")
    assert "OutputBaseFilename=Kestrel-Setup-{#AppVersion}" in iss
    assert 'Source: "..\\dist\\Kestrel\\*"' in iss
    assert "recursesubdirs" in iss
    assert "PrivilegesRequired=lowest" in iss
    assert "DefaultDirName={localappdata}\\Programs\\Kestrel" in iss
    # The mutex the installer waits on is the shell's, byte for byte.
    from kestrel.desktop import shell

    assert f"AppMutex={shell.MUTEX_NAME}" in iss
    # version.iss is generated from __version__ and committed in step.
    assert (PACKAGING / "version.iss").read_text(encoding="utf-8").strip() == (
        f'#define AppVersion "{__version__}"'
    )


def test_windows_build_script_is_gated_on_the_certificate_secrets():
    ps1 = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")
    assert "WINDOWS_CERT_PFX_BASE64" in ps1 and "WINDOWS_CERT_PASSWORD" in ps1
    assert "UNSIGNED" in ps1
    assert "kestrel-cli.exe" in ps1
    assert "Kestrel-Setup-$Version.exe" in ps1


# ---------------------------------------------------------------------------------- Linux
def test_apprun_forwards_every_argument_to_the_frozen_binary():
    apprun = (PACKAGING / "linux" / "AppRun").read_text(encoding="utf-8")
    assert apprun.startswith("#!/bin/sh")
    assert 'exec "$HERE/usr/lib/kestrel/kestrel" "$@"' in apprun


def test_desktop_entry_is_well_formed():
    desktop = (PACKAGING / "linux" / "kestrel.desktop").read_text(encoding="utf-8")
    assert desktop.startswith("[Desktop Entry]")
    for line in (
        "Type=Application",
        "Name=Kestrel",
        "Exec=kestrel %U",
        "Icon=kestrel",
        "Terminal=false",
    ):
        assert line in desktop, line
    assert re.search(r"^Categories=.*;$", desktop, re.MULTILINE)


def test_appimage_script_is_pinned_and_names_the_artifact():
    sh = (PACKAGING / "build_appimage.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in sh
    assert re.search(r'APPIMAGETOOL_URL=".*/releases/download/\d+\.\d+\.\d+/', sh), "pin a release"
    assert "continuous" not in re.search(r'APPIMAGETOOL_URL="([^"]+)"', sh).group(1)
    assert re.search(r'APPIMAGETOOL_SHA256="[0-9a-f]{64}"', sh)
    assert re.search(r'RUNTIME_SHA256="[0-9a-f]{64}"', sh)
    assert "--runtime-file" in sh
    assert "sha256sum -c" in sh
    assert "--appimage-extract-and-run" in sh
    assert "Kestrel-${VERSION}-${ARCH_NAME}.AppImage" in sh and 'ARCH_NAME="x86_64"' in sh


# ---------------------------------------------------------------------------------- macOS
def test_dmg_script_names_the_artifact_and_gates_signing():
    sh = (PACKAGING / "build_dmg.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in sh
    assert "Kestrel-${VERSION}.dmg" in sh
    assert "hdiutil create" in sh and "UDZO" in sh
    assert "ln -s /Applications" in sh
    assert "APPLE_SIGNING_IDENTITY" in sh and "UNSIGNED" in sh
    for var in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD"):
        assert var in sh, var
    assert "notarytool submit" in sh and "stapler staple" in sh
    assert "entitlements.plist" in sh
    plist = (PACKAGING / "macos" / "entitlements.plist").read_text(encoding="utf-8")
    assert "com.apple.security.cs.disable-library-validation" in plist


def test_every_package_data_file_is_shipped():
    """D-PKG-3: the PDF template was never in the spec's collect_data_files includes, so every
    preview and render in the packaged 0.2.0 failed with TemplateNotFound. Every non-Python
    file under kestrel/ must match an include pattern, unless it is named here as not needed at
    run time."""
    import fnmatch
    import re

    spec = SPEC.read_text(encoding="utf-8")
    m = re.search(r'collect_data_files\("kestrel", includes=\[(.*?)\]\)', spec, re.S)
    assert m, "the kestrel collect_data_files call moved; update this test"
    patterns = re.findall(r'"([^"]+)"', m.group(1))
    not_needed = {"db/migrations/script.py.mako"}  # alembic uses it only to write new revisions
    pkg = ROOT / "kestrel"
    missing = []
    for f in pkg.rglob("*"):
        if not f.is_file() or f.suffix in (".py", ".pyc") or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(pkg).as_posix()
        if rel.startswith("resources/embed_model/") or rel in not_needed:
            continue
        # PyInstaller's `**` matches zero or more directories; fnmatch needs the zero case spelled
        # out ("web/templates/**/*" also covers "web/templates/base.html").
        if not any(fnmatch.fnmatch(rel, q) for p in patterns for q in (p, p.replace("**/", ""))):
            missing.append(rel)
    assert not missing, f"package data not shipped by packaging/kestrel.spec: {missing}"


def test_the_spec_ships_jobspys_curl_cffi_library():
    """D-PKG-4 (amended): JobSpy 1.3.0 loads libcurl-impersonate from curl_cffi.libs, a sibling
    folder that is not a Python package, so it must be added to the bundle by name. tls_client is
    gone from JobSpy and from the spec."""
    spec = SPEC.read_text(encoding="utf-8")
    assert 'collect_all("curl_cffi"' in spec
    assert "curl_cffi.libs" in spec
    assert "tls_client" not in spec.replace("tls_client Go library", "")


def test_the_spec_skips_third_party_test_suites_and_unused_deprecated_modules():
    """D-PKG-5: every failed submodule import and deprecated package in the build log was a test
    suite or an optional backend Kestrel never loads. The filter and the excludes stay."""
    spec = SPEC.read_text(encoding="utf-8")
    assert '_SKIP_PARTS = {"tests", "testing", "conftest", "android", "torch", "cupy", "dask"}' in (
        spec
    )
    assert 'collect_submodules("sklearn", filter=_keep)' in spec
    assert "collect_submodules(pkg, filter=_keep)" in spec
    assert "collect_all(pkg, filter_submodules=_keep)" in spec
    assert '"authlib", "numpy.array_api"]' in spec


def test_the_app_never_imports_what_the_spec_excludes():
    """Excluding authlib and numpy.array_api is safe only while no Kestrel entry point reaches
    them. A fresh interpreter imports the desktop entry, the web app and every MCP module."""
    import subprocess

    code = (
        "import sys, importlib, pkgutil, kestrel.mcp, kestrel.desktop.__main__, kestrel.web.app\n"
        "for m in pkgutil.walk_packages(kestrel.mcp.__path__, 'kestrel.mcp.'):\n"
        "    importlib.import_module(m.name)\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] == 'authlib'"
        " or m.startswith('numpy.array_api'))\n"
        "print(bad)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
        check=True,
    )
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_the_test_filter_keeps_every_sklearn_module_the_reranker_needs():
    from PyInstaller.utils.hooks import collect_submodules

    spec = SPEC.read_text(encoding="utf-8")
    ns: dict = {}
    exec(spec[spec.index("_SKIP_PARTS =") : spec.index("for pkg in collect_all_pkgs")], ns)
    kept = set(collect_submodules("sklearn", filter=ns["_keep"]))
    assert not any(".tests" in m for m in kept)
    for need in (
        "sklearn.ensemble",
        "sklearn.decomposition",
        "sklearn.ensemble._hist_gradient_boosting",
        "sklearn.utils._typedefs",
    ):
        assert need in kept, need


def test_release_uploads_to_a_draft_and_publishes_it_last():
    """Immutable releases (on for the public repo) refuse an upload to a published release. A
    v0.6.0 release published from the web form before the build ended with no installers and a
    tag that can never be reused, so the workflow drafts, uploads, then publishes."""
    wf = _workflow("release.yml")
    steps = wf["jobs"]["release"]["steps"]
    gh = next(s for s in steps if str(s.get("uses", "")).startswith("softprops/"))
    assert gh["with"]["draft"] is True
    publish = next(s for s in steps if s.get("name") == "Publish the release")
    assert steps.index(gh) < steps.index(publish)
    assert f"steps.{gh['id']}.outputs.id" in publish["env"]["RELEASE_ID"]
    # -F sends a JSON boolean; -f would send the string "false", which the API refuses.
    assert "-F draft=false" in publish["run"]
    assert '-z "$RELEASE_ID"' in publish["run"]


def test_release_preflight_refuses_a_tag_that_already_has_a_release():
    """A tag whose release is already published fails in preflight, before 35 minutes of builds
    that could not be attached to it."""
    wf = _workflow("release.yml")
    steps = wf["jobs"]["preflight"]["steps"]
    check = next(s for s in steps if s.get("name") == "No published release for this tag")
    assert "github.event_name == 'push'" in check["if"]
    assert "gh release view" in check["run"] and "exit 1" in check["run"]
    assert "GH_TOKEN" in check["env"]
