#!/usr/bin/env bash
# Build Kestrel-<version>-x86_64.AppImage from the PyInstaller one-folder output (Linux only).
#
#   packaging/build_appimage.sh              # runs PyInstaller, then wraps dist/Kestrel
#   packaging/build_appimage.sh --skip-build # dist/Kestrel already exists
#
# Environment:
#   PYTHON          the interpreter with kestrel + pyinstaller installed (default: python)
#   APPIMAGETOOL    a local appimagetool to use instead of downloading the pinned one
#   APPIMAGE_RUNTIME_FILE  a runtime to use instead of the vendored one (packaging/linux/)
#
# The AppDir is laid out as usr/lib/kestrel/<one-folder> with an AppRun that execs the frozen
# binary and forwards every argument (so the AppImage is also the CLI). The result needs libfuse2
# on the target machine, like every AppImage; without it, run with --appimage-extract-and-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python}"
DIST="$ROOT/dist/Kestrel"
BUILD_DIR="$ROOT/build/appimage"
APPDIR="$BUILD_DIR/Kestrel.AppDir"
ARCH_NAME="x86_64"

# appimagetool is pinned to a release tag, never `continuous`. The 1.9.1 release publishes no
# checksum file, so the sha256 below was computed from that download on 2026-10-12 and is what a
# later download must match; bump both together when moving to a newer release.
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
# appimagetool 1.9 embeds a separately published runtime it would otherwise fetch from the moving
# `continuous` tag on every build. type2-runtime publishes no other tag, so the checksum is the pin:
# a mismatch means upstream moved and someone must look at the new runtime and bump this on
# purpose. The runtime is what makes the AppImage fall back to extract-and-run when FUSE is absent.
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64"
# 2026-10-19: upstream rebuilt `continuous` (commit fedfad51, its build 39320578745). Checked
# before bumping: GitHub's digest for the asset equals this hash; the file is a 944,632-byte
# ELF. Previous: 156f4bdbde9c52d01814600013e0a273f0118dc2de98975f3c8c63427ec79074.
RUNTIME_SHA256="617aa77314d57c8b8e4fae9c3f0b35af7b30d5529cb69e98f64f92510ed98a46"

VERSION="$(sed -nE 's/^__version__ = "([^"]+)"$/\1/p' "$ROOT/kestrel/__init__.py")"
if [ -z "$VERSION" ]; then
    echo "could not read __version__ from kestrel/__init__.py" >&2
    exit 1
fi
OUT="$ROOT/dist/Kestrel-${VERSION}-${ARCH_NAME}.AppImage"

if [ "$(uname -m)" != "x86_64" ]; then
    echo "this script builds the x86_64 AppImage only; host is $(uname -m)" >&2
    exit 1
fi

# ------------------------------------------------------------------ 1. the one-folder build
if [ "${1:-}" != "--skip-build" ]; then
    echo "==> bundled embedding model (kestrel.spec refuses to build without it)"
    (cd "$ROOT" && { "$PYTHON" scripts/fetch_embed_model.py --check ||
        "$PYTHON" scripts/fetch_embed_model.py; })
    echo "==> PyInstaller one-folder build"
    (cd "$ROOT" && "$PYTHON" -m PyInstaller packaging/kestrel.spec --noconfirm)
fi
if [ ! -x "$DIST/kestrel" ]; then
    echo "missing $DIST/kestrel; run PyInstaller first (or drop --skip-build)" >&2
    exit 1
fi

# ------------------------------------------------------------------ 2. the AppDir
echo "==> laying out $APPDIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib/kestrel" "$APPDIR/usr/bin" \
    "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/512x512/apps"
cp -a "$DIST/." "$APPDIR/usr/lib/kestrel/"
# A usr/bin symlink keeps `Exec=kestrel` in the .desktop file honest for tools that resolve it.
ln -s ../lib/kestrel/kestrel "$APPDIR/usr/bin/kestrel"
install -m 0755 "$ROOT/packaging/linux/AppRun" "$APPDIR/AppRun"
install -m 0644 "$ROOT/packaging/linux/kestrel.desktop" "$APPDIR/kestrel.desktop"
install -m 0644 "$ROOT/packaging/linux/kestrel.desktop" "$APPDIR/usr/share/applications/kestrel.desktop"
# The icon is the same 512 px mark the PWA uses (scripts/make_icons.py renders it from the SVG).
install -m 0644 "$ROOT/kestrel/web/static/icon-512.png" "$APPDIR/kestrel.png"
install -m 0644 "$ROOT/kestrel/web/static/icon-512.png" \
    "$APPDIR/usr/share/icons/hicolor/512x512/apps/kestrel.png"
ln -s kestrel.png "$APPDIR/.DirIcon"
# The runtime is MIT and statically links other libraries: its notice travels inside the AppImage.
mkdir -p "$APPDIR/usr/share/doc/kestrel"
install -m 0644 "$ROOT/packaging/linux/RUNTIME-NOTICE.txt" \
    "$APPDIR/usr/share/doc/kestrel/RUNTIME-NOTICE.txt"

# ------------------------------------------------------------------ 3. appimagetool
if [ -n "${APPIMAGETOOL:-}" ]; then
    TOOL="$APPIMAGETOOL"
else
    mkdir -p "$BUILD_DIR"
    TOOL="$BUILD_DIR/appimagetool-x86_64.AppImage"
    if [ ! -f "$TOOL" ] || ! echo "$APPIMAGETOOL_SHA256  $TOOL" | sha256sum -c --status; then
        echo "==> downloading appimagetool (pinned release 1.9.1)"
        curl -fsSL --retry 3 -o "$TOOL" "$APPIMAGETOOL_URL"
        echo "$APPIMAGETOOL_SHA256  $TOOL" | sha256sum -c -
    fi
    chmod +x "$TOOL"
fi
# Resolution order: APPIMAGE_RUNTIME_FILE, then the reviewed runtime vendored in the repository
# (used only when its sha256 matches the pin, so a stale or edited file falls through), then a
# download verified against the same pin. Normal builds therefore need no network for the runtime
# and an upstream rebuild of `continuous` cannot stop a release (#44).
VENDORED_RUNTIME="$ROOT/packaging/linux/runtime-x86_64"
if [ -n "${APPIMAGE_RUNTIME_FILE:-}" ]; then
    RUNTIME="$APPIMAGE_RUNTIME_FILE"
elif [ -f "$VENDORED_RUNTIME" ] &&
    echo "$RUNTIME_SHA256  $VENDORED_RUNTIME" | sha256sum -c --status; then
    echo "==> using the vendored AppImage runtime (sha256 matches the pin)"
    RUNTIME="$VENDORED_RUNTIME"
else
    if [ -f "$VENDORED_RUNTIME" ]; then
        echo "==> the vendored runtime does not match RUNTIME_SHA256; downloading the pinned one" >&2
    fi
    mkdir -p "$BUILD_DIR"
    RUNTIME="$BUILD_DIR/runtime-x86_64"
    if [ ! -f "$RUNTIME" ] || ! echo "$RUNTIME_SHA256  $RUNTIME" | sha256sum -c --status; then
        echo "==> downloading the AppImage runtime (pinned by sha256)"
        curl -fsSL --retry 3 -o "$RUNTIME" "$RUNTIME_URL"
        if ! echo "$RUNTIME_SHA256  $RUNTIME" | sha256sum -c -; then
            echo "the type2-runtime 'continuous' asset no longer matches RUNTIME_SHA256 in $0." >&2
            echo "Inspect the new runtime, then update RUNTIME_SHA256, or point" >&2
            echo "APPIMAGE_RUNTIME_FILE at a runtime you trust." >&2
            exit 1
        fi
    fi
fi

# ------------------------------------------------------------------ 4. the AppImage
# --appimage-extract-and-run lets the tool itself run without FUSE (containers, CI). ARCH is what
# appimagetool uses to name the output; VERSION is written into the .desktop file.
echo "==> writing $OUT"
rm -f "$OUT"
ARCH="$ARCH_NAME" VERSION="$VERSION" "$TOOL" --appimage-extract-and-run \
    --runtime-file "$RUNTIME" "$APPDIR" "$OUT"
chmod +x "$OUT"
ls -la "$OUT"
echo "done: $OUT"
