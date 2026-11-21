"""One atomic write helper and one exact SRT writer (review ARCH-10).

Files written by more than one process or thread (the window and the background agent
share the caches) used a fixed ``<name>.tmp``: two writers wrote into the same temporary
file, and the second rename found it gone. Every write now has its own temporary name.
"""

from __future__ import annotations

import json
import re
import stat
import threading
from pathlib import Path

import pytest
from source_policy import sources

from pudge import anilist_tracking, fsutil
from pudge.ocr import OCRCue
from pudge.subtitle_formats import parse_srt, srt_timestamp, write_exact_srt


def _hammer(write, count: int = 300) -> list[BaseException]:
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def run(worker: int) -> None:
        start.wait()
        for index in range(count):
            try:
                write(worker, index)
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)
                return

    threads = [threading.Thread(target=run, args=(worker,)) for worker in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return errors


def test_two_writers_of_one_cache_file_never_collide(tmp_path: Path) -> None:
    path = tmp_path / "anilist" / "mappings.json"
    path.parent.mkdir()
    payload = {f"key-{n}": {"media_id": n, "title": "x" * 200} for n in range(50)}

    errors = _hammer(lambda worker, index: anilist_tracking._write_json(path, {**payload, "w": worker}))

    assert errors == []
    assert json.loads(path.read_text(encoding="utf-8"))["w"] in (1, 2)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # still private
    assert sorted(p.name for p in path.parent.iterdir()) == ["mappings.json"]


def test_atomic_write_keeps_the_old_file_when_the_rename_fails(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def boom(_source, _target):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    with pytest.raises(OSError):
        fsutil.atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_atomic_write_uses_the_usual_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "subtitle.srt"
    fsutil.atomic_write_text(path, "text")
    reference = tmp_path / "plain.txt"
    reference.write_text("text", encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)


def test_temporary_siblings_are_unique_hidden_names_beside_the_target(tmp_path: Path) -> None:
    target = tmp_path / "audio.flac"
    names = {fsutil.temporary_sibling(target, ".flac.tmp") for _ in range(100)}
    assert len(names) == 100
    assert all(name.parent == tmp_path for name in names)
    assert all(name.name.startswith(".audio.flac.") and name.name.endswith(".flac.tmp") for name in names)


def _old_exact_srt(cues) -> str:
    """The SRT text the three former copies wrote (alignment guard, timing reference, OCR)."""

    def timestamp(seconds: float) -> str:
        milliseconds = max(0, int(round(float(seconds) * 1000.0)))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        whole_seconds, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"

    blocks = [
        f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{text}"
        for index, (start, end, text) in enumerate(cues, start=1)
        if text and end > start
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def test_exact_srt_writer_keeps_cues_as_given(tmp_path: Path) -> None:
    cues = [
        (0.0, 1.0004, "一"),
        (0.9, 2.5, "重なり\n二行目"),  # an overlap stays an overlap
        (3.0, 3.0, "no duration"),
        (4.0, 5.0, ""),
        (3599.9995, 3725.25, "<i>tag</i>"),
        (-1.0, 0.5, "before zero"),
    ]
    path = write_exact_srt(cues, tmp_path / "sub" / "exact.srt")
    assert path.read_text(encoding="utf-8") == _old_exact_srt(cues)
    assert len(parse_srt(path)) == 4
    assert write_exact_srt([], tmp_path / "empty.srt").read_text(encoding="utf-8") == ""
    assert srt_timestamp(3725.25) == "01:02:05,250" and srt_timestamp(-3) == "00:00:00,000"


def test_ocr_results_are_written_as_before(tmp_path: Path) -> None:
    cues = [OCRCue(1.0, 2.0, "こんにちは"), OCRCue(2.5, 4.0, "二\n行")]
    old = "".join(
        f"{index}\n{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}\n{cue.text}\n"
        + ("\n" if index < len(cues) else "")
        for index, cue in enumerate(cues, start=1)
    )
    path = write_exact_srt([(cue.start, cue.end, cue.text) for cue in cues], tmp_path / "ocr.srt")
    assert path.read_text(encoding="utf-8") == old


def test_no_fixed_temporary_file_names_are_left() -> None:
    # A temporary name built from the target alone ("x.json.tmp", "x.tmp.jpg") is shared by
    # every writer of that file; fsutil gives each write its own.
    fixed = re.compile(
        r"with_suffix\([^)\n]*\.tmp[^)\n]*\)|with_name\([^)\n]*\+\s*\"\.(?:adopt\.)?tmp[^\"]*\"\)"
    )
    found = [
        f"{path}:{text.count(chr(10), 0, match.start()) + 1}"
        for path, text in sources("pudge/**/*.py").items()
        if not path.startswith("pudge/whisper_mlx/")  # vendored, unchanged
        for match in fixed.finditer(text)
    ]
    assert found == []
