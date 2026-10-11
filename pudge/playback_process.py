"""Bounded cleanup of one owned playback process and its descendants."""

import json
import socket
import subprocess
import threading
import time

from .process_cleanup import OwnedProcessTree


def _close_requested(ipc_socket):
    """Read an explicit close intent; idle/paused playback alone never counts."""
    if not ipc_socket:
        return False
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(0.15)
            client.connect(str(ipc_socket))
            client.sendall(b'{"command":["get_property","user-data/pudge/closing"],"request_id":73}\n')
            data = b""
            received = 0
            while received < 16384:
                chunk = client.recv(4096)
                if not chunk:
                    break
                received += len(chunk)
                data += chunk
                while b"\n" in data:
                    line, data = data.split(b"\n", 1)
                    row = json.loads(line)
                    if isinstance(row, dict) and row.get("request_id") == 73:
                        return row.get("error") == "success" and row.get("data") is True
    except (OSError, ValueError):
        pass
    return False


def stop_playback_process(process, *, tree=None, ipc_socket=None):
    if ipc_socket and process.poll() is None:
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(0.25)
                client.connect(str(ipc_socket))
                client.sendall(json.dumps({"command": ["quit"]}).encode() + b"\n")
            process.wait(timeout=0.6)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if tree is not None:
        try:
            remaining = tree.stop(grace=0.3)
            if remaining:
                from .logging_utils import configure_logging

                configure_logging().warning("EVENT playback.children_remaining pids=%s", remaining)
        except OSError as exc:
            from .logging_utils import configure_logging

            configure_logging().warning("EVENT playback.children_inspection_failed error=%s", exc)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _closing_row(row):
    """An explicit close intent in an mpv IPC reply or property-change event."""
    if not isinstance(row, dict) or row.get("data") is not True:
        return False
    if row.get("request_id") == 73:
        return row.get("error") == "success"
    return row.get("event") == "property-change" and row.get("name") == "user-data/pudge/closing"


def _connect_ipc(ipc_socket, process, done, *, timeout=10.0):
    """Connect once mpv has created its IPC socket (shortly after it starts)."""
    deadline = time.monotonic() + timeout
    while not done.is_set() and process.poll() is None:
        client = socket.socket(socket.AF_UNIX)
        try:
            client.settimeout(0.5)
            client.connect(str(ipc_socket))
            return client
        except OSError:
            client.close()
        if time.monotonic() >= deadline or done.wait(0.25):
            return None
    return None


# Without an IPC connection the process tree is captured this often (it forks ``ps``).
FALLBACK_CAPTURE_SECONDS = 30.0


def wait_playback_process(process, *, ipc_socket=None):
    """Capture helper identities while mpv lives, before children reparent.

    One IPC connection stays open for the whole episode: mpv reports the
    close intent (``user-data/pudge/closing``) and its own shutdown as events,
    so nothing reconnects twice a second. The process tree (``ps``) is captured
    when playback starts and when mpv is about to exit, not every few seconds.
    """
    try:
        tree = OwnedProcessTree(process.pid)
    except OSError:
        tree = None
    done = threading.Event()

    def capture():
        if tree is not None:
            try:
                tree.capture()
            except OSError:
                pass

    def close_requested():
        from .logging_utils import configure_logging

        configure_logging().info("EVENT mpv.close_requested pid=%s", process.pid)
        capture()
        # Stop the owned root first so wait() can finish even if a Lua
        # shutdown callback/helper keeps mpv alive. The main thread then
        # cleans its captured descendants in the existing finally block.
        stop_playback_process(process, ipc_socket=ipc_socket)

    def watch_without_ipc():
        while not done.wait(FALLBACK_CAPTURE_SECONDS):
            if process.poll() is not None:
                return
            capture()

    def monitor():
        client = _connect_ipc(ipc_socket, process, done) if ipc_socket else None
        if client is None:
            watch_without_ipc()
            return
        with client:
            try:
                client.sendall(b'{"command":["observe_property",1,"user-data/pudge/closing"]}\n')
                client.sendall(b'{"command":["get_property","user-data/pudge/closing"],"request_id":73}\n')
            except OSError:
                return
            capture()  # helpers the scripts started with mpv
            client.settimeout(2.0)
            data = b""
            while not done.is_set():
                try:
                    chunk = client.recv(65536)
                except socket.timeout:
                    if process.poll() is not None:
                        return
                    continue
                except OSError:
                    return
                if not chunk:
                    return  # mpv closed the connection: it has exited
                data += chunk
                while b"\n" in data:
                    line, data = data.split(b"\n", 1)
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if _closing_row(row):
                        close_requested()
                        return
                    if isinstance(row, dict) and row.get("event") in {"shutdown", "end-file"}:
                        capture()  # mpv is about to exit: its helpers still have it as parent
                if len(data) > 1 << 20:
                    data = b""

    watcher = threading.Thread(target=monitor, name="mpv-children", daemon=True)
    watcher.start()
    try:
        return process.wait()
    finally:
        done.set()
        watcher.join(timeout=0.5)
        stop_playback_process(process, tree=tree, ipc_socket=ipc_socket)
