#!/bin/bash
# Download the Sparkle release the app links against (generate_appcast, generate_keys,
# sign_update) into .sparkle-tools/<version>/ and print that directory.
#
# The tarball is verified against a pinned SHA-256 before anything is unpacked:
# generate_appcast is later handed the private EdDSA key every installed copy trusts,
# so the tool must be exactly the file that was reviewed. Bumping Sparkle means
# updating the version in macos/Package.swift and the hash below together.
set -euo pipefail
cd "$(dirname "$0")/.."

# /bin/bash on macOS is 3.2 (no associative arrays), so a case table.
pinned_sha256() {
    case "$1" in
        2.10.0) echo "c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c" ;;
        *) echo "" ;;
    esac
}

# The version actually linked is the one pinned in Package.resolved; Package.swift only
# gives the minimum.
resolved_version() {
    python3 - <<'PY' 2>/dev/null
import json
for pin in json.load(open("macos/Package.resolved"))["pins"]:
    if pin["identity"] == "sparkle":
        print(pin["state"]["version"]); break
PY
}
VER=${SPARKLE_VERSION:-$(resolved_version)}
[ -n "$VER" ] || VER=$(sed -n 's/.*sparkle-project\/Sparkle.git", from: "\([0-9.]*\)".*/\1/p' macos/Package.swift)
[ -n "$VER" ] || { echo "could not determine the Sparkle version" >&2; exit 1; }
SHA=$(pinned_sha256 "$VER")
[ -n "$SHA" ] || { echo "no pinned SHA-256 for Sparkle $VER in $0; add it alongside the version bump" >&2; exit 1; }

DIR=.sparkle-tools/$VER
if [ ! -x "$DIR/bin/generate_appcast" ]; then
    echo "fetching Sparkle $VER tools" >&2
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    curl -fsSL "https://github.com/sparkle-project/Sparkle/releases/download/$VER/Sparkle-$VER.tar.xz" -o "$TMP/sparkle.tar.xz"
    echo "$SHA  $TMP/sparkle.tar.xz" | shasum -a 256 -c --status \
        || { echo "Sparkle-$VER.tar.xz does not match the pinned SHA-256; refusing to unpack it" >&2; exit 1; }
    mkdir -p "$DIR"
    tar -xJf "$TMP/sparkle.tar.xz" -C "$DIR"
fi
echo "$PWD/$DIR"
