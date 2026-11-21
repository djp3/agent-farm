#!/bin/bash
# Print the public half of a Sparkle key that was exported to a file.
#     usage: scripts/sparkle_public_key.sh <private-key-file>
#
# scripts/release.sh asks this before it builds, when it is to sign with an exported key
# (SPARKLE_PRIVATE_KEY_FILE): the public half must be the app's SUPublicEDKey. For a key in
# the keychain Sparkle's own generate_keys -p says it; for a file none of its tools does.
#
# The file holds the key as generate_keys -x writes it: base64 of a 32-byte Ed25519 seed,
# or, from older versions of Sparkle, of 96 bytes whose last 32 are the public key. Only the
# public half is printed. The key is read by this script alone and is not passed on.
set -euo pipefail
FILE=${1:?usage: sparkle_public_key.sh <private-key-file>}
[ -r "$FILE" ] || { echo "sparkle key: cannot read $FILE" >&2; exit 1; }

SPARKLE_KEY_FILE="$FILE" swift - <<'SWIFT'
import CryptoKit
import Foundation

func fail(_ why: String) -> Never {
    FileHandle.standardError.write(Data("sparkle key: \(why)\n".utf8))
    exit(1)
}

let path = ProcessInfo.processInfo.environment["SPARKLE_KEY_FILE"] ?? ""
guard let text = try? String(contentsOfFile: path, encoding: .utf8),
      let secret = Data(base64Encoded: text.trimmingCharacters(in: .whitespacesAndNewlines)) else {
    fail("the file is not base64 text")
}
switch secret.count {
case 32:
    guard let key = try? Curve25519.Signing.PrivateKey(rawRepresentation: secret) else { fail("not an Ed25519 seed") }
    print(key.publicKey.rawRepresentation.base64EncodedString())
case 96:
    print(secret.suffix(32).base64EncodedString())
default:
    fail("the key is \(secret.count) bytes; a Sparkle key is 32, or 96 in the older form")
}
SWIFT
