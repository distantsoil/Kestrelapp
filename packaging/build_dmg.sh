#!/usr/bin/env bash
# Build Kestrel-<version>.dmg from the PyInstaller bundle (macOS only).
#
#   packaging/build_dmg.sh              # runs PyInstaller (dist/Kestrel.app), then makes the dmg
#   packaging/build_dmg.sh --skip-build # dist/Kestrel.app already exists
#
# Environment:
#   PYTHON                   the interpreter with kestrel + pyinstaller installed (default: python)
#   APPLE_SIGNING_IDENTITY   "Developer ID Application: Name (TEAMID)"; absent = unsigned build
#   APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD
#                            notarytool credentials; all three present = notarize and staple
#
# Unsigned is the default and is a working build: macOS Gatekeeper shows "cannot be opened because
# the developer cannot be verified", and the operator right-clicks the app and chooses Open once.
# Signing and notarization remove that step; they need the operator's own Apple Developer
# certificate and are gated on the variables above so the same script serves both.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python}"
APP="$ROOT/dist/Kestrel.app"
STAGE="$ROOT/build/dmg"
ENTITLEMENTS="$ROOT/packaging/macos/entitlements.plist"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "this script needs macOS (hdiutil, codesign, notarytool)" >&2
    exit 1
fi

VERSION="$(sed -nE 's/^__version__ = "([^"]+)"$/\1/p' "$ROOT/kestrel/__init__.py")"
if [ -z "$VERSION" ]; then
    echo "could not read __version__ from kestrel/__init__.py" >&2
    exit 1
fi
DMG="$ROOT/dist/Kestrel-${VERSION}.dmg"

# ------------------------------------------------------------------ 1. the bundle
if [ "${1:-}" != "--skip-build" ]; then
    if [ ! -f "$ROOT/packaging/kestrel.icns" ]; then
        echo "==> packaging/kestrel.icns missing; generating it from the 512 px mark"
        (cd "$ROOT" && "$PYTHON" scripts/make_icons.py --icns)
    fi
    echo "==> bundled embedding model (kestrel.spec refuses to build without it)"
    (cd "$ROOT" && { "$PYTHON" scripts/fetch_embed_model.py --check ||
        "$PYTHON" scripts/fetch_embed_model.py; })
    echo "==> PyInstaller one-folder build + BUNDLE"
    (cd "$ROOT" && "$PYTHON" -m PyInstaller packaging/kestrel.spec --noconfirm)
fi
if [ ! -x "$APP/Contents/MacOS/Kestrel" ]; then
    echo "missing $APP/Contents/MacOS/Kestrel; run PyInstaller first (or drop --skip-build)" >&2
    exit 1
fi

# ------------------------------------------------------------------ 2. codesign (optional)
SIGNED=0
if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
    echo "==> codesign with: $APPLE_SIGNING_IDENTITY"
    # Inside-out: every Mach-O in the bundle first (PyInstaller ships hundreds of .so/.dylib files
    # that --deep does not reliably reach in the right order), then the main executable, then the
    # bundle itself. Hardened runtime + the entitlements a frozen Python needs.
    find "$APP/Contents" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) \
        ! -path "*/Contents/MacOS/Kestrel" -print0 |
        while IFS= read -r -d '' f; do
            if file -b "$f" | grep -q "Mach-O"; then
                codesign --force --options runtime --timestamp \
                    --entitlements "$ENTITLEMENTS" --sign "$APPLE_SIGNING_IDENTITY" "$f"
            fi
        done
    codesign --force --options runtime --timestamp \
        --entitlements "$ENTITLEMENTS" --sign "$APPLE_SIGNING_IDENTITY" "$APP/Contents/MacOS/Kestrel"
    codesign --force --options runtime --timestamp \
        --entitlements "$ENTITLEMENTS" --sign "$APPLE_SIGNING_IDENTITY" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
    SIGNED=1
else
    cat <<'EOF'
==> APPLE_SIGNING_IDENTITY is not set: the app will be UNSIGNED.
    It runs, but macOS Gatekeeper will refuse the first double-click. The operator right-clicks
    Kestrel.app, chooses Open, and confirms once; after that it opens normally. To remove that
    step, set APPLE_SIGNING_IDENTITY to your "Developer ID Application" certificate (and the three
    APPLE_* notarytool variables to notarize). See docs/RELEASING.md.
EOF
fi

# ------------------------------------------------------------------ 3. the dmg
echo "==> staging $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/Kestrel.app"
ln -s /Applications "$STAGE/Applications"

echo "==> writing $DMG"
rm -f "$DMG"
# hdiutil create is deprecated as of macOS 27 in favor of `diskutil image`. Probe for the new tool
# and use it when it is there; otherwise (older macOS, and every runner today) use hdiutil.
# UNVERIFIED: the `diskutil image create from` flags below are written from the macOS 27 release
# notes and have not been run on a macOS 27 host (issue #40). If that call fails for any reason the
# build says so and falls back to hdiutil, so a wrong flag can never block a release.
if diskutil image create --help >/dev/null 2>&1 &&
    diskutil image create from --format UDZO --volumeName "Kestrel $VERSION" "$STAGE" "$DMG"; then
    :
else
    echo "==> diskutil image create unavailable or failed; using hdiutil create"
    rm -f "$DMG"
    hdiutil create -volname "Kestrel $VERSION" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$DMG"
fi

# ------------------------------------------------------------------ 4. notarize (optional)
if [ "$SIGNED" = 1 ]; then
    codesign --force --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$DMG"
    if [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ] && [ -n "${APPLE_APP_PASSWORD:-}" ]; then
        echo "==> notarizing (this waits for Apple, usually a few minutes)"
        xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
            --password "$APPLE_APP_PASSWORD" --wait
        xcrun stapler staple "$DMG"
        xcrun stapler staple "$APP"
        echo "==> notarized and stapled"
    else
        echo "==> signed but NOT notarized: set APPLE_ID, APPLE_TEAM_ID and APPLE_APP_PASSWORD to" \
             "notarize. Gatekeeper will still warn on first open without notarization."
    fi
fi

ls -la "$DMG"
echo "done: $DMG"
