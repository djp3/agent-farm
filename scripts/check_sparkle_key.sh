#!/bin/bash
# Is the key a release would sign with the one that installed copies trust?
#   scripts/check_sparkle_key.sh <tools>      # the folder scripts/sparkle_tools.sh prints
#
# scripts/release.sh asks this before it builds anything. The key is first used when the
# appcast is written, which is after the build and after Apple's notary has had its minutes:
# a key that is missing, or is not the app's, should not cost a whole run to find.
#
# It reads what release.sh reads: SPARKLE_ACCOUNT (agent-farm unless set), or
# SPARKLE_PRIVATE_KEY_FILE for a key exported to a file (what the GitHub Actions release uses).
# The keychain's key is asked for by account, always: a login keychain can hold another
# project's Sparkle key under no account name, and that one is never this app's. Only the
# public half is read from the keychain; a key file's public half is worked out from it by
# scripts/sparkle_public_key.sh, which passes nothing else on.
set -euo pipefail
TOOLS=${1:?usage: check_sparkle_key.sh <tools folder>}
ACCOUNT=${SPARKLE_ACCOUNT:-agent-farm}
cd "$(dirname "$0")/.."

WANT=$(/usr/libexec/PlistBuddy -c "Print :SUPublicEDKey" macos/Info.plist)
if [ -n "${SPARKLE_PRIVATE_KEY_FILE:-}" ]; then
    # Sparkle's tools take a key on standard input ("-") or from a pipe, and a release cannot:
    # it reads the key here, again to sign and again to verify, and a pipe is empty after the
    # first. Said as that, not as a file that is missing.
    if [ "$SPARKLE_PRIVATE_KEY_FILE" = "-" ] \
            || { [ -e "$SPARKLE_PRIVATE_KEY_FILE" ] && [ ! -f "$SPARKLE_PRIVATE_KEY_FILE" ]; }; then
        echo "SPARKLE_PRIVATE_KEY_FILE must be a file, not \"-\" or a pipe ($SPARKLE_PRIVATE_KEY_FILE): a release reads the key more than once. Export it to a file (generate_keys -x), or use the key in the keychain" >&2
        exit 1
    fi
    [ -f "$SPARKLE_PRIVATE_KEY_FILE" ] && [ -r "$SPARKLE_PRIVATE_KEY_FILE" ] \
        || { echo "SPARKLE_PRIVATE_KEY_FILE names no file that can be read: $SPARKLE_PRIVATE_KEY_FILE" >&2; exit 1; }
    HAVE=$(scripts/sparkle_public_key.sh "$SPARKLE_PRIVATE_KEY_FILE") \
        || { echo "the file $SPARKLE_PRIVATE_KEY_FILE does not hold a Sparkle key" >&2; exit 1; }
    [ "$HAVE" = "$WANT" ] \
        || { echo "the Sparkle key in $SPARKLE_PRIVATE_KEY_FILE is not the one in macos/Info.plist (SUPublicEDKey): installed copies would take nothing it signs" >&2; exit 1; }
    echo "sparkle key: the file $SPARKLE_PRIVATE_KEY_FILE, the one in Info.plist"
    exit 0
fi
# generate_keys says why it failed where its answer would have been, on standard output. "No
# key" is one thing it may say; a keychain that is locked, or access that was refused or
# cancelled, are others, and the key is there all the while. So its own words are passed on.
if ! HAVE=$("$TOOLS/bin/generate_keys" --account "$ACCOUNT" -p 2>&1); then
    case "$HAVE" in
        *"No existing signing key found"*)
            echo "no Sparkle key in the login keychain under the account '$ACCOUNT' (DEVELOPMENT.md, \"Releasing\")" >&2 ;;
        *)
            echo "the Sparkle key under the account '$ACCOUNT' could not be had from the login keychain. generate_keys said:" >&2
            printf '%s\n' "$HAVE" | sed 's/^/    /' >&2 ;;
    esac
    exit 1
fi
[ "$HAVE" = "$WANT" ] \
    || { echo "the Sparkle key under the account '$ACCOUNT' is not the one in macos/Info.plist (SUPublicEDKey): installed copies would take nothing it signs" >&2; exit 1; }
echo "sparkle key: the login keychain's, account $ACCOUNT, the one in Info.plist"
