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
