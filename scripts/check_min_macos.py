#!/usr/bin/env python3
"""Refuse an app bundle in which any binary needs a newer macOS than the bundle declares.

    scripts/check_min_macos.py dist/agent-farm.app

Info.plist's LSMinimumSystemVersion is what Finder checks before it opens the app, and what
the update feed tells Sparkle. Every Mach-O file also records the oldest macOS it runs on
(its minos), chosen by whoever built it. Homebrew builds its Python for the macOS it is
built on, so after a macOS upgrade the next build would quietly need the new macOS while
still declaring the old one. build_app.sh runs this before signing, so it stops the build.
"""
from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",          # 64- and 32-bit, both byte orders, universal
               b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}          # universal with 64-bit offsets (lipo -fat64)


def version(v: str) -> tuple[int, ...]:
    """'27.0' -> (27, 0, 0), so '27' and '27.0' compare equal."""
    parts = [int(p) for p in v.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


MACOS_PLATFORM = {"1", "MACOS", "macos"}       # LC_BUILD_VERSION's platform for macOS (6 is Mac Catalyst)


def min_versions(otool_l: str) -> list[str]:
    """The oldest macOS each slice that runs here records in `otool -l` output: LC_BUILD_VERSION's
    minos for the macOS platform, or the older LC_VERSION_MIN_MACOSX's version. The app is
    Apple-silicon only, so Intel slices never run, and neither do other platforms' records (the
    Mac Catalyst one in a zippered library); their minimums don't count."""
    out, cmd, platform, arch = [], None, None, None
    for line in otool_l.splitlines():
        words = line.split()
        if line.endswith(":") and not line[:1].isspace():      # "path (architecture arm64):", or "path:"
            _, named, rest = line.rpartition(" (architecture ")
            arch, cmd = (rest[:-2] if named else None), None
        elif len(words) >= 2 and words[0] == "cmd":
            cmd, platform = words[1], None
        elif len(words) >= 2 and cmd == "LC_BUILD_VERSION" and words[0] == "platform":
            platform = words[1]
        elif len(words) >= 2 and (cmd, words[0]) in (("LC_BUILD_VERSION", "minos"),
                                                     ("LC_VERSION_MIN_MACOSX", "version")):
            runs_here = arch is None or arch.startswith("arm64")    # a thin file names no architecture
            macos = cmd == "LC_VERSION_MIN_MACOSX" or platform in MACOS_PLATFORM
            if runs_here and macos:
                out.append(words[1])
            cmd = None
    return out


def binaries(app: Path):
    for f in sorted(app.rglob("*")):
        if f.is_file() and not f.is_symlink():
            with open(f, "rb") as fh:
                if fh.read(4) in MACHO_MAGIC:
                    yield f


def load_commands(f: Path) -> str:
    """`otool -l` for every architecture in the file."""
    return subprocess.run(["otool", "-arch", "all", "-l", str(f)],
                          capture_output=True, text=True, check=True).stdout


def check(app: Path) -> tuple[str, str, list[tuple[Path, str]], int]:
    """(declared minimum, newest minimum any binary needs, binaries that need more than
    declared with the most each needs, binaries checked)."""
    declared = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())["LSMinimumSystemVersion"]
    newest, too_new, n = "0", [], 0
    for f in binaries(app):
        n += 1
        found = min_versions(load_commands(f))
        if not found:
            continue
        need = max(found, key=version)          # a universal binary needs what its neediest slice needs
        if version(need) > version(newest):
            newest = need
        if version(need) > version(declared):
            too_new.append((f.relative_to(app), need))
    return declared, newest, too_new, n


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    declared, newest, too_new, n = check(Path(argv[1]))
    if not too_new:
        print(f"{n} binaries checked: none needs a macOS newer than {declared}, the declared minimum "
              f"(the newest any needs is {newest})")
        return 0
    print(f"Info.plist declares macOS {declared} (LSMinimumSystemVersion), but {len(too_new)} "
          f"of {n} binaries need a newer macOS:", file=sys.stderr)
    for path, v in too_new[:10]:
        print(f"  {path}: macOS {v}", file=sys.stderr)
    if len(too_new) > 10:
        print(f"  ... and {len(too_new) - 10} more", file=sys.stderr)
    print("Build them for an older macOS, or raise the minimum: LSMinimumSystemVersion in "
          "macos/Info.plist, the platform in macos/Package.swift, and the README.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
