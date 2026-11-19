"""The build refuses an app in which a binary needs a newer macOS than Info.plist declares
(scripts/check_min_macos.py, run by build_app.sh before signing)."""
import importlib.util
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_min_macos", ROOT / "scripts" / "check_min_macos.py")
check_min_macos = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_min_macos)

OTOOL = """\
dist/x (architecture arm64):
Load command 9
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 27.0
      sdk 27.2
Load command 10
      cmd LC_SOURCE_VERSION
  cmdsize 16
  version 0.0
Load command 11
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 6
    minos 28.0
      sdk 27.2
dist/x (architecture arm64e.x1):
Load command 7
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 26.0
      sdk 27.0
dist/x (architecture x86_64):
Load command 8
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 29.0
      sdk 11.0
"""

THIN = """\
dist/y:
Load command 8
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 25.1
      sdk 27.0
"""


def test_reads_the_macos_minimum_of_each_apple_silicon_slice_and_nothing_else():
    # not LC_SOURCE_VERSION's version, the Mac Catalyst record (platform 6), or the Intel slice
    assert check_min_macos.min_versions(OTOOL) == ["27.0", "26.0"]
    assert check_min_macos.min_versions(THIN) == ["25.1"]           # a thin file names no architecture


def test_versions_compare_as_numbers():
    v = check_min_macos.version
    assert v("27") == v("27.0") == v("27.0.0")
    assert v("10.13") < v("11.0") < v("26.4") < v("27.0") < v("27.0.1")


def swift_platform(package_swift: str) -> str | None:
    """The macOS version in Package.swift's platforms, in either form: .macOS("27.0") or .macOS(.v27)."""
    m = re.search(r'\.macOS\(\s*(?:"([\d.]+)"|\.v(\d+(?:_\d+)*))\s*\)', package_swift)
    return None if m is None else (m.group(1) or m.group(2).replace("_", "."))


def test_both_forms_of_the_swift_platform_are_read():
    assert swift_platform('platforms: [.macOS("27.0")],') == "27.0"
    assert swift_platform("platforms: [.macOS(.v28)],") == "28"
    assert swift_platform("platforms: [.macOS(.v10_15)],") == "10.15"
    assert swift_platform("platforms: [.iOS(.v17)],") is None


def test_the_declared_minimum_matches_the_swift_package():
    declared = plistlib.loads((ROOT / "macos" / "Info.plist").read_bytes())["LSMinimumSystemVersion"]
    swift = swift_platform((ROOT / "macos" / "Package.swift").read_text())
    assert swift is not None, "no .macOS(...) platform found in macos/Package.swift"
    v = check_min_macos.version
    assert v(declared) == v(swift), f"Info.plist declares {declared}, Package.swift {swift}"


def _fake_bundle(tmp_path, declared, otool_by_name, monkeypatch, executable=None):
    """A bundle of files that only start like Mach-O, with otool's output for each made up."""
    app = tmp_path / "fake.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    info = {"LSMinimumSystemVersion": declared} | ({"CFBundleExecutable": executable} if executable else {})
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    for name in otool_by_name:
        (app / "Contents" / "MacOS" / name).write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 28)
    monkeypatch.setattr(check_min_macos, "load_commands", lambda f: otool_by_name[f.name])
    return app


def _slice(arch, minos):
    return (f"x (architecture {arch}):\nLoad command 9\n      cmd LC_BUILD_VERSION\n  cmdsize 32\n"
            f" platform 1\n    minos {minos}\n      sdk 27.2\n")


def test_a_universal_binary_is_reported_with_its_neediest_slice(tmp_path, monkeypatch):
    app = _fake_bundle(tmp_path, "27.0", {"uni": _slice("arm64", "27.1") + _slice("arm64e", "28.0")}, monkeypatch)
    declared, newest, too_new, n = check_min_macos.check(app)
    assert too_new == [(Path("Contents/MacOS/uni"), "28.0")] and newest == "28.0"   # not 27.1, the first


def test_the_build_stops_when_info_plist_and_the_swift_executable_disagree(tmp_path, monkeypatch, capsys):
    # Info.plist raised to 28.0, Package.swift left at 27.0: no binary needs more than 28.0,
    # so only the executable's own minimum gives it away.
    app = _fake_bundle(tmp_path, "28.0", {"AgentFarm": _slice("arm64", "27.0")}, monkeypatch, executable="AgentFarm")
    assert check_min_macos.main(["check", str(app)]) == 1
    err = capsys.readouterr().err
    assert "Contents/MacOS/AgentFarm" in err and "records macOS 27.0" in err and "Package.swift" in err


def test_the_build_goes_on_when_they_agree(tmp_path, monkeypatch, capsys):
    app = _fake_bundle(tmp_path, "27", {"AgentFarm": _slice("arm64", "27.0")}, monkeypatch, executable="AgentFarm")
    assert check_min_macos.main(["check", str(app)]) == 0
    assert "the app's executable records the same minimum" in capsys.readouterr().out


def test_the_wrong_arguments_print_how_to_run_it(capsys):
    assert check_min_macos.main(["check"]) == 2
    assert capsys.readouterr().err.startswith("usage: scripts/check_min_macos.py <app bundle>")


def test_every_mach_o_header_is_recognized_and_nothing_else(tmp_path):
    heads = {"thin64": b"\xcf\xfa\xed\xfe", "thin32": b"\xce\xfa\xed\xfe", "fat": b"\xca\xfe\xba\xbe",
             "fat64": b"\xca\xfe\xba\xbf", "fat64-swapped": b"\xbf\xba\xfe\xca", "text": b"#!/b"}
    for name, head in heads.items():
        (tmp_path / name).write_bytes(head + b"\0" * 28)
    assert sorted(f.name for f in check_min_macos.binaries(tmp_path)) == sorted(set(heads) - {"text"})


def _bundle(tmp_path, declared):
    app = tmp_path / "x.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"LSMinimumSystemVersion": declared}))
    shutil.copy("/bin/ls", app / "Contents" / "MacOS" / "ls")              # a real Mach-O
    (app / "Contents" / "MacOS" / "notes.txt").write_text("not a binary")
    return app


@pytest.mark.skipif(shutil.which("otool") is None, reason="needs the Xcode command line tools")
def test_a_binary_that_needs_a_newer_macos_fails_the_build(tmp_path):
    app = _bundle(tmp_path, "10.0")
    declared, newest, too_new, n = check_min_macos.check(app)
    assert n == 1 and declared == "10.0"
    assert [str(p) for p, _ in too_new] == ["Contents/MacOS/ls"]
    run = subprocess.run(["python3", ROOT / "scripts" / "check_min_macos.py", app], capture_output=True, text=True)
    assert run.returncode == 1 and "Contents/MacOS/ls" in run.stderr


@pytest.mark.skipif(shutil.which("otool") is None, reason="needs the Xcode command line tools")
def test_a_file_otool_cannot_read_stops_the_build_with_its_name(tmp_path, capsys):
    app = _bundle(tmp_path, "99.0")
    odd = app / "Contents" / "Resources" / "Hello.class"          # a Java class file starts 0xcafebabe too
    odd.parent.mkdir()
    odd.write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x41" + b"\x00" * 24)
    assert check_min_macos.main(["check", str(app)]) == 1
    err = capsys.readouterr().err
    assert "Hello.class" in err and "Traceback" not in err


@pytest.mark.skipif(shutil.which("otool") is None, reason="needs the Xcode command line tools")
def test_a_bundle_within_its_declared_minimum_passes(tmp_path):
    app = _bundle(tmp_path, "99.0")
    _, newest, too_new, n = check_min_macos.check(app)
    assert n == 1 and too_new == [] and check_min_macos.version(newest) > (10, 0, 0)
    run = subprocess.run(["python3", ROOT / "scripts" / "check_min_macos.py", app], capture_output=True, text=True)
    assert run.returncode == 0 and "none needs a macOS newer than 99.0" in run.stdout
