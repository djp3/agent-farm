#!/bin/bash
# Print the folder that holds the tools of the Sparkle release the app links: its bin/ has
# generate_appcast, generate_keys and sign_update.
#
# Nothing is downloaded for them. SwiftPM already has them: the package Sparkle publishes for
# it carries the framework the app links and, beside it, these tools, and SwiftPM checks that
# download against the checksum in Sparkle's own manifest. They are under
# macos/.build/artifacts, where scripts/build_app.sh finds the framework. A checkout that has
# never been built has no such folder yet, so the packages are resolved first when it is
# missing: a release wants the tools in preflight, before it builds.
#
# generate_appcast is handed the private EdDSA key every installed copy trusts, so the tools
# must be exactly the files that were reviewed, each time they are used. SwiftPM's check is
# made once, at download. This one is made on every run, and does not rest on SwiftPM's: each
# tool is verified against its own pinned SHA-256. macos/.build/ is ignored by git, so
# nothing else would notice a tool that was changed or damaged where it lies.
#
# Bumping Sparkle means updating the version in macos/Package.swift, the pin in
# macos/Package.resolved, and the hashes below together.
set -euo pipefail
cd "$(dirname "$0")/.."

TOOLS="generate_appcast generate_keys sign_update"

# /bin/bash on macOS is 3.2 (no associative arrays), so a case table: <version>:<tool>.
pinned_sha256() {
    case "$1:$2" in
        2.10.1:generate_appcast) echo "cdc53c1da293c5f89648929678df2072f72a9f18d91744a06fcb8cc0e12a3219" ;;
        2.10.1:generate_keys)    echo "c37679f98d9121981cf7b2d18592fdc8b44931bffa604acf10c9e11791b6222e" ;;
        2.10.1:sign_update)      echo "43c249771bafc3aa581228abae00731a012d324691b8292860896635050be76b" ;;
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
# `|| true`: under `set -e` an assignment takes the exit status of the command inside it, so
# a python3 that fails would end the script here, with nothing said, before the lines below.
VER=${SPARKLE_VERSION:-$(resolved_version || true)}
if [ -z "$VER" ]; then
    echo "could not read Sparkle's version from macos/Package.resolved (is python3 working?); going by macos/Package.swift" >&2
    VER=$(sed -n 's/.*sparkle-project\/Sparkle.git", from: "\([0-9.]*\)".*/\1/p' macos/Package.swift || true)
fi
[ -n "$VER" ] || { echo "could not determine the Sparkle version" >&2; exit 1; }
[ -n "$(pinned_sha256 "$VER" generate_appcast)" ] \
    || { echo "no pinned SHA-256 for the tools of Sparkle $VER in $0; add them alongside the version bump" >&2; exit 1; }

# Where SwiftPM unpacked Sparkle's package: the folder whose bin/ holds the tools.
where() {
    local tool
    # -print -quit, not `| head -1`: under pipefail a find cut off by head fails the pipeline.
    tool=$(find macos/.build/artifacts -type f -name generate_appcast -path "*/bin/*" -print -quit 2>/dev/null || true)
    [ -n "$tool" ] && (cd "$(dirname "$tool")/.." && pwd)
    return 0
}
DIR=$(where)
if [ -z "$DIR" ]; then
    echo "Sparkle's tools are not under macos/.build/artifacts yet: resolving the Swift packages" >&2
    (cd macos && swift package --only-use-versions-from-resolved-file resolve >&2)   # the pinned versions, or fail
    DIR=$(where)
fi
# SwiftPM keeps its own note of what it has downloaded. With that folder deleted by hand it
# still believes the tools are there, and resolving brings nothing back.
[ -n "$DIR" ] || { echo "SwiftPM resolved the packages and left no Sparkle tools under macos/.build/artifacts. Delete macos/.build (all of it: it is only a cache), or run 'swift package reset' in macos/, and run this again" >&2; exit 1; }

for tool in $TOOLS; do
    WANT=$(pinned_sha256 "$VER" "$tool")
    [ -n "$WANT" ] || { echo "no pinned SHA-256 for Sparkle $VER's $tool in $0" >&2; exit 1; }
    [ -f "$DIR/bin/$tool" ] || { echo "$DIR/bin/$tool is missing; delete macos/.build (all of it: it is only a cache) and run this again" >&2; exit 1; }
    echo "$WANT  $DIR/bin/$tool" | shasum -a 256 -c --status \
        || { echo "$DIR/bin/$tool is not the $tool of Sparkle $VER (its SHA-256 differs from the pinned one); refusing to use it. Delete macos/.build (all of it: it is only a cache) to have SwiftPM fetch it again" >&2; exit 1; }
done
echo "$DIR"
