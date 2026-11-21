from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_start_time(pid: int) -> float | None:
    """When a process started (seconds since the epoch), or None when that cannot be told."""
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            # comm can hold spaces and parentheses: fields after the last ')' start with
            # state (field 3); starttime (clock ticks after boot) is field 22.
            fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
            ticks = int(fields[19])
            boot = next(
                float(line.split()[1])
                for line in Path("/proc/stat").read_text().splitlines()
                if line.startswith("btime ")
            )
            return boot + ticks / float(os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError, IndexError, StopIteration):
            return None
    # macOS (as install.sh's session guard does it): lstart has one-second resolution.
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(int(pid)), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = result.stdout.strip()
    if result.returncode != 0 or not text:
        return None
    try:
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except (OverflowError, ValueError):
        return None


def session_process_alive(pid: int, started_at: object = 0.0, *, slack: float = 2.0) -> bool:
    """The process that wrote a session marker at ``started_at`` is still running.

    A live pid that started after the marker was written is another process
    reusing the number (after a crash and a reboot, for example). When the start
    time cannot be read, a live pid counts, as before.
    """
    if not pid_alive(pid):
        return False
    try:
        recorded = float(started_at or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        recorded = 0.0
    if recorded <= 0 or pid == os.getpid():
        return True
    started = process_start_time(pid)
    if started is None:
        return True
    return started <= recorded + slack
