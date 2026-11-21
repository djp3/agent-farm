"""How the Mac app updates itself, as its Info.plist, Package.swift and build scripts set it up.
What a release does with them, and what it refuses, is tests/test_release.py's to show: it
runs scripts/release.sh."""
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text()


def info() -> dict:
    with open(ROOT / "macos" / "Info.plist", "rb") as f:
        return plistlib.load(f)


def test_the_app_takes_no_feed_that_is_not_signed():
    """Without SURequireSignedFeed only the zip is signed: whoever could change appcast.xml
    could rewrite the notes or hide an update, and the app would believe it. Sparkle will not
    start with that key set and SUVerifyUpdateBeforeExtraction missing."""
    plist = info()
    assert plist["SURequireSignedFeed"] is True
    assert plist["SUVerifyUpdateBeforeExtraction"] is True
    assert plist["SUPublicEDKey"] and plist["SUFeedURL"].endswith("/releases/latest/download/appcast.xml")


def test_closing_the_monitors_window_quits_the_app_whatever_else_is_open():
    """applicationShouldTerminateAfterLastWindowClosed waits for the last window: with
    Sparkle's update window open, closing the monitor left the app running with no monitor."""
    swift = read("macos", "Sources", "AgentFarm", "AppDelegate.swift")
    close = swift[swift.index("func windowWillClose(_ note: Notification)"):]
    close = close[:close.index("\n    }\n")]
    assert "guard (note.object as? NSWindow) === window else { return }" in close, "that window, not any"
    assert "NSApp.terminate(nil)" in close
    assert "w.isReleasedWhenClosed = false" in swift, "the delegate keeps the window it holds"


def test_sparkle_is_signed_by_its_own_recipe_inside_out():
    """sparkle-project.org/documentation/sandboxing, "Code Signing": only the downloader keeps
    the entitlements it ships with. Autoupdate's name Sparkle's project, not this one."""
    sign = read("scripts", "sign_app.sh")
    steps = [line.strip() for line in sign.splitlines() if line.startswith("sign ")]
    sparkle = [step for step in steps if "$FW" in step]
    assert sparkle == ['sign "$FW/Versions/B/XPCServices/Installer.xpc"',
                       'sign --preserve-metadata=entitlements "$FW/Versions/B/XPCServices/Downloader.xpc"',
                       'sign "$FW/Versions/B/Autoupdate"',
                       'sign "$FW/Versions/B/Updater.app"',
                       'sign "$FW"']
    assert steps.index('sign "$FW"') < steps.index('sign "$APP"'), "the framework before the app that carries it"
    assert "--deep" not in " ".join(steps)


def test_sparkles_version_is_pinned_once_and_the_build_keeps_to_it():
    """Package.swift gives a minimum, Package.resolved the version linked, and
    scripts/sparkle_tools.sh the hashes of that version's tools: the three move together."""
    import json
    import re
    pins = json.loads(read("macos", "Package.resolved"))["pins"]
    version = next(pin["state"]["version"] for pin in pins if pin["identity"] == "sparkle")
    script = read("scripts", "sparkle_tools.sh")
    for tool in ("generate_appcast", "generate_keys", "sign_update"):
        assert re.search(rf'^\s*{re.escape(version)}:{tool}\)\s+echo "[0-9a-f]{{64}}" ;;$', script, re.M), \
            f"no pinned SHA-256 for the {tool} of Sparkle {version}, the version the app links"
    package = read("macos", "Package.swift")
    least = re.search(r'sparkle-project/Sparkle\.git", from: "([0-9.]+)"', package).group(1)
    assert tuple(map(int, least.split("."))) <= tuple(map(int, version.split("."))), \
        f"macos/Package.swift asks for Sparkle {least} or later and macos/Package.resolved pins {version}"
    assert "swift build --only-use-versions-from-resolved-file" in read("scripts", "build_app.sh"), \
        "the build does not move the pin"
    assert "swift package --only-use-versions-from-resolved-file resolve" in script


def test_only_a_release_looks_for_updates():
    """A copy built by hand has the installed app's bundle id, and so its Sparkle preferences:
    with automatic installs on, it would be replaced by the published app as it quit."""
    build = read("scripts", "build_app.sh")
    left_out = build[build.index('if [ "${WITH_UPDATES:-0}" != 1 ]; then'):]
    left_out = left_out[:left_out.index("\nfi\n")]
    assert '-c "Delete :SUFeedURL"' in left_out
    assert build.index('macos/Info.plist > "$APP/Contents/Info.plist"') < build.index('if [ "${WITH_UPDATES:-0}"') \
        < build.index('scripts/sign_app.sh "$APP"'), "taken out of the copy's own plist, before it is sealed"
    assert "WITH_UPDATES=1 CODESIGN_IDENTITY=" in read("scripts", "release.sh")
    swift = read("macos", "Sources", "AgentFarm", "AppDelegate.swift")
    assert 'updatesItself: Bool { Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") != nil }' in swift
    setup = swift[swift.index("private func setupUpdater()"):]
    assert setup.index("guard updatesItself else { return }") < setup.index("SPUStandardUpdaterController(")


def test_only_a_swift_run_lets_the_environment_choose_what_the_app_runs():
    """AGENT_FARM_CORE is for development. A local build is signed like a release (and holds
    the user's privacy grants), and has no feed: having no feed must not let it in."""
    swift = read("macos", "Sources", "AgentFarm", "AppDelegate.swift")
    dev = swift[swift.index("private var isDevelopmentRun: Bool {"):]
    assert dev[:dev.index("}")].split("{", 1)[1].strip() == "Bundle.main.bundleIdentifier == nil"
    assert 'if isDevelopmentRun, let p = ProcessInfo.processInfo.environment["AGENT_FARM_CORE"]' in swift


def test_the_app_looks_for_its_frameworks_in_one_place_or_is_not_built():
    build = read("scripts", "build_app.sh")
    assert 'NEEDED="@executable_path/../Frameworks"' in build
    checked = build.index('[ "$(rpaths)" = "$NEEDED" ] || {')
    assert checked < build.index('cp -R "$SPARKLE_FW" "$APP/Contents/Frameworks/"') < build.index('scripts/sign_app.sh "$APP"')
    assert "@executable_path/../Frameworks" in read("macos", "Package.swift"), "where the app finds the framework it carries"


def test_the_licenses_of_what_the_app_carries_go_with_every_copy():
    """Sparkle's framework is carried whole and SwiftTerm is compiled in; each license asks
    that its text go with every copy. The app only named them."""
    build = read("scripts", "build_app.sh")
    copied = build.index('cp "macos/.build/checkouts/$package/LICENSE" "$APP/Contents/Resources/LICENSE-$package"')
    assert "for package in Sparkle SwiftTerm; do" in build
    assert build.index('|| { echo "no license text for $package') < copied < build.index('scripts/sign_app.sh "$APP"'), \
        "there, or no build; and sealed by the signature"
    built = ROOT / "dist" / "agent-farm.app" / "Contents" / "Resources"
    if (built / "LICENSE-Sparkle").is_file():            # an app was built here: look at it
        assert "Permission is hereby granted" in (built / "LICENSE-Sparkle").read_text()
        assert "Miguel de Icaza" in (built / "LICENSE-SwiftTerm").read_text()


def _rpath_step() -> str:
    """build_app.sh's own lines that keep the one rpath, from rpaths() to the check after it."""
    build = read("scripts", "build_app.sh").splitlines()
    first = next(i for i, line in enumerate(build) if line.startswith("rpaths() {"))
    last = next(i for i, line in enumerate(build) if line.startswith('[ "$(rpaths)" = "$NEEDED" ]'))
    return "\n".join(build[first:last + 1])


def _executable(tmp_path, *rpaths: str):
    import shutil
    import subprocess
    if not shutil.which("cc"):
        import pytest
        pytest.skip("no C compiler to make an executable with rpaths")
    exe = tmp_path / "exe"
    flags = [f"-Wl,-rpath,{rp}" for rp in rpaths]
    subprocess.run(["cc", "-x", "c", "-", "-o", str(exe), *flags], input=b"int main(void){return 0;}\n", check=True)
    return exe


def _keep_one_rpath(exe):
    import subprocess
    script = f'set -euo pipefail\nEXE="{exe}"\nNEEDED="@executable_path/../Frameworks"\n{_rpath_step()}\n'
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)


def _rpaths_of(exe) -> list:
    import subprocess
    lines = subprocess.run(["otool", "-l", str(exe)], capture_output=True, text=True, check=True).stdout.splitlines()
    return [lines[i + 2].split("path ", 1)[1].rsplit(" (offset", 1)[0]
            for i, line in enumerate(lines) if line.strip() == "cmd LC_RPATH"]


def test_an_rpath_with_a_space_in_it_is_taken_out_whole(tmp_path):
    """otool prints "path /tmp/My Checkout/... (offset 12)": awk's $2 was "/tmp/My", which
    install_name_tool could not find, and the build stopped."""
    spaced = str(tmp_path / "My Checkout" / "macos" / ".build" / "release")
    exe = _executable(tmp_path, "@loader_path", spaced, "@executable_path/../Frameworks")
    assert spaced in _rpaths_of(exe)
    done = _keep_one_rpath(exe)
    assert done.returncode == 0, done.stderr
    assert _rpaths_of(exe) == ["@executable_path/../Frameworks"]


def test_without_the_rpath_it_needs_the_build_stops_and_says_where_it_looked(tmp_path):
    exe = _executable(tmp_path, "@loader_path", str(tmp_path / "My Checkout"))
    done = _keep_one_rpath(exe)
    assert done.returncode == 1
    assert "must look for its frameworks in @executable_path/../Frameworks alone" in done.stderr
