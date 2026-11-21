"""A JP+EN file goes to the Japanese speech clock with its Japanese lines only, and the
output guard judges the clock against those lines.  Invented data only."""

from __future__ import annotations

import random
from pathlib import Path

import pudge.syncing as syncing
from pudge.config import SyncConfig
from pudge.subtitle_formats import english_lines_removed, parse_srt, write_srt

_KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
_ENGLISH = [
    "I think you are right about this",
    "What is that thing over there",
    "We have to go now and you know it",
]


def _bilingual(path: Path, count: int = 80) -> list[tuple[float, float, str]]:
    rng = random.Random(3)
    cues = []
    for index in range(count):
        start = 10.0 + 4.0 * index
        japanese = "".join(rng.choice(_KANA) for _ in range(12))
        cues.append((start, start + 2.5, japanese))
        cues.append((start + 0.1, start + 2.4, _ENGLISH[index % 3]))
    write_srt(cues, path, preserve_overlaps=True)
    return cues


def test_english_lines_are_removed_from_cues_and_from_inside_a_cue():
    cues = [
        (1.0, 2.0, "こんにちは"),
        (1.0, 2.0, "What is that thing"),
        (3.0, 4.0, "そうだね\nI think you are right"),
    ]
    kept, removed = english_lines_removed(cues)
    assert kept == [(1.0, 2.0, "こんにちは"), (3.0, 4.0, "そうだね")]
    assert removed == 2


def test_speech_clock_reads_only_the_japanese_lines_of_a_bilingual_file(tmp_path: Path, monkeypatch):
    video = tmp_path / "capture.ts"
    video.write_bytes(b"video")
    subtitle = tmp_path / "ep.ja-en.srt"
    _bilingual(subtitle)
    seen: list[Path] = []

    def fake_lines(_video, source, *_a, **_k):
        seen.append(source)
        return None, {"reason": "stt_alass_no_safe_map", "sync_was_successful": False}

    monkeypatch.setattr(syncing, "_try_japanese_stt_fallback_lines", fake_lines)
    _path, result = syncing._try_japanese_stt_fallback(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )
    assert seen and seen[0] != subtitle
    texts = [text for _s, _e, text in parse_srt(seen[0])]
    assert len(texts) == 80 and not any("you" in text for text in texts)
    assert result["speech_source_english_lines_removed"] == 80
    assert result["speech_source"] == str(seen[0])

    # A Japanese-only file is passed through unchanged.
    plain = tmp_path / "ep.ja.srt"
    write_srt([(c[0], c[1], c[2]) for c in parse_srt(seen[0])], plain)
    seen.clear()
    _path, result = syncing._try_japanese_stt_fallback(
        video,
        plain,
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )
    assert seen == [plain] and "speech_source" not in result


def test_output_guard_compares_against_the_japanese_lines_the_clock_aligned(tmp_path: Path):
    subtitle = tmp_path / "ep.ja-en.srt"
    _bilingual(subtitle)
    japanese, _removed = english_lines_removed(parse_srt(subtitle))
    source = tmp_path / "japanese-lines.srt"
    write_srt(japanese, source)
    output = tmp_path / "clocked.srt"
    write_srt([(a + 130.0, b + 130.0, t) for a, b, t in japanese], output)

    against_source = {"sync_was_successful": True, "reason": "applied", "speech_source": str(source)}
    syncing._guard_alignment_result(subtitle, output, against_source, tmp_path / "cache")
    guard = against_source["alignment_output_guard"]
    assert guard["status"] != "rejected", guard
    assert guard["diagnostics"]["raw_cues"] == len(japanese)
