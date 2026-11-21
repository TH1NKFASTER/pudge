"""Which files the repository holds: git's list, not whatever sits in the working folder.

A developer's folder keeps files git does not track (leftovers of older versions such as
``rename_brand.py`` or ``docs/``, build output, editor files). Tests about the repository's
content ask git; outside a git checkout (an unpacked release) the folder holds exactly the
shipped files, so the folder itself is the answer.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=8)
def tracked_files(root: Path = ROOT) -> frozenset[str] | None:
    """Root-relative POSIX paths git tracks; None when ``root`` is not the top of a git checkout."""
    root = Path(root).resolve()
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
            return None
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=60, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return frozenset(name for name in listed.stdout.decode("utf-8", "surrogateescape").split("\0") if name)


def in_repository(relative: str, root: Path = ROOT) -> bool:
    """Whether the repository holds ``relative``: a file git tracks (and that was not deleted
    from the folder), or a folder with such files in it."""
    root = Path(root).resolve()
    relative = relative.strip("/")
    tracked = tracked_files(root)
    if tracked is None:
        return (root / relative).exists()
    folder = relative + "/"
    return any((path == relative or path.startswith(folder)) and (root / path).exists() for path in tracked)
