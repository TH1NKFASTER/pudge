from __future__ import annotations

import time
from pathlib import Path

import pytest

from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.reading_audio_alignment import (
    align_light_novel_to_transcript,
    light_novel_position_for_audio,
    normalize_reading_text,
)


def _service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "library.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def test_startup_poll_cannot_replace_requested_resume_with_stale_zero_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    book_id = 283
    service._startup_targets[book_id] = {
        "global_position": 33.152018,
        "local_position": 33.152018,
        "file_index": 0,
        "speed": 1.0,
        "launched_at": time.monotonic(),
    }
    service._last_positions[book_id] = 33.152018
    service._last_motion_at[book_id] = time.monotonic()
    monkeypatch.setattr(service, "_file_rows", lambda _book_id: [{"file_index": 0, "start": 0.0}])
    commands: list[list[object]] = []
    monkeypatch.setattr(
        service,
        "_ipc_command",
        lambda _path, command: commands.append(command) or {"error": "success"},
    )

    reconciled = service._reconcile_startup_position(
        book_id,
        tmp_path / "book.sock",
        1.162993,
    )

    assert reconciled == pytest.approx(33.152018)
    assert commands == [["seek", 33.152018, "absolute", "exact"]]
    assert book_id not in service._startup_targets


def test_startup_reconcile_accepts_live_clock_near_requested_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    book_id = 283
    service._startup_targets[book_id] = {
        "global_position": 33.152018,
        "local_position": 33.152018,
        "file_index": 0,
        "speed": 1.0,
        "launched_at": time.monotonic(),
    }
    monkeypatch.setattr(service, "_file_rows", lambda _book_id: [{"file_index": 0, "start": 0.0}])
    monkeypatch.setattr(
        service,
        "_ipc_command",
        lambda *_args, **_kwargs: pytest.fail("nearby live startup clock must not be reseeked"),
    )

    reconciled = service._reconcile_startup_position(
        book_id,
        tmp_path / "book.sock",
        33.4,
    )

    assert reconciled == pytest.approx(33.4)
    assert book_id not in service._startup_targets


def test_first_chapter_clock_does_not_advance_through_leading_silence() -> None:
    skipped_prefix = "これは電子版の前置きとして残っている文章です。"
    spoken = (
        "旅人は静かな丘を越えて市場へ向かった。"
        "朝の風は冷たく荷馬車の音だけが道に響いていた。"
        "やがて遠くに町の門が見えてきた。"
    )
    chapter_text = skipped_prefix + spoken
    alignment = align_light_novel_to_transcript(
        [{"chapter_index": 0, "title": "第一幕", "text": chapter_text}],
        [{"start": 3.1, "end": 11.92, "text": normalize_reading_text(spoken)}],
        duration=12.5,
        model="test-model",
        speech_regions=[{"start": 3.1, "end": 11.92}],
    )

    chapter = alignment["chapters"][0]
    assert chapter["start"] == pytest.approx(3.1)

    before_speech = light_novel_position_for_audio(alignment, 1.2)
    at_speech = light_novel_position_for_audio(alignment, 3.1)
    assert before_speech is not None
    assert at_speech is not None
    assert before_speech["chapter_char_offset_exact"] == pytest.approx(0.0)
    assert at_speech["chapter_char_offset_exact"] >= len(normalize_reading_text(skipped_prefix)) - 1


def test_paired_state_reconciles_live_startup_clock_before_mapping() -> None:
    source = Path(__file__).parents[1].joinpath("pudge/audiobooks.py").read_text(encoding="utf-8")
    paired = source[source.index("    def paired_state("):source.index("    def ", source.index("    def paired_state(") + 8)]
    assert "position = self._reconcile_startup_position(audiobook_id, ipc_path, live)" in paired
    assert paired.index("_reconcile_startup_position") < paired.index("light_novel_position_for_audio")
