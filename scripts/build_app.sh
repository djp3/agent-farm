#!/bin/bash
# Build dist/agent-farm.app from a checkout:
#   1. PyInstaller onedir of main.py            -> dist/python/agent-farm-core
#   2. SwiftPM release build of the host app     -> macos/.build/release/AgentFarm
#   3. assemble the bundle (binary, Sparkle.framework, the Python tree, icon, Info.plist)
#   4. check the minimum macOS (scripts/check_min_macos.py): stop if a binary needs more
#      than Info.plist declares, or the Swift executable records a different minimum
#   5. sign it: ad-hoc by default, or with CODESIGN_IDENTITY="Developer ID Application: ..."
# Only a release (WITH_UPDATES=1, which scripts/release.sh sets) carries the update feed: a
# copy built by hand never looks for updates, so it never replaces itself with a release.
# Needs: .venv with requirements.txt + requirements-build.txt, Xcode command line tools.
# The version comes from VERSION and is used for both CFBundleShortVersionString and
# CFBundleVersion (Sparkle compares the latter, dotted numbers compare naturally).
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(tr -d '[:space:]' < VERSION)
PY=./.venv/bin/python3
APP=dist/agent-farm.app
# Sign with the first Developer ID Application certificate when the keychain has one (by
# hash, since a renewed certificate can sit beside the old one under the same name), else
# ad-hoc. macOS privacy grants (Files and Folders) are tied to the signing
# identity, so local builds signed the same way as releases keep the user's permissions.
# No awk here stops at its first match: under pipefail, a `security` cut off while it still
# had lines to write fails the pipeline, and the assignment would end the script unheard.
IDENTITY=${CODESIGN_IDENTITY:-$(security find-identity -v -p codesigning 2>/dev/null | awk '/Developer ID Application/ && !n++ {print $2}')}
IDENTITY=${IDENTITY:--}

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

if [ "${SKIP_PYTHON:-0}" = 1 ] && [ -x dist/python/agent-farm-core/agent-farm-core ]; then
    step "python core: reusing dist/python/agent-farm-core (SKIP_PYTHON=1)"
else
    step "python core: PyInstaller onedir ($($PY --version))"
    $PY -m PyInstaller --noconfirm --clean --log-level WARN \
        --distpath dist/python --workpath build/pyinstaller packaging/agent-farm-core.spec
fi

step "host app: swift build"
BIN=macos/.build/release/AgentFarm
rm -f "$BIN"                                   # so an existing binary can only come from this build
# Held to Package.resolved: left to itself SwiftPM moves a pin that the manifest has outgrown
# and rewrites the file, mid-build, and the app would link a version no commit names.
( cd macos && swift build --only-use-versions-from-resolved-file -c release --product AgentFarm 2>&1 \
    | grep -E "error|warning: unre|Build complete"
  [ "${PIPESTATUS[0]}" -eq 0 ] )              # keep the compiler's verdict, not grep's
[ -x "$BIN" ] || { echo "swift build did not produce $BIN" >&2; exit 1; }
# -print -quit, not `| head -1`: under pipefail a find cut off by head fails the pipeline,
# and the assignment would end the script with nothing said.
SPARKLE_FW=$(find macos/.build/artifacts -type d -name Sparkle.framework -path "*macos-*" -print -quit)
[ -d "$SPARKLE_FW" ] || { echo "Sparkle.framework not found under macos/.build/artifacts" >&2; exit 1; }

step "icon"
# Redrawn when missing or older than its script or artwork, so an icon change reaches the next build.
ICNS=macos/Resources/AppIcon.icns
if [ ! -f "$ICNS" ] || [ scripts/make_icon.swift -nt "$ICNS" ] || [ macos/Resources/rooster.png -nt "$ICNS" ]; then
    swift scripts/make_icon.swift macos/Resources/rooster.png "$ICNS"
fi

step "assemble $APP ($VERSION)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Frameworks" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/AgentFarm"
# Where the app looks for Sparkle.framework. It needs one place, the one Package.swift sets:
# its own Contents/Frameworks. SwiftPM adds @loader_path, beside the executable, where a
# `swift run` finds the framework and the finished app has nothing; and it has been known to
# record an absolute path into .build. Whatever else is there is taken out, and the build
# stops if the one that matters is not: the app would not start.
EXE="$APP/Contents/MacOS/AgentFarm"
NEEDED="@executable_path/../Frameworks"
rpaths() { otool -l "$EXE" | awk '/LC_RPATH/{f=1} f&&/ path /{print $2; f=0}'; }
for rp in $(rpaths); do
    [ "$rp" = "$NEEDED" ] || install_name_tool -delete_rpath "$rp" "$EXE"
done
[ "$(rpaths)" = "$NEEDED" ] || { echo "the app's executable looks for its frameworks in: $(rpaths | tr '\n' ' '); it must be $NEEDED alone" >&2; exit 1; }
cp -R "$SPARKLE_FW" "$APP/Contents/Frameworks/"
cp -R dist/python/agent-farm-core "$APP/Contents/Resources/agent-farm-core"
cp macos/Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cp LICENSE "$APP/Contents/Resources/LICENSE"
cp macos/Resources/NOTICE-noto-emoji.txt "$APP/Contents/Resources/NOTICE-noto-emoji.txt"
# Other people's programs inside the app come under licenses of their own. Those of the two
# the Swift host links ask that their text go with every copy: Sparkle's (its framework is
# carried whole) and SwiftTerm's (compiled into the executable). They are taken from the very
# packages this copy was built from, so they are the licenses of the versions inside it, and
# go beside the app's own: LICENSE-Sparkle, LICENSE-SwiftTerm. A build that cannot find one
# stops here, before anything is signed.
for package in Sparkle SwiftTerm; do
    [ -s "macos/.build/checkouts/$package/LICENSE" ] \
        || { echo "no license text for $package under macos/.build/checkouts" >&2; exit 1; }
    cp "macos/.build/checkouts/$package/LICENSE" "$APP/Contents/Resources/LICENSE-$package"
done
sed "s/__VERSION__/$VERSION/g" macos/Info.plist > "$APP/Contents/Info.plist"
if [ "${WITH_UPDATES:-0}" != 1 ]; then
    # Only a release updates itself (WITH_UPDATES=1, which release.sh sets). A copy built here
    # has the installed app's bundle id, and so its Sparkle preferences: with automatic
    # installs on, it would be replaced by the published app as it quit. Without a feed the
    # app starts no updater (AppDelegate.updatesItself) and Check for Updates stays grey.
    /usr/libexec/PlistBuddy -c "Delete :SUFeedURL" -c "Delete :SUEnableAutomaticChecks" \
        -c "Delete :SUScheduledCheckInterval" "$APP/Contents/Info.plist"
    echo "no update feed in this copy: it is not a release (WITH_UPDATES=1 makes one that updates itself)"
fi
printf 'APPL????' > "$APP/Contents/PkgInfo"

step "minimum macOS"
# No binary may need a newer macOS than Info.plist declares (Homebrew's Python follows the
# macOS it was built on, so this moves when the build Mac is upgraded). Run by itself: it
# needs only Python's standard library, so a SKIP_PYTHON=1 build works without a venv.
scripts/check_min_macos.py "$APP"

step "sign"
scripts/sign_app.sh "$APP" "$IDENTITY"

echo
echo "built $APP  version $VERSION  ($(du -sh "$APP" | cut -f1))"
