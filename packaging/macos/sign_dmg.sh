#!/usr/bin/env bash
# Sign, notarize and verify the Kestrel disk image (#202). Apple's own tools only: hdiutil,
# codesign, xcrun notarytool, xcrun stapler and spctl. No package manager, no download and no
# Kestrel code runs here except `smoke`, which the workflow calls only after the keychain and the
# notary key are gone.
#
#   sign_dmg.sh sign <layout.dmg (UDRW)> <out.dmg>  sign the app inside-out, then the disk image
#   sign_dmg.sh notarize <out.dmg>                   submit with the API key, wait, staple
#   sign_dmg.sh verify production|test <out.dmg>     the checks a release must pass (see below)
#   sign_dmg.sh smoke <out.dmg>                      run the signed app's `version` once
#
# Environment:
#   SIGN_NAME   the certificate's full name, e.g. "Developer ID Application: Jane Doe (ABCDE12345)"
#   TEAM_ID     the ten-character Apple team ID
#   KEYCHAIN    the keychain that holds the certificate (optional; default search list)
#   NOTARY_KEY_FILE, NOTARY_KEY_ID, NOTARY_ISSUER   notarize only: the App Store Connect API key
#
# Nothing here prints a secret: the key file path and IDs are passed as arguments, never echoed,
# and the script never turns on `set -x`. Every check prints the tool's own output, so a failure
# can be pasted back without a re-run. Flags are cited in the private decision log D-REL-613.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENTITLEMENTS="$ROOT/packaging/macos/entitlements.plist"
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/kestrel-dmg-sign"
MNT="$WORK/mnt"

die() { echo "::error::$*" >&2; exit 1; }
need() { [ -n "${!1:-}" ] || die "$1 is not set"; }

attach() { # attach <dmg> <readwrite|readonly>
    mkdir -p "$MNT"
    hdiutil attach -nobrowse -noautoopen "-$2" -mountpoint "$MNT" "$1" > /dev/null
}
detach() {
    hdiutil detach "$MNT" > /dev/null 2>&1 || hdiutil detach -force "$MNT" > /dev/null
}

sign_one() { # sign_one <path> [entitlements]
    local args=(--force --options runtime --timestamp --sign "$SIGN_NAME")
    if [ -n "${KEYCHAIN:-}" ]; then args+=(--keychain "$KEYCHAIN"); fi
    if [ "${2:-}" = "entitlements" ]; then args+=(--entitlements "$ENTITLEMENTS"); fi
    codesign "${args[@]}" "$1"
}

# The first Authority= line of `codesign -dvv` is the leaf certificate's name, and TeamIdentifier=
# is the team. Both must equal what the workflow expects; if codesign prints neither line (its
# output format is not documented), the check fails closed and shows what it did print.
check_signer() { # check_signer <path>
    local out authority team
    out="$(codesign -dvv "$1" 2>&1)" || { echo "$out"; die "codesign could not read the signature of $1"; }
    echo "$out" | grep -E '^(Identifier|Format|Authority|TeamIdentifier|Timestamp)=' || true
    authority="$(echo "$out" | sed -n 's/^Authority=//p' | head -n 1)"
    team="$(echo "$out" | sed -n 's/^TeamIdentifier=//p' | head -n 1)"
    if [ "$authority" != "$SIGN_NAME" ]; then
        echo "$out"
        die "$1 is signed by \"$authority\", expected \"$SIGN_NAME\""
    fi
    if [ "$team" != "$TEAM_ID" ]; then
        echo "$out"
        die "$1 carries team \"$team\", expected \"$TEAM_ID\""
    fi
}

# Signing adds a signature to every Mach-O file, and dmgbuild hands over an image shrunk to its
# minimum, so the image grows by 400 MB first. `hdiutil resize -limits` prints the minimum, current
# and maximum size in 512-byte sectors; if its last line is not three numbers, stop and show it.
GROW_SECTORS=819200
grow() {
    local limits min cur max
    limits="$(hdiutil resize -limits "$1")"
    read -r min cur max <<< "$(echo "$limits" | tail -n 1)"
    case "$min$cur$max" in
        ''|*[!0-9]*) echo "$limits"; die "could not read the image size from hdiutil resize -limits" ;;
    esac
    hdiutil resize -sectors "$((cur + GROW_SECTORS))" "$1" > /dev/null
}

cmd_sign() {
    local layout="$1" out="$2" app
    need SIGN_NAME
    [ -f "$layout" ] || die "no disk image at $layout"
    rm -rf "$WORK"
    mkdir -p "$WORK"
    grow "$layout"
    attach "$layout" readwrite
    trap detach EXIT
    app="$MNT/Kestrel.app"
    [ -x "$app/Contents/MacOS/Kestrel" ] || die "the disk image holds no Kestrel.app"
    df -m "$MNT" | tail -n 1
    # Inside-out: every Mach-O file first (PyInstaller ships hundreds of .so and .dylib files that
    # --deep does not reach in the right order), then the main executable, then the bundle.
    while IFS= read -r -d '' f; do
        if file -b "$f" | grep -q "Mach-O"; then sign_one "$f" entitlements; fi
    done < <(find "$app/Contents" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) \
        ! -path "*/Contents/MacOS/Kestrel" -print0)
    # A .framework is then signed as a bundle, the deepest first, so its seal covers the files
    # signed above. Entitlements belong to executables, not frameworks.
    while IFS= read -r fw; do
        sign_one "$fw"
    done < <(find "$app/Contents" -type d -name "*.framework" |
        awk '{ print gsub("/", "/"), $0 }' | sort -rn | cut -d' ' -f2-)
    sign_one "$app/Contents/MacOS/Kestrel" entitlements
    sign_one "$app" entitlements
    codesign --verify --deep --strict --verbose=2 "$app"
    detach
    trap - EXIT
    rm -f "$out"
    hdiutil convert "$layout" -format UDZO -o "$out" > /dev/null
    sign_one "$out"
    echo "signed: $out"
}

cmd_notarize() {
    local dmg="$1" log="$WORK/notary.txt"
    need NOTARY_KEY_FILE
    need NOTARY_KEY_ID
    need NOTARY_ISSUER
    [ -f "$NOTARY_KEY_FILE" ] || die "the notary key file is missing"
    mkdir -p "$WORK"
    echo "==> notarizing (this waits for Apple, usually a few minutes)"
    if ! xcrun notarytool submit "$dmg" --key "$NOTARY_KEY_FILE" --key-id "$NOTARY_KEY_ID" \
        --issuer "$NOTARY_ISSUER" --wait > "$log" 2>&1; then
        cat "$log"
        notary_log "$log"
        die "notarytool submit failed; its output and Apple's log are above"
    fi
    cat "$log"
    if ! xcrun stapler staple "$dmg"; then
        notary_log "$log"
        die "stapling failed, so Apple did not accept the submission; Apple's log is above"
    fi
}

# Best effort: the first UUID notarytool printed is the submission; fetch Apple's log for it.
notary_log() {
    local id
    id="$(grep -Eo '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' "$1" \
        | head -n 1 || true)"
    if [ -n "$id" ]; then
        xcrun notarytool log "$id" --key "$NOTARY_KEY_FILE" --key-id "$NOTARY_KEY_ID" \
            --issuer "$NOTARY_ISSUER" || true
    fi
}

# production: the app's signature is valid and strict, the app and disk image are signed by the
#   expected name and team, the ticket is stapled, and Gatekeeper accepts both the disk image
#   (install) and the app (exec).
# test: the same signature and signer checks, and Gatekeeper must REFUSE the app: a test run can
#   never produce something macOS trusts (D-REL-612).
cmd_verify() {
    local mode="$1" dmg="$2" app rc
    need SIGN_NAME
    need TEAM_ID
    case "$mode" in production|test) ;; *) die "verify takes production or test, not $mode" ;; esac
    [ -f "$dmg" ] || die "no disk image at $dmg"
    mkdir -p "$WORK"
    # With assessments turned off, spctl accepts anything, so its verdict would prove nothing.
    local status
    status="$(spctl --status 2>&1 || true)"
    echo "spctl --status: $status"
    case "$status" in
        *disabled*) die "Gatekeeper assessments are turned off on this machine, so they cannot verify the build" ;;
    esac
    check_signer "$dmg"
    attach "$dmg" readonly
    trap detach EXIT
    app="$MNT/Kestrel.app"
    codesign --verify --deep --strict --verbose=2 "$app"
    check_signer "$app"
    if [ "$mode" = "production" ]; then
        xcrun stapler validate "$dmg" || die "no valid notarization ticket is stapled to $dmg"
        spctl -a -vvv -t install "$dmg" || die "Gatekeeper refused the disk image (output above)"
        spctl -a -vv -t exec "$app" || die "Gatekeeper refused Kestrel.app (output above)"
    else
        rc=0
        spctl -a -vv -t exec "$app" || rc=$?
        if [ "$rc" = 0 ]; then
            die "Gatekeeper accepted the test-signed app, so the test certificate is publicly trusted. Use an Apple Development certificate for the test (docs/RELEASING.md)."
        fi
        echo "Gatekeeper refused the test-signed app, as a test signature must be (exit $rc)."
    fi
    detach
    trap - EXIT
    echo "verified ($mode): $dmg"
}

cmd_smoke() {
    local dmg="$1"
    mkdir -p "$WORK"
    attach "$dmg" readonly
    trap detach EXIT
    KESTREL_DATA_HOME="$WORK/home" "$MNT/Kestrel.app/Contents/MacOS/Kestrel" version
    detach
    trap - EXIT
}

case "${1:-}" in
    sign) [ $# -eq 3 ] || die "usage: sign <layout.dmg> <out.dmg>"; cmd_sign "$2" "$3" ;;
    notarize) [ $# -eq 2 ] || die "usage: notarize <out.dmg>"; cmd_notarize "$2" ;;
    verify) [ $# -eq 3 ] || die "usage: verify production|test <out.dmg>"; cmd_verify "$2" "$3" ;;
    smoke) [ $# -eq 2 ] || die "usage: smoke <out.dmg>"; cmd_smoke "$2" ;;
    *) die "usage: sign_dmg.sh sign|notarize|verify|smoke ..." ;;
esac
