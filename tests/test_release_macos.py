"""macOS release: the disk image window (#198), Developer ID signing and notarization (#202) and
the Homebrew tap (#201).

The invariant: no signing certificate, notary key or tap token is ever printed or reachable from a
pull request, a fork or a dry run. The workflow tests read release.yml as data; the script tests
run the real step scripts and packaging/macos/sign_dmg.sh in bash with stand-in Apple tools, so
they check behavior rather than spelling. Nothing here touches the network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
MACOS = ROOT / "packaging" / "macos"
SIGN_DMG = MACOS / "sign_dmg.sh"
sys.path.insert(0, str(ROOT / "scripts"))

import homebrew_cask  # noqa: E402

BASH = shutil.which("bash")
# The stand-in tools are bash scripts on PATH; Windows cannot run them as commands.
posix_tools = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None, reason="stand-in tools need a POSIX shell"
)

DEV_NAME = "Developer ID Application: Sample Maker (ABCDE12345)"
TEST_NAME = "Apple Development: Sample Maker (FGHIJ67890)"
TEAM = "ABCDE12345"
TAG_RULE = [f"refs/tags/v{d}" for d in range(10)]


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _workflow_names() -> list[str]:
    return sorted(p.name for p in WORKFLOWS.iterdir() if p.suffix in (".yml", ".yaml"))


def _jobs() -> dict:
    return _workflow("release.yml")["jobs"]


def _step(job: dict, name: str) -> dict:
    found = [s for s in job["steps"] if s.get("name") == name]
    assert len(found) == 1, (name, [s.get("name") for s in job["steps"]])
    return found[0]


def _names(job: dict) -> list[str]:
    return [s.get("name") or str(s.get("uses")).split("@")[0] for s in job["steps"]]


# ------------------------------------------------------------------------------- preflight


def _run_mac_preflight(ref: str, event: str, values: dict[str, str]) -> tuple[int, str, dict]:
    """Run the real preflight step in bash, with the flags GitHub uses for `shell: bash`."""
    import tempfile

    step = _step(_jobs()["preflight"], "macOS signing is configured")
    assert step["shell"] == "bash" and step["id"] == "macsigncfg"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "output"
        out.touch()
        env = {
            "PATH": os.environ["PATH"],
            "GITHUB_REF": ref,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_OUTPUT": str(out),
        }
        # every input the step reads starts empty or "false", then the scenario sets some
        for key, source in step["env"].items():
            env[key] = "false" if "!= ''" in source else ""
        for var, value in values.items():
            keys = [k for k, src in step["env"].items() if f"vars.{var} " in src + " "]
            assert keys, var
            for key in keys:
                env[key] = (
                    ("true" if value.strip() else "false") if key.startswith("HAS_") else value
                )
        done = subprocess.run(  # noqa: S603
            [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        written = dict(
            line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if line
        )
    return done.returncode, done.stdout + done.stderr, written


_PROD = {
    "APPLE_DEVELOPER_ID_NAME": DEV_NAME,
    "APPLE_TEAM_ID": TEAM,
    "APPLE_NOTARY_KEY_ID": "KEY1234567",
    "APPLE_NOTARY_ISSUER_ID": "00000000-1111-2222-3333-444444444444",
}
_TEST = {"APPLE_TEST_SIGNING_NAME": TEST_NAME, "APPLE_TEAM_ID": TEAM}
_ALL = _PROD | _TEST
_MAC_NAMES = (
    "APPLE_DEVELOPER_ID_NAME",
    "APPLE_TEAM_ID",
    "APPLE_NOTARY_KEY_ID",
    "APPLE_NOTARY_ISSUER_ID",
)

# ref, event, values, expected mode or ("stop", names that must be reported missing)
_MAC_CASES = [
    ("refs/tags/v1.2.3", "push", _PROD, "production"),
    ("refs/tags/v1.2.3", "push", _ALL, "production"),
    ("refs/tags/v0.7.7-rc.1", "push", _PROD, "production"),
    ("refs/tags/v1.2.3", "push", {}, "none"),
    ("refs/tags/v1.2.3", "push", {"APPLE_TEAM_ID": TEAM}, "none"),
    ("refs/tags/v1.2.3", "push", _TEST, "none"),
    (
        "refs/tags/v1.2.3",
        "push",
        {"APPLE_DEVELOPER_ID_NAME": DEV_NAME},
        ("stop", ["APPLE_TEAM_ID", "APPLE_NOTARY_KEY_ID", "APPLE_NOTARY_ISSUER_ID"]),
    ),
    (
        "refs/tags/v1.2.3",
        "push",
        {"APPLE_NOTARY_KEY_ID": "KEY1234567"},
        ("stop", ["APPLE_DEVELOPER_ID_NAME", "APPLE_TEAM_ID", "APPLE_NOTARY_ISSUER_ID"]),
    ),
    (
        "refs/tags/v1.2.3",
        "push",
        _PROD | {"APPLE_TEAM_ID": ""},
        ("stop", ["APPLE_TEAM_ID"]),
    ),
    (
        "refs/tags/v1.2.3",
        "push",
        _PROD | {"APPLE_DEVELOPER_ID_NAME": " \t "},  # only whitespace counts as unset
        ("stop", ["APPLE_DEVELOPER_ID_NAME"]),
    ),
    # a name that is not a Developer ID Application certificate cannot sign a release
    ("refs/tags/v1.2.3", "push", _PROD | {"APPLE_DEVELOPER_ID_NAME": TEST_NAME}, ("stop", [])),
    # a test certificate that is the release certificate, in any spelling, is refused everywhere
    (
        "refs/heads/main",
        "workflow_dispatch",
        _ALL | {"APPLE_TEST_SIGNING_NAME": DEV_NAME},
        ("stop", []),
    ),
    (
        "refs/heads/main",
        "workflow_dispatch",
        _ALL | {"APPLE_TEST_SIGNING_NAME": "  " + DEV_NAME.upper() + "\t"},
        ("stop", []),
    ),
    ("refs/tags/v1.2.3", "push", _ALL | {"APPLE_TEST_SIGNING_NAME": DEV_NAME}, ("stop", [])),
    # a test name for any Developer ID certificate is refused: macOS trusts it
    (
        "refs/heads/main",
        "workflow_dispatch",
        _TEST | {"APPLE_TEST_SIGNING_NAME": "Developer ID Application: Other (ZZZZZ99999)"},
        ("stop", []),
    ),
    ("refs/heads/main", "workflow_dispatch", _TEST, "test"),
    ("refs/heads/main", "workflow_dispatch", _ALL, "test"),
    ("refs/heads/main", "workflow_dispatch", _PROD, "none"),
    # a dry run with the test name but no team is partly configured, and says so
    (
        "refs/heads/main",
        "workflow_dispatch",
        {"APPLE_TEST_SIGNING_NAME": TEST_NAME},
        ("stop", ["APPLE_TEAM_ID"]),
    ),
    # a tag that is not vX.Y.Z or vX.Y.Z-suffix stops before anything is built, whatever is set
    ("refs/tags/v1.2", "push", {}, ("stop", [])),
    ("refs/tags/v1.2.3.4", "push", _PROD, ("stop", [])),
    ("refs/tags/v10", "push", {}, ("stop", [])),
    ("refs/tags/v1.2.3-", "push", {}, ("stop", [])),
    ("refs/tags/v1.2.3-rc.1", "push", {}, "none"),
    ("refs/tags/v1.2", "workflow_dispatch", {}, "none"),  # the shape rule is for tag pushes
    ("refs/heads/main", "workflow_dispatch", _TEST | {"APPLE_TEST_SIGNING_NAME": "  "}, "none"),
    ("refs/heads/main", "push", _ALL, "none"),
    ("refs/heads/feature/x", "workflow_dispatch", _ALL, "none"),
    ("refs/heads/main-evil", "workflow_dispatch", _ALL, "none"),
    ("refs/tags/v1.2.3", "workflow_dispatch", _ALL, "none"),
    ("refs/tags/vNext", "push", _ALL, "none"),
    ("refs/tags/1.2.3", "push", _ALL, "none"),
    ("refs/pull/7/merge", "push", _ALL, "none"),
]


@posix_tools
@pytest.mark.parametrize(("ref", "event", "values", "expected"), _MAC_CASES)
def test_mac_preflight_chooses_the_mode_by_running_its_own_script(ref, event, values, expected):
    code, output, written = _run_mac_preflight(ref, event, values)
    stopped = isinstance(expected, tuple)
    assert (code != 0) is stopped, output
    assert ("::error::" in output) is stopped, output
    assert written == ({} if stopped else {"mac_sign_mode": expected}), (written, output)
    # a partly configured release lists exactly what is missing; other stops list nothing
    parts = output.split("Missing:", 1)
    reported = parts[1].split("(", 1)[0].split() if len(parts) == 2 else []
    assert reported == (expected[1] if stopped else []), output


@posix_tools
def test_an_unsigned_tag_release_says_so_and_a_partial_one_names_every_missing_value():
    code, output, _ = _run_mac_preflight("refs/tags/v1.2.3", "push", {})
    assert code == 0 and "::notice::" in output and "unsigned" in output
    for name in _MAC_NAMES:
        partial = {k: v for k, v in _PROD.items() if k != name}
        code, output, _ = _run_mac_preflight("refs/tags/v1.2.3", "push", partial)
        assert code != 0 and "partly configured" in output, (name, output)
        assert output.split("Missing:", 1)[1].split()[0] == name, output


def test_mac_preflight_reads_only_repository_variables_and_writes_only_the_mode():
    pre = _jobs()["preflight"]
    step = _step(pre, "macOS signing is configured")
    assert "secrets." not in str(step)  # the certificate and the key are never read here
    assert set(step["env"]) == {
        "DEVELOPER_ID_NAME",
        "TEST_SIGNING_NAME",
        "HAS_TEAM_ID",
        "HAS_NOTARY_KEY_ID",
        "HAS_NOTARY_ISSUER",
    }
    assert pre["outputs"]["mac_sign_mode"] == "${{ steps.macsigncfg.outputs.mac_sign_mode }}"


# ------------------------------------------------------------------------------- job conditions


def _eval_if(expression: str, *, ref: str, event: str, mode: str, version_tag: bool) -> bool:
    """Evaluate a job `if:` with GitHub's documented functions only."""
    python = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    python = python.replace(" not =", " !=")
    for name, value in (
        ("needs.preflight.outputs.mac_sign_mode", "MODE"),
        ("needs.preflight.outputs.is_version_tag", "VERSION_TAG"),
        ("github.event_name", "EVENT"),
        ("github.ref_name", "REF_NAME"),
        ("github.ref", "REF"),
    ):
        python = python.replace(name, value)
    names = {
        "REF": ref,
        "REF_NAME": ref.rsplit("/", 1)[-1] if ref.startswith("refs/tags/") else ref,
        "EVENT": event,
        "MODE": mode,
        "VERSION_TAG": "true" if version_tag else "false",
        "startsWith": lambda text, prefix: text.startswith(prefix),
        "contains": lambda haystack, needle: needle in haystack,
    }
    return bool(eval(python, {"__builtins__": {}}, names))  # noqa: S307


_REFS = [
    "refs/tags/v1.2.3",
    "refs/tags/v0.7.7-rc.1",
    "refs/tags/v10",
    "refs/tags/vNext",
    "refs/tags/1.2.3",
    "refs/heads/main",
    "refs/heads/main-evil",
    "refs/heads/feature/x",
    "refs/pull/7/merge",
]


def _is_version_tag(ref: str) -> bool:
    return bool(re.fullmatch(r"refs/tags/v[0-9].*", ref))


@pytest.mark.parametrize("ref", _REFS)
@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "pull_request"])
@pytest.mark.parametrize("mode", ["production", "test", "none"])
def test_each_mac_job_with_a_secret_runs_only_for_its_own_mode_ref_and_event(ref, event, mode):
    jobs = _jobs()
    tag = _is_version_tag(ref)
    kwargs = {"ref": ref, "event": event, "mode": mode, "version_tag": tag}
    prod = _eval_if(jobs["macos-sign"]["if"], **kwargs)
    test = _eval_if(jobs["macos-sign-test"]["if"], **kwargs)
    tap = _eval_if(jobs["homebrew-tap"]["if"], **kwargs)
    assert prod is (mode == "production" and event == "push" and tag)
    assert test is (mode == "test" and event == "workflow_dispatch" and ref == "refs/heads/main")
    # the tap: a stable version tag push only, whatever the signing mode
    assert tap is (event == "push" and tag and "-" not in ref)
    assert not (prod and test)


def test_the_jobs_are_wired_as_the_design_says():
    jobs = _jobs()
    for name in ("macos-sign", "macos-sign-test"):
        assert set(jobs[name]["needs"]) == {"preflight", "macos"}
        downloads = [
            s["with"]["name"]
            for s in jobs[name]["steps"]
            if "download-artifact@" in str(s.get("uses"))
        ]
        assert downloads == ["macos-layout"]
    release = jobs["release"]
    assert {"macos", "macos-sign", "macos-sign-test"} <= set(release["needs"])
    assert release["if"].endswith("!cancelled() && !failure()")
    # the tap waits for the published release and latest.json, and runs after no failure
    tap = jobs["homebrew-tap"]
    assert set(tap["needs"]) == {"preflight", "release", "website"}
    assert "always()" not in tap["if"] and "cancelled()" not in tap["if"]
    uploads = [
        s["with"]["name"]
        for s in jobs["website"]["steps"]
        if "upload-artifact@" in str(s.get("uses"))
    ]
    assert uploads == ["latest-json"]
    downloads = [
        s["with"]["name"] for s in tap["steps"] if "download-artifact@" in str(s.get("uses"))
    ]
    assert downloads == ["latest-json"]


def test_the_build_job_hands_over_the_layout_before_the_app_runs_and_never_publishes_it():
    build = _jobs()["macos"]
    assert "environment" not in build and "permissions" not in build
    assert "secrets." not in str(build)
    names = _names(build)
    layout = next(
        i for i, s in enumerate(build["steps"]) if s.get("with", {}).get("name") == "macos-layout"
    )
    assert (
        names.index("Build the layout image (to be signed)")
        < layout
        < names.index("Smoke test the bundle")
    )
    none = "needs.preflight.outputs.mac_sign_mode == 'none'"
    some = "needs.preflight.outputs.mac_sign_mode != 'none'"
    assert _step(build, "Build dmg")["if"] == none
    assert _step(build, "Build the layout image (to be signed)")["if"] == some
    assert "--rw" in _step(build, "Build the layout image (to be signed)")["run"]
    assert build["steps"][layout]["if"] == some
    uploads = {
        s["with"]["name"]: s.get("if")
        for s in build["steps"]
        if "upload-artifact@" in str(s.get("uses"))
    }
    assert uploads == {"macos-layout": some, "Kestrel-macos": none}
    # the release takes only Kestrel-* artifacts, so the layout image never reaches it
    download = next(
        s for s in _jobs()["release"]["steps"] if "download-artifact@" in str(s.get("uses"))
    )
    assert download["with"]["pattern"] == "Kestrel-*"


# ------------------------------------------------------------------------------- secrets


# Every place a macOS or tap secret may be named, in every workflow: (workflow, job, step, key).
_SECRET_PLACES = {
    (
        "release.yml",
        "macos-sign",
        "The signing secrets are set",
        "HAS_CERT",
        "APPLE_CERT_P12_BASE64",
    ),
    (
        "release.yml",
        "macos-sign",
        "The signing secrets are set",
        "HAS_CERT_PASSWORD",
        "APPLE_CERT_PASSWORD",
    ),
    (
        "release.yml",
        "macos-sign",
        "The signing secrets are set",
        "HAS_NOTARY_KEY",
        "APPLE_NOTARY_KEY_P8",
    ),
    (
        "release.yml",
        "macos-sign",
        "Import the signing certificate",
        "CERT_P12_BASE64",
        "APPLE_CERT_P12_BASE64",
    ),
    (
        "release.yml",
        "macos-sign",
        "Import the signing certificate",
        "CERT_PASSWORD",
        "APPLE_CERT_PASSWORD",
    ),
    ("release.yml", "macos-sign", "Notarize and staple", "NOTARY_KEY_P8", "APPLE_NOTARY_KEY_P8"),
    (
        "release.yml",
        "macos-sign-test",
        "The signing secrets are set",
        "HAS_CERT",
        "APPLE_TEST_CERT_P12_BASE64",
    ),
    (
        "release.yml",
        "macos-sign-test",
        "The signing secrets are set",
        "HAS_CERT_PASSWORD",
        "APPLE_TEST_CERT_PASSWORD",
    ),
    (
        "release.yml",
        "macos-sign-test",
        "Import the signing certificate",
        "CERT_P12_BASE64",
        "APPLE_TEST_CERT_P12_BASE64",
    ),
    (
        "release.yml",
        "macos-sign-test",
        "Import the signing certificate",
        "CERT_PASSWORD",
        "APPLE_TEST_CERT_PASSWORD",
    ),
    ("release.yml", "homebrew-tap", "actions/checkout", "token", "HOMEBREW_TAP_TOKEN"),
    ("release.yml", "homebrew-tap", "The tap token is set", "HAS_TAP_TOKEN", "HOMEBREW_TAP_TOKEN"),
    (
        "release.yml",
        "macos-sign",
        "The signing secrets are set",
        "HAS_TAP_TOKEN",
        "HOMEBREW_TAP_TOKEN",
    ),
    (
        "release.yml",
        "macos-sign-check",
        "No signing secret is set without the variables",
        "HAS_CERT",
        "APPLE_CERT_P12_BASE64",
    ),
    (
        "release.yml",
        "macos-sign-check",
        "No signing secret is set without the variables",
        "HAS_CERT_PASSWORD",
        "APPLE_CERT_PASSWORD",
    ),
    (
        "release.yml",
        "macos-sign-check",
        "No signing secret is set without the variables",
        "HAS_NOTARY_KEY",
        "APPLE_NOTARY_KEY_P8",
    ),
}
_OURS = re.compile(r"secrets\.((?:APPLE|HOMEBREW)_[A-Z0-9_]*)")


def _secret_places(text_of_workflow: dict, workflow: str) -> set[tuple]:
    """Every mention of a macOS or tap secret, located down to the step and the key. A mention
    anywhere else (a job env, a run script, a with: of another action) is located as such."""
    found = set()
    for job_name, job in text_of_workflow.get("jobs", {}).items():
        for key, value in job.items():
            if key != "steps":
                for secret in _OURS.findall(str(value)):
                    found.add((workflow, job_name, f"job {key}", "", secret))
        for step in job.get("steps", []):
            label = step.get("name") or str(step.get("uses", "")).split("@")[0]
            for key, value in step.items():
                if isinstance(value, dict):
                    for inner, text in value.items():
                        for secret in _OURS.findall(str(text)):
                            where = inner if key in ("env", "with") else f"{key}.{inner}"
                            found.add((workflow, job_name, label, where, secret))
                else:
                    for secret in _OURS.findall(str(value)):
                        found.add((workflow, job_name, label, key, secret))
    return found


def test_macos_and_tap_secrets_are_named_only_in_their_own_steps_in_any_workflow():
    found = set()
    for name in _workflow_names():
        found |= _secret_places(_workflow(name), name)
    assert found == _SECRET_PLACES


def test_the_secret_scan_locates_a_secret_wherever_it_is_named():
    wf = {
        "jobs": {
            "a": {"env": {"X": "${{ secrets.APPLE_CERT_PASSWORD }}"}, "steps": []},
            "b": {"steps": [{"name": "s", "run": "echo ${{ secrets.HOMEBREW_TAP_TOKEN }}"}]},
            "c": {
                "steps": [{"uses": "x/y@1", "with": {"k": "${{ secrets.APPLE_NOTARY_KEY_P8 }}"}}]
            },
        }
    }
    assert _secret_places(wf, "w.yml") == {
        ("w.yml", "a", "job env", "", "APPLE_CERT_PASSWORD"),
        ("w.yml", "b", "s", "run", "HOMEBREW_TAP_TOKEN"),
        ("w.yml", "c", "x/y", "k", "APPLE_NOTARY_KEY_P8"),
    }


def test_the_existence_checks_read_booleans_and_the_real_values_reach_only_step_env():
    jobs = _jobs()
    for job_name in ("macos-sign", "macos-sign-test"):
        check = _step(jobs[job_name], "The signing secrets are set")
        for value in check["env"].values():
            assert value.endswith(" != '' }}"), value
        assert "env" not in jobs[job_name]
    token_step = next(
        s for s in jobs["homebrew-tap"]["steps"] if "secrets.HOMEBREW_TAP_TOKEN }}" in str(s)
    )
    assert token_step["uses"].startswith("actions/checkout@")
    assert token_step["with"]["repository"] == "distantsoil/homebrew-kestrel"
    assert "env" not in token_step


def test_no_workflow_can_run_for_a_pull_request_target_and_release_runs_only_for_tags_or_by_hand():
    for name in _workflow_names():
        on = _workflow(name).get("on", _workflow(name).get(True))
        triggers = set(on) if isinstance(on, dict) else set(on if isinstance(on, list) else [on])
        assert "pull_request_target" not in triggers, name
        assert "workflow_run" not in triggers, name
    on = _workflow("release.yml").get("on", _workflow("release.yml").get(True))
    assert set(on) == {"push", "workflow_dispatch"}
    assert on["push"] == {"tags": ["v[0-9]*"]}
    # forks and other repositories stop at preflight, and every job needs preflight
    jobs = _jobs()
    assert jobs["preflight"]["if"] == "github.repository == 'distantsoil/Kestrelapp'"
    for job_name in ("macos", "macos-sign", "macos-sign-test", "homebrew-tap"):
        assert "preflight" in jobs[job_name]["needs"], job_name


def test_the_dry_run_job_names_nothing_that_belongs_to_a_release():
    test_job = str(_jobs()["macos-sign-test"])
    for name in (
        "vars.APPLE_DEVELOPER_ID_NAME",
        "vars.APPLE_NOTARY_KEY_ID",
        "vars.APPLE_NOTARY_ISSUER_ID",
        "secrets.APPLE_CERT_P12_BASE64",
        "secrets.APPLE_CERT_PASSWORD",
        "secrets.APPLE_NOTARY_KEY_P8",
        "sign_dmg.sh notarize",
        "notarytool",
    ):
        assert name not in test_job, name
    assert "Notarize and staple" not in _names(_jobs()["macos-sign-test"])
    assert _jobs()["macos-sign-test"]["environment"] == "release-test"
    assert _jobs()["macos-sign"]["environment"] == "release"
    assert _jobs()["homebrew-tap"]["environment"] == "release"


def test_the_dry_run_job_is_the_release_job_without_notarization():
    def normalized(job: dict) -> str:
        steps = [
            s
            for s in job["steps"]
            if s.get("name") not in ("Notarize and staple", "Signing marker")
        ]
        # the release job also checks for the notary key: drop that env entry and that line
        steps = yaml.safe_load(yaml.safe_dump(steps))
        for step in steps:
            step.get("env", {}).pop("HAS_NOTARY_KEY", None)
            step.get("env", {}).pop("HAS_TAP_TOKEN", None)
            if "run" in step:
                lines = step["run"].splitlines(keepends=True)
                kept = "".join(line for line in lines if "NOTARY_KEY" not in line)
                # and the release-only tap token check (a case block for stable tags)
                step["run"] = re.sub(
                    r'case "\$\{GITHUB_REF#refs/tags/\}" in\n.*?TAP_TOKEN.*?\nesac\n',
                    "",
                    kept,
                    flags=re.S,
                )
        # one long line per value, so a longer name cannot wrap the text differently
        text = yaml.safe_dump(steps, sort_keys=True, width=1_000_000)
        for a, b in (
            ("APPLE_TEST_CERT_", "CERT_"),
            ("APPLE_CERT_", "CERT_"),
            ("APPLE_TEST_SIGNING_NAME", "NAME"),
            ("APPLE_DEVELOPER_ID_NAME", "NAME"),
            ("release-test environment", "ENV"),
            ("release environment", "ENV"),
            ("verify test", "verify MODE"),
            ("verify production", "verify MODE"),
        ):
            text = text.replace(a, b)
        assert "NOTARY" not in text
        return text

    jobs = _jobs()
    assert normalized(jobs["macos-sign"]) == normalized(jobs["macos-sign-test"])


# The commands a job holding a secret may run, as an allowlist: anything else (a package manager,
# a download, an interpreter, the app) fails the test however it is spelled.
_SHELL_WORDS = {"[", "if", "then", "else", "elif", "fi", "case", "esac", "exit", "export", "trap"}
_ALLOWED_COMMANDS = {
    "macos-sign": _SHELL_WORDS
    | {"echo", "printf", "umask", "mkdir", "rm", "cat", "sed", "base64", "openssl", "security"}
    | {"packaging/macos/sign_dmg.sh"},
    "homebrew-tap": _SHELL_WORDS | {"python3", "mkdir", "cp", "cmp", "cd", "git", "echo"},
}
_ALLOWED_COMMANDS["macos-sign-test"] = _ALLOWED_COMMANDS["macos-sign"]
_ALLOWED_COMMANDS["macos-sign-check"] = _SHELL_WORDS | {"echo"}


_BOUNDARIES = {";", ";;", "&&", "||", "|", "(", ")", "then", "else", "elif", "do", "!"}


def _substitutions(token: str) -> list[str]:
    """The text of every `$( ... )` in a token, matched by parenthesis depth."""
    found, start = [], token.find("$(")
    while start != -1:
        depth, i = 0, start + 1
        while i < len(token):
            depth += {"(": 1, ")": -1}.get(token[i], 0)
            if depth == 0:
                break
            i += 1
        found.append(token[start + 2 : i])
        start = token.find("$(", i)
    return found


def _commands(script: str) -> set[str]:
    """The first word of every command in a shell script: each line, each part of a pipe or a
    list, and each command substitution (also inside quotes), with leading variable assignments
    removed. Quoted text is one word, so a message never reads as a command."""
    import shlex

    found: set[str] = set()
    for line in script.replace("\\\n", " ").splitlines():
        # each $( ... ) is read on its own, then stands as one plain word in its line
        for inner in _substitutions(line):
            found |= _commands(inner)
            line = line.replace(f"$({inner})", "SUBST", 1)
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
        first = True
        for index, token in enumerate(tokens):
            if token in _BOUNDARIES:
                first = True
                continue
            if first and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                continue
            # a case label (`pattern)` at the start of a line) is not a command
            if first and index + 1 < len(tokens) and tokens[index + 1] == ")":
                continue
            if first:
                found.add(token.strip("\"'"))
                first = False
    return found


def test_the_command_reader_sees_every_kind_of_command():
    script = (
        'A="$(openssl rand -hex 2)"\nprintf x | base64 --decode > f\n'
        '[ -n "$A" ] || B="$(curl -s x)"\nif true; then uv pip install y; fi\n'
        'case "$1" in a) exit 0 ;; esac\nX=1 brew install z\n'
    )
    found = _commands(script)
    assert {"openssl", "printf", "base64", "[", "curl", "uv", "brew", "case", "exit"} <= found
    # a quoted message is one word, never a command
    assert _commands('echo "::error::stop; curl || wget | npm"') == {"echo"}
    # a case label is not a command, but the command after it is
    labels = 'case "$X" in\n  *-*) ;;\n  *) curl x ;;\nesac\n'
    assert _commands(labels) == {"case", "curl", "esac"}


@pytest.mark.parametrize("job_name", ["macos-sign", "macos-sign-test", "homebrew-tap"])
def test_jobs_that_hold_a_secret_install_nothing_and_use_only_pinned_github_actions(job_name):
    job = _jobs()[job_name]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    commands = set().union(*(_commands(s.get("run", "")) for s in job["steps"]))
    assert commands <= _ALLOWED_COMMANDS[job_name], commands - _ALLOWED_COMMANDS[job_name]
    actions = {str(s["uses"]).split("@")[0] for s in job["steps"] if "uses" in s}
    assert actions <= {"actions/checkout", "actions/download-artifact", "actions/upload-artifact"}
    assert job["permissions"] == {"contents": "read"}
    # the job's own source checkout never keeps a credential on disk
    first = job["steps"][0]
    assert first["uses"].startswith("actions/checkout@")
    assert first["with"] == {"persist-credentials": False}
    unpinned = [
        s["uses"]
        for s in job["steps"]
        if "uses" in s and not re.fullmatch(r"[^@]+@[0-9a-f]{40}", s["uses"])
    ]
    assert unpinned == []
    # the only interpreter anywhere is the tap job's one standard-library script
    tap_line = (
        'python3 scripts/homebrew_cask.py --latest feed/latest.json --tag "$GITHUB_REF_NAME" \\'
    )
    expected = [tap_line] if job_name == "homebrew-tap" else []
    assert [line for line in runs.splitlines() if "python" in line] == expected


def test_nothing_runs_the_app_while_a_certificate_or_key_is_on_the_runner():
    for job_name in ("macos-sign", "macos-sign-test"):
        names = _names(_jobs()[job_name])
        remove = names.index("Remove the keychain and the notary key")
        assert (
            names.index("Import the signing certificate")
            < names.index("Sign the app and the disk image")
            < remove
        )
        assert ("Notarize and staple" in names) is (job_name == "macos-sign")
        assert (
            remove
            < names.index("Smoke test the signed app")
            < names.index("Verify the signature, ticket and Gatekeeper")
        )
        assert names.index("Verify the signature, ticket and Gatekeeper") < names.index(
            "Signing marker"
        )
        assert (
            _step(_jobs()[job_name], "Remove the keychain and the notary key")["if"] == "always()"
        )
    release = _names(_jobs()["macos-sign"])
    assert (
        release.index("Sign the app and the disk image")
        < release.index("Notarize and staple")
        < release.index("Remove the keychain and the notary key")
    )
    # the release job verifies as a release, the dry run as a test that macOS must refuse
    for job_name, mode in (("macos-sign", "production"), ("macos-sign-test", "test")):
        verify = _step(_jobs()[job_name], "Verify the signature, ticket and Gatekeeper")
        expected = f"packaging/macos/sign_dmg.sh verify {mode} dist/Kestrel-*.dmg"
        assert verify["run"].strip() == expected


def test_only_the_release_job_writes_the_signed_marker_and_the_feed_reads_it_that_way():
    from build_latest_json import parse_signing

    jobs = _jobs()
    lines = {}
    for job_name in ("macos", "macos-sign", "macos-sign-test"):
        run = _step(jobs[job_name], "Signing marker")["run"]
        lines[job_name] = re.search(r'echo "([^"]*)" > dist/SIGNING-macos.txt', run)[1]
    assert lines["macos-sign"] == "macos: signed"
    assert parse_signing(lines["macos-sign"]) == {"macos": True}
    assert parse_signing(lines["macos"]) == {"macos": False}
    assert parse_signing(lines["macos-sign-test"]) == {"macos": False}
    for job_name in ("macos", "linux", "windows", "windows-sign", "windows-sign-test", "release"):
        assert "macos: signed" not in str(jobs[job_name]), job_name


# --------------------------------------------------------------- step scripts with stand-in tools


def _tool(folder: Path, name: str, body: str) -> None:
    path = folder / name
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "HOME": str(tmp_path / "home"),
        "CALLS": str(tmp_path / "calls.log"),
        **extra,
    }


def _run_step(script: str, env: dict[str, str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


SECRET_P12 = "U2FtcGxlIGNlcnRpZmljYXRlIGJ5dGVz"  # invented: "Sample certificate bytes"
SECRET_PASSWORD = "invented-p12-password-91"
SECRET_KEY = "-----BEGIN PRIVATE KEY-----\ninvented-notary-key-line\n-----END PRIVATE KEY-----"


def _security_tool(tmp_path: Path, import_rc: int = 0) -> None:
    (tmp_path / "bin").mkdir(exist_ok=True)
    _tool(
        tmp_path / "bin",
        "security",
        f'echo "security $1" >> "$CALLS"\n'
        f'case "$1" in create-keychain) touch "${{@: -1}}" ;; import) exit {import_rc} ;; esac\n',
    )


@posix_tools
@pytest.mark.parametrize("import_rc", [0, 1])
def test_the_certificate_file_is_removed_and_nothing_secret_is_printed(tmp_path, import_rc):
    _security_tool(tmp_path, import_rc)
    (tmp_path / "runner").mkdir()
    step = _step(_jobs()["macos-sign"], "Import the signing certificate")
    env = _env(
        tmp_path,
        CERT_P12_BASE64=SECRET_P12,
        CERT_PASSWORD=SECRET_PASSWORD,
        GITHUB_ENV=str(tmp_path / "github_env"),
    )
    done = _run_step(step["run"], env)
    assert (done.returncode == 0) is (import_rc == 0), done.stderr
    assert not (tmp_path / "runner" / "kestrel-cert.p12").exists()
    printed = done.stdout + done.stderr
    for secret in (SECRET_P12, SECRET_PASSWORD, "Sample certificate bytes"):
        assert secret not in printed
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").split()
    assert "create-keychain" in calls and "import" in calls
    # the keychain path is recorded as soon as it exists, so cleanup finds it even after a failure
    assert (tmp_path / "github_env").read_text(encoding="utf-8").startswith("KEYCHAIN=")


@posix_tools
def test_the_keychain_and_key_are_removed_even_after_a_failed_run(tmp_path):
    _security_tool(tmp_path)
    runner = tmp_path / "runner"
    (runner / "notary").mkdir(parents=True)
    (runner / "notary" / "AuthKey.p8").write_text(SECRET_KEY, encoding="utf-8")
    keychain = runner / "kestrel-signing.keychain-db"
    keychain.write_text("x", encoding="utf-8")
    step = _step(_jobs()["macos-sign"], "Remove the keychain and the notary key")
    done = _run_step(step["run"], _env(tmp_path, KEYCHAIN=str(keychain)))
    assert done.returncode == 0, done.stderr
    assert not (runner / "notary").exists() and not keychain.exists()
    assert "security delete-keychain" in (tmp_path / "calls.log").read_text(encoding="utf-8")


def _fake_apple_tools(tmp_path: Path, **behavior: object) -> None:
    """hdiutil, codesign, xcrun, spctl and file stand-ins; every call is logged to $CALLS."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    _tool(
        b,
        "hdiutil",
        f'LIMITS="{behavior.get("limits", "1000 2000 9000")}"\n'
        'echo "hdiutil $*" >> "$CALLS"\n'
        'case "$1" in\n'
        '  attach) while [ $# -gt 0 ]; do [ "$1" = -mountpoint ] && M="$2"; shift; done\n'
        '    mkdir -p "$M/Kestrel.app/Contents/MacOS" "$M/Kestrel.app/Contents/Frameworks"\n'
        '    APP_EXE="$M/Kestrel.app/Contents/MacOS/Kestrel"\n'
        '    printf "#!/usr/bin/env bash\\necho Kestrel 0.0.0\\n" > "$APP_EXE"\n'
        '    chmod +x "$M/Kestrel.app/Contents/MacOS/Kestrel"\n'
        '    touch "$M/Kestrel.app/Contents/Frameworks/libsample.dylib"\n'
        '    F="$M/Kestrel.app/Contents/Frameworks/Sample.framework"\n'
        '    mkdir -p "$F/Versions/A/Frameworks/Inner.framework"\n'
        '    touch "$F/Versions/A/Sample"; chmod +x "$F/Versions/A/Sample" ;;\n'
        '  convert) while [ $# -gt 0 ]; do [ "$1" = -o ] && O="$2"; shift; done; touch "$O" ;;\n'
        '  resize) if [ "$2" = -limits ]; then echo "$LIMITS"; fi ;;\n'
        "esac\n",
    )
    authority = behavior.get("authority", DEV_NAME)
    team = behavior.get("team", TEAM)
    lines = (
        "" if behavior.get("no_identity") else f"Authority={authority}\\nTeamIdentifier={team}\\n"
    )
    _tool(
        b,
        "codesign",
        'echo "codesign $*" >> "$CALLS"\n'
        f'case "$1" in -dvv) printf "Identifier=sample\\n{lines}" >&2 ;; '
        f"--verify) exit {behavior.get('verify_rc', 0)} ;; esac\n",
    )
    _tool(
        b,
        "xcrun",
        'echo "xcrun $*" >> "$CALLS"\n'
        f'case "$1 $2" in "stapler validate") exit {behavior.get("staple_rc", 0)} ;; '
        f'"stapler staple") exit {behavior.get("staple_rc", 0)} ;; '
        f'"notarytool submit") echo "  id: 0a1b2c3d-0000-1111-2222-333344445555"; '
        f"exit {behavior.get('submit_rc', 0)} ;; esac\n",
    )
    _tool(
        b,
        "spctl",
        'echo "spctl $*" >> "$CALLS"\n'
        f'case "$*" in --status) echo "{behavior.get("status", "assessments enabled")}" ;; '
        f"*install*) exit {behavior.get('install_rc', 0)} ;; "
        f"*exec*) exit {behavior.get('exec_rc', 0)} ;; esac\n",
    )
    _tool(
        b,
        "file",
        'case "$2" in *.dylib|*.so|*/MacOS/*|*/Versions/A/Sample) echo "Mach-O 64-bit" ;;\n'
        "  *) echo data ;;\n"
        "esac\n",
    )


def _sign_dmg(tmp_path: Path, *args: str, **env: str) -> tuple[subprocess.CompletedProcess, str]:
    base = _env(tmp_path, SIGN_NAME=DEV_NAME, TEAM_ID=TEAM)
    base.update(env)
    done = subprocess.run(  # noqa: S603
        [BASH, str(SIGN_DMG), *args],
        env=base,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    calls = tmp_path / "calls.log"
    return done, calls.read_text(encoding="utf-8") if calls.exists() else ""


@pytest.fixture
def dmg(tmp_path: Path) -> Path:
    path = tmp_path / "Kestrel-0.0.0.dmg"
    path.write_bytes(b"invented disk image")
    return path


@posix_tools
def test_a_release_passes_verification_only_after_every_check_ran(tmp_path, dmg):
    _fake_apple_tools(tmp_path)
    done, calls = _sign_dmg(tmp_path, "verify", "production", str(dmg))
    assert done.returncode == 0, done.stdout + done.stderr
    for call in (
        f"codesign -dvv {dmg}",
        "codesign --verify --deep --strict --verbose=2",
        f"xcrun stapler validate {dmg}",
        f"spctl -a -vvv -t install {dmg}",
        "spctl -a -vv -t exec",
    ):
        assert call in calls, call
    assert "verified (production)" in done.stdout


@posix_tools
@pytest.mark.parametrize(
    ("behavior", "reason"),
    [
        ({"authority": "Developer ID Application: Someone Else (ZZZZZ99999)"}, "expected"),
        ({"team": "ZZZZZ99999"}, "expected"),
        ({"no_identity": True}, "expected"),
        ({"verify_rc": 1}, ""),
        ({"staple_rc": 1}, "ticket"),
        ({"install_rc": 3}, "refused the disk image"),
        ({"exec_rc": 3}, "refused Kestrel.app"),
        ({"status": "assessments disabled"}, "turned off"),
    ],
)
def test_a_release_fails_verification_on_any_wrong_answer_and_says_why(
    tmp_path, dmg, behavior, reason
):
    _fake_apple_tools(tmp_path, **behavior)
    done, _ = _sign_dmg(tmp_path, "verify", "production", str(dmg))
    assert done.returncode != 0
    assert reason in done.stdout + done.stderr
    assert "verified" not in done.stdout


@posix_tools
@pytest.mark.parametrize(("exec_rc", "passes"), [(3, True), (1, True), (0, False)])
def test_a_test_signature_must_be_refused_by_gatekeeper(tmp_path, dmg, exec_rc, passes):
    _fake_apple_tools(tmp_path, authority=TEST_NAME, exec_rc=exec_rc)
    done, calls = _sign_dmg(tmp_path, "verify", "test", str(dmg), SIGN_NAME=TEST_NAME)
    assert (done.returncode == 0) is passes, done.stdout + done.stderr
    assert "stapler" not in calls and "-t install" not in calls
    assert ("publicly trusted" in done.stderr) is (not passes)


@posix_tools
def test_signing_goes_inside_out_with_the_hardened_runtime_then_signs_the_image(tmp_path):
    _fake_apple_tools(tmp_path)
    layout = tmp_path / "Kestrel-layout.dmg"
    layout.write_bytes(b"invented layout image")
    out = tmp_path / "Kestrel-0.0.0.dmg"
    done, calls = _sign_dmg(tmp_path, "sign", str(layout), str(out), KEYCHAIN="/k/kc")
    assert done.returncode == 0, done.stdout + done.stderr
    signs = [line for line in calls.splitlines() if line.startswith("codesign --force")]
    names = [line.split()[-1].rsplit("/", 1)[-1] for line in signs]
    # the loose Mach-O files first (in any order), then frameworks deepest first, then the
    # main executable, the app and the disk image
    assert sorted(names[:2]) == ["Sample", "libsample.dylib"]
    assert names[2:] == [
        "Inner.framework",
        "Sample.framework",
        "Kestrel",
        "Kestrel.app",
        "Kestrel-0.0.0.dmg",
    ]
    for line in signs:
        assert "--options runtime --timestamp" in line and f"--sign {DEV_NAME}" in line
        assert "--keychain /k/kc" in line
    with_entitlements = [
        n for n, line in zip(names, signs, strict=True) if "entitlements.plist" in line
    ]
    assert sorted(with_entitlements) == ["Kestrel", "Kestrel.app", "Sample", "libsample.dylib"]
    order = calls.splitlines()
    convert = next(i for i, c in enumerate(order) if c.startswith("hdiutil convert"))
    assert convert < order.index(signs[-1]) and "-format UDZO" in order[convert]


@posix_tools
def test_the_layout_image_grows_before_it_is_signed_in_place(tmp_path):
    """dmgbuild shrinks its image to the minimum, so signing would run out of room."""
    _fake_apple_tools(tmp_path, limits="1000 2000 9000")
    layout = tmp_path / "Kestrel-layout.dmg"
    layout.write_bytes(b"invented layout image")
    done, calls = _sign_dmg(tmp_path, "sign", str(layout), str(tmp_path / "out.dmg"))
    assert done.returncode == 0, done.stdout + done.stderr
    order = calls.splitlines()
    grow = order.index(f"hdiutil resize -sectors {2000 + 819200} {layout}")
    attach = next(i for i, c in enumerate(order) if c.startswith("hdiutil attach"))
    assert order.index(f"hdiutil resize -limits {layout}") < grow < attach


@posix_tools
def test_an_unreadable_image_size_stops_signing_and_shows_what_hdiutil_said(tmp_path):
    _fake_apple_tools(tmp_path, limits="sizes unknown")
    layout = tmp_path / "Kestrel-layout.dmg"
    layout.write_bytes(b"invented layout image")
    done, calls = _sign_dmg(tmp_path, "sign", str(layout), str(tmp_path / "out.dmg"))
    assert done.returncode != 0 and "sizes unknown" in done.stdout
    assert "hdiutil attach" not in calls and "codesign" not in calls


def _build_tree(
    tmp_path: Path, *, dmgbuild_rc: int = 0, tiffutil_rc: int = 0, ds_store: str = ""
) -> dict:
    """A copy of the packaging files in a scratch tree, an unsigned Kestrel.app, and stand-ins for
    uname, tiffutil, hdiutil, diskutil and dmgbuild. Returns the environment for build_dmg.sh."""
    tree = tmp_path / "tree"
    (tree / "packaging" / "macos").mkdir(parents=True)
    shutil.copy(ROOT / "packaging" / "build_dmg.sh", tree / "packaging" / "build_dmg.sh")
    for name in (
        "dmg_settings.py",
        "dmg_privacy.py",
        "dmg-background.png",
        "dmg-background@2x.png",
        "sign_dmg.sh",
    ):
        shutil.copy(MACOS / name, tree / "packaging" / "macos" / name)
    (tree / "packaging" / "kestrel.icns").write_bytes(b"icon")
    (tree / "kestrel").mkdir()
    (tree / "kestrel" / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    exe = tree / "dist" / "Kestrel.app" / "Contents" / "MacOS" / "Kestrel"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    b = tmp_path / "bin"
    b.mkdir()
    _tool(b, "uname", "echo Darwin\n")
    _tool(b, "tiffutil", f'echo "tiffutil $*" >> "$CALLS"; exit {tiffutil_rc}\n')
    _tool(b, "diskutil", 'echo "diskutil $*" >> "$CALLS"; exit 1\n')
    # hdiutil: attach shows a volume whose hidden files carry $DS_STORE_TEXT
    _tool(
        b,
        "hdiutil",
        'echo "hdiutil $*" >> "$CALLS"\n'
        'case "$1" in\n'
        '  attach) while [ $# -gt 0 ]; do [ "$1" = -mountpoint ] && M="$2"; shift; done\n'
        '    mkdir -p "$M"; printf "Bud1 %s" "$DS_STORE_TEXT" > "$M/.DS_Store"\n'
        '    printf "%s" "$BACKGROUND_TEXT" > "$M/.background.tiff" ;;\n'
        '  create) touch "${@: -1}" ;;\n'
        "esac\n",
    )
    _tool(
        b, "dmgbuild", f'echo "dmgbuild $*" >> "$CALLS"; touch "${{@: -1}}"; exit {dmgbuild_rc}\n'
    )
    env = _env(tmp_path, PYTHON=sys.executable, DMGBUILD=str(b / "dmgbuild"))
    env["DS_STORE_TEXT"] = ds_store
    env["BACKGROUND_TEXT"] = ""
    (tmp_path / "home").mkdir()
    env["TREE"] = str(tree)
    return env


def _run_build(env: dict, *args: str) -> tuple[subprocess.CompletedProcess, str]:
    done = subprocess.run(  # noqa: S603
        [BASH, str(Path(env["TREE"]) / "packaging" / "build_dmg.sh"), "--skip-build", *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done, Path(env["CALLS"]).read_text(encoding="utf-8")


def _dmgbuild_defines(calls: str) -> dict[str, str]:
    line = next(c for c in calls.splitlines() if c.startswith("dmgbuild "))
    return dict(re.findall(r"-D (\w+)=(\S*)", line))


@posix_tools
def test_the_build_lays_out_the_window_with_the_retina_background(tmp_path):
    env = _build_tree(tmp_path)
    done, calls = _run_build(env)
    assert done.returncode == 0, done.stdout + done.stderr
    defines = _dmgbuild_defines(calls)
    assert defines["format"] == "UDZO" and defines["size"] == ""
    assert defines["background"].endswith("/build/dmg-layout/background.tiff")
    assert defines["volume_icon"].endswith("/packaging/kestrel.icns")
    assert "dmg_settings.py" in calls and "Kestrel 1.2.3" in calls
    assert "hdiutil create" not in calls and "diskutil image create from" not in calls
    dmgbuild = next(c for c in calls.splitlines() if c.startswith("dmgbuild "))
    assert dmgbuild.endswith(f"{env['TREE']}/dist/Kestrel-1.2.3.dmg")
    assert "UNSIGNED" in done.stdout


@posix_tools
def test_the_read_write_build_leaves_room_and_names_the_layout_image(tmp_path):
    env = _build_tree(tmp_path)
    done, calls = _run_build(env, "--rw")
    assert done.returncode == 0, done.stdout + done.stderr
    defines = _dmgbuild_defines(calls)
    assert defines["format"] == "UDRW" and re.fullmatch(r"[0-9]+m", defines["size"])
    assert int(defines["size"][:-1]) >= 400
    assert "Kestrel-layout.dmg" in calls and "UNSIGNED" not in done.stdout


@posix_tools
def test_a_failed_retina_merge_uses_a_1x_copy_with_no_2x_beside_it(tmp_path):
    env = _build_tree(tmp_path, tiffutil_rc=1)
    done, calls = _run_build(env)
    assert done.returncode == 0, done.stdout + done.stderr
    background = Path(_dmgbuild_defines(calls)["background"])
    assert background.name == "background.png" and background.is_file()
    assert not (background.parent / "background@2x.png").exists()


@posix_tools
@pytest.mark.parametrize("where", ["dmgbuild fails", "path in .DS_Store", "home in background"])
def test_any_window_failure_or_leak_makes_the_plain_image(tmp_path, where):
    env = _build_tree(tmp_path, dmgbuild_rc=1 if where == "dmgbuild fails" else 0)
    if where == "path in .DS_Store":
        env["DS_STORE_TEXT"] = env["TREE"] + "/dist/Kestrel.app"
    if where == "home in background":
        env["BACKGROUND_TEXT"] = env["HOME"] + "/Desktop"
    done, calls = _run_build(env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "making the plain image" in done.stdout
    plain = [c for c in calls.splitlines() if c.startswith("hdiutil create")]
    assert len(plain) == 1 and "-format UDZO" in plain[0]
    # a leak is named by kind and file, never by the value found
    leaked = where != "dmgbuild fails"
    assert ("contains forbidden value" in done.stdout) is leaked
    named = [line for line in done.stdout.splitlines() if line.startswith("dmg_privacy:")]
    assert len(named) == (1 if leaked else 0), done.stdout
    assert [line for line in named if env["TREE"] in line or env["HOME"] in line] == []


@posix_tools
@pytest.mark.parametrize("submit_rc", [0, 1])
def test_the_notary_key_file_is_removed_and_never_printed(tmp_path, submit_rc):
    _fake_apple_tools(tmp_path, submit_rc=submit_rc)
    (tmp_path / "runner").mkdir()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "Kestrel-0.0.0.dmg").write_bytes(b"invented")
    packaging = tmp_path / "packaging" / "macos"
    packaging.mkdir(parents=True)
    shutil.copy(SIGN_DMG, packaging / "sign_dmg.sh")
    step = _step(_jobs()["macos-sign"], "Notarize and staple")
    env = _env(tmp_path, NOTARY_KEY_P8=SECRET_KEY, NOTARY_KEY_ID="KEY1234567", NOTARY_ISSUER="iss")
    # the step is run through bash so the test does not depend on the file's executable bit
    script = step["run"].replace(
        "packaging/macos/sign_dmg.sh ", "bash packaging/macos/sign_dmg.sh "
    )
    assert script != step["run"]
    done = _run_step(script, env, cwd=tmp_path)
    assert (done.returncode == 0) is (submit_rc == 0), done.stdout + done.stderr
    assert not (tmp_path / "runner" / "notary").exists()
    assert "invented-notary-key-line" not in done.stdout + done.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "notarytool submit dist/Kestrel-0.0.0.dmg --key" in calls and "--wait" in calls
    # on failure Apple's log is fetched for the submission notarytool named, and nothing is stapled
    failed = submit_rc != 0
    assert ("notarytool log 0a1b2c3d-0000-1111-2222-333344445555" in calls) is failed
    assert ("stapler staple dist/Kestrel-0.0.0.dmg" in calls) is (not failed)


def test_no_macos_step_or_script_traces_commands():
    for job_name in ("macos", "macos-sign", "macos-sign-test", "homebrew-tap"):
        for step in _jobs()[job_name]["steps"]:
            assert "set -x" not in step.get("run", "") and "xtrace" not in step.get("run", "")
    for path in (SIGN_DMG, ROOT / "packaging" / "build_dmg.sh"):
        code = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "set -x" not in code and "xtrace" not in code, path


# ------------------------------------------------------------------------------- the cask


def _feed(version: str = "1.2.3", **mac: object) -> dict:
    name = f"Kestrel-{version}.dmg"
    entry = {
        "file": name,
        "url": f"https://github.com/sample-owner/sample-app/releases/download/v{version}/{name}",
        "sha256": "a" * 64,
        "signed": True,
    }
    entry.update(mac)
    return {"version": version, "downloads": {"macos": entry}}


def _render(feed: dict, tag: str = "v1.2.3") -> str | None:
    return homebrew_cask.render(feed, tag=tag, repo="sample-owner/sample-app")


def test_the_cask_renders_from_a_signed_feed():
    cask = _render(_feed())
    assert cask is not None
    assert 'cask "kestrel" do' in cask and cask.rstrip().endswith("end")
    assert 'version "1.2.3"' in cask and f'sha256 "{"a" * 64}"' in cask
    assert (
        'url "https://github.com/sample-owner/sample-app/releases/download/v1.2.3/Kestrel-1.2.3.dmg"'
        in cask
    )
    assert 'app "Kestrel.app"' in cask and 'homepage "https://kestrelapp.co"' in cask
    assert "depends_on macos: :ventura" in cask and "depends_on arch: :arm64" in cask
    assert "@" not in cask.replace("@VERSION@", "")  # every placeholder was filled


def test_the_cask_never_lists_the_users_data_folder_for_zap():
    cask = _render(_feed()) or ""
    zap = cask.split("zap trash:", 1)[1]
    paths = re.findall(r'"([^"]+)"', zap)
    assert paths and all(
        p.startswith(("~/Library/Caches/", "~/Library/HTTPStorages/")) for p in paths
    )
    assert "Application Support" not in zap


def test_the_cask_minimum_macos_matches_the_bundle():
    spec = (ROOT / "packaging" / "kestrel.spec").read_text(encoding="utf-8")
    assert '"LSMinimumSystemVersion": "13.0"' in spec  # macOS 13 is Ventura
    assert "target_arch" not in spec  # built natively on the Apple silicon runner


@pytest.mark.parametrize(
    ("feed", "tag", "reason"),
    [
        (_feed("1.2.4"), "v1.2.3", "not the tag"),
        (
            _feed(url="https://github.com/other/app/releases/download/v1.2.3/Kestrel-1.2.3.dmg"),
            "v1.2.3",
            "URL",
        ),
        (_feed(url="https://example.invalid/Kestrel-1.2.3.dmg"), "v1.2.3", "URL"),
        (_feed(file="Kestrel-1.2.3-evil.dmg"), "v1.2.3", "file"),
        (_feed(sha256="A" * 64), "v1.2.3", "SHA-256"),
        (_feed(sha256="a" * 63), "v1.2.3", "SHA-256"),
        (_feed(sha256='a" ; system "x'), "v1.2.3", "SHA-256"),
        (_feed(sha256=None), "v1.2.3", "SHA-256"),
        (_feed("1.2.3-rc.1"), "v1.2.3-rc.1", "stable version tag"),
        (_feed(), "1.2.3", "stable version tag"),
        ({"version": "1.2.3", "downloads": {}}, "v1.2.3", "no macOS download"),
        ([], "v1.2.3", "not an object"),
    ],
)
def test_the_cask_refuses_a_value_it_cannot_vouch_for(feed, tag, reason):
    with pytest.raises(homebrew_cask.CaskError, match=reason):
        _render(feed, tag)


def test_the_repository_name_is_checked_before_it_reaches_the_cask():
    for repo in ("owner", "owner/name/extra", "../name", 'owner/na"me', ""):
        with pytest.raises(homebrew_cask.CaskError, match="owner/name"):
            homebrew_cask.render(_feed(), tag="v1.2.3", repo=repo)


@pytest.mark.parametrize("signed", [False, None, "true", 1])
def test_an_unsigned_mac_build_is_skipped_not_published(signed):
    assert _render(_feed(signed=signed)) is None


def _main(tmp_path: Path, feed: object, *, license_text: bytes | None = b"Sample terms\r\nline\n"):
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps(feed), encoding="utf-8")
    lic = tmp_path / "LICENSE.md"
    if license_text is not None:
        lic.write_bytes(license_text)
    out = tmp_path / "out"
    output = tmp_path / "github_output"
    os.environ["GITHUB_OUTPUT"] = str(output)
    try:
        code = homebrew_cask.main(
            [
                "--latest",
                str(latest),
                "--tag",
                "v1.2.3",
                "--repo",
                "sample-owner/sample-app",
                "--license",
                str(lic),
                "--out",
                str(out),
            ]
        )
    finally:
        del os.environ["GITHUB_OUTPUT"]
    return code, out, output.read_text(encoding="utf-8").splitlines()


def test_main_writes_the_cask_and_a_byte_identical_license(tmp_path, capsys):
    code, out, outputs = _main(tmp_path, _feed())
    assert code == 0
    assert (out / "LICENSE").read_bytes() == b"Sample terms\r\nline\n"
    assert (out / "Casks" / "kestrel.rb").read_text(encoding="utf-8") == _render(_feed())
    assert outputs[-1] == "publish=true"


def test_main_fails_without_a_license_and_publishes_nothing(tmp_path, capsys):
    code, out, outputs = _main(tmp_path, _feed(), license_text=None)
    assert code == 1 and not out.exists()
    assert outputs == ["publish=false"]
    assert "license" in capsys.readouterr().err


def test_main_skips_an_unsigned_build_and_says_so(tmp_path, capsys):
    code, out, outputs = _main(tmp_path, _feed(signed=False))
    assert code == 0 and not out.exists()
    assert outputs == ["publish=false"]
    assert homebrew_cask.SKIPPED in capsys.readouterr().out


def test_main_refuses_a_bad_feed_and_publishes_nothing(tmp_path, capsys):
    code, out, outputs = _main(tmp_path, _feed(sha256="bad"))
    assert code == 1 and not out.exists() and outputs == ["publish=false"]


def test_the_tap_job_copies_the_license_it_checked_and_pushes_only_when_told_to():
    tap = _jobs()["homebrew-tap"]
    render = _step(tap, "Render the cask")
    assert render["id"] == "cask" and "--license LICENSE.md" in render["run"]
    push = _step(tap, "Commit and push")
    assert push["if"] == "steps.cask.outputs.publish == 'true'"
    assert "cmp LICENSE.md tap/LICENSE" in push["run"]
    checkout = next(s for s in tap["steps"] if "secrets.HOMEBREW_TAP_TOKEN }}" in str(s))
    assert checkout["if"] == "steps.cask.outputs.publish == 'true'"
    # the commit message names only the version
    assert 'git commit -m "kestrel ${GITHUB_REF_NAME#v}"' in push["run"]
    assert (ROOT / "LICENSE.md").is_file()


# ------------------------------------------------------------------------------- the dmg window


def _settings(**defines: str) -> dict:
    code = (MACOS / "dmg_settings.py").read_text(encoding="utf-8")
    scope: dict = {"defines": {"app": "/build/dist/Kestrel.app", **defines}}
    exec(compile(code, "dmg_settings.py", "exec"), scope)  # noqa: S102
    return scope


def test_the_window_layout_places_both_icons_inside_a_plain_window():
    s = _settings(background="/b/background.tiff", volume_icon="/b/kestrel.icns")
    (_, _), (width, height) = s["window_rect"]
    assert (width, height) == s["WINDOW_SIZE"] == (660, 400)
    assert set(s["icon_locations"]) == {"Kestrel.app", "Applications"}
    half = s["icon_size"] // 2
    for x, y in s["icon_locations"].values():
        assert half <= x <= width - half and half <= y <= height - half
    assert s["symlinks"] == {"Applications": "/Applications"}
    assert s["files"] == ["/build/dist/Kestrel.app"]
    for flag in (
        "show_toolbar",
        "show_sidebar",
        "show_status_bar",
        "show_pathbar",
        "show_tab_view",
    ):
        assert s[flag] is False, flag
    assert s["background"] == "/b/background.tiff" and s["icon"] == "/b/kestrel.icns"
    assert s["format"] == "UDZO" and s["size"] is None


def test_the_layout_matches_the_background_artwork():
    svg = (MACOS / "dmg-background.svg").read_text(encoding="utf-8")
    s = _settings()
    assert 'viewBox="0 0 660 400"' in svg
    (ax, ay), (bx, by) = s["APP_POSITION"], s["APPLICATIONS_POSITION"]
    assert f"({ax}, {ay})" in svg and f"({bx}, {by})" in svg
    arrow = re.search(r'd="M(\d+) (\d+) H(\d+)"', svg)
    assert ay == by == int(arrow[2]) and ax < int(arrow[1]) < int(arrow[3]) < bx


def test_the_settings_refuse_a_format_other_than_the_two_the_build_uses():
    assert _settings(format="UDRW")["format"] == "UDRW"
    with pytest.raises(ValueError, match="UDZO or UDRW"):
        _settings(format="UDBZ")


def _background_tools():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_dmg_background", ROOT / "scripts" / "make_dmg_background.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _palette() -> tuple[dict[str, tuple[int, int, int]], tuple[int, int, int, int]]:
    """The token colors the SVG names, as RGB, and the mark's box (the only drawn image)."""
    tools = _background_tools()
    tokens = tools.light_tokens(
        (ROOT / "kestrel" / "web" / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
    )
    svg = (MACOS / "dmg-background.svg").read_text(encoding="utf-8")
    names = set(re.findall(r"var\(--([a-z0-9-]+)\)", svg))
    colors = {n: tuple(int(tokens[n][i : i + 2], 16) for i in (1, 3, 5)) for n in names}
    image = re.search(r'<image x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', svg)
    return colors, tuple(int(v) for v in image.groups())


def _off_palette(img, colors, box, scale: int, tolerance: float = 4.0) -> list[tuple]:
    """Every pixel color outside the mark's box that is not a palette color or a blend of two
    (anti-aliased edges are blends). Returns the offending colors."""
    pal = list(colors.values())
    pairs = [(a, b) for i, a in enumerate(pal) for b in pal[i:]]
    x0, y0, w, h = (v * scale for v in box)
    seen: dict[tuple, bool] = {}
    bad = []
    width, height = img.size
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            if x0 <= x < x0 + w and y0 <= y < y0 + h:
                continue
            c = pixels[x, y]
            if c not in seen:
                seen[c] = any(_on_segment(c, a, b, tolerance) for a, b in pairs)
            if not seen[c]:
                bad.append(c)
    return bad


def _on_segment(c, a, b, tolerance) -> bool:
    d = [bb - aa for aa, bb in zip(a, b, strict=True)]
    length = sum(v * v for v in d)
    t = (
        0.0
        if length == 0
        else max(
            0.0, min(1.0, sum((cc - aa) * v for cc, aa, v in zip(c, a, d, strict=True)) / length)
        )
    )
    return all(abs(cc - (aa + t * v)) <= tolerance for cc, aa, v in zip(c, a, d, strict=True))


def test_every_background_color_is_a_light_theme_token():
    svg = (MACOS / "dmg-background.svg").read_text(encoding="utf-8")
    body = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
    assert re.findall(r"#[0-9A-Fa-f]{3,8}\b", body) == []  # no color outside the tokens
    paints = re.findall(r'(?:fill|stroke)="([^"]*)"', body)
    assert paints and all(p == "none" or re.fullmatch(r"var\(--[a-z0-9-]+\)", p) for p in paints)
    tools = _background_tools()
    tokens = tools.light_tokens(
        (ROOT / "kestrel" / "web" / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
    )
    assert tools.with_tokens(svg, tokens).count("var(--") == 0
    assert tokens["brand"] == "#4F5BD5"  # the Paper and Indigo brand (D-UI-53)
    with pytest.raises(KeyError, match="--not-a-token"):
        tools.with_tokens('fill="var(--not-a-token)"', tokens)


@pytest.mark.parametrize(
    ("name", "size", "scale"),
    [
        ("dmg-background.png", (660, 400), 1),
        ("dmg-background@2x.png", (1320, 800), 2),
    ],
)
def test_the_rendered_background_uses_only_token_colors_and_their_blends(name, size, scale):
    from PIL import Image

    img = Image.open(MACOS / name).convert("RGB")
    assert img.size == size
    colors, box = _palette()
    assert set(colors) == {"bg", "soft", "ink", "muted", "brand"}
    assert _off_palette(img, colors, box, scale) == []
    # every token the SVG names is really drawn, so a stale PNG with old colors fails
    present = {c for _, c in img.getcolors(size[0] * size[1])}
    assert set(colors.values()) <= present


def test_the_palette_check_catches_an_off_brand_color_and_skips_only_the_mark():
    from PIL import Image

    colors, box = _palette()
    img = Image.new("RGB", (660, 400), colors["bg"])
    teal = (0, 150, 136)
    img.putpixel((5, 390), teal)  # outside the mark: caught
    img.putpixel((box[0] + 1, box[1] + 1), teal)  # inside the mark's box: the mark's own colors
    assert _off_palette(img, colors, box, 1) == [teal]
    # a blend of two tokens passes; a color off the line between them does not
    a, b = colors["bg"], colors["brand"]
    mid = tuple((x + y) // 2 for x, y in zip(a, b, strict=True))
    assert _on_segment(mid, a, b, 4.0) and not _on_segment((mid[0] + 40, mid[1], mid[2]), a, b, 4.0)
    assert box == (24, 14, 36, 36)


def test_the_background_copy_is_plain_and_american():
    svg = (MACOS / "dmg-background.svg").read_text(encoding="utf-8")
    text = " ".join(re.findall(r">([^<>]+)</text>", svg))
    assert text == "Kestrel Drag Kestrel to Applications"
    assert "\u2014" not in svg


def test_dmgbuild_is_pinned_by_hash_and_installed_only_in_the_build_job():
    reqs = (MACOS / "dmg-requirements.txt").read_text(encoding="utf-8")
    pins = re.findall(r"^([a-z-]+)==([0-9.]+) \\\n\s+--hash=sha256:([0-9a-f]{64})$", reqs, re.M)
    assert {name for name, _, _ in pins} == {"dmgbuild", "ds-store", "mac-alias"}
    install = _step(_jobs()["macos"], "Install")["run"]
    assert "--require-hashes -r packaging/macos/dmg-requirements.txt" in install
    assert "--only-binary :all:" in install
    # D-REL-68: pyproject carries [tool.uv] constraint-dependencies (certifi>=...), which uv applies
    # to this install and --require-hashes refuses (the 0.7.7 macOS job failed on it).
    # --no-config keeps the project's constraints out of the hash-pinned install.
    assert "--no-config" in install.split("--require-hashes")[0]
    assert "constraint-dependencies" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for name in _workflow_names():
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert text.count("dmg-requirements.txt") == (1 if name == "release.yml" else 0)


def test_the_dmg_script_lays_out_the_window_and_keeps_the_plain_fallback():
    code = "\n".join(
        line
        for line in (ROOT / "packaging" / "build_dmg.sh").read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    laid = code.index('$DMGBUILD -s "$MACOS/dmg_settings.py"')
    for define in (
        '-D background="$BACKGROUND"',
        '-D volume_icon="$ROOT/packaging/kestrel.icns"',
        '-D format="$FORMAT"',
    ):
        assert define in code[laid : laid + 400], define
    assert code.index("tiffutil -cathidpicheck") < laid
    privacy = code.index("dmg_privacy.py")
    assert '--forbid "$ROOT" --forbid "$HOME"' in code[privacy : privacy + 200]
    # any failure of the window, or a privacy hit, makes the plain image instead of stopping
    fallback = code.index('if [ "$LAID_OUT" != 1 ]; then')
    assert (
        laid
        < fallback
        < code.index("ln -s /Applications")
        < code.index("diskutil image create from")
    )
    # the read-write layout image for the signing job, and never a signature in CI
    assert 'OUT="$ROOT/dist/Kestrel-layout.dmg"' in code and "FORMAT=UDRW" in code
    assert "APPLE_SIGNING_IDENTITY" not in str(_jobs()["macos"])


def test_the_privacy_check_finds_a_path_in_any_encoding_and_never_prints_it(tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("dmg_privacy", MACOS / "dmg_privacy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    secret_path = "/Users/sample-builder/work/kestrel"
    clean = tmp_path / "clean"
    clean.write_bytes(b"Bud1\x00\x00" + "Kestrel.app".encode("utf-16-be"))
    for name, blob in (
        ("utf8", secret_path.encode("utf-8")),
        ("utf16be", secret_path.encode("utf-16-be")),
        ("utf16le", secret_path.encode("utf-16-le")),
    ):
        leaky = tmp_path / name
        leaky.write_bytes(b"\x00\x01" + blob + b"\x00")
        assert module.main([str(clean), str(leaky), "--forbid", secret_path]) == 1, name
        printed = capsys.readouterr().out
        assert secret_path not in printed and "sample-builder" not in printed
        assert f"{name} contains forbidden value #1" in printed
    assert module.main([str(clean), "--forbid", secret_path]) == 0
    assert module.main([str(clean), "--forbid", " "]) == 2  # nothing to check is not a pass


# ------------------------------------------------------------------ the dormant path (0.7.7)


def test_with_no_apple_values_a_release_ships_unsigned_says_so_and_leaves_the_tap_alone():
    """The path that ships while Developer ID signing is dormant, end to end: preflight picks
    `none`, no job that can read a signing secret runs, the build job publishes the disk image
    with the unsigned line, the feed marks it unsigned, and the tap job skips."""
    from build_latest_json import build_feed

    jobs = _jobs()
    ref, event, mode = "refs/tags/v1.2.3", "push", "none"
    kwargs = {"ref": ref, "event": event, "mode": mode, "version_tag": True}
    assert not _eval_if(jobs["macos-sign"]["if"], **kwargs)
    assert not _eval_if(jobs["macos-sign-test"]["if"], **kwargs)
    assert _eval_if(jobs["homebrew-tap"]["if"], **kwargs)  # it runs, and must skip by itself

    build = jobs["macos"]
    runs = [
        s
        for s in build["steps"]
        if s.get("if") in (None, f"needs.preflight.outputs.mac_sign_mode == '{mode}'")
    ]
    names = _names({"steps": runs})
    assert "Build dmg" in names and "Build the layout image (to be signed)" not in names
    uploads = [s["with"]["name"] for s in runs if "upload-artifact@" in str(s.get("uses"))]
    assert uploads == ["Kestrel-macos"]
    marker = re.search(
        r'echo "([^"]*)" > dist/SIGNING-macos.txt', _step(build, "Signing marker")["run"]
    )[1]
    assert marker.startswith("macos: unsigned: ")

    changelog = "# Changelog\n\n## [1.2.3] - 2026-11-15\n\n### Added\n\n- An invented change.\n"
    sums = "\n".join(
        f"{'b' * 64}  {name}"
        for name in (
            "Kestrel-Setup-1.2.3.exe",
            "Kestrel-1.2.3.dmg",
            "Kestrel-1.2.3-x86_64.AppImage",
        )
    )
    signing = "\n".join(["windows: signed", marker, "linux: unsigned: not signed"])
    feed = build_feed(
        version="1.2.3",
        changelog=changelog,
        sums=sums,
        signing=signing,
        assets_base="https://github.com/sample-owner/sample-app/releases/download/v1.2.3",
        download_page="https://kestrelapp.co/download",
        published_at="2026-11-15T00:00:00Z",
    )
    latest = json.loads(feed.model_dump_json(by_alias=True))
    assert latest["downloads"]["macos"]["signed"] is False
    assert _render(latest) is None


@posix_tools
def test_with_no_apple_values_the_preflight_never_stops_a_release():
    for ref, event in (
        ("refs/tags/v1.2.3", "push"),
        ("refs/tags/v1.2.3-rc.1", "push"),
        ("refs/heads/main", "workflow_dispatch"),
        ("refs/heads/feature/x", "workflow_dispatch"),
    ):
        code, output, written = _run_mac_preflight(ref, event, {})
        assert code == 0 and written == {"mac_sign_mode": "none"}, (ref, output)


# ------------------------------------------------------- secrets set without their variables


@pytest.mark.parametrize("ref", _REFS)
@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "pull_request"])
@pytest.mark.parametrize("mode", ["production", "test", "none"])
def test_the_secret_check_runs_only_for_a_version_tag_that_is_not_being_signed(ref, event, mode):
    kwargs = {"ref": ref, "event": event, "mode": mode, "version_tag": _is_version_tag(ref)}
    runs = _eval_if(_jobs()["macos-sign-check"]["if"], **kwargs)
    assert runs is (mode == "none" and event == "push" and _is_version_tag(ref))


def test_the_secret_check_reads_only_booleans_and_runs_nothing_else():
    job = _jobs()["macos-sign-check"]
    assert job["environment"] == "release" and job["permissions"] == {"contents": "read"}
    assert set(job["needs"]) == {"preflight"}
    assert [s for s in job["steps"] if "uses" in s] == []
    (step,) = job["steps"]
    assert all(v.endswith(" != '' }}") for v in step["env"].values())
    assert _commands(step["run"]) <= _ALLOWED_COMMANDS["macos-sign-check"]
    assert "macos-sign-check" in _jobs()["release"]["needs"]


@posix_tools
@pytest.mark.parametrize(
    ("present", "stops"),
    [
        ((), False),
        (("HAS_CERT",), True),
        (("HAS_NOTARY_KEY",), True),
        (("HAS_CERT", "HAS_CERT_PASSWORD", "HAS_NOTARY_KEY"), True),
    ],
)
def test_a_release_with_secrets_but_no_variables_stops_and_names_them(tmp_path, present, stops):
    (step,) = _jobs()["macos-sign-check"]["steps"]
    env = _env(tmp_path)
    for key in step["env"]:
        env[key] = "true" if key in present else "false"
    done = _run_step(step["run"], env)
    assert (done.returncode != 0) is stops, done.stdout + done.stderr
    names = {
        "HAS_CERT": "APPLE_CERT_P12_BASE64",
        "HAS_CERT_PASSWORD": "APPLE_CERT_PASSWORD",
        "HAS_NOTARY_KEY": "APPLE_NOTARY_KEY_P8",
    }
    listed = done.stdout.split("has", 1)[1].split(" but ", 1)[0].split() if stops else []
    assert listed == [names[k] for k in step["env"] if k in present]


@posix_tools
@pytest.mark.parametrize(("has_token", "stops"), [("true", False), ("false", True)])
def test_a_missing_tap_token_stops_with_its_name_before_the_checkout(tmp_path, has_token, stops):
    tap = _jobs()["homebrew-tap"]
    names = _names(tap)
    check = _step(tap, "The tap token is set")
    assert check["if"] == "steps.cask.outputs.publish == 'true'"
    assert check["env"] == {"HAS_TAP_TOKEN": "${{ secrets.HOMEBREW_TAP_TOKEN != '' }}"}
    checkout = next(
        i for i, s in enumerate(tap["steps"]) if "secrets.HOMEBREW_TAP_TOKEN }}" in str(s)
    )
    assert names.index("The tap token is set") < checkout
    done = _run_step(check["run"], _env(tmp_path, HAS_TAP_TOKEN=has_token))
    assert (done.returncode != 0) is stops
    assert ("HOMEBREW_TAP_TOKEN is not set" in done.stdout) is stops


@posix_tools
def test_without_dmgbuild_the_build_says_so_loudly_and_makes_the_plain_image(tmp_path):
    env = _build_tree(tmp_path)
    del env["DMGBUILD"]
    # an interpreter that has no dmgbuild: `python -c "import dmgbuild"` fails
    _tool(tmp_path / "bin", "nodmgpython", 'echo "nodmgpython $*" >> "$CALLS"; exit 1\n')
    env["PYTHON"] = str(tmp_path / "bin" / "nodmgpython")
    done, calls = _run_build(env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "dmgbuild is not installed, so this is the plain image" in done.stdout
    assert "dmg-requirements.txt" in done.stdout
    assert "with the Kestrel window" not in done.stdout and "dmgbuild " not in calls
    plain = [c for c in calls.splitlines() if c.startswith("hdiutil create")]
    assert len(plain) == 1 and "-format UDZO" in plain[0]


def test_the_local_build_steps_install_dmgbuild_and_show_the_window_in_both_modes():
    doc = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
    section = doc.split("#### The macOS disk image window, checked on a Mac", 1)[1].split(
        "\n## ", 1
    )[0]
    commands = [
        line.strip() for line in section.splitlines() if line and not line.startswith(("#", " "))
    ]
    order = [
        "uv sync --extra dev",
        "uv pip install --no-config --only-binary :all: --require-hashes"
        " -r packaging/macos/dmg-requirements.txt",
        "uv run python scripts/fetch_embed_model.py",
        'env -u APPLE_SIGNING_IDENTITY PYTHON="$(uv python find)" packaging/build_dmg.sh',
    ]
    positions = [commands.index(c) for c in order]
    assert positions == sorted(positions)
    # the install line is the one the release job runs
    assert order[1] in _step(_jobs()["macos"], "Install")["run"]
    assert section.count("set dark mode to false") >= 1 and "set dark mode to true" in section
    assert (
        'open "dist/Kestrel-$VERSION.dmg"' in section
        and 'hdiutil detach "/Volumes/Kestrel $VERSION"' in section
    )


@posix_tools
@pytest.mark.parametrize(
    ("tag", "has_token", "stops"),
    [
        ("v1.2.3", "true", False),
        ("v1.2.3", "false", True),  # a stable release would publish, then fail the tap
        ("v1.2.3-rc.1", "false", False),  # a prerelease never updates the tap
    ],
)
def test_a_signed_release_checks_the_tap_token_before_anything_is_published(
    tmp_path, tag, has_token, stops
):
    step = _step(_jobs()["macos-sign"], "The signing secrets are set")
    assert step["env"]["HAS_TAP_TOKEN"] == "${{ secrets.HOMEBREW_TAP_TOKEN != '' }}"
    env = _env(tmp_path, GITHUB_REF=f"refs/tags/{tag}", HAS_TAP_TOKEN=has_token)
    for key in ("HAS_CERT", "HAS_CERT_PASSWORD", "HAS_NOTARY_KEY"):
        env[key] = "true"
    done = _run_step(step["run"], env)
    assert (done.returncode != 0) is stops, done.stdout + done.stderr
    assert ("HOMEBREW_TAP_TOKEN" in done.stdout) is stops
    # the check runs in the job the release waits for, so a missing token stops the release
    assert "macos-sign" in _jobs()["release"]["needs"]
    assert "HOMEBREW_TAP_TOKEN" not in str(_jobs()["macos-sign-test"])
