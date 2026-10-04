"""Bounded cleanup of one owned playback process and its descendants."""
import json
import socket
import subprocess
import threading

from .process_cleanup import OwnedProcessTree


def _close_requested(ipc_socket):
    """Read an explicit close intent; idle/paused playback alone never counts."""
    if not ipc_socket:
        return False
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(.15)
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
                client.settimeout(.25)
                client.connect(str(ipc_socket))
                client.sendall(json.dumps({"command": ["quit"]}).encode() + b"\n")
            process.wait(timeout=.6)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if tree is not None:
        try:
            remaining = tree.stop(grace=.3)
            if remaining:
                from .logging_utils import configure_logging
                configure_logging().warning("EVENT playback.children_remaining pids=%s", remaining)
        except OSError as exc:
            from .logging_utils import configure_logging
            configure_logging().warning("EVENT playback.children_inspection_failed error=%s", exc)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=.6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def wait_playback_process(process, *, ipc_socket=None):
    """Capture helper identities while mpv lives, before children reparent."""
    try:
        tree = OwnedProcessTree(process.pid)
    except OSError:
        tree = None
    done = threading.Event()
    def monitor():
        while not done.wait(.5):
            if process.poll() is not None:
                return
            if _close_requested(ipc_socket):
                from .logging_utils import configure_logging
                configure_logging().info("EVENT mpv.close_requested pid=%s", process.pid)
                # Stop the owned root first so wait() can finish even if a Lua
                # shutdown callback/helper keeps mpv alive. The main thread then
                # cleans its captured descendants in the existing finally block.
                stop_playback_process(process, ipc_socket=ipc_socket)
                return
            if tree is not None:
                try:
                    tree.capture()
                except OSError:
                    pass
    watcher = threading.Thread(target=monitor, name="mpv-children", daemon=True)
    watcher.start()
    try:
        return process.wait()
    finally:
        done.set()
        watcher.join(timeout=.5)
        stop_playback_process(process, tree=tree, ipc_socket=ipc_socket)
