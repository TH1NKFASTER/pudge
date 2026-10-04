from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent: int
    started: str
    state: str


def process_snapshot() -> dict[int, ProcessIdentity]:
    if sys.platform == 'linux':
        return _linux_process_snapshot()
    # lstart works on macOS and Linux. Never collect arguments or credentials.
    process = subprocess.Popen(
        ['ps', '-axo', 'pid=,ppid=,lstart=,stat='],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    try:
        output, _ = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    if process.returncode:
        raise OSError('Cannot inspect owned process tree')
    result = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if pid > 1 and pid != process.pid:
            result[pid] = ProcessIdentity(pid, parent, ' '.join(fields[2:7]), fields[7])
    return result


def _linux_process_snapshot(proc: Path = Path('/proc')) -> dict[int, ProcessIdentity]:
    # A container's /proc can expose outer PIDs while os.kill uses inner PIDs.
    # Only translate processes in our own PID namespace; unrelated namespaces
    # can have the same inner PID. Start ticks also protect against PID reuse.
    namespace = os.readlink(proc / 'self/ns/pid')
    rows: dict[int, tuple[int, int, str, str]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.readlink(entry / 'ns/pid') != namespace:
                continue
            status = dict(line.split(':', 1) for line in (entry / 'status').read_text().splitlines()
                          if ':' in line)
            local_pid = int(status.get('NSpid', entry.name).split()[-1])
            outer_parent = int(status['PPid'])
            # comm can contain spaces and parentheses. Fields after the last
            # ')' start with state (field 3); starttime is field 22.
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            rows[int(entry.name)] = (local_pid, outer_parent, fields[19], fields[0])
        except (OSError, ValueError, KeyError, IndexError):
            continue  # Exited or inaccessible; never guess a process identity.
    if os.getpid() not in {row[0] for row in rows.values()}:
        raise OSError('Cannot inspect own PID namespace')
    return {
        pid: ProcessIdentity(pid, rows[parent][0] if parent in rows else 0, 'ticks:' + started, state)
        for pid, parent, started, state in rows.values() if pid > 1
    }


class OwnedProcessTree:
    """Remember descendants before service shutdown can reparent them."""

    def __init__(self, root: int | None = None) -> None:
        self._lock = threading.RLock()
        self.root = os.getpid() if root is None else int(root)
        self.owned: dict[int, ProcessIdentity] = {}
        self._excluded: dict[int, ProcessIdentity] = {}
        self._root_started: str | None = None
        self.capture()

    def capture(self) -> None:
        snapshot = process_snapshot()
        root = snapshot.get(self.root)
        if self._root_started is None and root is not None:
            self._root_started = root.started
        parents = {self.root} if root is not None and root.started == self._root_started else set()
        parents.update(pid for pid, row in self.owned.items()
                       if pid in snapshot and snapshot[pid].started == row.started)
        excluded = {pid for pid, row in self._excluded.items()
                    if pid in snapshot and snapshot[pid].started == row.started}
        parents.update(excluded)
        while True:
            children = {pid for pid, row in snapshot.items()
                        if row.parent in parents and pid not in parents}
            if not children:
                break
            parents.update(children)
        for pid in parents - {self.root}:
            self.owned[pid] = snapshot[pid]
        while True:
            children = {pid for pid, row in snapshot.items()
                        if row.parent in excluded and pid not in excluded}
            if not children:
                break
            excluded.update(children)
        for pid in excluded:
            self._excluded[pid] = snapshot[pid]
            self.owned.pop(pid, None)

    def exclude_subtrees(self, roots: list[int]) -> None:
        """Leave detached playback launchers and their save helpers running."""
        with self._lock:
            self.capture()
            excluded = {int(pid) for pid in roots if int(pid) in self.owned}
            while True:
                children = {pid for pid, row in self.owned.items()
                            if row.parent in excluded and pid not in excluded}
                if not children:
                    break
                excluded.update(children)
            for pid in excluded:
                self._excluded[pid] = self.owned.pop(pid)

    def _alive(self) -> dict[int, ProcessIdentity]:
        current = process_snapshot()
        return {pid: row for pid, row in list(self.owned.items())
                if pid in current and current[pid].started == row.started
                and not current[pid].state.startswith('Z')}

    def _signal(self, sig: signal.Signals) -> None:
        # Recheck process identity to avoid signaling a reused PID. No global
        # killall, process-name matching, or foreign process group is used.
        for pid in self._alive():
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def stop(self, *, grace: float = .6) -> list[int]:
        with self._lock:
            self.capture()
            self._signal(signal.SIGTERM)
            deadline = time.monotonic() + max(0, grace)
            while self._alive() and time.monotonic() < deadline:
                time.sleep(.025)
            # Include workers created during cancellation, before their parent dies.
            self.capture()
            self._signal(signal.SIGKILL)
            deadline = time.monotonic() + .6
            while self._alive() and time.monotonic() < deadline:
                time.sleep(.025)
            return sorted(self._alive())
