"""A disk that fills up while Pudge writes one file (for tests of atomic writes).

Pudge writes through pudge.fsutil: a temporary file beside the target, then a rename.
Inside ``disk_full_while_writing(target)`` the write of ``target``'s temporary file stops
half way with ENOSPC; every other file is written normally. It yields the set of affected
file descriptors (empty: the code under test never tried to write ``target``).
"""

from __future__ import annotations

import errno
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest


@contextmanager
def disk_full_while_writing(target: Path) -> Iterator[set[int]]:
    from pudge import fsutil

    real_temporary, real_fdopen = fsutil.umask_temporary_file, fsutil.os.fdopen
    doomed: set[int] = set()

    def temporary_file(directory, prefix, suffix=".tmp"):
        fd, name = real_temporary(directory, prefix, suffix)
        if Path(directory) == Path(target).parent and prefix == f".{Path(target).name}.":
            doomed.add(fd)
        return fd, name

    class HalfWritten:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.handle.close()

        def write(self, data):
            self.handle.write(data[: len(data) // 2])
            raise OSError(errno.ENOSPC, "No space left on device")

    def fdopen(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        return HalfWritten(handle) if fd in doomed else handle

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fsutil, "umask_temporary_file", temporary_file)
        patch.setattr(fsutil.os, "fdopen", fdopen)
        yield doomed
