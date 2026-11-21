"""A cached Japanese transcript reports the files of this cache, not where it was made.

A cache copied from another machine (or folder) kept the recorded absolute paths: a step that
opened the word transcript found nothing there, so a cached run took other steps than the run
that transcribed. Invented data only."""

from __future__ import annotations

import json
from pathlib import Path

from pudge.subtitles import stt


def _cached(tmp_path: Path, monkeypatch, *, with_words: bool) -> tuple[Path, Path]:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"not really a video")
    monkeypatch.setattr(
        stt, "_select_japanese_audio_stream", lambda *a, **k: (0, {"start_offset_seconds": 0.0})
    )
    monkeypatch.setattr(stt, "_sampled_content_fingerprint", lambda path: "fp")
    monkeypatch.setattr(stt.audio_timeline, "inner_gaps", lambda *a, **k: None)
    cache = tmp_path / "cache"
    root = cache / "stt" / stt._cache_key("fp", "tiny", 0, 0.0, None)
    root.mkdir(parents=True)
    (root / "reference.ja.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nあいう\n", encoding="utf-8")
    elsewhere = "/somewhere/else/pudge-cache/stt/old"
    (root / "metadata.json").write_text(
        json.dumps(
            {
                "model": "tiny",
                "reference_path": f"{elsewhere}/reference.ja.srt",
                "transcript_path": f"{elsewhere}/transcription.json",
            }
        ),
        encoding="utf-8",
    )
    if with_words:
        (root / "transcription.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
    return video, cache


def test_cache_hit_points_at_its_own_transcript(tmp_path: Path, monkeypatch) -> None:
    video, cache = _cached(tmp_path, monkeypatch, with_words=True)
    reference, info = stt.prepare_japanese_stt_reference(
        video, cache, ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", model="tiny", timeout_seconds=60
    )
    assert info["cache"] == "hit"
    assert info["reference_path"] == str(reference)
    assert info["transcript_path"] == str(reference.parent / "transcription.json")


def test_cache_hit_without_word_transcript_says_so(tmp_path: Path, monkeypatch) -> None:
    video, cache = _cached(tmp_path, monkeypatch, with_words=False)
    reference, info = stt.prepare_japanese_stt_reference(
        video, cache, ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", model="tiny", timeout_seconds=60
    )
    assert info["cache"] == "hit" and info["reference_path"] == str(reference)
    assert "transcript_path" not in info
