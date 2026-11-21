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
