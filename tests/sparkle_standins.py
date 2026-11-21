"""What the tests of the update scripts share: a made-up appcast, and stand-ins for Sparkle's
tools, so that a script can be run for real with no keychain asked and no network reached."""
from __future__ import annotations

from pathlib import Path

SIGNATURE = "hKCB3wzZctOg9isMgUzf4m8b3jGOw/B9UIjT+m92mP1SaproXMd/bntCqfIk8yU9UGeQXwNA9XcK2ztR29QgCA=="


def shim(path: Path, text: str) -> None:
    """An executable stand-in at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def appcast(zip_signed: bool = True, feed_signed: bool = True, notes: bool = True, covers: int = 0) -> bytes:
    """A made-up appcast, as generate_appcast lays one out. `covers` is added to the length
    the feed's signature claims: anything but 0 is a file changed after it was signed."""
    signature = f' sparkle:edSignature="{SIGNATURE}"' if zip_signed else ""
    description = ('<description sparkle:format="markdown"><![CDATA[A made-up release.\n\n- one thing\n]]></description>'
                   if notes else "")
    body = (f'<?xml version="1.0" standalone="yes"?>'
            f'<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle" version="2.0">\n'
            f'    <channel>\n        <title>agent-farm</title>\n        <item>\n'
            f'            <title>9.9.9</title>\n'
            f'            <sparkle:version>9.9.9</sparkle:version>\n'
            f'            <sparkle:shortVersionString>9.9.9</sparkle:shortVersionString>\n'
            f'            <sparkle:minimumSystemVersion>27.0</sparkle:minimumSystemVersion>\n'
            f'            {description}\n'
            f'            <enclosure url="https://example.invalid/agent-farm-9.9.9.zip" length="18590525" '
            f'type="application/octet-stream"{signature}/>\n'
            f'        </item>\n    </channel>\n</rss>').encode()
    if feed_signed:
        body += f"<!-- sparkle-signatures:\nedSignature: {SIGNATURE}\nlength: {len(body) + covers}\n-->\n".encode()
    return body


# Sparkle's generate_keys: it notes how it was asked ($ASKED), and answers what it was told to
# ($ANSWER), or fails with $REFUSAL, or says there is no key. Like the real one, it says why it
# failed on standard output.
GENERATE_KEYS = """#!/bin/sh
echo "$*" >> "$ASKED"
[ -z "${REFUSAL:-}" ] || { echo "$REFUSAL"; exit 1; }
[ -n "${ANSWER:-}" ] || { echo "ERROR! No existing signing key found!"; exit 1; }
echo "$ANSWER"
"""
