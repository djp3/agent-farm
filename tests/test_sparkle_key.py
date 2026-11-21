"""scripts/check_sparkle_key.sh, which a release runs before it builds anything: is the key it
would sign with the one installed copies trust? These run it against a generate_keys of their
own, so no keychain is asked anything."""
import base64
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from sparkle_standins import GENERATE_KEYS, shim

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_sparkle_key.sh"
with open(ROOT / "macos" / "Info.plist", "rb") as _f:
    THE_APPS_KEY = plistlib.load(_f)["SUPublicEDKey"]
ANOTHER_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="        # made up: thirty-two zero bytes
# RFC 8032's first Ed25519 test vector: a published seed and the public key it makes.
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
SEEDS_KEY = base64.b64encode(bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")).decode()

@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    shutil.copy(ROOT / "scripts" / "sparkle_public_key.sh", tmp_path / "scripts" / "sparkle_public_key.sh")
    (tmp_path / "macos").mkdir()
    shutil.copy(ROOT / "macos" / "Info.plist", tmp_path / "macos" / "Info.plist")
    shim(tmp_path / "tools" / "bin" / "generate_keys", GENERATE_KEYS)
    return tmp_path


def run(checkout: Path, **env) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "ASKED": str(checkout / "asked"), **env}
    return subprocess.run(["/bin/bash", str(checkout / "scripts" / SCRIPT.name), str(checkout / "tools")],
                          capture_output=True, text=True, env=env)


def asked(checkout: Path) -> list:
    path = checkout / "asked"
    return path.read_text().splitlines() if path.exists() else []


def test_the_key_the_app_trusts_passes(checkout):
    done = run(checkout, ANSWER=THE_APPS_KEY)
    assert done.returncode == 0, done.stderr
    assert "account agent-farm" in done.stdout


def test_the_keychain_is_asked_by_this_apps_account_and_only_for_the_public_half(checkout):
    """A login keychain can hold another project's Sparkle key under no account name. Asked
    without an account, generate_keys answers with that one."""
    run(checkout, ANSWER=THE_APPS_KEY)
    assert asked(checkout) == ["--account agent-farm -p"]
    run(checkout, ANSWER=THE_APPS_KEY, SPARKLE_ACCOUNT="another-account")
    assert asked(checkout)[-1] == "--account another-account -p"


def test_a_key_that_is_not_the_apps_stops_the_release(checkout):
    done = run(checkout, ANSWER=ANOTHER_KEY)
    assert done.returncode == 1 and done.stdout == ""
    assert "is not the one in macos/Info.plist" in done.stderr and "'agent-farm'" in done.stderr


def test_no_key_under_the_account_stops_the_release(checkout):
    done = run(checkout)
    assert done.returncode == 1 and "no Sparkle key in the login keychain under the account 'agent-farm'" in done.stderr


def trusting(checkout: Path, key: str) -> None:
    """The checkout's app trusts this public key."""
    plist = checkout / "macos" / "Info.plist"
    with open(plist, "rb") as f:
        data = plistlib.load(f)
    data["SUPublicEDKey"] = key
    with open(plist, "wb") as f:
        plistlib.dump(data, f)


def test_a_key_file_is_held_to_the_apps_key_and_the_keychain_left_alone(checkout):
    """What the GitHub Actions release signs with. Its public half is worked out from it and
    compared, as the keychain's is."""
    key = checkout / "exported.key"
    key.write_text(base64.b64encode(SEED).decode() + "\n")
    trusting(checkout, SEEDS_KEY)
    done = run(checkout, SPARKLE_PRIVATE_KEY_FILE=str(key))
    assert done.returncode == 0, done.stderr
    assert str(key) in done.stdout and asked(checkout) == []
    trusting(checkout, THE_APPS_KEY)
    done = run(checkout, SPARKLE_PRIVATE_KEY_FILE=str(key))
    assert done.returncode == 1 and "is not the one in macos/Info.plist" in done.stderr and asked(checkout) == []


def test_a_key_file_in_sparkles_older_form_is_read_too(checkout):
    """Older versions of Sparkle exported 96 bytes, the last 32 of them the public key."""
    key = checkout / "exported.key"
    key.write_text(base64.b64encode(bytes(64) + base64.b64decode(SEEDS_KEY)).decode())
    trusting(checkout, SEEDS_KEY)
    assert run(checkout, SPARKLE_PRIVATE_KEY_FILE=str(key)).returncode == 0


def test_a_key_file_that_is_no_key_or_no_file_stops_the_release(checkout):
    key = checkout / "exported.key"
    key.write_text("made up")
    done = run(checkout, SPARKLE_PRIVATE_KEY_FILE=str(key))
    assert done.returncode == 1 and "does not hold a Sparkle key" in done.stderr
    assert str(SEED.hex()) not in done.stderr + done.stdout
    gone = run(checkout, SPARKLE_PRIVATE_KEY_FILE=str(checkout / "nowhere.key"))
    assert gone.returncode == 1 and "names no file that can be read" in gone.stderr and asked(checkout) == []


def test_only_the_public_half_of_a_key_file_is_ever_printed(checkout):
    key = checkout / "exported.key"
    key.write_text(base64.b64encode(SEED).decode())
    done = subprocess.run(["/bin/bash", str(checkout / "scripts" / "sparkle_public_key.sh"), str(key)],
                          capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert done.returncode == 0 and done.stdout.strip() == SEEDS_KEY
    assert base64.b64encode(SEED).decode() not in done.stdout + done.stderr
