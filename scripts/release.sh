#!/bin/bash
# Cut a release of the Mac app and publish it where Sparkle looks for it.
#
#   scripts/release.sh
#
# Reads VERSION, builds dist/agent-farm.app signed with a Developer ID, notarizes and
# staples it, zips it, writes a Sparkle appcast signed with the agent-farm EdDSA key,
# tags v<VERSION>, and creates the GitHub release with the zip and appcast.xml attached.
# Installed apps poll https://github.com/djp3/agent-farm/releases/latest/download/appcast.xml,
# so creating the release is what ships the update.
#
# One-time setup (README, "Releasing"):
#   - a "Developer ID Application" certificate in the login keychain
#   - notarization credentials: a notarytool keychain profile named agent-farm
#       xcrun notarytool store-credentials agent-farm --apple-id <id> --team-id <team> --password <app-specific>
#     or NOTARY_KEY_ID / NOTARY_ISSUER_ID / NOTARY_KEY_FILE (App Store Connect API key; what CI uses)
#   - the Sparkle key:  .sparkle-tools/<ver>/bin/generate_keys --account agent-farm
#     or SPARKLE_PRIVATE_KEY_FILE pointing at an exported key (what CI uses)
#   - gh logged in with push access to the repo
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(tr -d '[:space:]' < VERSION)
TAG="v$VERSION"
REPO=${GITHUB_REPO:-djp3/agent-farm}
NOTARY_PROFILE=${NOTARY_PROFILE:-agent-farm}
SPARKLE_ACCOUNT=${SPARKLE_ACCOUNT:-agent-farm}
APP=dist/agent-farm.app
ZIP=dist/agent-farm-$VERSION.zip

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { echo "release: $*" >&2; exit 1; }

step "preflight $TAG"
# Sign by certificate hash rather than by name: a keychain can hold two certificates with
# the same name (a renewed one beside the old), and codesign rejects an ambiguous name.
IDENTITY=${CODESIGN_IDENTITY:-$(security find-identity -v -p codesigning | awk '/Developer ID Application/ {print $2; exit}')}
[ -n "$IDENTITY" ] || die "no 'Developer ID Application' identity in the keychain (or set CODESIGN_IDENTITY)"
IDENTITY_NAME=$(security find-identity -v -p codesigning | awk -F'"' -v h="$IDENTITY" '$0 ~ h {print $2; exit}')
[ "${ALLOW_DIRTY:-0}" = 1 ] || git diff --quiet HEAD -- || die "working tree has uncommitted changes (ALLOW_DIRTY=1 overrides)"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists; bump VERSION first"
gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1 && die "release $TAG already exists on GitHub"
TOOLS=$(scripts/fetch_sparkle_tools.sh)
if [ -n "${NOTARY_KEY_FILE:-}" ]; then
    NOTARY_ARGS=(--key "$NOTARY_KEY_FILE" --key-id "${NOTARY_KEY_ID:?NOTARY_KEY_ID}" --issuer "${NOTARY_ISSUER_ID:?NOTARY_ISSUER_ID}")
else
    xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1 \
        || die "no notarytool keychain profile '$NOTARY_PROFILE'; create it with: xcrun notarytool store-credentials $NOTARY_PROFILE ..."
    NOTARY_ARGS=(--keychain-profile "$NOTARY_PROFILE")
fi
if [ -n "${SPARKLE_PRIVATE_KEY_FILE:-}" ]; then
    SPARKLE_KEY_ARGS=(--ed-key-file "$SPARKLE_PRIVATE_KEY_FILE")
else
    SPARKLE_KEY_ARGS=(--account "$SPARKLE_ACCOUNT")
fi
echo "identity: ${IDENTITY_NAME:-$IDENTITY} ($IDENTITY)"
echo "sparkle tools: $TOOLS"

step "build"
CODESIGN_IDENTITY="$IDENTITY" scripts/build_app.sh

step "notarize"
rm -f dist/notarize.zip
ditto -c -k --keepParent "$APP" dist/notarize.zip
xcrun notarytool submit dist/notarize.zip "${NOTARY_ARGS[@]}" --wait
xcrun stapler staple "$APP"
rm -f dist/notarize.zip
spctl --assess --type execute --verbose=2 "$APP"

step "archive + appcast"
rm -rf dist/appcast "$ZIP"
mkdir -p dist/appcast
ditto -c -k --keepParent "$APP" "$ZIP"
cp "$ZIP" dist/appcast/
"$TOOLS/bin/generate_appcast" "${SPARKLE_KEY_ARGS[@]}" \
    --download-url-prefix "https://github.com/$REPO/releases/download/$TAG/" \
    --link "https://github.com/$REPO/releases" \
    -o dist/appcast/appcast.xml dist/appcast
grep -q "sparkle:edSignature" dist/appcast/appcast.xml || die "appcast.xml carries no EdDSA signature"

step "publish $TAG"
git tag -a "$TAG" -m "agent-farm $VERSION"
git push origin "$TAG"
gh release create "$TAG" --repo "$REPO" --title "agent-farm $VERSION" --generate-notes \
    "$ZIP" dist/appcast/appcast.xml
echo
echo "released: https://github.com/$REPO/releases/tag/$TAG"
