"""Size limits for unpacking EPUB, CBZ and subtitle archives.

A tiny zip can unpack to gigabytes (a "zip bomb"): a 0.18 MB EPUB used to take
the parser to 1.3 GB of memory because every member was read whole. Members are
now read with a cap that is checked against the declared size first and against
the bytes actually read (the declared size can lie). Archives that are processed
as a whole also get a total budget. The limits are far above any real book page,
manga page or subtitle file, so only a broken or hostile archive meets them.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from .i18n import err

MB = 1024 * 1024
# One image: an EPUB illustration, a manga page, a bitmap subtitle stream.
IMAGE_MEMBER_LIMIT = 256 * MB
# One (X)HTML/OPF/NCX/TXT document or text subtitle.
TEXT_MEMBER_LIMIT = 64 * MB
# Everything read from one EPUB or CBZ (and one volume copied out of a torrent zip).
BOOK_ARCHIVE_LIMIT = 4 * 1024 * MB
# Everything unpacked from one downloaded subtitle archive.
SUBTITLE_ARCHIVE_LIMIT = 1024 * MB

_CHUNK = MB


class ArchiveTooLargeError(ValueError):
    """An archive member, or the whole archive, unpacks to more than Pudge accepts."""


def _megabytes(limit: int) -> int:
    return max(1, int(limit) // MB)


class ArchiveBudget:
    """Uncompressed bytes still allowed for one archive."""

    def __init__(self, limit: int) -> None:
        self.limit = int(limit)
        self.used = 0

    def check(self, size: int) -> None:
        if self.used + max(0, int(size)) > self.limit:
            raise ArchiveTooLargeError(err("archive.tooLarge", limit=_megabytes(self.limit)))

    def take(self, size: int) -> None:
        self.check(size)
        self.used += max(0, int(size))


def _member_info(archive: zipfile.ZipFile, member: str | zipfile.ZipInfo) -> zipfile.ZipInfo:
    # KeyError for a missing member, exactly like ZipFile.read().
    return member if isinstance(member, zipfile.ZipInfo) else archive.getinfo(member)


def _too_large(info: zipfile.ZipInfo, limit: int) -> ArchiveTooLargeError:
    name = info.filename.rsplit("/", 1)[-1] or info.filename
    return ArchiveTooLargeError(err("archive.memberTooLarge", name=name, limit=_megabytes(limit)))


def _checked_info(
    archive: zipfile.ZipFile,
    member: str | zipfile.ZipInfo,
    limit: int,
    budget: ArchiveBudget | None,
) -> zipfile.ZipInfo:
    info = _member_info(archive, member)
    if info.file_size > limit:
        raise _too_large(info, limit)
    if budget is not None:
        budget.check(info.file_size)
    return info


def _chunks(
    archive: zipfile.ZipFile,
    member: str | zipfile.ZipInfo,
    limit: int,
    budget: ArchiveBudget | None,
):
    info = _checked_info(archive, member, limit, budget)
    total = 0
    with archive.open(info) as handle:
        while True:
            chunk = handle.read(min(_CHUNK, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise _too_large(info, limit)
            if budget is not None:
                budget.take(len(chunk))
            yield chunk


def read_member(
    archive: zipfile.ZipFile,
    member: str | zipfile.ZipInfo,
    *,
    limit: int,
    budget: ArchiveBudget | None = None,
) -> bytes:
    """``ZipFile.read`` that refuses a member unpacking to more than ``limit`` bytes."""
    info = _checked_info(archive, member, limit, budget)
    # ZipFile stops reading at the declared size (a member holding more fails its
    # CRC check), so this read is bounded; the length is checked again regardless.
    data = archive.read(member)
    if len(data) > limit:
        raise _too_large(info, limit)
    if budget is not None:
        budget.take(len(data))
    return data


def extract_member(
    archive: zipfile.ZipFile,
    member: str | zipfile.ZipInfo,
    destination: Path,
    *,
    limit: int,
    budget: ArchiveBudget | None = None,
) -> int:
    """Copy one member to ``destination`` with the same cap; no partial file is left behind."""
    written = 0
    try:
        with destination.open("wb") as handle:
            for chunk in _chunks(archive, member, limit, budget):
                handle.write(chunk)
                written += len(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return written


def tree_size(root: Path) -> int:
    total = 0
    for folder, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                continue
    return total


def run_extraction(
    command: list[str],
    root: Path,
    *,
    limit: int | None,
    timeout: float,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run an unpacking tool (bsdtar, 7-Zip) into ``root``; stop it once ``root`` outgrows ``limit``.

    Returns the exit code and the end of the tool's output. Raises
    ``ArchiveTooLargeError`` or ``subprocess.TimeoutExpired``; the caller owns
    ``root`` (a temporary directory) and removes what was unpacked. ``limit``
    None: no size limit (only the timeout).
    """
    with tempfile.TemporaryFile() as output:
        # Output goes to a file: a long listing must never fill a pipe and stall the tool.
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, env=env
        )
        started = time.monotonic()
        try:
            while True:
                try:
                    process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if limit is not None and tree_size(root) > limit:
                    raise ArchiveTooLargeError(err("archive.tooLarge", limit=_megabytes(limit)))
                if time.monotonic() - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
        except BaseException:
            process.kill()
            process.wait()
            raise
        if limit is not None and tree_size(root) > limit:
            raise ArchiveTooLargeError(err("archive.tooLarge", limit=_megabytes(limit)))
        size = output.seek(0, os.SEEK_END)
        output.seek(max(0, size - 4000))
        text = output.read().decode("utf-8", errors="replace")
    return int(process.returncode or 0), text
