#!/usr/bin/env python3
"""Before a build: is every library the app and its build need installed, at its pinned version?

    .venv/bin/python3 scripts/check_requirements.py requirements.txt requirements-build.txt

Run it with the Python the build uses: it looks at the libraries installed in the Python that
runs it. PyInstaller packs what it finds there, and when it cannot find a library it only warns
and goes on building. The app that comes of it fails on its first import ("No module named
'rich'"), on the Mac of whoever opens it. A checkout whose .venv has the build's requirements
and not the app's builds exactly that.

Every line of each file must be an exact pin of a release (name==1.2.3), with comments and
blank lines allowed: a line the check could not read would be a library it never looked at.
Names compare the way pip compares them (case, "-", "_" and "." alike), and so do versions, as
far as the check reads them: release numbers compare as numbers (1.02 is 1.2, and 1.2.0 is too),
and a local label on what is installed (1.2+local) is left out, as a pin without one takes any.
A pin of anything else (a pre-, post- or dev release, a local label, an epoch) is refused rather
than half-understood: pip's full rules live in the packaging library, which this cannot count on.

It needs nothing but Python's standard library. Exit 0 when every pin is installed at its
version, 1 naming each one that is missing or at another version and the pip line that installs
them, 2 when a file cannot be read or holds a line that is not an exact pin of a release.
"""
from __future__ import annotations

import os
import re
import sys
from importlib import metadata

NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
RELEASE = r"[0-9]+(?:\.[0-9]+)*"
PIN = re.compile(rf"({NAME})==({RELEASE})")
ANY_PIN = re.compile(rf"({NAME})==(\S+)")
INSTALLED = re.compile(rf"({RELEASE})(?:\+[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*)?")


class Unreadable(Exception):
    """A requirements file the check cannot vouch for."""


def pins(path: str) -> list[tuple[str, str]]:
    """Each (name, version) the file pins, in its order."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise Unreadable(f"{path}: cannot be read ({e.strerror})") from None
    found = []
    for n, line in enumerate(lines, 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = PIN.fullmatch(line)
        if not m:
            if ANY_PIN.fullmatch(line):
                raise Unreadable(f"{path}:{n}: the check reads only release numbers like 1.2.3 (no pre-, post- or "
                                 f"dev release, local label or epoch), so this pin would go unchecked: {line}")
            raise Unreadable(f"{path}:{n}: not an exact pin (name==version), so it would go unchecked: {line}")
        found.append((m.group(1), m.group(2)))
    return found


def _release(version: str) -> tuple[int, ...] | None:
    """A release's numbers as pip compares them: 1.02 is 1.2, 1.2.0 is 1.2, and 1.20 is not.
    A local label (1.2+local) is left out, since a pin without one takes any. None for anything
    that is not a release (1.2rc1, 1.2.post1, 1!1.2), which no pin the check reads matches."""
    m = INSTALLED.fullmatch(version)
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def problems(path: str) -> list[str]:
    """What is wrong with this Python for the pins in path, one line each."""
    found = []
    for name, want in pins(path):
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            found.append(f"{name} is not installed ({path} pins {want})")
            continue
        if _release(have) != _release(want):
            found.append(f"{name} is {have}, and {path} pins {want}")
    return found


def _python() -> str:
    """This Python as the person running the build would type it."""
    here = os.path.relpath(sys.executable)
    return here if not here.startswith("..") else sys.executable


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: " + __doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        found = {path: problems(path) for path in argv}
    except Unreadable as e:
        print(e, file=sys.stderr)
        return 2
    wrong = [path for path in argv if found[path]]
    if not wrong:
        print(f"every library pinned in {' and '.join(argv)} is installed at its version")
        return 0
    print(f"the build stops here: the Python it uses ({_python()}) does not have the libraries "
          f"pinned for it:", file=sys.stderr)
    for path in wrong:
        for line in found[path]:
            print(f"    {line}", file=sys.stderr)
    print("PyInstaller packs only what this Python has, and an app built without a library it "
          "imports fails as it opens. Install what is pinned with:", file=sys.stderr)
    print(f"    {_python()} -m pip install " + " ".join(f"-r {path}" for path in wrong), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
