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
