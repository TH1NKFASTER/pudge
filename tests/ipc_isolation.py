"""Short temporary Unix-socket paths independent of pytest's base directory."""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


@contextmanager
def short_socket_directory():
    # Unix sockets limit the entire pathname in bytes. A nested TMPDIR can
    # exceed that limit before mpv adds its socket filename.
    base = Path("/tmp")
    with TemporaryDirectory(prefix="p-ipc-", dir=base if base.is_dir() else None) as name:
        yield Path(name)
