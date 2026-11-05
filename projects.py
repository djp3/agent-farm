"""
Which project a folder belongs to, for splitting the week's limit across projects.

A project is a git repo: a worktree counts toward the repo it came from, and two clones of
one remote count as one. A folder outside any repo counts toward the folder its session
started in when it lies under that, so subfolders stay together, and otherwise as itself.

Read-only. It looks for .git upward from the folder and reads the repo's config for the
origin remote. A URL can carry a token, so only its last two segments are kept, and nothing
from the file reaches the log.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_ORIGIN = re.compile(r'^\s*\[remote "origin"\][^\[]*?^\s*url\s*=\s*(\S+)', re.M)
_OWNER_REPO = re.compile(r"([^/:]+)/([^/:]+?)(?:\.git)?/?$")


def _linked_repo(folder: str, dot_git: str) -> tuple[str, str]:
    """A .git file points at the real git dir: a worktree's says where its main repo's is, in
    a commondir file, and a submodule is a repo of its own."""
    try:
        with open(dot_git, encoding="utf-8", errors="replace") as fh:
            git_dir = os.path.normpath(os.path.join(folder, fh.read().split("gitdir:", 1)[1].strip()))
    except (OSError, IndexError):
        return folder, dot_git
    try:
        with open(os.path.join(git_dir, "commondir"), encoding="utf-8", errors="replace") as fh:
            common = os.path.normpath(os.path.join(git_dir, fh.read().strip()))
    except OSError:
        return folder, git_dir
    return (os.path.dirname(common) if os.path.basename(common) == ".git" else common), common


def _find_repo(folder: str) -> tuple[str, str] | None:
    """(the repo's top folder, its git dir) for the repo a folder is in, or None."""
    d = os.path.normpath(folder)
    while True:
        dot_git = os.path.join(d, ".git")
        if os.path.isdir(dot_git):
            return d, dot_git
        if os.path.isfile(dot_git):
            return _linked_repo(d, dot_git)
        parent = os.path.dirname(d)
        if parent == d or not parent:
            return None
        d = parent


def _origin(git_dir: str) -> str:
    """"owner/repo" from the origin remote, or "" when there is none."""
    try:
        with open(os.path.join(git_dir, "config"), encoding="utf-8", errors="replace") as fh:
            found = _ORIGIN.search(fh.read())
    except OSError:
        return ""
    name = _OWNER_REPO.search(found.group(1)) if found else None
    return f"{name.group(1)}/{name.group(2)}" if name else ""


def _within(folder: str, parent: str) -> bool:
    return folder == parent or folder.startswith(parent.rstrip("/") + "/")


class Projects:
    """Remembers each folder's repo, so a folder is looked up once. Used from one thread."""

    def __init__(self) -> None:
        self._repos: dict[str, str] = {}        # folder -> "owner/repo", repo folder, or ""

    def _repo(self, folder: str) -> str:
        known = self._repos.get(folder)
        if known is None:
            found = _find_repo(folder)
            known = self._repos[folder] = (_origin(found[1]) or found[0]) if found else ""
        return known

    def key(self, cwd: str, launch: str = "") -> str:
        """The project of a message run in `cwd`, in a session that started in `launch`."""
        return self._repo(cwd) or (launch if launch and _within(cwd, launch) else cwd)


def project_label(key: str) -> str:
    """What the panel calls a project: the repo or folder's own name."""
    if key == str(Path.home()):
        return "~"
    return key.rstrip("/").rsplit("/", 1)[-1] or key
