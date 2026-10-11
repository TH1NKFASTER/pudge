"""One Pudge window per data directory.

The first window holds an exclusive lock in the data directory. A second launch
does not open another writer on the same database; it asks the first window to
come to the front through a local socket and exits. A dev copy with its own
PUDGE_HOME has its own data directory, so both can still run side by side.
"""

from __future__ import annotations

import fcntl
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable

from .branding import DATA_DIR

_LOGGER = logging.getLogger(__name__)
_FOCUS_MESSAGE = b"focus\n"


def _socket_path(root: Path) -> Path:
    """The focus socket; a short /tmp name when the data path is too long for AF_UNIX."""
    path = root / "app.sock"
    if len(os.fsencode(path)) < 100:
        return path
    import hashlib

    return Path("/tmp") / f"pudge-{hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:16]}.sock"


class InstanceLock:
    def __init__(self, handle, root: Path) -> None:
        self._handle = handle
        self.root = root
        self.socket_path = _socket_path(root)
        self._server: socket.socket | None = None

    def serve_focus_requests(self, on_focus: Callable[[], None]) -> bool:
        """Listen for "focus" from later launches (blocks in accept: no polling)."""
        path = self.socket_path
        try:
            path.unlink(missing_ok=True)  # we hold the lock, so it is stale
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(4)
        except OSError as exc:
            _LOGGER.warning("SKIP step=single_instance.focus_server error=%r", exc)
            return False
        self._server = server

        def loop() -> None:
            while True:
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                with connection:
                    try:
                        connection.settimeout(2.0)
                        message = connection.recv(64)
                    except OSError:
                        continue
                if message.strip() == _FOCUS_MESSAGE.strip():
                    try:
                        on_focus()
                    except Exception as exc:  # noqa: BLE001 - focusing is best effort
                        _LOGGER.warning("FALLBACK step=single_instance.focus error=%r", exc)

        threading.Thread(target=loop, name="pudge-single-instance", daemon=True).start()
        return True

    def release(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self.socket_path.unlink(missing_ok=True)
            self._server = None
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
        except (OSError, ValueError):
            pass


def acquire(root: Path | None = None) -> InstanceLock | None:
    """The lock for this data directory, or None when another window holds it."""
    directory = Path(root or DATA_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "app.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except OSError as exc:
        # A filesystem without locks must not stop the app from starting.
        _LOGGER.warning("FALLBACK step=single_instance.lock error=%r", exc)
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        pass
    return InstanceLock(handle, directory)


def request_focus(root: Path | None = None, *, timeout: float = 2.0) -> bool:
    """Ask the running window to come to the front."""
    path = _socket_path(Path(root or DATA_DIR))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(_FOCUS_MESSAGE)
        return True
    except OSError:
        return False
