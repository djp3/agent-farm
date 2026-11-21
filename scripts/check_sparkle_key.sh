#!/bin/bash
# Is the key a release would sign with the one that installed copies trust?
#   scripts/check_sparkle_key.sh <tools> --account <name>         # the key in the login keychain
#   scripts/check_sparkle_key.sh <tools> --ed-key-file <file>     # a key exported to a file
# <tools> is the folder scripts/sparkle_tools.sh prints.
#
# scripts/release.sh asks this before it builds anything. The key is first used when the
# appcast is written, which is after the build and after Apple's notary has had its minutes:
# a key that is missing, or is not the app's, should not cost a whole run to find. (And there
# it would only leave the signature out: generate_appcast warns and goes on.)
#
# Which key is the release's to say, once: it hands this the very arguments it will hand
# Sparkle's tools to sign with, so the key vouched for here is the key that signs. This reads
# no setting of its own for it. The keychain's key is asked for by account, always: a login
# keychain can hold another project's Sparkle key under no account name, and that one is never
# this app's. Only the public half is read from the keychain; a key file's public half is
# worked out from it by scripts/sparkle_public_key.sh, which passes nothing else on.
set -euo pipefail
USAGE="usage: check_sparkle_key.sh <tools folder> (--account <name> | --ed-key-file <file>)"
TOOLS=${1:?$USAGE}
HOW=${2:?$USAGE}
WHICH=${3:?$USAGE}
# What was named from where this was called is made whole before moving to the checkout:
# the tools folder, and a key file ("-" is no file's name, and is refused below as it is).
case "$TOOLS" in /*) ;; *) TOOLS="$PWD/$TOOLS" ;; esac
case "$HOW" in
    --account) ;;
    --ed-key-file) case "$WHICH" in /*|-) ;; *) WHICH="$PWD/$WHICH" ;; esac ;;
    *) echo "$USAGE" >&2; exit 2 ;;
esac
cd "$(dirname "$0")/.."

WANT=$(/usr/libexec/PlistBuddy -c "Print :SUPublicEDKey" macos/Info.plist)
if [ "$HOW" = "--ed-key-file" ]; then
    # Sparkle's tools take a key on standard input ("-") or from a pipe, and a release cannot:
    # it reads the key here, again to sign and again to verify, and a pipe is empty after the
    # first. Said as that, not as a file that is missing.
    if [ "$WHICH" = "-" ] || { [ -e "$WHICH" ] && [ ! -f "$WHICH" ]; }; then
        echo "the key must be a file, not \"-\" or a pipe ($WHICH): a release reads the key more than once. Export it to a file (generate_keys -x), or use the key in the keychain" >&2
        exit 1
    fi
    [ -f "$WHICH" ] && [ -r "$WHICH" ] \
        || { echo "the key file names no file that can be read: $WHICH" >&2; exit 1; }
    HAVE=$(scripts/sparkle_public_key.sh "$WHICH") \
        || { echo "the file $WHICH does not hold a Sparkle key" >&2; exit 1; }
    [ "$HAVE" = "$WANT" ] \
        || { echo "the Sparkle key in $WHICH is not the one in macos/Info.plist (SUPublicEDKey): installed copies would take nothing it signs" >&2; exit 1; }
    echo "sparkle key: the file $WHICH, the one in Info.plist"
    exit 0
fi
# generate_keys says why it failed where its answer would have been, on standard output. "No
# key" is one thing it may say; a keychain that is locked, or access that was refused or
# cancelled, are others, and the key is there all the while. So its own words are passed on.
if ! HAVE=$("$TOOLS/bin/generate_keys" --account "$WHICH" -p 2>&1); then
    case "$HAVE" in
        *"No existing signing key found"*)
            echo "no Sparkle key in the login keychain under the account '$WHICH' (DEVELOPMENT.md, \"Releasing\")" >&2 ;;
        *)
            echo "the Sparkle key under the account '$WHICH' could not be had from the login keychain. generate_keys said:" >&2
            printf '%s\n' "$HAVE" | sed 's/^/    /' >&2 ;;
    esac
    exit 1
fi
[ "$HAVE" = "$WANT" ] \
    || { echo "the Sparkle key under the account '$WHICH' is not the one in macos/Info.plist (SUPublicEDKey): installed copies would take nothing it signs" >&2; exit 1; }
echo "sparkle key: the login keychain's, account $WHICH, the one in Info.plist"
