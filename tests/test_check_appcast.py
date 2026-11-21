"""scripts/check_appcast.py holds a release to both of an appcast's signatures: the zip's, in
its enclosure, and the feed's own, at its end. The release script used to search the file for
"edSignature", which the first satisfies alone; these tell the two apart."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from sparkle_standins import appcast

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_appcast.py"
BY_SPARKLE = ROOT / "tests" / "data" / "appcast_signed_by_sparkle.xml"

spec = importlib.util.spec_from_file_location("check_appcast", SCRIPT)
check_appcast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_appcast)


def run(tmp_path, data: bytes) -> subprocess.CompletedProcess:
    path = tmp_path / "appcast.xml"
    path.write_bytes(data)
    return subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)


def test_an_appcast_signed_both_ways_holds(tmp_path):
    assert check_appcast.problems(appcast()) == []
    done = run(tmp_path, appcast())
    assert done.returncode == 0 and "the feed is signed" in done.stdout and done.stderr == ""


def test_what_sparkle_itself_wrote_holds():
    """A feed generate_appcast 2.10.1 wrote for an app that asks for a signed feed, with a
    throwaway key: the shape checked for here is the one Sparkle writes. It was made for
    agent-grange-viz's tests of the same change, hence its name, and is kept byte for byte:
    the length its signature claims counts every one of them."""
    data = BY_SPARKLE.read_bytes()
    assert check_appcast.problems(data) == []
    assert data.rstrip().endswith(b"-->") and b"sparkle-signatures" in data


def test_a_signed_zip_does_not_make_a_signed_feed(tmp_path):
    """What generate_appcast writes when the app does not set SURequireSignedFeed. The zip's
    signature is there, so a search for "edSignature" is satisfied, and nothing vouches for
    the version on offer or the notes."""
    data = appcast(feed_signed=False)
    assert b"sparkle:edSignature" in data, "the old check passed this"
    found = check_appcast.problems(data)
    assert len(found) == 1 and "the feed itself is not signed" in found[0] and "SURequireSignedFeed" in found[0]
    done = run(tmp_path, data)
    assert done.returncode == 1 and "the feed itself is not signed" in done.stderr


def test_a_signed_feed_does_not_make_a_signed_zip(tmp_path):
    """What generate_appcast writes when the key it was given is not the app's: no signature
    on the zip. (It then signs no feed either; this is that fault alone.)"""
    found = check_appcast.problems(appcast(zip_signed=False))
    assert len(found) == 1 and "9.9.9: the zip is not signed" in found[0]
    assert run(tmp_path, appcast(zip_signed=False)).returncode == 1


def test_neither_signature_is_two_faults():
    found = check_appcast.problems(appcast(zip_signed=False, feed_signed=False))
    assert len(found) == 2 and any("zip is not signed" in f for f in found) and any("feed itself" in f for f in found)


@pytest.mark.parametrize("covers", [-4, 4])
def test_a_feed_changed_after_it_was_signed_is_caught_by_its_length(covers):
    found = check_appcast.problems(appcast(covers=covers))
    assert len(found) == 1 and "changed after it was signed" in found[0]


def test_text_after_the_signature_means_it_is_not_at_the_end():
    found = check_appcast.problems(appcast() + b"<!-- added later -->\n")
    assert any("the feed itself is not signed" in f for f in found)


def test_an_update_without_its_notes_is_refused():
    found = check_appcast.problems(appcast(notes=False))
    assert found == ["9.9.9 carries no release notes"]


def test_what_cannot_be_read_is_not_passed(tmp_path):
    assert "it is not XML" in check_appcast.problems(b"<rss><channel>")[0]
    assert check_appcast.problems(b"<rss><channel></channel></rss>")[0] == "it offers no update"
    missing = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "nowhere.xml")], capture_output=True, text=True)
    assert missing.returncode == 2 and "nowhere.xml" in missing.stderr
    assert subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True).returncode == 2
