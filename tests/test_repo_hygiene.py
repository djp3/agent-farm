"""What the repository holds: no build output, wherever it was made. agent-frolic committed a
.build that SwiftPM had left at the top of its checkout, where only macos/.build was ignored."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_OUTPUT = (".build", "build", "dist", "__pycache__")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


@pytest.fixture(autouse=True)
def a_checkout():
    if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        pytest.skip("not a git checkout")


def test_no_build_output_is_tracked():
    tracked = git("ls-files").stdout.splitlines()
    held = [path for path in tracked if set(Path(path).parts[:-1]) & set(BUILD_OUTPUT)]
    assert held == []


@pytest.mark.parametrize("path", [".build/release/x", "macos/.build/artifacts/x", "scripts/.build/x",
                                  "build/pyinstaller/x", "dist/agent-farm.app/x", "grange/__pycache__/x.pyc"])
def test_build_output_is_ignored_wherever_it_is_made(path):
    assert git("check-ignore", "--no-index", "-q", path).returncode == 0, f"{path} would not be ignored"
