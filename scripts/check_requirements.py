#!/usr/bin/env python3
"""Before a build: is every library the app and its build need installed, at its pinned version?

    .venv/bin/python3 scripts/check_requirements.py requirements.txt requirements-build.txt

Run it with the Python the build uses: it looks at the libraries installed in the Python that
runs it. PyInstaller packs what it finds there, and when it cannot find a library it only warns
and goes on building. The app that comes of it fails on its first import ("No module named
'rich'"), on the Mac of whoever opens it. A checkout whose .venv has the build's requirements
and not the app's builds exactly that.

Every line of each file must be an exact pin (name==version), with comments and blank lines
allowed: a line the check could not read would be a library it never looked at. Names compare
the way pip compares them (case, "-", "_" and "." alike).

It needs nothing but Python's standard library. Exit 0 when every pin is installed at its
version, 1 naming each one that is missing or at another version and the pip line that installs
them, 2 when a file cannot be read or holds a line that is not an exact pin.
"""
from __future__ import annotations

import os
import re
import sys
from importlib import metadata

PIN = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==([0-9][A-Za-z0-9.+!-]*)")


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
            raise Unreadable(f"{path}:{n}: not an exact pin (name==version), so it would go unchecked: {line}")
        found.append((m.group(1), m.group(2)))
    return found


def _release(version: str) -> tuple:
    """1.2 and 1.2.0 are one version to pip; 1.2 and 1.20 are not."""
    parts = version.split(".")
    while len(parts) > 1 and parts[-1] == "0":
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
