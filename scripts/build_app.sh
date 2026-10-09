#!/bin/bash
# Build dist/agent-farm.app from a checkout:
#   1. PyInstaller onedir of main.py            -> dist/python/agent-farm-core
#   2. SwiftPM release build of the host app     -> macos/.build/release/AgentFarm
#   3. assemble the bundle (binary, Sparkle.framework, the Python tree, icon, Info.plist)
#   4. sign it: ad-hoc by default, or with CODESIGN_IDENTITY="Developer ID Application: ..."
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
# ad-hoc. macOS privacy grants (Files and Folders, Automation) are tied to the signing
# identity, so local builds signed the same way as releases keep the user's permissions.
IDENTITY=${CODESIGN_IDENTITY:-$(security find-identity -v -p codesigning 2>/dev/null | awk '/Developer ID Application/ {print $2; exit}')}
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
( cd macos && swift build -c release --product AgentFarm 2>&1 | grep -E "error|warning: unre|Build complete" || true )
BIN=macos/.build/release/AgentFarm
[ -x "$BIN" ] || { echo "swift build did not produce $BIN" >&2; exit 1; }
SPARKLE_FW=$(find macos/.build/artifacts -type d -name Sparkle.framework -path "*macos-*" | head -1)
[ -d "$SPARKLE_FW" ] || { echo "Sparkle.framework not found under macos/.build/artifacts" >&2; exit 1; }

step "icon"
[ -f macos/Resources/AppIcon.icns ] || swift scripts/make_icon.swift macos/Resources/AppIcon.icns

step "assemble $APP ($VERSION)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Frameworks" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/AgentFarm"
# SwiftPM records an absolute rpath into .build for the Sparkle artifact; the app
# only needs the @executable_path/../Frameworks one set in Package.swift.
otool -l "$APP/Contents/MacOS/AgentFarm" | awk '/LC_RPATH/{f=1} f&&/ path /{print $2; f=0}' | while read -r rp; do
    case "$rp" in /*) install_name_tool -delete_rpath "$rp" "$APP/Contents/MacOS/AgentFarm" 2>/dev/null || true ;; esac
done
cp -R "$SPARKLE_FW" "$APP/Contents/Frameworks/"
cp -R dist/python/agent-farm-core "$APP/Contents/Resources/agent-farm-core"
cp macos/Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cp LICENSE "$APP/Contents/Resources/LICENSE"
sed "s/__VERSION__/$VERSION/g" macos/Info.plist > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

step "sign"
scripts/sign_app.sh "$APP" "$IDENTITY"

echo
echo "built $APP  version $VERSION  ($(du -sh "$APP" | cut -f1))"
