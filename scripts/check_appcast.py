#!/usr/bin/env python3
"""Refuse an appcast that is not signed in both of the ways the app relies on.

    scripts/check_appcast.py dist/appcast/appcast.xml

An appcast can carry two signatures, and they vouch for different things:

  • each update's <enclosure> has sparkle:edSignature, the signature of the zip. The app
    checks it before it unpacks an update (SUVerifyUpdateBeforeExtraction);
  • the feed itself ends in a "sparkle-signatures" comment, the signature of everything
    before it: the versions on offer, the macOS each needs, where each zip is, the release
    notes. The app takes no feed without it (SURequireSignedFeed).

Sparkle's generate_appcast writes the second only when the app in the archive asks for a
signed feed, and writes neither when the key it was given is not the one in the app. A feed
with the first and not the second looks signed to anything that only searches for
"edSignature", which is what scripts/release.sh used to do.

This checks the shape: that both are there, that the feed's signature says it covers exactly
the bytes before it, and that every update carries its notes. Whether the signature is the
right one is for Sparkle's sign_update --verify, which release.sh runs after this.
It needs nothing but Python's standard library. Exit 0 when the appcast holds, 1 with the
reasons when it does not, 2 when it cannot be read.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET

SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
FEED_SIGNATURE = re.compile(rb"<!--\s*sparkle-signatures:\s*\nedSignature:\s*(\S+)\s*\nlength:\s*(\d+)\s*\n-->\s*\Z")


def problems(data: bytes) -> list:
    """What is wrong with this appcast, in words; nothing when it holds."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return [f"it is not XML: {e}"]
    found = []
    items = root.findall("./channel/item")
    if not items:
        found.append("it offers no update")
    for item in items:
        name = item.findtext(f"{{{SPARKLE}}}shortVersionString") or item.findtext("title") or "an update"
        enclosure = item.find("enclosure")
        if enclosure is None:
            found.append(f"{name} has no enclosure: no zip to download")
        elif not (enclosure.get(f"{{{SPARKLE}}}edSignature") or "").strip():
            found.append(f"{name}: the zip is not signed (its enclosure has no sparkle:edSignature)")
        notes = item.find("description")
        if notes is None or not (notes.text or "").strip():
            found.append(f"{name} carries no release notes")
    signed = FEED_SIGNATURE.search(data)
    if signed is None:
        found.append("the feed itself is not signed: it does not end in a sparkle-signatures comment. "
                     "generate_appcast writes one only when the app sets SURequireSignedFeed")
    elif int(signed.group(2)) != signed.start():
        found.append(f"the feed's signature covers {int(signed.group(2))} bytes and {signed.start()} come before it: "
                     f"the file was changed after it was signed")
    return found


def main(argv: list) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        with open(argv[1], "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"check_appcast: {e}", file=sys.stderr)
        return 2
    found = problems(data)
    for line in found:
        print(f"check_appcast: {argv[1]}: {line}", file=sys.stderr)
    if not found:
        print(f"{argv[1]}: the zip is signed, the feed is signed, and the notes are in it")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
