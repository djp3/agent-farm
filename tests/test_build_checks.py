"""The build stops before it makes an app that cannot start.

PyInstaller packs the libraries it finds in the Python it runs in, and when one is missing it
only warns and builds on: the app it made failed on its first import ("No module named 'rich'")
for someone whose .venv had the build's requirements and not the app's. scripts/build_app.sh now
looks for every pinned library first (scripts/check_requirements.py), and starts the core it
built once before it makes an app around it. Every library name here is made up."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check_requirements.py"
_spec = importlib.util.spec_from_file_location("check_requirements", CHECK)
check_requirements = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_requirements)


def _installed(site: Path, name: str, version: str) -> None:
    """A library as importlib.metadata finds one: its dist-info, which is all the check reads."""
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")


@pytest.fixture
def site(tmp_path):
    site = tmp_path / "site"
    _installed(site, "frobnicate", "1.2")
    _installed(site, "widget_kit", "3.0.0")
    return site


def _check(tmp_path, site, files: dict):
    """The check, run on these requirements files (name: text) with the libraries in site."""
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return subprocess.run([sys.executable, str(CHECK), *files], capture_output=True, text=True,
                          cwd=tmp_path, env={"PYTHONPATH": str(site), "PATH": "/usr/bin:/bin"})


def test_every_pin_installed_at_its_version_passes(tmp_path, site):
    """Comments and blank lines are skipped, names compare as pip compares them, 3.0 is 3.0.0."""
    done = _check(tmp_path, site, {"requirements.txt": "# what the app needs\nFrobnicate==1.2   # a note\n\n",
                                   "requirements-build.txt": "widget-kit==3.0\n"})
    assert done.returncode == 0, done.stderr
    assert "installed at its version" in done.stdout


def test_a_missing_library_stops_it_naming_the_library_and_the_line_that_installs_it(tmp_path, site):
    done = _check(tmp_path, site, {"requirements.txt": "frobnicate==1.2\nnot-here-lib==2.0\n",
                                   "requirements-build.txt": "widget-kit==3.0.0\n"})
    assert done.returncode == 1
    assert "not-here-lib is not installed (requirements.txt pins 2.0)" in done.stderr
    assert "an app built without a library it imports fails as it opens" in done.stderr
    assert "-m pip install -r requirements.txt\n" in done.stderr        # and only the file that needs it
    assert "frobnicate" not in done.stderr


def test_a_library_at_another_version_stops_it_too(tmp_path, site):
    done = _check(tmp_path, site, {"requirements.txt": "frobnicate==1.20\n", "requirements-build.txt": "widget-kit==3.1\n"})
    assert done.returncode == 1
    assert "frobnicate is 1.2, and requirements.txt pins 1.20" in done.stderr
    assert "widget-kit is 3.0.0, and requirements-build.txt pins 3.1" in done.stderr
    assert "-m pip install -r requirements.txt -r requirements-build.txt" in done.stderr


def test_release_numbers_compare_as_numbers_the_way_pip_compares_them():
    release = check_requirements._release
    assert release("1.02") == release("1.2") == release("1.2.0") == release("1.2+local.3")
    assert release("1.2") != release("1.20")
    for not_a_release in ("1.2rc1", "1.2-rc1", "1.2.post1", "1.2.dev0", "1!1.2", "v1.2", "1.2+"):
        assert release(not_a_release) is None, not_a_release


def test_an_equal_release_written_another_way_passes(tmp_path):
    """pip takes 1.02 for 1.2, and 1.2+local for a pin of 1.2 (a pin with no local label takes any)."""
    site = tmp_path / "site"
    _installed(site, "gizmo", "1.2")
    _installed(site, "sprocket", "1.2+local.3")
    done = _check(tmp_path, site, {"requirements.txt": "gizmo==1.02\nsprocket==1.2\n"})
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("installed", ["1.2rc1", "1.2.post1", "1.2.dev0", "1!1.2"])
def test_a_pre_post_or_dev_release_installed_is_not_the_release_pinned(tmp_path, installed):
    site = tmp_path / "site"
    _installed(site, "gizmo", installed)
    done = _check(tmp_path, site, {"requirements.txt": "gizmo==1.2\n"})
    assert done.returncode == 1
    assert f"gizmo is {installed}, and requirements.txt pins 1.2" in done.stderr


@pytest.mark.parametrize("version", ["1.0+build", "1.0rc1", "1.0-rc1", "1.0.post1", "1.0.dev2", "1!1.0"])
def test_a_pin_of_anything_but_a_release_is_refused_not_half_understood(tmp_path, site, version):
    """1.0+build and 1.0+build.0 are different builds, 1.0rc1 and 1.0-rc1 one pre-release: pip's
    rules for those live in the packaging library, so the check refuses them rather than guess."""
    done = _check(tmp_path, site, {"requirements.txt": f"widget-kit==3.0\ngizmo=={version}\n"})
    assert done.returncode == 2
    assert "requirements.txt:2: the check reads only release numbers like 1.2.3" in done.stderr
    assert f"so this pin would go unchecked: gizmo=={version}" in done.stderr


@pytest.mark.parametrize("line", ["frobnicate>=1.2", "frobnicate", "-r other.txt", "frobnicate==1.2; sys_platform == 'darwin'"])
def test_a_line_it_cannot_read_as_an_exact_pin_is_refused_not_skipped(tmp_path, site, line):
    done = _check(tmp_path, site, {"requirements.txt": f"widget-kit==3.0\n{line}\n"})
    assert done.returncode == 2
    assert "requirements.txt:2: not an exact pin" in done.stderr and "would go unchecked" in done.stderr


def test_a_file_it_cannot_read_is_said_so(tmp_path, site):
    done = subprocess.run([sys.executable, str(CHECK), "nowhere.txt"], capture_output=True, text=True,
                          cwd=tmp_path, env={"PYTHONPATH": str(site), "PATH": "/usr/bin:/bin"})
    assert done.returncode == 2 and "nowhere.txt: cannot be read" in done.stderr


def test_the_repositorys_own_requirements_are_all_exact_pins():
    """Each line of both files is read as a pin, so none of them goes unchecked; the app's
    own libraries and the build's are among them."""
    app = dict(check_requirements.pins(str(ROOT / "requirements.txt")))
    build = dict(check_requirements.pins(str(ROOT / "requirements-build.txt")))
    assert "textual" in app and "psutil" in app
    assert "pyinstaller" in build


def test_the_lock_pins_each_library_where_the_requirements_do():
    """A release on GitHub installs from requirements.lock and then builds, and the build checks
    the pins in requirements*.txt: a lock left behind by a bumped pin would stop that release
    after its runner had set everything up. pip-compile writes both from the same pins."""
    import re
    norm = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    lock = {norm(m.group(1)): m.group(2) for m in
            re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", (ROOT / "requirements.lock").read_text(), re.M)}
    for path in ("requirements.txt", "requirements-build.txt"):
        for name, version in check_requirements.pins(str(ROOT / path)):
            assert lock.get(norm(name)) == version, \
                f"{path} pins {name}=={version}, requirements.lock has {lock.get(norm(name))}: regenerate the lock"


# --- build_app.sh's own lines for the Python core, run against stand-ins -----------------------

NO_RICH = ('Traceback (most recent call last):\n  File "main.py", line 28, in <module>\n'
           "ModuleNotFoundError: No module named 'rich'\n")


def _core_step() -> str:
    """build_app.sh's own lines from CORE= to the line that says the core starts."""
    build = (ROOT / "scripts" / "build_app.sh").read_text().splitlines()
    first = next(i for i, line in enumerate(build) if line.startswith("CORE="))
    last = next(i for i, line in enumerate(build) if line.startswith('echo "$CORE starts"'))
    return "\n".join(build[first:last + 1])


def _core(path: Path, starts: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "echo 'usage: agent-farm-core [-h]'\nexit 0\n" if starts else f"cat >&2 <<'EOF'\n{NO_RICH}EOF\nexit 1\n"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _build(tmp_path, site, *, core_starts=True, requirements="frobnicate==1.2\n", venv=True, env=None):
    """A checkout in tmp_path: the check script, made-up requirements, a .venv Python that runs
    the check with the real one and stands in for PyInstaller by leaving a core behind."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(CHECK, tmp_path / "scripts" / "check_requirements.py")
    (tmp_path / "requirements.txt").write_text(requirements)
    (tmp_path / "requirements-build.txt").write_text("widget-kit==3.0\n")
    _core(tmp_path / "core-standin", core_starts)
    if venv:
        py = tmp_path / ".venv" / "bin" / "python3"
        py.parent.mkdir(parents=True)
        py.write_text(f"""#!/bin/bash
case "$1" in
    --version) echo "Python 3.14.0" ;;
    -m) echo "$*" >> pyinstaller-ran
        mkdir -p dist/python/agent-farm-core && cp core-standin dist/python/agent-farm-core/agent-farm-core ;;
    *) PYTHONPATH="{site}" exec "{sys.executable}" "$@" ;;
esac
""")
        py.chmod(0o755)
    script = f"set -euo pipefail\nPY=./.venv/bin/python3\nstep() {{ echo \"== $*\"; }}\n{_core_step()}\necho went-on\n"
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, cwd=tmp_path,
                          env={"PATH": "/usr/bin:/bin", **(env or {})})


def test_a_build_with_everything_installed_and_a_core_that_starts_goes_on(tmp_path, site):
    done = _build(tmp_path, site)
    assert done.returncode == 0 and "went-on" in done.stdout, done.stderr
    assert "PyInstaller" in (tmp_path / "pyinstaller-ran").read_text()
    assert "dist/python/agent-farm-core/agent-farm-core starts" in done.stdout


def test_a_missing_library_stops_the_build_before_pyinstaller_runs(tmp_path, site):
    done = _build(tmp_path, site, requirements="frobnicate==1.2\nrich-standin==14.0\n")
    assert done.returncode == 1 and "went-on" not in done.stdout
    assert "rich-standin is not installed" in done.stderr and "-m pip install -r requirements.txt" in done.stderr
    assert not (tmp_path / "pyinstaller-ran").exists()


def test_a_core_that_cannot_start_stops_the_build_with_what_it_said(tmp_path, site):
    """The app this would have made is the one that said "No module named 'rich'" on opening."""
    done = _build(tmp_path, site, core_starts=False)
    assert done.returncode == 1 and "went-on" not in done.stdout
    assert "    ModuleNotFoundError: No module named 'rich'" in done.stderr
    assert "cannot start (what it said is above), so no app was built around it" in done.stderr


def test_a_reused_core_is_started_too(tmp_path, site):
    """SKIP_PYTHON=1 skips PyInstaller and the check of the Python it would have used, not the
    core: an old one that cannot start stops the build as a new one would."""
    _core(tmp_path / "dist" / "python" / "agent-farm-core" / "agent-farm-core", starts=False)
    done = _build(tmp_path, site, venv=False, env={"SKIP_PYTHON": "1"})
    assert done.returncode == 1 and "went-on" not in done.stdout
    assert "reusing dist/python/agent-farm-core/agent-farm-core" in done.stdout
    assert "No module named 'rich'" in done.stderr
    assert not (tmp_path / "pyinstaller-ran").exists()


def test_without_a_venv_the_build_says_how_to_make_one(tmp_path, site):
    done = _build(tmp_path, site, venv=False)
    assert done.returncode == 1 and "went-on" not in done.stdout
    assert "no Python at ./.venv/bin/python3" in done.stderr
    assert "python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-build.txt" in done.stderr
