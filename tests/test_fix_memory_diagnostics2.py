"""MEM2: what the Mac's hourly memory lines showed, and making the rest visible.

* parsed audiobook alignments (2 entries / 50 MB that never shrank) and their reports, and
  Statistics' decoded word units (~23 MB), go after a while unused; word units are bounded;
* the Settings preflight for Screen Recording no longer imports Quartz;
* the hourly line / debug bundle add the largest Python object types, malloc in-use vs
  allocated, and where each heavy module was first imported from;
* a step that leaves RSS > 50 MB higher logs ``EVENT memory_step``.
"""

from __future__ import annotations

import importlib.abc
import json
import logging
import runpy
import sys
import time
from pathlib import Path

from pudge import memory_diagnostics, visual_novels
from pudge.audiobooks import AudiobookService
from pudge.consumption_words import WordUnits
from pudge.database import Database
from pudge.logging_utils import timed_step
from pudge.memory_diagnostics import (
    MemoryReporter,
    heap_line,
    malloc_stats,
    memory_step,
    object_types,
    watch_heavy_imports,
)


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _logger(name: str) -> tuple[logging.Logger, _Lines]:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _Lines()
    logger.handlers[:] = [handler]
    return logger, handler


# ------------------------------------------------------------------ caches


def test_audiobook_alignment_caches_go_when_unused(tmp_path: Path, monkeypatch) -> None:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "c"
    )
    monkeypatch.setattr(service, "_alignment_fingerprint", lambda *args, **kwargs: "invented-fingerprint")
    payload = {"schema": "reading-audio-v3", "segments": [{"text": f"文{n}"} for n in range(1000)]}
    path = service._alignment_path(3, 4)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(service, "_attach_runtime_alignment_context", lambda payload, *args: None)
    assert service._load_alignment(3, 4)["schema"] == "reading-audio-v3"
    service._load_alignment_report(3, 4)
    assert len(service._alignment_payload_cache) == 1 and len(service._alignment_report_cache) == 1
    assert service.release_idle() == 0  # just used
    monkeypatch.setattr(AudiobookService, "ALIGNMENT_CACHE_IDLE_SECONDS", 0.0)
    assert service.release_idle() == 2
    assert service._alignment_payload_cache == {} and service._alignment_report_cache == {}
    assert len(service._load_alignment(3, 4)["segments"]) == 1000  # read again from disk


def test_word_units_cache_is_bounded_and_goes_when_unused(tmp_path: Path, monkeypatch) -> None:
    units = WordUnits(Database(tmp_path / "db.sqlite3"))
    keys = [f"unit-{n}" for n in range(12)]
    units.enqueue({key: ("light_novel", {"chapter_key": key}) for key in keys})
    for key in keys:
        units.store(key, {"words": [[100 + i, 0] for i in range(40_000)]})
    found = units.lookup(keys)
    assert all(status == "ready" for status, _words in found.values())
    # 12 x 40k words: at most READY_MAX_WORDS stay decoded (was: up to 5000 units).
    assert units._ready_words <= WordUnits.READY_MAX_WORDS and len(units._ready) < 12
    assert units.release_idle() == 0
    monkeypatch.setattr(WordUnits, "READY_IDLE_SECONDS", 0.0)
    assert units.release_idle() > 0 and units._ready == {} and units._ready_words == 0
    assert units.lookup(keys[:1])[keys[0]][0] == "ready"


# ------------------------------------------------------------------ Quartz


class _NoQuartz(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.asked = 0

    def find_spec(self, name, path=None, target=None):
        if name == "Quartz":
            self.asked += 1
            raise ImportError("Quartz must not be imported")
        return None


def test_screen_recording_preflight_does_not_import_quartz(monkeypatch) -> None:
    guard = _NoQuartz()
    monkeypatch.setattr(sys, "meta_path", [guard, *sys.meta_path])
    monkeypatch.delitem(sys.modules, "Quartz", raising=False)
    monkeypatch.setattr(visual_novels.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(visual_novels, "_core_graphics_preflight", lambda: True)
    assert visual_novels.VisualNovelService._screen_recording_access() is True
    monkeypatch.setattr(visual_novels, "_core_graphics_preflight", lambda: False)
    assert visual_novels.VisualNovelService._screen_recording_access() is False
    assert guard.asked == 0 and "Quartz" not in sys.modules


# ------------------------------------------------------------------ what is in the heap


def test_object_types_count_untracked_strings_too() -> None:
    kept = [f"{n:0300d}" for n in range(30_000)]
    types = {name: (count, size) for name, count, size in object_types(top=40)}
    assert types["str"][0] >= 30_000 and types["str"][1] >= 8.0
    del kept


def test_heap_line_has_malloc_and_types() -> None:
    stats = malloc_stats()
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        assert 0 < stats["in_use"] <= stats["allocated"]
    line = heap_line()
    assert line.startswith("py_blocks=") and " types=" in line
    assert ("malloc_in_use_mb=" in line) == bool(stats)


def test_hourly_line_names_where_heavy_modules_came_from(tmp_path: Path, monkeypatch) -> None:
    logger, handler = _logger("t.mem2.import")
    module_dir = tmp_path / "mods"
    module_dir.mkdir()
    (module_dir / "pudge_fake_heavy_mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    importer = tmp_path / "pudge" / "fake_importer.py"
    importer.parent.mkdir()
    importer.write_text("import pudge_fake_heavy_mod\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(module_dir))
    monkeypatch.setattr(memory_diagnostics, "_HEAVY_MODULES", ("pudge_fake_heavy_mod",))
    monkeypatch.setattr(memory_diagnostics, "IMPORT_ORIGINS", {})
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    monkeypatch.delitem(sys.modules, "pudge_fake_heavy_mod", raising=False)
    watch_heavy_imports(logger)
    runpy.run_path(str(importer))
    assert memory_diagnostics.IMPORT_ORIGINS == {"pudge_fake_heavy_mod": "fake_importer.py:1"}
    assert any(
        line.startswith("EVENT memory_import module=pudge_fake_heavy_mod by=fake_importer.py:1")
        for line in handler.lines
    )

    class _Api:
        pass

    line = MemoryReporter(_Api(), logger).log_once("debug_bundle")
    assert "heavy_from=pudge_fake_heavy_mod@fake_importer.py:1" in line and " types=" in line
    monkeypatch.delitem(sys.modules, "pudge_fake_heavy_mod", raising=False)


# ------------------------------------------------------------------ steps


def test_a_step_that_grows_rss_by_more_than_50_mb_is_logged(monkeypatch) -> None:
    logger, handler = _logger("t.mem2.step")
    readings = iter([100 * 2**20, 120 * 2**20, 100 * 2**20, 400 * 2**20, 100 * 2**20, 300 * 2**20])
    monkeypatch.setattr(memory_diagnostics, "_rss_bytes", lambda: next(readings))
    with memory_step(logger, "small"):
        pass
    with memory_step(logger, "iphone.request", route="GET:/api/v2/anime"):
        pass
    with timed_step(logger, "library.scan"):
        pass
    steps = [line for line in handler.lines if line.startswith("EVENT memory_step")]
    assert len(steps) == 2
    assert steps[0].startswith("EVENT memory_step step=iphone.request delta_mb=300.0 rss_mb=400.0 ")
    assert steps[0].endswith("route=GET:/api/v2/anime")
    assert steps[1].startswith("EVENT memory_step step=library.scan delta_mb=200.0 rss_mb=300.0 ")


def test_real_rss_growth_is_seen_by_a_step() -> None:
    # RSS is the whole process: inside a full suite another test's leftover thread can free
    # tens of MB during the step (a shuffled run on a busy machine saw no line once). The step
    # is tried up to three times; code that never reports real growth still fails every time.
    logger, handler = _logger("t.mem2.real")
    for _attempt in range(3):
        with memory_step(logger, "allocate"):
            block = bytearray(80 * 2**20)
            for index in range(0, len(block), 4096):
                block[index] = 1
        del block
        if handler.lines:
            break
    assert any(line.startswith("EVENT memory_step step=allocate delta_mb=") for line in handler.lines)
    time.sleep(0)
