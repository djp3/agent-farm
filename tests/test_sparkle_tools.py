"""scripts/sparkle_tools.sh hands a release the tools it gives the update key to: the ones
SwiftPM already fetched with Sparkle's framework, each checked against its own pinned hash.
These run it, in a scratch checkout with a swift of their own, and see what it refuses."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sparkle_standins import shim

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sparkle_tools.sh"
VERSION = "2.10.1"
TOOLS = ("generate_appcast", "generate_keys", "sign_update")
ARTIFACT = Path("macos") / ".build" / "artifacts" / "sparkle" / "Sparkle"   # where SwiftPM unpacks Sparkle's package
REAL = ROOT / ARTIFACT / "bin"                                               # there once the app has been built here
RESOLVE = "package --only-use-versions-from-resolved-file resolve (in macos)"

SWIFT = """#!/bin/sh
# A swift for the tests: it resolves nothing over any network. It notes how it was called,
# and when $SWIFT_BRINGS names a folder it copies that to where SwiftPM would unpack Sparkle.
echo "$* (in $(basename "$PWD"))" >> "$SWIFT_LOG"
if [ -n "${SWIFT_BRINGS:-}" ]; then
    mkdir -p .build/artifacts/sparkle/Sparkle && cp -R "$SWIFT_BRINGS" .build/artifacts/sparkle/Sparkle/bin
fi
"""


@pytest.fixture
def checkout(tmp_path):
    """As much of a checkout as the script reads: itself, and the pin in macos/."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    (tmp_path / "macos").mkdir()
    (tmp_path / "macos" / "Package.resolved").write_text(json.dumps(
        {"pins": [{"identity": "sparkle", "state": {"version": VERSION}}]}))
    (tmp_path / "macos" / "Package.swift").write_text(
        f'        .package(url: "https://github.com/sparkle-project/Sparkle.git", from: "{VERSION}"),\n')
    shim(tmp_path / "bin" / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim(tmp_path / "bin" / "swift", SWIFT)
    return tmp_path


def run(checkout: Path, **env) -> subprocess.CompletedProcess:
    env = {"PATH": f"{checkout / 'bin'}:/usr/bin:/bin", "SWIFT_LOG": str(checkout / "swift.log"), **env}
    return subprocess.run(["/bin/bash", str(checkout / "scripts" / SCRIPT.name)], capture_output=True, text=True, env=env)


def tools(folder: Path, **contents) -> Path:
    """Tools in `folder`. A tool is the real one, when this checkout has been built, unless
    `contents` gives its bytes; None leaves it out."""
    folder.mkdir(parents=True)
    for tool in TOOLS:
        if tool in contents:
            if contents[tool] is not None:
                (folder / tool).write_bytes(contents[tool])
                (folder / tool).chmod(0o755)
        else:
            shutil.copy(REAL / tool, folder / tool)
    return folder


def place(checkout: Path, **contents) -> Path:
    """Tools where SwiftPM would have unpacked them."""
    return tools(checkout / ARTIFACT / "bin", **contents)


def resolved(checkout: Path) -> list:
    log = checkout / "swift.log"
    return log.read_text().splitlines() if log.exists() else []


needs_the_real_tools = pytest.mark.skipif(not all((REAL / t).is_file() for t in TOOLS),
                                          reason="Sparkle's tools are not under macos/.build/artifacts (the app was not built here)")
FAKE = {tool: b"not Sparkle's " + tool.encode() for tool in TOOLS}


# ---- the tools SwiftPM already has ----------------------------------------------------------------
@needs_the_real_tools
def test_the_tools_swiftpm_fetched_are_handed_back_and_nothing_is_fetched(checkout):
    place(checkout)
    done = run(checkout)
    assert done.returncode == 0, done.stderr
    assert Path(done.stdout.strip()) == (checkout / ARTIFACT).resolve()
    assert resolved(checkout) == [], "they were there: SwiftPM is not asked for anything"


def test_nothing_is_downloaded_a_second_time():
    """It used to fetch Sparkle's whole release (16 MB, 91 MB unpacked) for three tools that
    SwiftPM had already put on disk, byte for byte."""
    script = SCRIPT.read_text()
    assert "curl" not in script and "tar " not in script and ".sparkle-tools" not in script
    assert not (ROOT / "scripts" / "fetch_sparkle_tools.sh").exists()


@needs_the_real_tools
def test_a_checkout_never_built_has_its_packages_resolved_first(checkout):
    """A release wants the tools in preflight, before it builds, when macos/.build may not
    exist yet."""
    brought = tools(checkout / "from-swiftpm")
    done = run(checkout, SWIFT_BRINGS=str(brought))
    assert done.returncode == 0, done.stderr
    assert resolved(checkout) == [RESOLVE], "held to the pinned versions: it does not move them"
    assert Path(done.stdout.strip()) == (checkout / ARTIFACT).resolve()
    assert "resolving the Swift packages" in done.stderr


def test_when_resolving_brings_no_tools_it_says_to_delete_the_whole_cache(checkout):
    """SwiftPM notes what it has downloaded. With only macos/.build/artifacts deleted it
    believes the tools are still there, and resolving brings nothing back."""
    done = run(checkout)
    assert done.returncode == 1 and done.stdout == "" and resolved(checkout) == [RESOLVE]
    assert "left no Sparkle tools" in done.stderr and "Delete macos/.build (all of it" in done.stderr


# ---- every run checks every tool ------------------------------------------------------------------
def test_tools_that_are_not_sparkles_are_refused(checkout):
    """macos/.build/ is ignored by git, so the release's clean-tree check does not see it."""
    place(checkout, **FAKE)
    done = run(checkout)
    assert done.returncode == 1 and done.stdout == "", "no folder is handed to the release"
    assert f"bin/generate_appcast is not the generate_appcast of Sparkle {VERSION}" in done.stderr
    assert "refusing to use it" in done.stderr and resolved(checkout) == []


@needs_the_real_tools
@pytest.mark.parametrize("changed", TOOLS)
def test_one_tool_changed_among_the_real_ones_is_caught(checkout, changed):
    there = place(checkout)
    with open(there / changed, "ab") as f:
        f.write(b"\0")
    done = run(checkout)
    assert done.returncode == 1 and done.stdout == ""
    assert f"bin/{changed} is not the {changed} of Sparkle {VERSION}" in done.stderr


@needs_the_real_tools
def test_half_a_tool_is_refused(checkout):
    half = (REAL / "generate_appcast").read_bytes()[:4096]
    place(checkout, generate_appcast=half)
    done = run(checkout)
    assert done.returncode == 1 and done.stdout == "" and "generate_appcast is not the generate_appcast" in done.stderr


@needs_the_real_tools
def test_a_tool_that_is_not_there_is_said_to_be_missing(checkout):
    place(checkout, sign_update=None)
    done = run(checkout)
    assert done.returncode == 1 and "bin/sign_update is missing" in done.stderr and "delete macos/.build" in done.stderr


@needs_the_real_tools
def test_what_swiftpm_brings_is_checked_too(checkout):
    """SwiftPM checks its download against Sparkle's manifest. This check does not rest on
    that one."""
    brought = tools(checkout / "from-swiftpm", generate_keys=b"not Sparkle's generate_keys")
    done = run(checkout, SWIFT_BRINGS=str(brought))
    assert done.returncode == 1 and done.stdout == ""
    assert f"bin/generate_keys is not the generate_keys of Sparkle {VERSION}" in done.stderr


# ---- which version ------------------------------------------------------------------------------------
def broken_python(checkout: Path) -> None:
    shim(checkout / "bin" / "python3", "#!/bin/sh\nexit 1\n")


def test_when_the_pin_cannot_be_read_it_says_so_and_goes_by_the_manifest(checkout):
    """Under `set -e` an assignment takes the exit status of the command inside it: with a
    python3 that fails, the script used to end on that line, silently, before the fallback on
    the next line or the message after it."""
    broken_python(checkout)
    place(checkout, **FAKE)
    done = run(checkout)
    assert "could not read Sparkle's version from macos/Package.resolved" in done.stderr
    assert f"of Sparkle {VERSION}" in done.stderr, "it went on, with the version macos/Package.swift names"


def test_when_no_version_can_be_found_it_says_so(checkout):
    broken_python(checkout)
    (checkout / "macos" / "Package.swift").write_text("// no Sparkle here\n")
    done = run(checkout)
    assert done.returncode == 1 and done.stdout == "" and resolved(checkout) == []
    assert "could not determine the Sparkle version" in done.stderr


def test_a_version_whose_tools_are_not_pinned_is_refused_before_anything_is_resolved(checkout):
    (checkout / "macos" / "Package.resolved").write_text(json.dumps(
        {"pins": [{"identity": "sparkle", "state": {"version": "2.11.0"}}]}))
    place(checkout, **FAKE)
    done = run(checkout)
    assert done.returncode == 1 and "no pinned SHA-256 for the tools of Sparkle 2.11.0" in done.stderr
    assert resolved(checkout) == []
