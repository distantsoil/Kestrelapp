# Releasing Kestrel

A release is one tag. CI builds the three installers from it and publishes them to a GitHub
Release. Nothing is built by hand on the operator's machine, and nothing is signed unless the
signing secrets exist.

## Cut a release

1. Bump the version in exactly one place: `__version__` in `kestrel/__init__.py`. pyproject reads
   it through hatch; the window title, `/health`, `/about`, the installer file names and the macOS
   bundle version all come from it. Then regenerate the Inno Setup include so the committed file
   matches (`uv run python packaging/gen_version_iss.py`).
2. Make sure `main` is green (`.github/workflows/ci.yml`: ruff and pytest on Ubuntu, macOS and
   Windows).
3. Tag and push:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

4. `.github/workflows/release.yml` runs three build jobs and then a release job:

   | Job | Runner | Script | Artifact |
   |---|---|---|---|
   | Windows installer | `windows-latest` | `packaging/build_windows.ps1` | `Kestrel-Setup-<version>.exe` |
   | macOS dmg | `macos-latest` (Apple silicon) | `packaging/build_dmg.sh` | `Kestrel-<version>.dmg` |
   | Linux AppImage | `ubuntu-latest` | `packaging/build_appimage.sh` | `Kestrel-<version>-x86_64.AppImage` |

   Each job smoke-tests the frozen CLI (`version`, `doctor --quick`) before uploading. The release
   job attaches the three files plus `SHA256SUMS.txt` to a GitHub Release named after the tag, with
   release notes generated from the merged pull requests and commits since the previous tag.

5. Check the Release page, download one installer and launch it on a machine that is not the build
   machine. The window opening and `<kestrel> doctor` running are the acceptance test.

### Dry run

Actions, Release, Run workflow (`workflow_dispatch`). The **platform** choice builds `all` three
installers (the default) or just `windows`, `macos` or `linux`. Results are uploaded as workflow
artifacts under the run; no Release is created. Use it to test a packaging change before tagging,
and pick a single platform when you are only testing one installer: macOS minutes cost 10x and
Windows 2x, so a Windows-only dry run costs a fraction of a full one. A tag push ignores the
choice and always builds and publishes all three.

### Building locally

Every script runs PyInstaller itself and takes `--skip-build` (`-SkipBuild` on Windows) when
`dist/` already holds the build. `PYTHON` names the interpreter with kestrel and pyinstaller
installed (`uv sync --extra dev` installs both).

```bash
PYTHON=.venv/bin/python packaging/build_appimage.sh      # Linux
PYTHON=.venv/bin/python packaging/build_dmg.sh           # macOS
$env:PYTHON=".venv\Scripts\python.exe"; packaging\build_windows.ps1   # Windows, needs Inno Setup 6
```

## Signing

The builds are **unsigned by default** and that is a working release: Windows SmartScreen asks
for "More info, Run anyway" once, macOS Gatekeeper needs a right-click Open once, Linux has no
equivalent. Signing removes those steps and needs the maintainer's own certificates, added as
repository secrets (Settings, Secrets and variables, Actions). The build scripts print exactly
which secrets are missing and what the operator will see.

### Windows (Authenticode)

| Secret | Value |
|---|---|
| `WINDOWS_CERT_PFX_BASE64` | the code-signing certificate and key as a .pfx, base64-encoded: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))` |
| `WINDOWS_CERT_PASSWORD` | the .pfx password |
| `WINDOWS_CERT_TIMESTAMP_URL` (optional) | RFC 3161 timestamp server; default `http://timestamp.digicert.com` |

`build_windows.ps1` signs `Kestrel.exe`, `kestrel-cli.exe` and the finished installer with
`signtool` from the Windows SDK (present on the GitHub runner). An EV certificate removes the
SmartScreen prompt immediately; an OV certificate removes it once the file has built reputation.

### macOS (Developer ID + notarization)

| Secret | Value |
|---|---|
| `APPLE_CERT_P12_BASE64` | the "Developer ID Application" certificate and key exported from Keychain Access as .p12, base64-encoded: `base64 -i cert.p12` |
| `APPLE_CERT_PASSWORD` | the .p12 password |
| `APPLE_SIGNING_IDENTITY` | the certificate's common name, for example `Developer ID Application: Jane Doe (ABCDE12345)` |
| `APPLE_ID` | the Apple ID that owns the developer account |
| `APPLE_TEAM_ID` | the ten-character team id |
| `APPLE_APP_PASSWORD` | an app-specific password for that Apple ID (appleid.apple.com, Sign-In and Security) |

The workflow imports the certificate into a temporary keychain, `build_dmg.sh` signs every Mach-O
in the bundle inside-out with the hardened runtime and `packaging/macos/entitlements.plist`, signs
the dmg, submits it with `notarytool --wait`, and staples the ticket. With only
`APPLE_SIGNING_IDENTITY` and the certificate, the app is signed but not notarized, which still
triggers Gatekeeper; all six are needed for a quiet first launch.

### Linux

AppImages are not signed in a way desktops enforce. `SHA256SUMS.txt` on the Release is the
integrity check. `build_appimage.sh` pins `appimagetool` to release 1.9.1 by URL and sha256 and
the AppImage runtime by sha256 (its upstream publishes only a moving `continuous` tag), so a build
fails loudly rather than silently picking up a changed tool.

## What is verified where

- **Linux** is verified in CI and in the cloud build session: the frozen binary writes
  `runtime.json`, answers `/health`, falls back to the browser when no GTK WebKit is present, and
  dispatches CLI arguments; the AppImage runs with and without FUSE (the runtime extracts itself
  when `fusermount` is missing).
- **macOS** builds only on a macOS runner (`hdiutil`, `codesign`, `notarytool`, `iconutil`). The
  bundle structure, `Info.plist` keys and `packaging/kestrel.icns` are checked by
  `tests/test_packaging.py`; the window itself is verified on the first tagged build.
- **Windows** builds only on a Windows runner (Inno Setup, `signtool`). The `.iss` and the
  `kestrel-cli.exe` twin are checked by the same tests; the installer is verified by installing it.
