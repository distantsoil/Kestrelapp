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
   the CHANGELOG section for that version as its body (see "Release notes and the website").

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

## Release notes and the website

The release notes are the operator-facing CHANGELOG section, not a list generated from pull
requests. The same text feeds the update checker's "What's new" dialog (SPEC-INTERFACES 1.5).

For each release:

1. Write the section in `CHANGELOG.md` before tagging: a heading `## [X.Y.Z] - YYYY-MM-DD` with
   `### New`, `### Fixed` and `### Changed` bullets, plain and operator-facing. No pull request
   numbers, commit hashes, branch names or repository paths.
2. Check the section: `uv run python scripts/changelog_section.py X.Y.Z` must print its body (the
   `### New`, `### Fixed` and `### Changed` parts, without the version heading).
   A prerelease version (one containing `-`, such as `0.5.0-rc.1`) may have no section of its own:
   the `preflight` job and the release body then use the `[Unreleased]` section
   (`scripts/changelog_section.py X.Y.Z-rc.1 --allow-unreleased`). A stable version always needs
   its own section.
3. Tag and push (step 3 above). Add the `WEBSITE_TOKEN` secret before the first tag (see "One-time
   setup" below): the `preflight` job runs before any installer is built and fails a stable tag
   push with a clear error if the secret is empty.
4. The release job publishes the GitHub Release with that section as its body.
5. The `website` job commits `latest.json` to `distantsoil/kestrelapp-site` with the
   `WEBSITE_TOKEN` secret. Cloudflare Pages rebuilds on push. The feed holds the last 10 sections,
   newest first. A tag with a prerelease suffix (for example `v0.4.0-rc.1`) is never written.

Installers stay on GitHub Releases. The site's download buttons link to the GitHub asset URLs,
because Cloudflare Pages caps each file at 25 MiB. Kestrel never downloads or installs anything;
the operator downloads the installer from the site.

### One-time setup on the public repository

Settings, Secrets and variables, Actions, on `distantsoil/Kestrelapp`:

| Name | Kind | Value |
|---|---|---|
| `WEBSITE_REPO` | repository variable | `distantsoil/kestrelapp-site`. The website job uses this default when the variable is unset |
| `WEBSITE_TOKEN` | secret | a fine-grained personal access token: resource owner `distantsoil`, repository access "Only select repositories" with `distantsoil/kestrelapp-site` alone, permission Contents: read and write, nothing else |

Rotation: the token expires on the date chosen at creation. Record the dates here when it is set
and when it is rotated. Created: `____`. Rotate by: `____`. Last rotated: `____`. Replace the
secret the same day. The website job must fail loudly when the token is rejected, never skip
the update silently.

### Website side

In `distantsoil/kestrelapp-site`:

- The download page reads `/latest.json` to show the current version and the download buttons.
- A `_headers` file at the site root sets a short cache on the feed:

  ```
  /latest.json
    Cache-Control: max-age=300
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
