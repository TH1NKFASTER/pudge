from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .process_utils import pid_alive
from .fsutil import atomic_write_json

_MARKER_NAME = "foreground-work.json"
# A marker nobody refreshed for this long is stale (its process hung or was replaced). The
# process playing a video refreshes it every MARKER_REFRESH_SECONDS, also for 10-hour mpv sessions.
_MAX_AGE_SECONDS = 6 * 3600
MARKER_REFRESH_SECONDS = 10 * 60


def _marker(cache_dir: Path) -> Path:
    return cache_dir / _MARKER_NAME


_pid_alive = pid_alive


def _pid_command(pid: int) -> str:
    if pid <= 0 or os.name != "posix":
        return ""
    try:
        completed = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return ""
    if completed.returncode != 0:
        return ""
    return " ".join((completed.stdout or "").strip().split())


# Waiting background workers ask every 0.5 s; the command check forks ``ps``.
# A marker whose command was verified stays trusted for a short while (the
# cheap PID liveness check still runs every time).
_COMMAND_CHECK_TTL_SECONDS = 30.0
_verified_marker: dict[tuple[int, float, str], float] = {}


def foreground_active(cache_dir: Path) -> bool:
    marker = _marker(cache_dir)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
        created_at = float(payload.get("created_at") or 0.0)
        alive_at = max(created_at, float(payload.get("refreshed_at") or 0.0))
        expected_command = " ".join(str(payload.get("command") or "").split())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if time.time() - alive_at > _MAX_AGE_SECONDS or not _pid_alive(pid):
        marker.unlink(missing_ok=True)
        return False
    # A live PID can be reused after an unclean foreground worker exit.  New
    # markers keep the original command fingerprint so an unrelated process
    # cannot block all background work for up to six hours.
    if expected_command:
        key = (pid, created_at, expected_command)
        verified_at = _verified_marker.get(key)
        if verified_at is None or time.monotonic() - verified_at > _COMMAND_CHECK_TTL_SECONDS:
            current_command = _pid_command(pid)
            if current_command and current_command != expected_command:
                _verified_marker.pop(key, None)
                marker.unlink(missing_ok=True)
                return False
            _verified_marker.clear()
            _verified_marker[key] = time.monotonic()
    return True


def mark_foreground(cache_dir: Path, *, video: Path | None = None, mpv_ipc: Path | None = None) -> Path:
    """Claim the foreground. ``mpv_ipc``: the socket of the mpv this process plays the video
    in, so the intro-skipper pass can tell a paused player from a playing one."""
    marker = _marker(cache_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "created_at": time.time(),
        "video": str(video.resolve()) if video is not None else "",
        "command": _pid_command(os.getpid()),
    }
    if mpv_ipc is not None:
        payload["mpv_ipc"] = str(mpv_ipc)
    atomic_write_json(marker, payload, ensure_ascii=False)
    return marker


def refresh_foreground(cache_dir: Path) -> bool:
    """Say "still here" in this process's marker (its identity and pause key stay)."""
    marker = _marker(cache_dir)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or int(payload.get("pid") or 0) != os.getpid():
        return False
    payload["refreshed_at"] = time.time()
    temporary = marker.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(marker)
    except OSError:
        return False
    return True


def keep_foreground(cache_dir: Path, *, interval: float = MARKER_REFRESH_SECONDS):
    """Refresh this process's marker every ``interval`` s until the returned stop() is called."""
    import threading

    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(max(0.01, float(interval))):
            refresh_foreground(cache_dir)

    threading.Thread(target=loop, name="foreground-marker", daemon=True).start()
    return stop.set


def clear_foreground(cache_dir: Path, *, pid: int | None = None) -> None:
    marker = _marker(cache_dir)
    if pid is not None:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if int(payload.get("pid") or 0) != int(pid):
                return
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    marker.unlink(missing_ok=True)


# ------------------------------------------------- paused foreground player ---
# Only the intro-skipper pass may run while the video mpv is paused (opt-in, see
# WorkScheduler.background_block_reason(foreground_paused_ok=True)). Waiters ask every
# 0.5 s: one IPC answer is reused for a short while, and a query never blocks longer
# than _IPC_TIMEOUT_SECONDS. No socket, no answer or a preparation (no mpv): "playing".

PAUSE_SETTLE_SECONDS = 30.0
_IPC_TIMEOUT_SECONDS = 0.2
_PLAYBACK_CACHE_SECONDS = 1.5
_PAUSE_FILE = "foreground-pause.json"
_playback_cache: dict[str, tuple[float, str, float]] = {}


def _marker_payload(cache_dir: Path) -> dict | None:
    try:
        payload = json.loads(_marker(cache_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def foreground_video(cache_dir: Path) -> Path | None:
    """The video the foreground process plays or prepares (None without a foreground)."""
    payload = _marker_payload(cache_dir) if foreground_active(cache_dir) else None
    value = str((payload or {}).get("video") or "").strip()
    return Path(value) if value else None


def _mpv_properties(ipc_path: str, names: tuple[str, ...], timeout: float) -> dict[str, object] | None:
    """{name: value} of the properties mpv answered; None when it did not answer in time."""
    import socket
    import stat

    try:
        info = os.stat(ipc_path)
        if not stat.S_ISSOCK(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            return None
    except OSError:
        return None
    deadline = time.monotonic() + float(timeout)
    values: dict[str, object] = {}
    answered: set[int] = set()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(max(0.01, deadline - time.monotonic()))
            client.connect(ipc_path)
            client.sendall(
                b"".join(
                    json.dumps({"command": ["get_property", name], "request_id": 7100 + index}).encode()
                    + b"\n"
                    for index, name in enumerate(names)
                )
            )
            buffer = b""
            while len(answered) < len(names):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                client.settimeout(remaining)
                chunk = client.recv(4096)
                if not chunk or len(buffer) > 65536:
                    return None
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    index = (
                        row.get("request_id") - 7100
                        if isinstance(row, dict) and isinstance(row.get("request_id"), int)
                        else -1
                    )
                    if 0 <= index < len(names):
                        answered.add(index)
                        if row.get("error") == "success":
                            values[names[index]] = row.get("data")
    except (OSError, ValueError, TypeError):
        return None
    return values


def _paused_since(cache_dir: Path, key: str, position: object) -> float:
    """Since when mpv has been paused at this position (kept across agent processes)."""
    path = cache_dir / _PAUSE_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        record = None
    if isinstance(record, dict) and record.get("key") == key:
        old = record.get("position")
        same = (old is None and position is None) or (
            isinstance(old, (int, float))
            and isinstance(position, (int, float))
            and abs(old - position) < 0.01
        )
        if same:
            try:
                return float(record["since"])
            except (KeyError, TypeError, ValueError):
                pass
    since = time.time()
    try:
        atomic_write_json(path, {"key": key, "position": position, "since": since})
    except OSError:
        pass
    return since


def foreground_playback(cache_dir: Path) -> tuple[str, float]:
    """("none" | "playing" | "paused", seconds paused) of the foreground video mpv.

    "paused" also covers an idle mpv (no file). Fails closed: a foreground without an mpv
    socket (subtitle preparation the user waits for), a missing socket or no answer within
    200 ms is "playing".
    """
    if not foreground_active(cache_dir):
        return "none", 0.0
    payload = _marker_payload(cache_dir) or {}
    ipc = str(payload.get("mpv_ipc") or "").strip()
    if not ipc:
        return "playing", 0.0
    key = f"{payload.get('pid')}|{payload.get('created_at')}|{ipc}"
    now = time.monotonic()
    cached = _playback_cache.get(key)
    if cached is not None and now - cached[0] < _PLAYBACK_CACHE_SECONDS:
        state, since = cached[1], cached[2]
    else:
        values = _mpv_properties(ipc, ("pause", "idle-active", "time-pos"), _IPC_TIMEOUT_SECONDS)
        if values is None or not isinstance(values.get("pause"), bool):
            state, since = "playing", 0.0
        elif values.get("pause") is True or values.get("idle-active") is True:
            position = values.get("time-pos") if isinstance(values.get("time-pos"), (int, float)) else None
            state, since = "paused", _paused_since(cache_dir, key, position)
        else:
            state, since = "playing", 0.0
            (cache_dir / _PAUSE_FILE).unlink(missing_ok=True)
        _playback_cache.clear()
        _playback_cache[key] = (now, state, since)
    return state, (max(0.0, time.time() - since) if state == "paused" else 0.0)
