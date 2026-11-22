#!/usr/bin/env bash
# Build Kestrel-<version>.dmg from the PyInstaller bundle (macOS only).
#
#   packaging/build_dmg.sh              # runs PyInstaller (dist/Kestrel.app), then makes the dmg
#   packaging/build_dmg.sh --skip-build # dist/Kestrel.app already exists
#   packaging/build_dmg.sh --rw         # (with or without --skip-build) a read-write layout image,
#                                       # dist/Kestrel-layout.dmg, for packaging/macos/sign_dmg.sh
#
# Environment:
#   PYTHON          the interpreter with kestrel + pyinstaller installed (default: python)
#   DMGBUILD        the dmgbuild command (default: "$PYTHON -m dmgbuild"); the pinned install is in
#                   packaging/macos/dmg-requirements.txt
#   APPLE_SIGNING_IDENTITY, APPLE_TEAM_ID   local builds only: sign the finished image with
#                   packaging/macos/sign_dmg.sh (not notarized). The release workflow never sets
#                   these here; it signs and notarizes in its own job (docs/RELEASING.md).
#
# The window (#198): a Paper and Indigo background with a drag-to-Applications cue, fixed icon
# positions and window size, no toolbar or sidebar, and the Kestrel mark as the volume icon,
# written by dmgbuild straight into .DS_Store (no Finder scripting). If dmgbuild is missing or
# fails, or its layout files would carry a build path or account name, the build says so and makes
# the plain image instead, so the window can never block a release.
#
# Unsigned is the default and is a working build: macOS Gatekeeper shows "cannot be opened because
# the developer cannot be verified", and the operator right-clicks the app and chooses Open once.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python}"
APP="$ROOT/dist/Kestrel.app"

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
SKIP_BUILD=0
for arg in "$@"; do
    if [ "$arg" = "--skip-build" ]; then SKIP_BUILD=1; fi
done
if [ "$SKIP_BUILD" = 0 ]; then
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

# ------------------------------------------------------------------ 2. the dmg
FORMAT=UDZO
OUT="$DMG"
for arg in "$@"; do
    if [ "$arg" = "--rw" ]; then
        FORMAT=UDRW
        OUT="$ROOT/dist/Kestrel-layout.dmg"
    fi
done
LAYOUT="$ROOT/build/dmg-layout"
MACOS="$ROOT/packaging/macos"
DMGBUILD_SET="${DMGBUILD:-}"
DMGBUILD="${DMGBUILD:-$PYTHON -m dmgbuild}"
VOLNAME="Kestrel $VERSION"
# Signing adds a signature to every Mach-O file; the read-write image gets room for that.
SIZE=""
if [ "$FORMAT" = UDRW ]; then
    SIZE="$(( $(du -sm "$APP" | cut -f1) + 400 ))m"
fi
rm -f "$OUT"
rm -rf "$LAYOUT"
mkdir -p "$LAYOUT"

# One background file holding both resolutions, so Retina Finder draws the @2x image.
BACKGROUND="$LAYOUT/background.tiff"
if ! tiffutil -cathidpicheck "$MACOS/dmg-background.png" "$MACOS/dmg-background@2x.png" \
    -out "$BACKGROUND" > /dev/null 2>&1; then
    echo "==> tiffutil could not combine the two backgrounds; using the 1x image"
    # A copy with no @2x file beside it, so dmgbuild does not try to combine them itself.
    BACKGROUND="$LAYOUT/background.png"
    cp "$MACOS/dmg-background.png" "$BACKGROUND"
fi

# The layout files must not name this machine: no build path, home folder or account name.
layout_is_private() {
    local mnt="$LAYOUT/check" rc=0
    mkdir -p "$mnt"
    hdiutil attach -nobrowse -noautoopen -readonly -mountpoint "$mnt" "$OUT" > /dev/null || return 1
    # every hidden file at the volume root: .DS_Store, dmgbuild's .background.tiff, the volume icon
    "$PYTHON" "$MACOS/dmg_privacy.py" "$mnt"/.[!.]* --forbid "$ROOT" --forbid "$HOME" || rc=$?
    hdiutil detach "$mnt" > /dev/null 2>&1 || hdiutil detach -force "$mnt" > /dev/null
    return "$rc"
}

LAID_OUT=0
if [ -z "$DMGBUILD_SET" ] && ! "$PYTHON" -c "import dmgbuild" > /dev/null 2>&1; then
    # Said loudly: this plain image is not the one to check for the window (#198).
    echo "==> dmgbuild is not installed, so this is the plain image with no window layout."
    echo "    To build the window: uv pip install --only-binary :all: --require-hashes \\"
    echo "        -r packaging/macos/dmg-requirements.txt"
else
    echo "==> writing $OUT with the Kestrel window (dmgbuild)"
    if $DMGBUILD -s "$MACOS/dmg_settings.py" -D app="$APP" -D background="$BACKGROUND" \
        -D volume_icon="$ROOT/packaging/kestrel.icns" -D format="$FORMAT" -D size="$SIZE" \
        "$VOLNAME" "$OUT"; then
        if layout_is_private; then
            LAID_OUT=1
        else
            echo "==> the window layout would carry a local path or account name; making the plain image"
        fi
    else
        echo "==> dmgbuild failed; making the plain image (no background or layout)"
    fi
fi

if [ "$LAID_OUT" != 1 ]; then
    rm -f "$OUT"
    STAGE="$ROOT/build/dmg"
    echo "==> staging $STAGE"
    rm -rf "$STAGE"
    mkdir -p "$STAGE"
    cp -R "$APP" "$STAGE/Kestrel.app"
    ln -s /Applications "$STAGE/Applications"
    if [ "$FORMAT" = UDRW ]; then
        hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -ov -format UDRW -fs HFS+ \
            -size "$SIZE" "$OUT"
    # hdiutil create is deprecated as of macOS 27 in favor of `diskutil image`. Probe for the new
    # tool and use it when it is there; otherwise (older macOS, and every runner today) use hdiutil.
    # VERIFIED on macOS 27.0.1 (Apple Silicon), 2026-10-20: the `diskutil image create from` call
    # below wrote the dmg with no fallback (issue #40). If that call fails for any reason the build
    # says so and falls back to hdiutil, so a changed flag can never block a release.
    elif diskutil image create --help >/dev/null 2>&1 &&
        diskutil image create from --format UDZO --volumeName "$VOLNAME" "$STAGE" "$OUT"; then
        :
    else
        echo "==> diskutil image create unavailable or failed; using hdiutil create"
        rm -f "$OUT"
        hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$OUT"
    fi
fi

# ------------------------------------------------------------------ 3. signing (local only)
if [ "$FORMAT" = UDZO ] && [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
    echo "==> local build: signing with packaging/macos/sign_dmg.sh (not notarized)"
    LAYOUT_RW="$ROOT/build/Kestrel-sign-layout.dmg"
    rm -f "$LAYOUT_RW"
    hdiutil convert "$OUT" -format UDRW -o "$LAYOUT_RW" > /dev/null
    hdiutil resize -size "$(( $(du -sm "$APP" | cut -f1) + 400 ))m" "$LAYOUT_RW" > /dev/null
    SIGN_NAME="$APPLE_SIGNING_IDENTITY" TEAM_ID="${APPLE_TEAM_ID:-}" \
        "$MACOS/sign_dmg.sh" sign "$LAYOUT_RW" "$OUT"
elif [ "$FORMAT" = UDZO ]; then
    echo "==> UNSIGNED: APPLE_SIGNING_IDENTITY is not set, so this disk image is not signed."
    echo "    It runs, but macOS Gatekeeper refuses the first double-click. The operator right-clicks"
    echo "    Kestrel.app, chooses Open, and confirms once; after that it opens normally. Releases are"
    echo "    signed and notarized by the release workflow once it is set up (docs/RELEASING.md)."
fi

ls -la "$OUT"
echo "done: $OUT"
