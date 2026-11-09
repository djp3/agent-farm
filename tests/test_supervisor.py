"""The supervisor restarts the app when a .py file changes at any depth, including a save that
renames a temporary file into place, and never for a file under the folders it skips."""
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent

import supervisor

ROOT = "/Users/someone/agent-farm"


def restarts(event) -> bool:
    seen = []
    supervisor.PythonFileChangeHandler(seen.append).dispatch(event)
    return bool(seen)


def test_a_python_file_anywhere_restarts_the_app():
    assert restarts(FileModifiedEvent(f"{ROOT}/main.py"))
    assert restarts(FileCreatedEvent(f"{ROOT}/tests/test_new.py"))
    assert restarts(FileModifiedEvent("main.py"))
    assert restarts(FileMovedEvent(f"{ROOT}/.main.py.tmp", f"{ROOT}/main.py"))     # an editor's atomic save


def test_other_files_and_skipped_folders_do_not():
    assert not restarts(FileModifiedEvent(f"{ROOT}/README.md"))
    assert not restarts(FileModifiedEvent(f"{ROOT}/.venv/lib/python3.14/site-packages/x.py"))
    assert not restarts(FileModifiedEvent(f"{ROOT}/dist/agent-farm.app/Contents/Resources/x.py"))
    assert not restarts(FileMovedEvent(f"{ROOT}/build/a.py", f"{ROOT}/build/b.py"))
