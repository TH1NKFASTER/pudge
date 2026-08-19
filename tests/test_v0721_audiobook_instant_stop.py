from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

from pudge.audiobooks import AudiobookService, _STOP_GRACE_SECONDS


class SlowFakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(float(timeout or 0.0))
        if self.returncode is None:
            raise subprocess.TimeoutExpired("mpv", timeout)
        return self.returncode


def _service(process: SlowFakeProcess, ipc_path: Path) -> AudiobookService:
    service = AudiobookService.__new__(AudiobookService)
    service._lock = threading.Lock()
    service._players = {7: process}
    service._ipc_paths = {7: ipc_path}
    service._last_positions = {7: 42.75}
    service._speeds = {7: 1.0}
    service._sleep_deadlines = {}
    service._sleep_chapter_ends = {}
    return service


def test_stop_uses_cached_position_and_kills_slow_mpv_without_ipc_reads(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    process = SlowFakeProcess()
    ipc_path = tmp_path / "book.sock"
    service = _service(process, ipc_path)
    commands: list[list[list[Any]]] = []
    saved: list[tuple[int, float]] = []

    monkeypatch.setattr(
        service,
        "_ipc_commands_no_wait",
        lambda _path, payload: commands.append(payload) or True,
    )
    monkeypatch.setattr(
        service,
        "_global_position",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Stop must not perform blocking IPC position reads")
        ),
    )
    monkeypatch.setattr(
        service,
        "set_position",
        lambda book_id, position: saved.append((int(book_id), float(position))),
    )
    monkeypatch.setattr(
        service,
        "book",
        lambda book_id: {"id": int(book_id), "position": saved[-1][1] if saved else 0.0},
    )

    result = service.stop(7)

    assert result["stopped"] is True
    assert saved == [(7, 42.75)]
    assert commands == [[
        ["set_property", "mute", True],
        ["set_property", "pause", True],
        ["quit"],
    ]]
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_timeouts
    assert max(process.wait_timeouts) <= _STOP_GRACE_SECONDS + 0.001
    assert 7 not in service._players
    assert 7 not in service._ipc_paths
    assert 7 not in service._last_positions


def test_stop_source_has_no_blocking_position_query() -> None:
    import inspect

    source = inspect.getsource(AudiobookService.stop)

    assert "_global_position" not in source
    assert "_ipc_commands_no_wait" in source
    assert "_wait_or_kill" in source
