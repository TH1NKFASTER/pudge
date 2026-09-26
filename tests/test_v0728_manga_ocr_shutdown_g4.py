from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pudge import manga


def test_cooperative_shutdown_writes_stop_file_before_wait(tmp_path: Path) -> None:
    stop = tmp_path / "stop"

    class Cooperative:
        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            assert stop.read_text() == "stop\n"
            assert timeout == 0.2
            return 0

    manga._stop_ocr_process_tree(Cooperative(), stop, grace_seconds=0.2)
    assert stop.exists()


def test_escalation_terminates_job_process_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stop = tmp_path / "stop"
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(manga.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    class Stubborn:
        pid = 312345
        waits = 0

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            assert stop.exists()
            self.waits += 1
            if self.waits < 3:
                raise subprocess.TimeoutExpired(cmd="mock-ocr", timeout=timeout)
            return 0

    process = Stubborn()
    manga._stop_ocr_process_tree(process, stop, grace_seconds=0.1)
    assert signals == [(312345, signal.SIGTERM), (312345, signal.SIGKILL)]
    assert process.waits == 3


def test_coordinator_exit_after_sigterm_still_kills_straggler_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX process groups are required")
    stop = tmp_path / "stop"
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(manga.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    class Coordinator:
        pid = 312346
        waits = 0

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(cmd="mock-ocr", timeout=timeout)
            return 0

    manga._stop_ocr_process_tree(Coordinator(), stop, grace_seconds=0.1)
    assert signals == [(312346, signal.SIGTERM), (312346, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
def test_shutdown_reaps_stubborn_coordinator_and_stops_its_child(tmp_path: Path) -> None:
    """Real process-group test: terminating the coordinator alone is not enough."""
    child_pid_file = tmp_path / "child.pid"
    parent_script = tmp_path / "coordinator.py"
    parent_script.write_text(
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [sys.executable, str(parent_script), str(child_pid_file)],
        start_new_session=True,
    )
    child_pid: int | None = None
    try:
        for _ in range(100):
            if child_pid_file.exists():
                child_pid = int(child_pid_file.read_text())
                break
            time.sleep(0.02)
        assert child_pid is not None, "coordinator failed to spawn child"
        manga._stop_ocr_process_tree(parent, tmp_path / "stop", grace_seconds=0.1)
        assert parent.poll() is not None
        # A child may briefly remain as a zombie waiting to be reaped, but it
        # must not be an active Python/OCR worker after process-group kill.
        for _ in range(100):
            try:
                status = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(child_pid)],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
            except OSError:
                status = ""
            if not status or status.startswith("Z"):
                break
            time.sleep(0.02)
        assert not status or status.startswith("Z"), f"child still active: {status}"
    finally:
        if parent.poll() is None:
            os.killpg(parent.pid, signal.SIGKILL)
            parent.wait(timeout=5)


def test_coordinator_exited_but_child_group_is_reaped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Exit of the coordinator PID does not imply its OCR models have exited."""
    calls: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(manga.os, "killpg", lambda pid, sig: calls.append((pid, sig)))

    class Returned:
        pid = 123456

        def poll(self) -> int:
            return 0

    manga._stop_ocr_process_tree(Returned(), tmp_path / "stop")
    if os.name == "posix":
        assert calls == [(123456, signal.SIGKILL)]
    else:
        assert not calls
