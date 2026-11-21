"""MEM6: the open, idle window's footprint grew 437 → 1056 MB while anonymous, compressed and
region memory stayed flat. The memory line now names the kernel's ledgers that phys_footprint
also counts (graphics, media, neural, network, purgeable …, only those the kernel filled and
not zero) with ``rest`` = footprint − internal − compressed, and the main window's state
(visible / occluded / minimized), read on the main thread."""

from __future__ import annotations

import ctypes
import threading
from types import SimpleNamespace

from pudge import memory_diagnostics as md

MB = 2**20


def _fake_task_info(words: int, **values):
    def task_info(task, flavor, info_ref, count_ref):
        info = info_ref._obj
        for name, value in values.items():
            setattr(info, name, value)
        count_ref._obj.value = words
        return 0

    return task_info


def test_struct_matches_the_xnu_revisions() -> None:
    T = md._TaskVmInfo
    assert T.ledger_phys_footprint_peak.offset == 42 * 4  # TASK_VM_INFO_REV2_COUNT
    assert T.limit_bytes_remaining.offset == 84 * 4  # REV3
    assert T.decompressions.offset == 86 * 4  # REV4
    assert T.ledger_swapins.offset == 87 * 4  # REV5 (pack 4: no padding)
    assert T.ledger_tag_neural_nofootprint_total.offset == 89 * 4  # REV6
    assert ctypes.sizeof(T) == 93 * 4  # REV7


def test_ledgers_the_kernel_filled_and_nonzero_only(monkeypatch) -> None:
    monkeypatch.setattr(md.sys, "platform", "darwin")
    values = dict(
        phys_footprint=1056 * MB,
        internal=260 * MB,
        compressed=160 * MB,
        external=94 * MB,
        ledger_tag_graphics_footprint=600 * MB,
        ledger_tag_media_footprint=12 * MB,
        ledger_phys_footprint_peak=1100 * MB,
        limit_bytes_remaining=2**63 - 1,  # no limit: left out
        ledger_swapins=0,
    )
    monkeypatch.setattr(md, "_TASK_VM", (_fake_task_info(93, **values), 0))
    info = md.vm_info()
    assert info["footprint"] == 1056.0 and info["rest"] == 636.0
    assert info["graphics"] == 600.0 and info["media"] == 12.0 and info["footprint_peak"] == 1100.0
    assert "limit_left" not in info and "swapins" not in info and "neural" not in info

    # An older kernel (rev2): the ledger words were not filled, whatever the buffer holds.
    monkeypatch.setattr(md, "_TASK_VM", (_fake_task_info(42, **values), 0))
    old = md.vm_info()
    assert "graphics" not in old and "footprint_peak" not in old and old["rest"] == 636.0


def test_vm_part_of_the_line_shows_the_ledgers(monkeypatch) -> None:
    monkeypatch.setattr(md, "vm_info", lambda: {"footprint": 900.0, "rest": 480.0, "graphics": 450.0})
    monkeypatch.setattr(md, "vm_regions", lambda: {})
    monkeypatch.setattr(md, "obmalloc_stats", lambda: {})
    monkeypatch.setattr(md, "sqlite_memory", lambda: {})
    assert md.vm_line() == "vm=footprint:900.0,rest:480.0,graphics:450.0"


class _Native:
    def __init__(self, visible=True, occlusion=2, minimized=False):
        self._v, self._o, self._m = visible, occlusion, minimized
        self.threads: list[str] = []

    def isVisible(self):
        self.threads.append(threading.current_thread().name)
        return self._v

    def occlusionState(self):
        return self._o

    def isMiniaturized(self):
        return self._m


def test_window_state_read_on_the_main_thread_or_busy() -> None:
    native = _Native(visible=True, occlusion=0, minimized=False)  # behind other windows
    api = SimpleNamespace(window=SimpleNamespace(native=native))
    posted: list = []
    result: dict = {}

    def other_thread():
        result["line"] = md.window_line(api, call_after=posted.append, wait=5.0)

    worker = threading.Thread(target=other_thread, name="memory-report")
    worker.start()
    while not posted:
        threading.Event().wait(0.01)
    posted.pop()()  # the "main thread" runs it
    worker.join()
    assert result["line"] == "window=visible:1,occluded:1,minimized:0"
    assert native.threads == [threading.current_thread().name]

    stuck: dict = {}
    thread = threading.Thread(
        target=lambda: stuck.update(line=md.window_line(api, call_after=lambda fn: None, wait=0.05))
    )
    thread.start()
    thread.join()
    assert stuck["line"] == "window=busy"
    hidden = SimpleNamespace(
        window=SimpleNamespace(native=_Native(visible=False, occlusion=0, minimized=True))
    )
    assert md.window_line(hidden) == "window=visible:0,occluded:1,minimized:1"  # this is the main thread
    assert md.window_line(SimpleNamespace(window=None)) == ""
    assert md.window_line(None) == ""


def test_hourly_line_carries_the_window_state(monkeypatch) -> None:
    class _Api:
        window = SimpleNamespace(native=_Native())

    monkeypatch.setattr(md, "vm_line", lambda: "vm=footprint:1.0")
    api = _Api()  # the reporter holds it weakly
    line = md.MemoryReporter(api, None).log_once("hourly")
    assert "vm=footprint:1.0 window=visible:1,occluded:0,minimized:0 rss_before_release_mb=" in line
