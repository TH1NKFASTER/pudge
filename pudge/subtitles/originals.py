"""The original subtitle files of an episode, offered in mpv beside the prepared ones.

User: "let the subtitles start with the computed ones, but just in case give every variant
there is (without changes, just the originals)". When an episode's subtitles are prepared,
every text subtitle file found for it (Jimaku, local folders) is copied unchanged next to the
durable prepared subtitles; mpv adds them as extra tracks that are not selected.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Iterable

ORIGINALS_DIRNAME = "originals"
MAX_ORIGINALS = 12
MAX_ORIGINAL_BYTES = 8 * 1024 * 1024
ORIGINAL_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".vtt"})


def originals_root(database_path: Path) -> Path:
    """``<database folder>/prepared-subtitles/originals``: not under the evictable cache."""
    return Path(database_path).expanduser().resolve().parent / "prepared-subtitles" / ORIGINALS_DIRNAME


def originals_dir(root: Path, video: Path) -> Path:
    digest = hashlib.sha1(str(Path(video).expanduser().resolve()).encode("utf-8")).hexdigest()[:24]
    return Path(root) / digest


def list_originals(root: Path, video: Path) -> list[Path]:
    folder = originals_dir(root, video)
    try:
        files = [path for path in folder.iterdir() if path.is_file() and not path.name.startswith(".")]
    except OSError:
        return []
    return sorted(files, key=lambda path: path.name.casefold())


def _distinct(files: Iterable[Path]) -> list[tuple[Path, bytes]]:
    chosen: list[tuple[Path, bytes]] = []
    seen: set[str] = set()
    for value in files:
        path = Path(value)
        try:
            if path.suffix.casefold() not in ORIGINAL_EXTENSIONS or not path.is_file():
                continue
            size = path.stat().st_size
            if size <= 0 or size > MAX_ORIGINAL_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha1(data).hexdigest()
        if digest in seen:
            continue  # the same file again (an archive and its unpacked copy)
        seen.add(digest)
        chosen.append((path, data))
        if len(chosen) >= MAX_ORIGINALS:
            break
    return chosen


def save_originals(root: Path, video: Path, files: Iterable[Path]) -> list[Path]:
    """Keep byte-for-byte copies of ``files`` (best first) for ``video``; the latest set wins.

    Names stay as they were, numbered so mpv lists them in this order.
    Nothing is rewritten when the set did not change.
    """
    chosen = _distinct(files)
    if not chosen:
        return list_originals(root, video)
    wanted = {f"{index:02d} {path.name}": data for index, (path, data) in enumerate(chosen, start=1)}
    folder = originals_dir(root, video)
    current = list_originals(root, video)
    try:
        if sorted(path.name for path in current) == sorted(wanted) and all(
            path.read_bytes() == wanted[path.name] for path in current
        ):
            return current
    except OSError:
        pass
    temporary = folder.with_name(f".{folder.name}.{time.time_ns()}.tmp")
    try:
        temporary.mkdir(parents=True)
        for name, data in wanted.items():
            (temporary / name).write_bytes(data)
        previous: Path | None = None
        if folder.exists():
            previous = folder.with_name(f".{folder.name}.{time.time_ns()}.old")
            os.replace(folder, previous)
        try:
            os.replace(temporary, folder)
        except OSError:
            # The new set could not take the name: the previous originals come back
            # (they used to be deleted first, and a failed rename left none at all).
            if previous is not None and not folder.exists():
                os.replace(previous, folder)
            raise
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)
    except OSError:
        shutil.rmtree(temporary, ignore_errors=True)
        return list_originals(root, video)
    return list_originals(root, video)
