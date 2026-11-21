"""The packaging contract, checked without running PyInstaller: the one spec keeps the narrowing
Follow-on 4 proved necessary and builds per platform; the frozen entry point dispatches arguments
to the CLI; the release workflow names the three installers; the Inno Setup script installs the
one-folder output under the agreed name; the AppImage and dmg scripts are pinned and forward
arguments. The builds themselves run in CI (docs/RELEASING.md)."""

from __future__ import annotations

import ast
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
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


def test_spec_copies_the_metadata_fastmcp_reads_at_import():
    """fastmcp reads its own version from package metadata when imported; the packaged app had
    none and the MCP server could not start (#130). Removing the metadata line fails this."""
    src = _code(SPEC)
    assert re.search(r"^\s+copy_metadata,$", src, re.M)  # imported
    m = re.search(r"for _dist in \(([^)]*)\):", src)
    assert m, "the spec must loop copy_metadata over the distributions fastmcp looks up"
    names = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert {"fastmcp", "fastmcp-slim", "mcp"} <= names
    assert "copy_metadata(_dist" in src


def test_every_distribution_fastmcp_reads_by_name_is_in_the_spec():
    """The names come from fastmcp's own source, so a new lookup after an upgrade fails here."""
    import importlib.util

    root = Path(importlib.util.find_spec("fastmcp").origin).parent
    found: set[str] = set()
    for f in root.rglob("*.py"):
        found.update(re.findall(
            r'\b_?(?:version|distribution)\(\s*"([A-Za-z0-9_.-]+)"',
            f.read_text(encoding="utf-8")))
    found.discard("")
    src = _code(SPEC)
    for dist in found:
        assert f'"{dist}"' in src, f"fastmcp reads metadata for {dist}; add it to kestrel.spec"
    assert {"fastmcp-slim", "fastmcp", "mcp"} <= found


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

    def _end(code: int) -> None:  # the real one ends the process at once (os._exit)
        raise SystemExit(code)

    monkeypatch.setattr(entry, "end_process", _end)
    monkeypatch.setattr(sys, "argv", ["Kestrel"])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 0
    assert calls == ["desktop"]


def test_end_process_flushes_logs_then_exits_at_once(monkeypatch):
    from kestrel.desktop import __main__ as entry

    seen: list = []
    monkeypatch.setattr("logging.shutdown", lambda: seen.append("logs"))
    monkeypatch.setattr("os._exit", lambda code: seen.append(("exit", code)))
    entry.end_process(3)
    assert seen == ["logs", ("exit", 3)]


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


# The release waits for the three platform jobs and the two Windows signing jobs; the signing jobs
# are skipped when signing is not configured, and a failed one still blocks the release.
_RELEASE_NEEDS = {
    "preflight",
    "linux",
    "macos",
    "macos-sign",
    "macos-sign-test",
    "macos-sign-check",
    "windows",
    "windows-tools",
    "windows-sign",
    "windows-sign-test",
    "windows-verify",
}


def test_release_workflow_names_the_three_artifacts():
    wf = _workflow("release.yml")
    on = wf.get("on", wf.get(True))  # PyYAML reads the bare `on:` key as boolean True
    assert on["push"]["tags"] == ["v[0-9]*"]
    assert "workflow_dispatch" in on

    jobs = wf["jobs"]
    uploads = {}
    for job in ("linux", "macos", "windows"):
        for step in jobs[job]["steps"]:
            is_upload = str(step.get("uses", "")).startswith("actions/upload-artifact@")
            if is_upload and step["with"]["name"].startswith("Kestrel-"):
                uploads[job] = step["with"]["path"]
    assert "dist/Kestrel-Setup-*.exe" in uploads["windows"].split()
    assert "dist/Kestrel-*.dmg" in uploads["macos"].split()
    assert "dist/Kestrel-*-x86_64.AppImage" in uploads["linux"].split()

    release = jobs["release"]
    assert set(release["needs"]) == _RELEASE_NEEDS
    assert "needs.preflight.outputs.is_version_tag == 'true'" in release["if"]
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
        "windows": ["HAS_WINDOWS_CERT"],  # the marker reads a flag; the old text named secrets
        # the macOS build job only ever writes the unsigned line; "macos: signed" is written by
        # macos-sign after its checks pass (tests/test_release_macos.py, #202)
        "macos": [],
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
        upload = next(
            s
            for s in steps
            if str(s.get("uses", "")).startswith("actions/upload-")
            and s["with"]["name"].startswith("Kestrel-")
        )
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


# Signing in release.yml (#194). The jobs: preflight decides the mode, windows builds,
# windows-tools fetches the tools without a token, windows-sign (production, tags only) and
# windows-sign-test (dispatch from main) are the only jobs that hold a credential, and
# windows-verify checks the result without one.
_SIGN_NONE = "needs.preflight.outputs.sign_mode == 'none'"
_SIGN_ANY = "needs.preflight.outputs.sign_mode != 'none'"
_SIGN_JOBS = ("windows-sign", "windows-sign-test")
# The names that mean "production": nothing but windows-sign may mention them (D-REL-18q).
_PRODUCTION_NAMES = ("vars.ARTIFACT_SIGNING_PROFILE", "secrets.AZURE_CLIENT_ID")
# Everything a job that can mint a token must never run: package managers, downloads, installers.
_NOT_IN_THE_TOKEN_JOB = (
    "choco",
    "Invoke-WebRequest",
    "nuget",
    "Start-Process",
    "/VERYSILENT",
    "Expand-Archive",
    "uv ",
    "python",
    "pip ",
    "npm",
    "curl",
    "doctor --quick",
)
# GitHub's expression functions (the status functions included). An expression that calls
# anything else does not load at all: release.yml once called substring(), which exists in no
# workflow language, and the whole file would have failed.
_GITHUB_EXPRESSION_FUNCTIONS = frozenset(
    {
        "contains",
        "startsWith",
        "endsWith",
        "format",
        "join",
        "toJSON",
        "fromJSON",
        "hashFiles",
        "success",
        "always",
        "cancelled",
        "failure",
    }
)


def _grants_id_token(permissions: object) -> bool:
    """Whether a `permissions:` value lets a job request an OIDC token: the mapping form with
    id-token: write, or the write-all string. read-all and an absent value do not. Matching the
    substring "id-token" missed write-all, which grants it without naming it."""
    if isinstance(permissions, str):
        return permissions.strip() == "write-all"
    if isinstance(permissions, dict):
        return str(permissions.get("id-token", "none")).strip() == "write"
    return False


def _job_grants_id_token(wf: dict, job: dict) -> bool:
    """The job's own permissions replace the workflow's; a job with none inherits them."""
    return _grants_id_token(job["permissions"] if "permissions" in job else wf.get("permissions"))


def _jobs() -> dict:
    return _workflow("release.yml")["jobs"]


def _sign_job(name: str = "windows-sign") -> dict:
    """A signing job, found among the jobs that hold the id-token permission, so a test asserting
    a property of it fails on that property when no such job exists."""
    wf = _workflow("release.yml")
    holders = sorted(n for n, j in wf["jobs"].items() if _job_grants_id_token(wf, j))
    assert holders == sorted(_SIGN_JOBS), holders
    return _jobs()[name]


def _windows_jobs() -> list[dict]:
    return [j for n, j in _jobs().items() if n.startswith("windows")]


def _step(job: dict, name: str) -> dict:
    return next(s for s in job["steps"] if s.get("name") == name)


def _names(job: dict) -> list[str]:
    return [s.get("name") or s.get("uses") for s in job["steps"]]


def _expressions(name: str) -> list[str]:
    """Every expression in a workflow: the `${{ }}` ones, and every `if:` value, which is an
    expression whether or not it carries the braces. release.yml's broken condition was a bare
    `if:`, so a search for `${{` alone would not have seen it."""
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    found = re.findall(r"\$\{\{(.+?)\}\}", text, re.S)
    for job in _workflow(name)["jobs"].values():
        found.append(str(job.get("if", "")))
        found.extend(str(step.get("if", "")) for step in job.get("steps", []))
    return [one for one in found if one.strip()]


def _called_functions(expression: str) -> set[str]:
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression))


def test_no_workflow_expression_calls_a_function_github_does_not_have():
    """An undefined function is not a runtime error in one job: the workflow fails to load, so
    nothing runs at all. Neither YAML parsing nor a text search for step names catches it."""
    for name in _workflow_names():
        for expression in _expressions(name):
            unknown = _called_functions(expression) - _GITHUB_EXPRESSION_FUNCTIONS
            assert not unknown, f"{name}: {sorted(unknown)} in {expression.strip()[:90]}"


def test_the_expression_check_reads_bare_if_conditions_too():
    """The condition that broke carried no braces, so the scanner must read `if:` values."""
    conditions = [one.strip() for one in _expressions("release.yml")]
    assert str(_sign_job()["if"]).strip() in conditions


def test_the_expression_function_check_rejects_an_invented_function():
    assert _called_functions("substring(github.ref, 11, 1) != ''") - _GITHUB_EXPRESSION_FUNCTIONS
    assert not _called_functions("startsWith(github.ref, 'x')") - _GITHUB_EXPRESSION_FUNCTIONS


def _eval_if(expression: str, *, ref: str, event: str, mode: str) -> bool:
    """Evaluate a job `if:`. Only the functions GitHub documents are implemented, and the test
    above proves no expression in the repository calls anything else."""
    python = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    python = python.replace("needs.preflight.outputs.sign_mode", "MODE")
    python = python.replace("github.event_name", "EVENT").replace("github.ref", "REF")
    functions = {
        "startsWith": lambda text, prefix: text.startswith(prefix),
        "endsWith": lambda text, suffix: text.endswith(suffix),
        "contains": lambda haystack, needle: needle in haystack,
    }
    names = {"REF": ref, "EVENT": event, "MODE": mode, **functions}
    return bool(eval(python, {"__builtins__": {}}, names))  # noqa: S307


_PROD_INPUTS = ("CLIENT", "TENANT", "SUBSCRIPTION", "ENDPOINT", "ACCOUNT", "PUBLISHER")
_TEST_INPUTS = (
    "TEST_CLIENT",
    "TENANT",
    "SUBSCRIPTION",
    "ENDPOINT",
    "ACCOUNT",
    "PUBLISHER",
    "TEST_PROFILE",
)
# The two certificate profile names a scenario sets (variables, not secrets): production,
# test. "same" gives both the same name; "no production" leaves the production one empty.
_PROFILES = {
    "same profile": ("kestrel-public", "kestrel-public"),
    "same profile, other case": ("Kestrel-Public", "kestrel-public"),
    "same profile, padded": ("kestrel-public", "  kestrel-public\t"),
    "blank test profile": ("kestrel-public", " \t "),
    "blank production profile": ("  ", "kestrel-test"),
    "no production profile": ("", "kestrel-test"),
}
_SCENARIOS = {
    "everything": set(_PROD_INPUTS) | set(_TEST_INPUTS),
    "production only": set(_PROD_INPUTS),
    "test only": set(_TEST_INPUTS),
    "partly set": {"CLIENT", "TENANT"},
    "same profile": set(_PROD_INPUTS) | set(_TEST_INPUTS),
    "same profile, other case": set(_PROD_INPUTS) | set(_TEST_INPUTS),
    "same profile, padded": set(_PROD_INPUTS) | set(_TEST_INPUTS),
    "blank test profile": set(_PROD_INPUTS) | set(_TEST_INPUTS),
    "blank production profile": set(_PROD_INPUTS) | set(_TEST_INPUTS),
    "no production profile": set(_PROD_INPUTS) | set(_TEST_INPUTS),
    "shared only": {"TENANT", "SUBSCRIPTION", "ENDPOINT", "ACCOUNT", "PUBLISHER"},
    "nothing": set(),
}
# ref, event, scenario -> the mode preflight must choose, or "stop" when the run must fail.
_PREFLIGHT_CASES = [
    ("refs/tags/v1.2.3", "push", "everything", "production"),
    ("refs/tags/v1.2.3", "push", "production only", "production"),
    ("refs/tags/v0.7.7-rc.1", "push", "everything", "production"),
    ("refs/tags/v10", "push", "production only", "production"),
    ("refs/tags/v1.2.3", "push", "test only", "none"),  # production is not configured at all
    ("refs/tags/v1.2.3", "push", "nothing", "none"),
    ("refs/tags/v1.2.3", "push", "partly set", "stop"),  # never an unsigned release by accident
    ("refs/tags/v1.2.3", "push", "shared only", "stop"),  # the production client is the missing one
    # a dry run that could name the public profile is refused, whichever run it is
    ("refs/tags/v1.2.3", "push", "same profile", "stop"),
    ("refs/heads/main", "workflow_dispatch", "same profile", "stop"),
    ("refs/heads/feature/signing", "workflow_dispatch", "same profile", "stop"),
    # the comparison is trimmed and case-blind: a variant of the public name is the public name
    ("refs/heads/main", "workflow_dispatch", "same profile, other case", "stop"),
    ("refs/tags/v1.2.3", "push", "same profile, other case", "stop"),
    ("refs/heads/main", "workflow_dispatch", "same profile, padded", "stop"),
    ("refs/tags/v1.2.3", "push", "same profile, padded", "stop"),
    # a name that is only whitespace counts as unset: no test mode, so no test login
    ("refs/heads/main", "workflow_dispatch", "blank test profile", "none"),
    ("refs/tags/v1.2.3", "push", "blank test profile", "production"),
    ("refs/tags/v1.2.3", "push", "blank production profile", "stop"),
    # a release with no production profile name stops before it can build anything
    ("refs/tags/v1.2.3", "push", "no production profile", "stop"),
    ("refs/heads/main", "workflow_dispatch", "everything", "test"),
    ("refs/heads/main", "workflow_dispatch", "test only", "test"),
    ("refs/heads/main", "workflow_dispatch", "production only", "none"),  # no test profile
    ("refs/heads/main", "workflow_dispatch", "partly set", "none"),
    ("refs/heads/main", "push", "everything", "none"),  # a push to main is not a release
    ("refs/tags/v1.2.3", "workflow_dispatch", "everything", "none"),  # a tag signs only by push
    ("refs/tags/vNext", "push", "everything", "none"),
    ("refs/tags/v", "push", "everything", "none"),
    ("refs/tags/release-1", "push", "everything", "none"),
    ("refs/tags/1.2.3", "push", "everything", "none"),
    ("refs/heads/feature/signing", "workflow_dispatch", "everything", "none"),
    ("refs/heads/main-evil", "workflow_dispatch", "everything", "none"),
    ("refs/pull/7/merge", "push", "everything", "none"),
]


def _is_version_tag(ref: str) -> bool:
    return bool(re.fullmatch(r"refs/tags/v[0-9].*", ref))


def _run_preflight(ref: str, event: str, scenario: str) -> tuple[int, dict[str, str]]:
    """Run the real preflight script in bash and read what it wrote to GITHUB_OUTPUT."""
    import subprocess
    import tempfile

    step = _step(_jobs()["preflight"], "Windows signing is configured")
    assert step["shell"] == "bash"  # so GitHub runs it with exactly the flags used below
    bash = shutil.which("bash")
    assert bash, "bash is needed to run the preflight script (Git for Windows provides it)"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "output"
        out.touch()
        env = {
            "PATH": os.environ["PATH"],
            "GITHUB_REF": ref,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_OUTPUT": str(out),
        }
        for key in step["env"]:
            env[key] = "false"
        production, test = _PROFILES.get(scenario, ("kestrel-public", "kestrel-test"))
        env["PROD_PROFILE"] = production
        env["TEST_PROFILE"] = test
        for name in _SCENARIOS[scenario]:
            env[f"HAS_{name}"] = "true"
        done = subprocess.run(  # noqa: S603
            # the flags GitHub uses for `shell: bash`, which the step names (D-REL-18zb)
            [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        written = dict(
            line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if line
        )
    return done.returncode, written | {"stderr": done.stdout + done.stderr}


@pytest.mark.parametrize(("ref", "event", "scenario", "expected"), _PREFLIGHT_CASES)
def test_preflight_chooses_the_signing_mode_by_running_its_own_script(
    ref, event, scenario, expected
):
    code, written = _run_preflight(ref, event, scenario)
    stopped = expected == "stop"
    # a partly configured tag release fails with the reason; every other case succeeds
    assert (code != 0) is stopped, written["stderr"]
    reasons = ("partly configured", "same certificate profile", "ARTIFACT_SIGNING_PROFILE is empty")
    assert any(reason in written["stderr"] for reason in reasons) is stopped, written["stderr"]
    # the mode written is the expected one, or none was written because the script stopped
    assert written.get("sign_mode", "stop") == expected, written
    assert written.get("is_version_tag", "stop") == (
        "stop" if stopped else ("true" if _is_version_tag(ref) else "false")
    )


def test_the_configured_flags_cover_every_signing_input_and_name_no_certificate_profile():
    pre = _step(_jobs()["preflight"], "Windows signing is configured")
    assert pre["id"] == "signcfg"
    expected = {
        "HAS_CLIENT": "secrets.AZURE_CLIENT_ID",
        "HAS_TEST_CLIENT": "secrets.AZURE_TEST_CLIENT_ID",
        "HAS_TENANT": "secrets.AZURE_TENANT_ID",
        "HAS_SUBSCRIPTION": "secrets.AZURE_SUBSCRIPTION_ID",
        "HAS_ENDPOINT": "vars.ARTIFACT_SIGNING_ENDPOINT",
        "HAS_ACCOUNT": "vars.ARTIFACT_SIGNING_ACCOUNT",
        "HAS_PUBLISHER": "vars.ARTIFACT_SIGNING_PUBLISHER",
        "HAS_TEST_PROFILE": "vars.ARTIFACT_SIGNING_TEST_PROFILE",
    }
    for key, source in expected.items():
        assert pre["env"][key] == f"${{{{ {source} != '' }}}}", key
    assert set(pre["env"]) == set(expected) | {"PROD_PROFILE", "TEST_PROFILE"}
    assert pre["env"]["PROD_PROFILE"] == "${{ vars.ARTIFACT_SIGNING_PROFILE }}"
    assert pre["env"]["TEST_PROFILE"] == "${{ vars.ARTIFACT_SIGNING_TEST_PROFILE }}"
    outputs = _jobs()["preflight"]["outputs"]
    assert set(outputs) == {"sign_mode", "is_version_tag", "released_at", "mac_sign_mode"}
    for output in ("sign_mode", "is_version_tag"):
        assert outputs[output] == f"${{{{ steps.signcfg.outputs.{output} }}}}"
    # the release date output comes from its own step, never from the signing step (#209)
    assert outputs["released_at"] == "${{ steps.reldate.outputs.released_at }}"
    # the version-tag rule is the same one the push trigger uses
    triggers = _workflow("release.yml").get("on") or _workflow("release.yml").get(True)
    assert triggers["push"]["tags"] == ["v[0-9]*"]
    jobs = _jobs()
    for name in ("release", "website"):
        assert "needs.preflight.outputs.is_version_tag == 'true'" in jobs[name]["if"], name
        assert "preflight" in jobs[name]["needs"], name


@pytest.mark.parametrize(("ref", "event", "mode"), [
    (ref, event, mode)
    for ref in sorted({case[0] for case in _PREFLIGHT_CASES})
    for event in ("push", "workflow_dispatch", "pull_request")
    for mode in ("production", "test", "none")
])
def test_each_signing_job_runs_only_for_its_own_mode_ref_and_event(ref, event, mode):
    """The jobs repeat the ref rule, so a mode output that was somehow wrong still cannot sign
    from an unexpected ref or event. The production job: a version-tag push in production mode.
    The test job: a dispatch from main in test mode."""
    production = _eval_if(_sign_job("windows-sign").get("if"), ref=ref, event=event, mode=mode)
    test = _eval_if(_sign_job("windows-sign-test").get("if"), ref=ref, event=event, mode=mode)
    assert production is (mode == "production" and event == "push" and _is_version_tag(ref))
    is_dispatch_from_main = event == "workflow_dispatch" and ref == "refs/heads/main"
    assert test is (mode == "test" and is_dispatch_from_main)
    assert not (production and test)


def test_only_the_two_signing_jobs_hold_a_token_or_an_environment_in_any_workflow():
    holders, environments = [], {}
    for name in _workflow_names():
        wf = _workflow(name)
        for job_name, job in wf["jobs"].items():
            if _job_grants_id_token(wf, job):
                holders.append((name, job_name))
            if job.get("environment"):
                environments[(name, job_name)] = job["environment"]
        # a workflow-level grant would hand every job the token, so each workflow says what it
        # grants, and what it grants is a mapping without id-token
        assert isinstance(wf.get("permissions"), dict), name
        assert not _grants_id_token(wf.get("permissions")), name
    assert sorted(holders) == [
        ("release.yml", "windows-sign"),
        ("release.yml", "windows-sign-test"),
    ]
    assert environments == {
        ("release.yml", "windows-sign"): "release",
        ("release.yml", "windows-sign-test"): "release-test",
        # #202 and #201: the macOS signing jobs and the tap job, guarded in test_release_macos.py
        ("release.yml", "macos-sign"): "release",
        ("release.yml", "macos-sign-test"): "release-test",
        ("release.yml", "homebrew-tap"): "release",
        ("release.yml", "macos-sign-check"): "release",
    }
    for job in (_sign_job("windows-sign"), _sign_job("windows-sign-test")):
        assert job["permissions"] == {"contents": "read", "id-token": "write"}


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        ({"contents": "read", "id-token": "write"}, True),
        ({"id-token": "write"}, True),
        ("write-all", True),
        ({"id-token": "none"}, False),
        ({"id-token": "read"}, False),
        ({"contents": "write"}, False),
        ("read-all", False),
        ({}, False),
        (None, False),
    ],
)
def test_the_token_check_understands_every_form_of_permissions(permissions, expected):
    assert _grants_id_token(permissions) is expected


@pytest.mark.parametrize(
    ("workflow_level", "job_level", "expected"),
    [
        ("write-all", None, True),  # inherited by a job that sets nothing
        ("write-all", {"contents": "read"}, False),  # the job narrows it
        ({"contents": "read"}, "write-all", True),
        ({"contents": "read"}, None, False),
        (None, None, False),
        ("read-all", None, False),
    ],
)
def test_a_job_that_inherits_or_overrides_permissions_is_judged_by_what_it_ends_up_with(
    workflow_level, job_level, expected
):
    wf = {} if workflow_level is None else {"permissions": workflow_level}
    job = {} if job_level is None else {"permissions": job_level}
    assert _job_grants_id_token(wf, job) is expected


def test_the_workflow_scan_reads_yml_and_yaml_files(tmp_path):
    for name in ("a.yml", "b.yaml", "c.txt"):
        (tmp_path / name).write_text("jobs: {}\n", encoding="utf-8")
    assert _workflow_file_names(tmp_path) == ["a.yml", "b.yaml"]
    assert _workflow_names() == _workflow_file_names(WORKFLOWS)


def test_nothing_but_the_production_job_names_the_production_environment_or_profile():
    """D-REL-18q. The dry run must not be able to hold a credential that works on the production
    profile: separate environments, separate app registrations, and the Signer role granted
    per certificate profile. So no other job in any workflow may reference them."""
    offenders = []
    for name in _workflow_names():
        for job_name, job in _workflow(name)["jobs"].items():
            text = str(job)
            mentions = [token for token in _PRODUCTION_NAMES if token in text]
            if job.get("environment") == "release":
                mentions.append("environment: release")
            if mentions and (name, job_name) != ("release.yml", "windows-sign"):
                offenders.append((name, job_name, mentions))
    # The macOS signing job and the tap job use the release environment for their own secrets
    # only; neither may name a Windows production value (tests/test_release_macos.py, #202).
    for job_name in ("macos-sign", "homebrew-tap", "macos-sign-check"):
        entry = ("release.yml", job_name, ["environment: release"])
        assert entry in offenders, job_name
        offenders.remove(entry)
    # preflight reads the production client id as a yes/no flag only, never as a value, and the
    # production profile name only to refuse a test profile that equals it (never as an output)
    offenders = [one for one in offenders if one[:2] != ("release.yml", "preflight")]
    assert offenders == []
    pre = _jobs()["preflight"]
    assert str(pre).count("secrets.AZURE_CLIENT_ID") == 1
    assert "secrets.AZURE_CLIENT_ID != ''" in str(pre)
    assert str(pre).count("vars.ARTIFACT_SIGNING_PROFILE") == 1
    # no profile name
    assert set(pre["outputs"]) == {"sign_mode", "is_version_tag", "released_at", "mac_sign_mode"}
    prod, test = _sign_job("windows-sign"), _sign_job("windows-sign-test")
    assert prod["environment"] == "release" and test["environment"] == "release-test"
    for job, client, profile in (
        (prod, "secrets.AZURE_CLIENT_ID", "vars.ARTIFACT_SIGNING_PROFILE"),
        (test, "secrets.AZURE_TEST_CLIENT_ID", "vars.ARTIFACT_SIGNING_TEST_PROFILE"),
    ):
        login = next(s for s in job["steps"] if "azure/login@" in str(s.get("uses")))
        assert login["with"]["client-id"] == f"${{{{ {client} }}}}"
        sign = _step(job, "Sign the app files")["with"]
        assert sign["certificate-profile-name"] == f"${{{{ {profile} }}}}"
    test_text = str(test)
    assert not any(token in test_text for token in _PRODUCTION_NAMES)


def test_the_test_job_is_the_production_job_with_only_its_own_names():
    """The dry run exercises the same steps as a release, so a check cannot exist in one and not
    the other. Everything differs only by credential, profile and environment names."""
    def normalized(job: dict) -> str:
        text = yaml.safe_dump(job["steps"], sort_keys=True)
        for source in (
            "secrets.AZURE_TEST_CLIENT_ID",
            "secrets.AZURE_CLIENT_ID",
        ):
            text = text.replace(source, "CLIENT")
        for source in ("vars.ARTIFACT_SIGNING_TEST_PROFILE", "vars.ARTIFACT_SIGNING_PROFILE"):
            text = text.replace(source, "PROFILE")
        return text

    assert normalized(_sign_job("windows-sign")) == normalized(_sign_job("windows-sign-test"))
    assert _sign_job("windows-sign")["needs"] == _sign_job("windows-sign-test")["needs"]


def test_the_jobs_are_wired_to_each_other_as_the_design_says():
    jobs = _jobs()
    for name in _SIGN_JOBS:
        job = _sign_job(name)
        assert set(job["needs"]) == {"preflight", "windows", "windows-tools"}
        downloads = [
            s["with"]["name"] for s in job["steps"] if "download-artifact" in str(s.get("uses"))
        ]
        assert downloads == ["windows-app", "signing-kit"]
    verify = jobs["windows-verify"]
    assert set(verify["needs"]) == {"preflight", "windows-sign", "windows-sign-test"}
    # one signing job is skipped by design, so verify needs a status function to run after it,
    # and a failed signing job must still stop it
    assert "!cancelled() && !failure()" in verify["if"]
    assert "needs.preflight.outputs.sign_mode != 'none'" in verify["if"]
    assert set(jobs["windows-tools"]["needs"]) == {"preflight"}
    assert jobs["windows-tools"].get("if") == _SIGN_ANY
    kit = [s["with"]["name"] for s in jobs["windows-tools"]["steps"] if "with" in s]
    assert kit == ["signing-kit"]
    app = [s for s in jobs["windows"]["steps"] if s.get("with", {}).get("name") == "windows-app"]
    assert [s.get("if") for s in app] == [_SIGN_ANY]
    assert verify["steps"][0]["with"]["name"] == "windows-signed"


def test_the_app_folder_is_kept_before_the_smoke_test_can_write_into_it():
    names = _names(_jobs()["windows"])
    steps = _jobs()["windows"]["steps"]
    kept = next(i for i, s in enumerate(steps) if s.get("with", {}).get("name") == "windows-app")
    assert kept < names.index("Smoke test the frozen CLI")
    assert names.index("Build the app folder (to be signed)") < kept


@pytest.mark.parametrize("name", _SIGN_JOBS)
def test_the_token_jobs_run_no_installer_package_manager_or_download(name):
    signing = _sign_job(name)
    runs = "\n".join(s.get("run", "") for s in signing["steps"])
    for word in _NOT_IN_THE_TOKEN_JOB:
        assert word not in runs, word
    uses = [str(s.get("uses", "")).split("@")[0] for s in signing["steps"] if "uses" in s]
    allowed = {
        "actions/checkout",
        "actions/download-artifact",
        "actions/upload-artifact",
        "azure/login",
        "azure/artifact-signing-action",
    }
    assert set(uses) <= allowed, uses
    names = _names(signing)
    login = names.index("Azure login (federated credential)")
    assert names.index("Verify every binary in the signing kit before the login") < login
    assert names.index("Check the signing inputs and warn about a re-run") < login


def test_the_unsigned_job_installs_inno_only_on_the_unsigned_path():
    """No unpinned `choco install` on the signing path: with signing configured the compiler is
    the hash-pinned one in the kit."""
    step = _step(_jobs()["windows"], "Inno Setup")
    assert step.get("if") == _SIGN_NONE
    assert 'VersionInfo.ProductVersion' in step["run"]
    assert '-not $have.StartsWith("6.7.1")' in step["run"]
    assert "choco install innosetup --version 6.7.1" in step["run"] and "--force" in step["run"]
    assert step["run"].index('-not $have.StartsWith("6.7.1")') < step["run"].index("choco install")
    # and nothing on the signing path installs anything with choco
    for name in ("windows-tools", "windows-sign", "windows-sign-test", "windows-verify"):
        assert "choco install" not in str(_jobs()[name]), name


@pytest.mark.parametrize("name", _SIGN_JOBS)
def test_every_binary_handed_to_the_token_job_is_checked_against_an_expected_publisher(name):
    """A list the same job wrote proves nothing, so the kit is checked by Authenticode: every
    executable file in it, including the compiler's own .e32 and .dll files."""
    check = _step(_sign_job(name), "Verify every binary in the signing kit before the login")["run"]
    for extension in (".exe", ".dll", ".e32", ".pyd"):
        assert f'"{extension}"' in check, extension
    assert "-Recurse" in check and "Get-AuthenticodeSignature" in check
    assert '$sig.Status -ne "Valid"' in check  # nothing but a valid signature passes
    assert "MANIFEST" not in check  # not a list this pipeline wrote itself
    publishers = re.search(r"\$publishers = @\{(?P<body>.+?)\n\s*\}", check, re.S)["body"]
    folders = dict(re.findall(r'"(\w+)"\s*=\s*@\((.+?)\)', publishers))
    assert set(folders) == {"signtool", "dlib", "inno"}
    assert "Microsoft Corporation" in folders["signtool"] and "Microsoft" in folders["dlib"]
    assert folders["inno"].strip() != "" and "Microsoft" not in folders["inno"]
    assert "$expected -notcontains $cn" in check and "unexpected folder" in check
    assert check.count("throw") >= 3
    assert '$binaries.Count -lt 4' in check  # an empty or stripped kit is not a pass
    assert 'ProductVersion.StartsWith("6.7.1")' in check


def test_everything_downloaded_for_signing_is_pinned_by_hash_without_a_package_manager():
    tools = _jobs()["windows-tools"]
    assert "permissions" not in tools and "environment" not in tools
    run = _step(tools, "Fetch SignTool, the Artifact Signing dlib and Inno Setup")["run"]
    pinned = re.findall(r'Get-Pinned "(?P<url>[^"]+)" \$\w+ "(?P<sha>[0-9A-F]{64})"', run)
    urls = {url for url, _ in pinned}
    assert any("microsoft.windows.sdk.buildtools/10.0.28000.2957/" in url for url in urls)
    assert any("microsoft.artifactsigning.client/1.0.151/" in url for url in urls)
    assert any("innosetup/6.7.1" in url for url in urls)
    assert len({sha for _, sha in pinned}) == len(pinned) == 3
    # the installer inside the Inno package is pinned too, and a mismatch throws
    assert re.search(r'-ne "4D11E8050B6185E0D49BD9E8CC661A7A59F44959A621D31D11033124C4E8A7B0"', run)
    assert run.count("throw") >= 3
    # no package manager resolves anything: the packages are fetched as files and hash-checked
    for invocation in ("nuget.exe", "choco install", "Install-Package", "Install-Module"):
        assert invocation not in run, invocation
    assert "secrets." not in str(tools) and "AZURE_" not in str(tools)


@pytest.mark.parametrize("name", _SIGN_JOBS)
def test_only_the_signing_tools_run_while_the_login_is_live(name):
    steps = _sign_job(name)["steps"]
    names = _names(_sign_job(name))
    uses = [str(s.get("uses", "")) for s in steps]
    login = next(i for i, u in enumerate(uses) if u.startswith("azure/login@"))
    logout = names.index("Sign out of Azure")
    assert steps[logout].get("if") == "always()"
    live = steps[login + 1 : logout]
    assert [s.get("name") for s in live] == ["Sign the app files", "Pack and sign the installer"]
    assert live[0]["uses"].startswith("azure/artifact-signing-action@")
    pack = live[1]["run"]
    assert "build_windows.ps1 -SkipBuild" in pack and 'Join-Path $kit "inno"' in pack
    # before the login only first-party steps and the checks run; after it only the signature
    # check (a shell step with no action and no secret) and then the upload
    before = [str(s.get("uses", "")).split("@")[0] for s in steps[:login] if "uses" in s]
    assert before == ["actions/checkout", "actions/download-artifact", "actions/download-artifact"]
    after = steps[logout + 1 :]
    assert [(s.get("name"), str(s.get("uses", "")).split("@")[0]) for s in after] == [
        ("Check the signatures before the upload", ""),
        (None, "actions/upload-artifact"),
    ]
    assert "secrets." not in str(after[0])


def _spec_executable_count() -> int:
    """How many executables PyInstaller puts at the top of the app folder, read from the spec."""
    spec = (PACKAGING / "kestrel.spec").read_text(encoding="utf-8")
    return len(re.findall(r"\bEXE\(", spec[: spec.index("coll = COLLECT")]))


@pytest.mark.parametrize("name", _SIGN_JOBS)
def test_only_kestrels_own_executables_are_selected_for_signing(name):
    """The signature budget (D-REL-18o): Basic includes 5,000 signatures a month, one per file,
    and a PyInstaller folder holds hundreds of files other companies made. Only the two
    executables at the top of the folder are selected; the expected count is the number of
    executables the spec builds."""
    listing = _step(_sign_job(name), "List the files to sign")["run"]
    limit = int(re.search(r"\$maxToSign = (\d+)", listing)[1])
    selection = re.search(r"\$mine = @\((?P<command>[^\n]+)\)\n", listing)["command"]
    assert "-Recurse" not in selection  # nothing under _internal is ever selected
    assert '-File -Filter "*.exe"' in selection
    assert '.Status -eq "NotSigned"' in listing  # a file that is already signed is not signed again
    assert "if ($todo.Count -gt $maxToSign) { throw" in listing
    assert _spec_executable_count() == limit == 2
    for exe in ("Kestrel.exe", "kestrel-cli.exe"):
        assert f"./Kestrel/{exe}" in listing
    assert "signing calls by the signing action" in listing and "(limit $maxToSign)" in listing
    sign = _step(_sign_job(name), "Sign the app files")["with"]
    assert str(sign.get("files-catalog")).endswith("dist\\sign-catalog.txt")
    assert "files" not in sign and "files-folder" not in sign  # no fixed name list, no folder
    assert sign["timestamp-rfc3161"] == "http://timestamp.acs.microsoft.com"
    assert sign["timestamp-digest"] == "SHA256" and sign["file-digest"] == "SHA256"
    assert int(sign["timeout"]) >= 1800
    names = _names(_sign_job(name))
    assert names.index("List the files to sign") < names.index("Azure login (federated credential)")


@pytest.mark.parametrize("name", _SIGN_JOBS)
def test_the_signature_budget_per_run_is_exactly_four(name):
    signing = _sign_job(name)
    listing = _step(signing, "List the files to sign")["run"]
    pack = _step(signing, "Pack and sign the installer")["run"]
    action = int(re.search(r"\$maxToSign = (\d+)", listing)[1])
    inno = int(re.search(r"\$expectedInnoCalls = (\d+)", pack)[1])
    assert (action, inno) == (2, 2) and action + inno == 4  # against 5,000 a month
    # the installer AND the uninstaller must both be signed: one is not enough
    assert "if ($calls -ne $expectedInnoCalls) { throw" in pack
    assert "signing attempts by Inno Setup: $calls" in pack


def test_a_signing_retry_or_a_rerun_cannot_spend_signatures_unseen():
    iss = ISS.read_text(encoding="utf-8")
    block = re.search(r"#ifdef KestrelSignTool(?P<body>.+?)#endif", iss, re.S)["body"]
    assert "SignToolRetryCount=0" in block  # Inno retries twice by default, each a billed signature
    call = (PACKAGING / "sign_call.ps1").read_text(encoding="utf-8")
    assert "Add-Content -Path $env:KESTREL_SIGN_COUNTER" in call
    counted = call.index("Add-Content -Path $env:KESTREL_SIGN_COUNTER")
    assert counted < call.index("& $env:KESTREL_SIGNTOOL sign")  # attempts, not only successes
    for name in _SIGN_JOBS:
        step = _step(_sign_job(name), "Check the signing inputs and warn about a re-run")
        assert step["env"]["RUN_ATTEMPT"] == "${{ github.run_attempt }}"
        assert '$env:RUN_ATTEMPT -ne "1"' in step["run"] and "::warning::" in step["run"]
        assert "spends about 4 more signatures" in step["run"]
        for variable in ("SIGN_ENDPOINT", "SIGN_ACCOUNT", "SIGN_PROFILE"):
            assert variable in step["run"] and variable in step["env"]


def test_signing_secrets_are_scoped_to_the_steps_that_use_them():
    jobs = _jobs()
    # no job-level environment in a Windows job carries a secret or a signing input
    for job in _windows_jobs():
        assert "secrets." not in str(job.get("env", "")), job["name"]
        assert "AZURE_" not in str(job.get("env", "")), job["name"]
    # the certificate-file secrets reach only the one step that runs the build script
    build = _step(jobs["windows"], "Build installer")
    assert set(build["env"]) == {"WINDOWS_CERT_PFX_BASE64", "WINDOWS_CERT_PASSWORD"}
    steps = jobs["windows"]["steps"]
    holders = [s.get("name") for s in steps if "WINDOWS_CERT_PASSWORD" in str(s.get("env"))]
    assert holders == ["Build installer"]
    # the Azure secrets appear only as the login's three inputs
    for job in _windows_jobs():
        for step in job["steps"]:
            is_login = str(step.get("uses", "")).startswith("azure/login@")
            body = "" if is_login else str(step)
            assert "AZURE_" not in body, (job["name"], step.get("name"))
            assert "secrets.WINDOWS_CERT" not in str(step.get("run", "")), step.get("name")
    for name in _SIGN_JOBS:
        login = next(s for s in _sign_job(name)["steps"] if "azure/login@" in str(s.get("uses")))
        assert set(login["with"]) == {"client-id", "tenant-id", "subscription-id"}


def test_no_signing_step_prints_writes_or_uploads_a_secret():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    for needle in ("azure-client-secret", "azure-password", "AZURE_CLIENT_SECRET", "trace: true"):
        assert needle not in text, needle
    for job in _windows_jobs():
        for step in job["steps"]:
            run = step.get("run", "")
            assert "secrets." not in run, (job["name"], step.get("name"))
            assert "GITHUB_ENV" not in run and "GITHUB_OUTPUT" not in run, step.get("name")
            assert "Out-File" not in run and "ConvertFrom-SecureString" not in run
            assert "$env:AZURE" not in run and "Write-Host $env" not in run
    # what leaves a signing job is the installer; the metadata lives in the runner's temp folder
    for name in _SIGN_JOBS:
        steps = _sign_job(name)["steps"]
        uploads = [s["with"] for s in steps if "upload-artifact" in str(s.get("uses"))]
        assert [u["path"] for u in uploads] == ["dist/Kestrel-Setup-*.exe"]
        meta = _step(_sign_job(name), "Write the signing metadata")["run"]
        assert '$dir = Join-Path $env:RUNNER_TEMP "signing-meta"' in meta
    final = [s["with"]["path"] for s in _jobs()["windows-verify"]["steps"] if "with" in s]
    assert "dist/Kestrel-Setup-*.exe\ndist/SIGNING-windows.txt\n" in final


def test_the_unsigned_marker_no_longer_points_at_the_old_certificate_route():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "add WINDOWS_CERT_PFX_BASE64 and WINDOWS_CERT_PASSWORD to sign" not in text
    marker = _step(_jobs()["windows"], "Signing marker")["run"]
    assert "windows: unsigned: Windows signing is not configured for this build" in marker
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from build_latest_json import parse_signing
    finally:
        sys.path.pop(0)
    lines = re.findall(r'-Value "(windows: [^"]+)"', marker)
    assert [parse_signing(line)["windows"] for line in lines] == [True, False]


def test_a_build_without_the_signing_configuration_is_the_unsigned_build_it_always_was():
    jobs = _jobs()
    build = jobs["windows"]
    assert "permissions" not in build and "environment" not in build
    plain = _step(build, "Build installer")
    assert plain.get("if") == _SIGN_NONE
    assert plain["run"] == "$env:PYTHON = (uv python find)\npackaging\\build_windows.ps1\n"
    only = _step(build, "Build the app folder (to be signed)")
    assert only.get("if") == _SIGN_ANY and "-BuildOnly" in only["run"]
    marker = _step(build, "Signing marker")
    assert marker.get("if") == _SIGN_NONE
    steps = [s for s in build["steps"] if "upload-artifact" in str(s.get("uses"))]
    uploads = {s["with"]["name"]: s.get("if") for s in steps}
    assert uploads == {"windows-app": _SIGN_ANY, "Kestrel-windows": _SIGN_NONE}
    # the release goes on without the signing jobs when they are skipped, and stops if one fails
    release = jobs["release"]
    assert release["if"].endswith("&& !cancelled() && !failure()")
    steps = release["steps"]
    download = next(s for s in steps if "download-artifact@" in str(s.get("uses", "")))
    # never the app folder, the signing kit or the interim installer
    assert download["with"].get("pattern") == "Kestrel-*"
    assert set(release["needs"]) == _RELEASE_NEEDS


_SIGNATURE_CHECK = PACKAGING / "signature_check.ps1"
# Every member of System.Management.Automation.SignatureStatus (learn.microsoft.com, SDK 7.6).
# There is no UntrustedRoot: that is an X509ChainStatusFlags value (D-REL-18za).
_SIGNATURE_STATUSES = {
    "Valid",
    "UnknownError",
    "NotSigned",
    "HashMismatch",
    "NotTrusted",
    "NotSupportedFileFormat",
    "Incompatible",
}
_RULE_LOAD_SIGN = '. (Join-Path (Get-Location) "packaging\\signature_check.ps1")'
_RULE_LOAD_VERIFY = (
    '. (Join-Path $env:GITHUB_WORKSPACE "kestrel-src\\packaging\\signature_check.ps1")'
)


_SIMPLE_NAME = (
    "GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)"
)


def _ps_function(name: str) -> str:
    """The body of one PowerShell function in the rule file, up to the next function."""
    text = _SIGNATURE_CHECK.read_text(encoding="utf-8")
    assert re.findall(r"^function (\S+) \{$", text, re.M) == [
        "Write-KestrelTestSignatureDiagnostics",
        "Get-KestrelSignatureProblem",
    ]
    body = text.split(f"function {name} {{\n", 1)[1]
    return body.split("\nfunction ", 1)[0]


def _returns(block: str) -> list[str]:
    return [line.strip() for line in block.splitlines() if "return" in line]


def test_one_signature_rule_never_passes_a_trusted_test_signature():
    """D-REL-18zc. In test mode a file passes only when it is not Valid, is UnknownError or
    NotTrusted, and is timestamped; the signer and chain are printed, not judged, until a dry
    run shows what a test certificate looks like. Valid is checked first."""
    body = _ps_function("Get-KestrelSignatureProblem")
    assert '[ValidateSet("production", "test")][string]$Mode' in body
    assert '$what = "${Path}: status $($sig.Status)"' in body
    # a literal path: a file name with [ or ] is never read as a wildcard
    assert body.count("Get-AuthenticodeSignature") == 1
    assert "$sig = Get-AuthenticodeSignature -LiteralPath $Path" in body
    assert "-FilePath" not in _SIGNATURE_CHECK.read_text(encoding="utf-8")
    start = body.index('if ($Mode -eq "test") {')
    end = body.index("\n    }\n", start)
    test = body[start:end]
    assert test.splitlines()[1].strip() == (
        "Write-KestrelTestSignatureDiagnostics -Path $Path -Signature $sig -Publisher $Publisher"
    )
    checks = _returns(test)
    assert len(checks) == 4
    assert checks[0].startswith('if ($sig.Status -eq "Valid") { return "$what; ')  # first
    assert "must not be publicly trusted" in checks[0] and "misconfigured" in checks[0]
    assert checks[1].startswith(
        'if (@("UnknownError", "NotTrusted") -notcontains $sig.Status) { return "$what; '
    )
    assert checks[2] == 'if (-not $sig.TimeStamperCertificate) { return "$what; no timestamp" }'
    assert checks[3] == "return $null"  # passing needs all three; nothing else decides it
    named = set(re.findall(r'\$sig\.Status -(?:eq|ne) "(\w+)"', body))
    named |= set(re.findall(r'"(\w+)"', checks[1].split("-notcontains")[0]))
    assert named == {"Valid", "UnknownError", "NotTrusted"} and named <= _SIGNATURE_STATUSES
    # the test branch judges neither the chain nor the name
    assert "Chain" not in test and "GetNameInfo" not in test and "$Publisher)" not in test


def test_one_signature_rule_holds_a_release_to_valid_our_name_and_a_timestamp():
    """Production: Valid, the signer name read by GetNameInfo (not a regex) equal to the
    publisher exactly, and a timestamp."""
    body = _ps_function("Get-KestrelSignatureProblem")
    production = body[body.index("\n    }\n", body.index('if ($Mode -eq "test") {')) :]
    assert _returns(production) == [
        'if ($sig.Status -ne "Valid") { return "$what; a release signature must be Valid" }',
        "if ($name -cne $Publisher) { return \"$what; signed by '$name', expected '$Publisher'\" }",
        'if (-not $sig.TimeStamperCertificate) { return "$what; no timestamp" }',
        "return $null",
    ]
    assert '$name = ""' in production
    name_line = f"if ($sig.SignerCertificate) {{ $name = $sig.SignerCertificate.{_SIMPLE_NAME} }}"
    assert production.index(name_line) < production.index("if ($name -cne $Publisher)")
    assert "[regex]" not in _SIGNATURE_CHECK.read_text(encoding="utf-8")


def test_test_mode_prints_the_signer_and_chain_for_the_owner_to_paste_back():
    """The diagnostics the follow-up pins the test root and name from: signer name, subject,
    issuer, thumbprint, and a chain built from the file's own certificates without revocation.
    A diagnostics failure is printed, never thrown."""
    body = _ps_function("Write-KestrelTestSignatureDiagnostics")
    assert 'Write-Host "----- BEGIN TEST SIGNATURE DIAGNOSTICS: $Path -----"' in body
    assert 'Write-Host "----- END TEST SIGNATURE DIAGNOSTICS -----"' in body
    assert f"$name = $cert.{_SIMPLE_NAME}" in body
    for label in ("signer name:", "signer subject:", "signer issuer:", "signer thumbprint:"):
        assert f'Write-Host "{label} ' in body, label
    for value in ("$cert.Subject", "$cert.Issuer", "$cert.Thumbprint"):
        assert value in body
    assert "$embedded.Import($Path)" in body
    assert "[void]$chain.ChainPolicy.ExtraStore.AddRange($embedded)" in body
    assert "X509RevocationMode]::NoCheck" in body
    assert "$built = $chain.Build($cert)" in body and "chain built: $built; flags: " in body
    assert "foreach ($element in $chain.ChainElements)" in body
    assert "$element.Certificate.Subject" in body and "$element.Certificate.Thumbprint" in body
    assert "timestamp: " in body
    # it prints; it never decides a result, and everything after the first line sits inside one
    # try whose catch prints, so no error in it can stop the run
    assert "return" not in body and "throw" not in body
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    begin = lines.index('Write-Host "----- BEGIN TEST SIGNATURE DIAGNOSTICS: $Path -----"')
    assert [line for line in lines[begin + 1 :] if not line.startswith("#")][0] == "try {"
    assert lines[-5:] == [
        "} catch {",
        'Write-Host "diagnostics stopped: $($_.Exception.Message)"',
        "}",
        'Write-Host "----- END TEST SIGNATURE DIAGNOSTICS -----"',
        "}",  # the end of the function
    ]
    assert "} finally {\n                $chain.Dispose()" in body
    # the chain has its own catch, so the timestamp line still prints after a chain failure
    chain_catch = (
        "} catch {\n"
        "                # A chain that cannot be built is reported here, so the lines below"
        " still print.\n"
        '                Write-Host "chain: not built: $($_.Exception.Message)"\n'
        "            } finally {"
    )
    assert body.index("$embedded.Import($Path)") < body.index(chain_catch)
    assert body.index(chain_catch) < body.index('Write-Host "timestamp: none"')


# Method calls whose results are void, so they may stand alone on a line.
_VOID_CALLS = {"$embedded.Import($Path)", "$chain.Dispose()"}


def test_no_value_leaks_into_the_signature_rules_result():
    """A PowerShell function returns every uncaptured value, so a bare `$chain.Build($cert)`
    would make a passing file return True and fail. Every statement that calls a method or
    names a value is assigned, voided, printed, returned, or a known void call."""
    text = _SIGNATURE_CHECK.read_text(encoding="utf-8")
    code = text.split("#>", 1)[1]
    loose = []
    for raw in code.splitlines():
        line = raw.strip()
        if not line.startswith("$") or line in _VOID_CALLS:
            continue
        if re.match(r"^\$[\w:.]+(?:\[[^\]]*\])? = ", line):
            continue
        loose.append(line)
    assert loose == []
    assert "[void]$chain.ChainPolicy.ExtraStore.AddRange($embedded)" in code




def test_windows_verify_uses_the_same_signature_rule_for_every_file_of_ours():
    """The signing jobs and windows-verify load one rule file, so they cannot drift."""
    verify = _jobs()["windows-verify"]
    names = _names(verify)
    check_at = names.index("Install, check every signature and start the app")
    checkout = [
        i for i, step in enumerate(verify["steps"]) if "actions/checkout@" in str(step.get("uses"))
    ]
    assert len(checkout) == 1 and checkout[0] < check_at
    # into its own folder, after the download, so it cannot clean dist
    assert verify["steps"][checkout[0]]["with"] == {
        "persist-credentials": False,
        "path": "kestrel-src",
    }
    script = verify["steps"][check_at]["run"]
    assert script.startswith(_RULE_LOAD_VERIFY + "\n")
    call = (
        "$problem = Get-KestrelSignatureProblem -Path $f.FullName -Mode $env:SIGN_MODE"
        " -Publisher $env:EXPECTED_PUBLISHER"
    )
    assert call in script and "if ($problem) { $bad += $problem }" in script
    assert script.index("foreach ($f in $ours)") < script.index(call)
    assert "$accepted" not in script and "UntrustedRoot" not in script
    # Get-AuthenticodeSignature here reads only third-party files; ours go through the rule
    assert script.count("Get-AuthenticodeSignature") == 1
    assert "(Get-AuthenticodeSignature -FilePath $f.FullName).Status" in script.split(
        "foreach ($f in $theirs)"
    )[1]


def test_each_signing_job_checks_every_signed_file_before_its_upload():
    """D-REL-18z. windows-sign-test uploads windows-signed before windows-verify runs, so a test
    signature that Windows trusts must fail inside the signing job, before the upload step. The
    check covers the signing action's catalog, the installer, and every file Inno Setup signed
    that is on disk, through the same rule windows-verify uses.
    """
    name = "Check the signatures before the upload"
    for job_name in ("windows-sign-test", "windows-sign"):
        job = _sign_job(job_name)
        names = _names(job)
        upload = next(
            i
            for i, step in enumerate(job["steps"])
            if "actions/upload-artifact@" in str(step.get("uses"))
        )
        assert name in names, job_name
        check_at = names.index(name)
        # it runs after the last signing step and right before the upload, on the default
        # success condition, so a failure here leaves nothing to upload
        assert check_at == upload - 1, (job_name, names)
        assert names.index("Pack and sign the installer") < check_at
        check = job["steps"][check_at]
        assert check.get("if") is None and check.get("continue-on-error") is None
        assert job["steps"][upload].get("if") is None
        assert check["env"] == {
            "SIGN_MODE": "${{ needs.preflight.outputs.sign_mode }}",
            "EXPECTED_PUBLISHER": "${{ vars.ARTIFACT_SIGNING_PUBLISHER }}",
        }
        script = check["run"]
        assert script.startswith(_RULE_LOAD_SIGN + "\n")
        assert "Get-AuthenticodeSignature" not in script and "$accepted" not in script
        call = (
            "$problem = Get-KestrelSignatureProblem -Path $f -Mode $env:SIGN_MODE"
            " -Publisher $env:EXPECTED_PUBLISHER"
        )
        assert call in script and "if ($problem) { $bad += $problem }" in script
        # every file signed: the action's catalog, the one installer, and Inno's own list
        assert 'Join-Path $dist "sign-catalog.txt"' in script
        assert '-Filter "Kestrel-Setup-*.exe"' in script
        assert "$installers.Count -ne 1" in script
        assert '"inno-signing-calls.txt"' in script
        assert "foreach ($f in $signed)" in script
        assert script.index("foreach ($f in $signed)") < script.index(call)
        # each source lands in the list the loop checks, not only in a variable beside it
        assert "$signed = @($catalog | ForEach-Object" in script
        assert "$signed += $installers[0].FullName" in script
        assert "foreach ($p in $innoCalls)" in script and "$signed += (Resolve-Path" in script
        assert script.index("$signed = @($signed | Select-Object -Unique)") < script.index(
            "foreach ($f in $signed)"
        )
        assert "throw" in script.split("foreach ($f in $signed)")[1]
    # the counter and catalog it reads are the ones the signing steps write
    for job_name in ("windows-sign-test", "windows-sign"):
        job = _sign_job(job_name)
        assert '"inno-signing-calls.txt"' in _step(job, "Pack and sign the installer")["run"]
        assert '"dist\\sign-catalog.txt"' in _step(job, "List the files to sign")["run"]


def test_the_test_mode_marker_never_reads_as_signed():
    """scripts/build_latest_json.py decides from this line whether the website calls a download
    signed. The test profile must read as unsigned, with the reason."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from build_latest_json import parse_signing
    finally:
        sys.path.pop(0)
    marker = _step(_jobs()["windows-verify"], "Signing marker")["run"]
    lines = re.findall(r'Set-Content -Path dist\\SIGNING-windows\.txt -Value "([^"]+)"', marker)
    assert len(lines) == 2, lines
    states = {line: parse_signing(line)["windows"] for line in lines}
    test_line = next(line for line in lines if "test profile" in line)
    assert states[test_line] is False
    assert "Windows does not trust" in test_line
    assert states[next(line for line in lines if line not in {test_line})] is True


def test_the_verify_job_checks_four_named_files_and_our_publisher_and_only_verifies_the_rest():
    verify = _jobs()["windows-verify"]
    assert "permissions" not in verify
    check = _step(verify, "Install, check every signature and start the app")
    assert check["env"]["EXPECTED_PUBLISHER"] == "${{ vars.ARTIFACT_SIGNING_PUBLISHER }}"
    script = check["run"]
    assert "if (-not $env:EXPECTED_PUBLISHER) { throw" in script
    top_level = '@(Get-ChildItem -Path $target -File -Filter "*.exe")'
    assert f"$ours = @(Get-Item $installer) + {top_level}" in script
    # publisher (case-sensitive) and timestamp are checked by the shared rule
    assert "Get-KestrelSignatureProblem -Path $f.FullName" in script
    # the four files must exist and be among the ones checked, so an install that left one out
    # cannot pass by having nothing to check
    required = re.search(r"\$required = @\((?P<list>[^\n]+)\)\n", script)["list"]
    assert required.count("Join-Path $target") == 3 and "$installer" in required
    for exe in ("Kestrel.exe", "kestrel-cli.exe", "unins000.exe"):
        assert exe in required
    assert "Test-Path $r" in script and ": missing" in script
    assert "$ours.FullName -notcontains (Resolve-Path $r).Path" in script
    # everyone else's files are verified, never signed: Valid or NotSigned, anything else fails
    assert '$status -eq "Valid"' in script and '$status -eq "NotSigned"' in script
    assert "else { $bad += " in script
    assert "-Wait -PassThru" in script and "$install.ExitCode -ne 0" in script
    assert "kestrel-cli.exe" in script and "$LASTEXITCODE -ne 0" in script


def test_inno_signs_the_installer_and_uninstaller_and_the_script_changes_nothing_without_it():
    iss = ISS.read_text(encoding="utf-8")
    directive = (
        r"#ifdef KestrelSignTool\s+SignTool=kestrelsign\s+SignedUninstaller=yes\s+"
        r"(?:;[^\n]*\n\s*)?SignToolRetryCount=0\s+#endif"
    )
    assert re.search(directive, iss)
    ps1 = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")
    assert "if ($env:KESTREL_INNO_SIGNTOOL)" in ps1 and "/DKestrelSignTool=1" in ps1
    assert "[switch]$BuildOnly" in ps1
    for name in _SIGN_JOBS:
        pack = _step(_sign_job(name), "Pack and sign the installer")["run"]
        assert "build_windows.ps1 -SkipBuild" in pack
        assert "packaging\\sign_call.ps1" in pack and "-File $q{0}$q $f" in pack
    # the one script Inno calls: signs with the timestamp and the dlib, counts the attempt, holds
    # no credential of its own
    call = (PACKAGING / "sign_call.ps1").read_text(encoding="utf-8")
    assert "/tr http://timestamp.acs.microsoft.com" in call and "/dlib" in call
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in call
    env_names = set(re.findall(r"\$env:([A-Z_]+)", call))
    assert env_names == {
        "KESTREL_SIGNTOOL",
        "KESTREL_SIGN_DLIB",
        "KESTREL_SIGN_META",
        "KESTREL_SIGN_COUNTER",
    }


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
    "azure/login": 3,
    "azure/artifact-signing-action": 2,
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
    assert set(release["needs"]) == _RELEASE_NEEDS
    assert "github.event_name == 'push'" in release["if"]  # a dry run never publishes


def test_the_ci_test_job_has_a_time_limit_well_under_the_default():
    """D-CI-13: a hung test once kept a runner busy for hours, because a job with no limit runs
    for GitHub's 6-hour default. D-REL-65 (2026-11-21): the suite grew to 15,800 tests, the Windows
    job measured about 92 minutes and Ubuntu about 49, so the limits are 120 and 90, still far
    under 6 hours."""
    limit = _workflow("ci.yml")["jobs"]["test"].get("timeout-minutes")
    assert limit == "${{ matrix.os == 'windows-latest' && 120 || 90 }}"


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


def _workflow_file_names(folder: Path) -> list[str]:
    """Every workflow file in a folder: GitHub runs both .yml and .yaml."""
    return sorted(p.name for p in folder.iterdir() if p.suffix in {".yml", ".yaml"})


def _workflow_names() -> list[str]:
    """Every workflow file, so a new one (the security export, D-SEC-6) is guarded too."""
    return _workflow_file_names(WORKFLOWS)


# Every action in every workflow is pinned by full commit SHA with its release tag in a trailing
# comment: the release workflow can reach the signing credential, and a moved tag in any workflow
# would change code that runs with the repository's rights.


def _uses_in(wf: dict) -> list[str]:
    """Every `uses:` of a parsed workflow: job-level (reusable workflows) and step-level, read
    from the YAML so quoting and spacing cannot hide one."""
    found = []
    for job in wf["jobs"].values():
        found.append(job.get("uses"))
        found.extend(step.get("uses") for step in job.get("steps", []))
    return [str(u) for u in found if u is not None]


def _pin_problems(wf: dict, raw: str) -> list[str]:
    """Why a workflow's actions are not pinned as required: a local or unparsable reference, a
    moved tag, a SHA without its tag comment, or a pre-Node-24 major."""
    problems = []
    for uses in _uses_in(wf):
        match = re.fullmatch(r"([\w.-]+/[\w.-]+)(?:/[\w./-]+)?@([\w.-]+)", uses)
        if not match:
            problems.append(f"{uses}: not an owner/name@ref action (local or unpinned)")
            continue
        action, ref = match.groups()
        if action not in NODE24_ACTION_FLOORS:
            problems.append(f"{uses}: no Node 24 floor for {action}")
            continue
        tag = re.search(re.escape(f"{action}@{ref}") + r"[ \t]+#[ \t]*(v\d+\.\d+\.\d+)", raw)
        if not (re.fullmatch(r"[0-9a-f]{40}", ref) and tag):
            problems.append(f"{uses}: needs a full SHA and a tag comment")
            continue
        if int(re.match(r"v(\d+)", tag.group(1)).group(1)) < NODE24_ACTION_FLOORS[action]:
            problems.append(f"{uses}: predates Node 24")
    return problems


def test_every_action_in_every_workflow_is_pinned_by_commit_and_meets_its_node24_floor():
    names = _workflow_names()
    assert {"ci.yml", "release.yml", "security-export.yml"} <= set(names)
    for name in names:
        raw = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert _pin_problems(_workflow(name), raw) == [], name
        assert _uses_in(_workflow(name)), name  # none was skipped because it used no action


@pytest.mark.parametrize(
    "uses",
    [
        '"actions/checkout@main"',  # quoted, moved branch
        "'actions/checkout@v4'",  # quoted, below the Node 24 floor
        "./.github/actions/anything",  # a local action
        "actions/unknown@v1",  # no floor recorded
        "actions/checkout@v5",  # a major tag
        "azure/login@v3",  # a signing action by tag
        "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",  # SHA without its tag comment
        "docker://alpine:3",  # a container
    ],
)
def test_the_pin_check_catches_what_a_text_search_missed(uses):
    raw = f"jobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {uses}\n"
    assert _pin_problems(yaml.safe_load(raw), raw) != []


def test_the_pin_check_catches_a_reusable_workflow_and_accepts_the_real_pins():
    raw = "jobs:\n  x:\n    uses: ./.github/workflows/other.yml\n"
    assert _pin_problems(yaml.safe_load(raw), raw) != []
    sha = "a641126d1b8aa4d1fa005f4f92df94a3a4c4c906"
    good = f"jobs:\n  x:\n    steps:\n      - uses: azure/login@{sha} # v3.1.0\n"
    assert _pin_problems(yaml.safe_load(good), good) == []


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
    assert set(job["needs"]) == {"preflight", "release"}
    cond = job["if"]
    assert "github.event_name == 'push'" in cond
    # the version-tag rule lives in preflight, so the trigger, the release and the feed agree
    assert "needs.preflight.outputs.is_version_tag == 'true'" in cond
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
    # #40: hdiutil is deprecated on macOS 27. Probe for diskutil first, fall back to hdiutil.
    assert "diskutil image create" in sh and "hdiutil create" in sh and "UDZO" in sh
    code = _code(PACKAGING / "build_dmg.sh")  # comments cannot satisfy the order check
    probe = code.index("diskutil image create --help")
    # the download (UDZO) path: probe, diskutil, then hdiutil as the fallback
    assert probe < code.index("diskutil image create from") < code.index("hdiutil create", probe)
    assert "-format UDZO" in code[code.index("hdiutil create", probe) :]
    assert "VERIFIED on macOS 27" in sh  # the diskutil path ran on a real macOS 27 host
    assert "ln -s /Applications" in sh
    assert "APPLE_SIGNING_IDENTITY" in sh and "UNSIGNED" in sh
    # #202: signing, notarization and stapling live in sign_dmg.sh, which the release workflow
    # runs in its own job; a local build with APPLE_SIGNING_IDENTITY signs through the same script
    assert "packaging/macos/sign_dmg.sh" in sh and '"$MACOS/sign_dmg.sh" sign' in code
    signer = (PACKAGING / "macos" / "sign_dmg.sh").read_text(encoding="utf-8")
    assert "notarytool submit" in signer and "stapler staple" in signer
    assert "entitlements.plist" in signer
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
        encoding="utf-8",
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


def test_release_is_titled_with_the_product_name():
    # softprops falls back to the bare tag ("v0.6.1"); earlier releases read "Kestrel v0.x".
    steps = _workflow("release.yml")["jobs"]["release"]["steps"]
    gh = next(s for s in steps if str(s.get("uses", "")).startswith("softprops/"))
    assert gh["with"]["name"] == "Kestrel ${{ github.ref_name }}"


def test_release_preflight_refuses_a_tag_that_already_has_a_release():
    """A tag whose release is already published fails in preflight, before 35 minutes of builds
    that could not be attached to it."""
    wf = _workflow("release.yml")
    steps = wf["jobs"]["preflight"]["steps"]
    check = next(s for s in steps if s.get("name") == "No published release for this tag")
    assert "github.event_name == 'push'" in check["if"]
    assert "gh release view" in check["run"] and "exit 1" in check["run"]
    assert "GH_TOKEN" in check["env"]


# ------------------------------------------------------------------------- tray icons (#39)
_ORANGE_HEXES = ("EE8A4E", "C9551F", "D4622A")


def test_mark_svgs_carry_no_retired_orange():
    """Orange #D4622A is retired as a brand color (D-UI-53). The light tray mark still had the old
    orange bird gradient, so the idle tray icon stayed orange."""
    for name in ("kestrel-mark.svg", "kestrel-mark-light.svg"):
        svg = (ROOT / "docs" / "images" / name).read_text(encoding="utf-8").upper()
        for hex_ in _ORANGE_HEXES:
            assert hex_ not in svg, f"{name} still contains {hex_}"


def _is_orange(r: int, g: int, b: int) -> bool:
    import colorsys

    h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return 10 <= h * 360 <= 30 and s > 0.4 and r > b + 60


def test_the_orange_detector_flags_orange_and_spares_indigo_and_the_attention_red():
    assert _is_orange(0xD4, 0x62, 0x2A) and _is_orange(0xEE, 0x8A, 0x4E)
    assert not _is_orange(0x4F, 0x5B, 0xD5) and not _is_orange(0x7B, 0x86, 0xF0)
    assert not _is_orange(0xB3, 0x26, 0x1E)  # the attention dot, hue about 3 degrees


def test_tray_pngs_have_no_orange_pixel():
    """Regenerate with `uv run python scripts/make_icons.py` after any change to the marks."""
    from PIL import Image

    icons = ROOT / "kestrel" / "resources" / "icons"
    names = ("tray-idle.png", "tray-working.png", "tray-working_att.png")
    for name in names:
        im = Image.open(icons / name).convert("RGBA")
        bad = [
            px[:3]
            for px in im.getdata()
            if px[3] == 255 and _is_orange(px[0], px[1], px[2])
        ]
        assert not bad, f"{name} has {len(bad)} orange pixels, e.g. {bad[0]}"


# ------------------------------------------------------ the embed model on a fresh clone (#41)
def test_every_build_script_fetches_the_embed_model_before_pyinstaller():
    """kestrel.spec refuses to build without kestrel/resources/embed_model/*, and the model is
    never committed, so a fresh clone failed inside PyInstaller. Each local build script now
    checks for it and fetches it first (release.yml already does)."""
    for name, runner in (
        ("build_dmg.sh", "PyInstaller packaging/kestrel.spec"),
        ("build_appimage.sh", "PyInstaller packaging/kestrel.spec"),
        ("build_windows.ps1", "PyInstaller packaging\\kestrel.spec"),
    ):
        text = _code(PACKAGING / name)
        assert "fetch_embed_model.py" in text, name
        assert "--check" in text.split("fetch_embed_model.py", 1)[1][:60], name
        assert text.index("fetch_embed_model.py") < text.index(runner), name


def test_releasing_doc_mentions_the_embed_model_for_local_builds():
    doc = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
    local = doc.split("### Building locally", 1)[1].split("\n## ", 1)[0]
    assert "fetch_embed_model.py" in local


# ------------------------------------------------------------ the vendored AppImage runtime (#44)
VENDORED_RUNTIME = PACKAGING / "linux" / "runtime-x86_64"


def _runtime_pin() -> str:
    sh = (PACKAGING / "build_appimage.sh").read_text(encoding="utf-8")
    return re.search(r'^RUNTIME_SHA256="([0-9a-f]{64})"', sh, re.MULTILINE).group(1)


def test_the_vendored_runtime_matches_the_pinned_hash():
    """Upstream rebuilds its moving `continuous` asset, and every rebuild used to stop a Linux
    release until someone looked at the new file. The reviewed runtime now lives in the repo; a
    bump must change this file and RUNTIME_SHA256 together (docs/RELEASING.md)."""
    import hashlib

    data = VENDORED_RUNTIME.read_bytes()
    assert data[:4] == b"\x7fELF"
    assert hashlib.sha256(data).hexdigest() == _runtime_pin()


def test_the_vendored_runtime_is_marked_binary_so_git_never_rewrites_it():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    rules = [ln.split() for ln in attrs if ln.strip() and not ln.lstrip().startswith("#")]
    assert ["packaging/linux/runtime-x86_64", "binary"] in rules
    # Last matching line wins, so the override has to come after the `* text=auto eol=lf` rule.
    flat = [r[0] for r in rules]
    assert flat.index("packaging/linux/runtime-x86_64") > flat.index("*")


def test_appimage_resolves_the_runtime_override_then_vendored_then_download():
    code = _code(PACKAGING / "build_appimage.sh")
    override = code.index('if [ -n "${APPIMAGE_RUNTIME_FILE:-}" ]')
    vendored = code.index('elif [ -f "$VENDORED_RUNTIME" ]')
    download = code.index('curl -fsSL --retry 3 -o "$RUNTIME" "$RUNTIME_URL"')
    assert override < vendored < download
    # The vendored file is used only when its hash matches the pin; a stale one falls through.
    assert "sha256sum -c --status" in code[vendored:download]


def test_the_runtime_notice_carries_the_mit_text_and_travels_with_the_appimage():
    notice = (PACKAGING / "linux" / "RUNTIME-NOTICE.txt").read_text(encoding="utf-8")
    assert "type2-runtime" in notice and "MIT License" in notice
    assert "Permission is hereby granted, free of charge" in notice
    assert _runtime_pin() in notice  # says which runtime build it describes
    assert "RUNTIME-NOTICE.txt" in _code(PACKAGING / "build_appimage.sh")  # copied into the AppDir


def test_releasing_doc_explains_how_to_bump_the_vendored_runtime():
    doc = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
    assert "packaging/linux/runtime-x86_64" in doc and "RUNTIME_SHA256" in doc
    assert "RUNTIME-NOTICE.txt" in doc


def test_every_app_shortcut_carries_the_desktop_app_identity():
    # The shortcut's AppUserModelID must equal the one the process declares, or Windows keeps
    # attributing toasts to "Kestrel.exe" with a stale icon.
    from kestrel.desktop.shell import APP_USER_MODEL_ID

    lines = [
        ln
        for ln in ISS.read_text(encoding="utf-8").splitlines()
        if ln.startswith("Name: ") and 'Filename: "{app}\\Kestrel.exe"' in ln
    ]
    assert len(lines) == 3
    for ln in lines:
        assert f'AppUserModelID: "{APP_USER_MODEL_ID}"' in ln, ln


def test_iss_refreshes_shortcuts_and_the_notification_identity():
    """Round 4: a toast showed the pre-upgrade orange mark. Every install and upgrade deletes and
    rewrites the shortcuts with the AUMID and the exe's icon, writes the notification identity
    key (removed on uninstall), and tells Explorer that icons changed."""
    from kestrel.desktop import shell

    iss = ISS.read_text(encoding="utf-8")
    shortcuts = [
        line for line in iss.splitlines() if line.startswith("Name:") and "Kestrel.exe" in line
    ]
    assert len(shortcuts) == 3
    for line in shortcuts:
        assert f'AppUserModelID: "{shell.APP_USER_MODEL_ID}"' in line
        assert 'IconFilename: "{app}\\Kestrel.exe"' in line
    deletes = iss[iss.index("[InstallDelete]") : iss.index("[Icons]")]
    for where in ("{group}", "{autodesktop}", "{userstartup}"):
        assert f'Name: "{where}\\Kestrel.lnk"' in deletes
    registry = iss[iss.index("[Registry]") : iss.index("[Run]")]
    key = f'Subkey: "{shell.AUMID_KEY}"'
    values = [line for line in registry.splitlines() if line.startswith("Root:")]
    assert len(values) == 2
    for line in values:
        assert "Root: HKCU" in line and key in line and "uninsdeletekey" in line
    assert 'ValueName: "DisplayName"; ValueData: "Kestrel"' in registry
    # The installed .ico is the shipped package-data copy, at its one-folder path.
    assert "{app}\\_internal\\kestrel\\resources\\icons\\kestrel.ico" in registry
    code = iss[iss.index("[Code]") :]
    assert "SHChangeNotify@shell32.dll stdcall" in code
    assert "SHCNE_ASSOCCHANGED = $08000000" in code
    assert "ssPostInstall" in code
    assert code.count("SHChangeNotify(SHCNE_ASSOCCHANGED") == 2  # install and uninstall


def test_the_shipped_app_icon_is_the_installer_icon():
    """The notification IconUri points at package data; it must be the same indigo mark as the
    installer and exe icon (scripts/make_icons.py writes both)."""
    from kestrel import paths

    with paths.resource_path(paths.packaged_app_icon()) as shipped:
        assert shipped.read_bytes() == (PACKAGING / "kestrel.ico").read_bytes()


def test_security_export_keeps_its_token_away_from_third_party_actions():
    """D-SEC-6: the alerts token reaches only GitHub's own gh CLI, the job is read-only, and it
    runs only when started by hand."""
    wf = _workflow("security-export.yml")
    assert set(wf[True]) == {"workflow_dispatch"}  # YAML reads the key `on` as True
    assert wf["permissions"] == {"contents": "read"}
    for job in wf["jobs"].values():
        for step in job["steps"]:
            env = step.get("env", {})
            if "ALERTS_READ_TOKEN" in str(env):
                assert "uses" not in step and "gh api" in step["run"]
            if "uses" in step:
                assert "ALERTS_READ_TOKEN" not in str(step)


def test_phonenumbers_region_files_and_metadata_would_be_bundled_and_lookups_are_unused():
    """#207: country rules are imported by name at run time, so PyInstaller's own collectors must
    find them; checked with the collectors the spec calls, not by reading the spec. The geocoder,
    carrier and timezone data stay out, so nothing Kestrel imports may need them."""
    import subprocess
    import sys

    from PyInstaller.utils.hooks import collect_submodules, copy_metadata

    mods = set(collect_submodules("phonenumbers.data"))
    assert {"phonenumbers.data.region_US", "phonenumbers.data.region_GB",
            "phonenumbers.data.region_DE", "phonenumbers.data.region_IT"} <= mods
    assert len(mods) > 200  # every region and every number-type module, not a handful
    assert any("phonenumbers" in dest for _src, dest in copy_metadata("phonenumbers"))
    code = (
        "import sys; from kestrel import phone, linkedin; "
        "phone.format_phone('+44 20 7946 0000', 'US'); "
        "bad = [m for m in sys.modules if m.startswith(('phonenumbers.geodata', "
        "'phonenumbers.carrierdata', 'phonenumbers.timezone'))]; "
        "print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         encoding="utf-8", timeout=120, check=True).stdout
    assert out.strip() == "[]"
    # The spec itself must ask for them. Read as code (comments and strings that merely mention a
    # call do not count): this is the executed line that ships the files (D-PHONE-1).
    found = _spec_phone_calls()
    assert found["hiddenimports"] and found["metadata"]
    assert found["lookups"] == {"phonenumbers.geodata", "phonenumbers.carrierdata",
                                "phonenumbers.timezone"}
    assert found["excluded"]

def _spec_phone_calls(source: str | None = None) -> dict:
    """What kestrel.spec EXECUTES about phonenumbers, found in its syntax tree: the hidden import
    of the region modules, the metadata copy, the lookup packages and their use in `excludes`."""
    import ast

    tree = ast.parse(source if source is not None else SPEC.read_text(encoding="utf-8"))

    def call_named(node: ast.AST, name: str, arg: str) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == name and bool(node.args)
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == arg)

    out: dict = {"hiddenimports": False, "metadata": False, "lookups": set(), "excluded": False}
    for stmt in tree.body:
        if isinstance(stmt, ast.AugAssign):
            if (isinstance(stmt.target, ast.Name) and stmt.target.id == "hiddenimports"
                    and call_named(stmt.value, "collect_submodules", "phonenumbers.data")):
                out["hiddenimports"] = True
            if (isinstance(stmt.target, ast.Name) and stmt.target.id == "datas"
                    and call_named(stmt.value, "copy_metadata", "phonenumbers")):
                out["metadata"] = True
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == "_PHONE_LOOKUP_DATA"
                and isinstance(stmt.value, ast.List)):
            out["lookups"] = {e.value for e in stmt.value.elts if isinstance(e, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "excludes":
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            out["excluded"] = "_PHONE_LOOKUP_DATA" in names
    return out


def test_the_phone_spec_check_cannot_be_fooled_by_a_comment():
    """Round 4: commenting a line out, or only naming it in a string, fails the check."""
    spec = SPEC.read_text(encoding="utf-8")
    line = 'hiddenimports += collect_submodules("phonenumbers.data")'
    assert line in spec
    assert _spec_phone_calls(spec)["hiddenimports"]
    assert not _spec_phone_calls(spec.replace(line, "# " + line))["hiddenimports"]
    assert not _spec_phone_calls(spec.replace(line, "_note = '" + line + "'"))["hiddenimports"]
    meta = 'datas += copy_metadata("phonenumbers")'
    assert not _spec_phone_calls(spec.replace(meta, "# " + meta))["metadata"]
    assert not _spec_phone_calls(spec.replace("+ _PHONE_LOOKUP_DATA,", ","))["excluded"]
