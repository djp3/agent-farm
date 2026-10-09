#!/bin/bash
# Download the Sparkle release matching macos/Package.swift (generate_appcast,
# generate_keys, sign_update) into .sparkle-tools/<version>/ and print that directory.
set -euo pipefail
cd "$(dirname "$0")/.."
VER=${SPARKLE_VERSION:-$(sed -n 's/.*sparkle-project\/Sparkle.git", from: "\([0-9.]*\)".*/\1/p' macos/Package.swift)}
[ -n "$VER" ] || { echo "could not read the Sparkle version from macos/Package.swift" >&2; exit 1; }
DIR=.sparkle-tools/$VER
if [ ! -x "$DIR/bin/generate_appcast" ]; then
    echo "fetching Sparkle $VER tools" >&2
    mkdir -p "$DIR"
    curl -fsSL "https://github.com/sparkle-project/Sparkle/releases/download/$VER/Sparkle-$VER.tar.xz" | tar -xJ -C "$DIR"
fi
echo "$PWD/$DIR"
