"""Listen together: the audiobook starts where it was asked to and the clock does not run
through the silence before the first words (behaviour; audit 71Б rewrite)."""

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
from t71g22_support import ln_audio_pair


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


def test_paired_state_shows_the_requested_resume_not_the_stale_startup_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # mpv has just started for "resume at 33.15 s" but still reports its pre-seek clock
    # (1.16 s). The reader's poll must get 33.15 s and the text position of 33.15 s:
    # mapping the stale clock first made the reader jump back to the chapter start.
    _ln, audio, ln_id, audio_id = ln_audio_pair(tmp_path, ["狼と香辛料。" * 200])
    alignment = {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 0,
                "start": 0.0,
                "end": 100.0,
                "normalized_length": 1000,
                "anchors": [{"offset": 0, "time": 0.0}, {"offset": 1000, "time": 100.0}],
            }
        ],
    }
    monkeypatch.setattr(audio, "_load_alignment", lambda *_args: alignment)
    commands: list[list[object]] = []

    def mpv(_path: Path, command: list[object]) -> dict[str, object]:
        commands.append(list(command))
        answers = {"time-pos": 1.162993, "playlist-pos": 0, "pause": False}
        if command[0] == "get_property":
            return {"error": "success", "data": answers.get(str(command[1]))}
        return {"error": "success"}

    monkeypatch.setattr(AudiobookService, "_ipc_command", staticmethod(mpv))
    audio._ipc_paths[audio_id] = tmp_path / "mpv.sock"
    audio._startup_targets[audio_id] = {
        "global_position": 33.152018,
        "local_position": 33.152018,
        "file_index": 0,
        "speed": 1.0,
        "launched_at": time.monotonic(),
    }

    state = audio.paired_state(ln_id)

    assert state["position"] == pytest.approx(33.152018)
    assert state["chapter_char_offset_exact"] == pytest.approx(331.52018)
    assert ["seek", 33.152018, "absolute", "exact"] in commands
    assert isinstance(state["position_lookup_ms"], float) and state["position_lookup_ms"] >= 0
