"""Listen together: punctuation holds the text clock through acoustic pauses, and
alignments made by the earlier clock are rebuilt (behaviour; audit 71Б rewrite). The
reader holding its highlight on such a pause: tests/test_v0744_ln_linear_mora_clock.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge import audiobooks
from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.reading_audio_alignment import (
    align_light_novel_to_transcript,
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
    normalize_reading_text,
)


def _punctuated_alignment() -> tuple[dict[str, object], int]:
    first = "吾輩は猫である。"
    rest = (
        "名前はまだない。どこで生まれたか見当がつかぬ。"
        "何でも薄暗いじめじめした所で泣いていたことだけは記憶している。"
    )
    alignment = align_light_novel_to_transcript(
        [
            {
                "chapter_index": 0,
                "title": "第一章",
                "text": first + rest,
            }
        ],
        [
            {
                "start": 0.0,
                "end": 1.2,
                "text": normalize_reading_text(first),
            },
            {
                "start": 2.0,
                "end": 10.0,
                "text": normalize_reading_text(rest),
            },
        ],
        duration=10.5,
        model="test-model",
        speech_regions=[
            {"start": 0.0, "end": 1.2},
            {"start": 2.0, "end": 10.0},
        ],
    )
    return alignment, len(normalize_reading_text(first))


def test_exact_matches_produce_dense_character_clock() -> None:
    alignment, _pause_offset = _punctuated_alignment()
    chapter = alignment["chapters"][0]
    anchors = chapter["anchors"]

    assert alignment["anchor_count"] == len(anchors)
    assert alignment["anchor_count"] > alignment["matched_anchor_count"]
    assert max(int(right["offset"]) - int(left["offset"]) for left, right in zip(anchors, anchors[1:])) <= 2


def test_punctuation_holds_offset_through_acoustic_pause() -> None:
    alignment, pause_offset = _punctuated_alignment()
    chapter = alignment["chapters"][0]
    pause = [
        row
        for row in chapter["anchors"]
        if pause_offset - 0.01 <= float(row["offset"]) <= pause_offset and 1.2 <= float(row["time"]) <= 2.0
    ]

    assert chapter["punctuation_pause_count"] == 1
    assert pause == [
        {"offset": pause_offset - 0.001, "time": 1.2},
        {"offset": pause_offset - 0.001, "time": 1.999},
        {"offset": pause_offset, "time": 2.0},
    ]

    middle = light_novel_position_for_audio(alignment, 1.6)
    assert middle is not None
    assert middle["chapter_char_offset_exact"] == pytest.approx(pause_offset - 0.001)

    # Seeking to the first character after punctuation starts after the silence.
    assert audio_position_for_light_novel_offset(
        alignment,
        0,
        pause_offset,
    ) == pytest.approx(2.0)


def _paired_book(tmp_path: Path) -> tuple[AudiobookService, int, int, list[dict]]:
    """A two-chapter light novel linked to a one-file audiobook, and an alignment of its
    chapters (as the alignment cache stores it)."""
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir.mkdir(parents=True)
    novels = LightNovelService(cfg)
    source = tmp_path / "book.txt"
    source.write_text("吾輩は猫である。名前はまだない。", encoding="utf-8")
    ln_id = int(novels.import_file(source)["id"])
    with novels._connect() as conn:
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (ln_id, 1, "Part 2", "どこで生まれたか見当がつかぬ。", "part-2"),
        )
        texts = [
            str(row["text"])
            for row in conn.execute(
                "SELECT text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index", (ln_id,)
            )
        ]
    audio = AudiobookService(
        Database(cfg.library.database_path),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=cfg.paths.cache_dir,
    )
    audio_file = tmp_path / "book.m4b"
    audio_file.write_bytes(b"audio")
    audio_id = int(
        audio._upsert(
            path=audio_file,
            title="Book",
            duration=20.0,
            files=[
                {
                    "index": 0,
                    "path": str(audio_file),
                    "title": "Book",
                    "duration": 20.0,
                    "start": 0.0,
                    "end": 20.0,
                }
            ],
            chapters=[{"index": 0, "title": "Book", "start": 0.0, "end": 20.0}],
        )["id"]
    )
    audio.link_light_novel(ln_id, audio_id, prepare_alignment=False)
    chapters = []
    for index, text in enumerate(texts):
        length = len(normalize_reading_text(text))
        chapters.append(
            {
                "chapter_index": index,
                "title": f"Part {index + 1}",
                "normalized_length": length,
                "confidence": 0.9,
                "anchors": [
                    {"offset": 0, "time": 10.0 * index},
                    {"offset": length, "time": 10.0 * index + 9.0},
                ],
            }
        )
    return audio, ln_id, audio_id, chapters


def _cache(
    audio: AudiobookService, audio_id: int, chapters: list[dict], path: Path, revision: str | None
) -> None:
    processing = {"transcript_fingerprint": audio._transcript_fingerprint(audio_id)}
    if revision is not None:
        processing["alignment_algorithm_revision"] = revision
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "reading-audio-v3",
                "model": audio.stt_model,
                "confidence": 0.9,
                "chapters": chapters,
                "anchor_count": 4,
                "processing": processing,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_alignment_made_by_the_earlier_clock_is_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # v0.7.50 changed how the clock is built (punctuation pauses). An alignment cached by
    # the earlier "acoustic" clock is not used — neither at the cache path that clock had
    # nor through the migration of compatible caches — so the book is aligned again.
    audio, ln_id, audio_id, chapters = _paired_book(tmp_path)
    current_path = audio._alignment_path(ln_id, audio_id)
    with monkeypatch.context() as patch:
        patch.setattr(audiobooks, "_READING_AUDIO_ALIGNMENT_REVISION", "reading-audio-v3-acoustic")
        audio._fingerprint_cache.clear()
        old_path = audio._alignment_path(ln_id, audio_id)
    audio._fingerprint_cache.clear()
    assert old_path != current_path
    _cache(audio, audio_id, chapters, old_path, "reading-audio-v3-acoustic")
    assert audio.alignment_status(ln_id) == {"status": "not_prepared", "ready": False}
    assert not current_path.exists()

    # The same alignment without that revision mark is adopted (copied to the current path).
    _cache(audio, audio_id, chapters, old_path, None)
    status = audio.alignment_status(ln_id)
    assert status["ready"] is True and status["matched_chapters"] == 2
    assert current_path.is_file()
