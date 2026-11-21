"""Embedded timing tracks: untagged tracks count, typesetting fragments do not.

SHIROBAKO E12 (sync benchmark): the release's only text track is tagged "und"; it was
ignored, audio-only timing failed and the raw Netflix file was shown 0.47 s off.
BLUE REFLECTION RAY E07: the English track carries an animated sign as one event per
letter (6277 events around ~600 lines), which swamped every line-start comparison.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pudge import syncing
from pudge.subtitle_formats import parse_srt, write_exact_srt


def _stream(
    index: int, language: str | None, title: str = "", *, default: int = 0, codec: str = "ass"
) -> dict:
    tags = {"title": title}
    if language is not None:
        tags["language"] = language
    return {
        "index": index,
        "codec_type": "subtitle",
        "codec_name": codec,
        "tags": tags,
        "disposition": {"default": default, "forced": 0},
    }


@pytest.mark.parametrize("language", ["und", None, "por", "chi"])
def test_an_untagged_or_other_language_track_is_a_timing_reference(language: str | None) -> None:
    selected = syncing._select_timing_reference_stream([_stream(2, language)])
    assert selected is not None and selected["index"] == 2


def test_english_still_wins_and_signs_or_japanese_stay_out() -> None:
    assert syncing._select_timing_reference_stream([_stream(2, "und"), _stream(3, "eng")])["index"] == 3
    assert syncing._select_timing_reference_stream([_stream(2, "und", "Signs & Songs")]) is None
    assert syncing._select_timing_reference_stream([_stream(2, "jpn")]) is None


def _lines() -> list[tuple[float, float, str]]:
    return [(10.0 + i * 4.0, 12.5 + i * 4.0, f"Line number {i} of the dialogue") for i in range(150)]


def _letter_sign(at: float, word: str = "TheSubtitle") -> list[tuple[float, float, str]]:
    return [(at + k * 0.01, at + 0.2 + k * 0.01, letter) for k, letter in enumerate(word)]


def test_letter_by_letter_sign_events_are_dropped_from_the_timing_track() -> None:
    cues = sorted(
        _lines() + [cue for at in range(20, 600, 6) for cue in _letter_sign(float(at) + 0.7)],
        key=lambda cue: cue[0],
    )
    kept, dropped = syncing._drop_typesetting_fragments(cues)
    assert dropped == len(cues) - 150
    assert kept == _lines()


def test_a_track_with_a_few_short_lines_is_kept_whole() -> None:
    cues = sorted(_lines() + _letter_sign(301.0), key=lambda cue: cue[0])
    kept, dropped = syncing._drop_typesetting_fragments(cues)
    assert dropped == 0 and kept == cues


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_extracted_timing_reference_has_only_the_lines(tmp_path: Path) -> None:
    track = tmp_path / "track.srt"
    write_exact_srt(
        sorted(_lines() + [cue for at in range(20, 600, 6) for cue in _letter_sign(float(at) + 0.7)]),
        track,
    )
    video = tmp_path / "episode.mkv"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(track), "-map", "0:s", "-c:s", "srt",
            "-metadata:s:s:0", "language=und", str(video),
        ],
        check=True,
        timeout=60,
    )  # fmt: skip
    output, result = syncing.extract_embedded_timing_reference(video, tmp_path / "cache")
    assert output is not None, result
    assert result["typesetting_fragments_removed"] > 0
    assert len(parse_srt(output)) == 150


@pytest.mark.parametrize("title", ["Typesetting", "TS", "S&S", "Lyrics", "OP/ED", "Titles"])
def test_untagged_sign_and_song_tracks_are_not_timing_references(title: str) -> None:
    # Review: the +40 for untagged tracks must not admit signs/typesetting/lyrics tracks.
    assert syncing._select_timing_reference_stream([_stream(2, "und", title)]) is None


def test_full_subtitles_is_still_dialogue() -> None:
    assert syncing._select_timing_reference_stream([_stream(2, "und", "Full Subtitles [sam]")]) is not None


def _mkv(tmp_path: Path, cues: list[tuple[float, float, str]], language: str = "und") -> Path:
    track = tmp_path / "track.srt"
    write_exact_srt(cues, track)
    video = tmp_path / "episode.mkv"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(track), "-map", "0:s", "-c:s", "srt",
         "-metadata:s:s:0", f"language={language}", str(video)],
        check=True,
        timeout=60,
    )  # fmt: skip
    return video


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_a_sparse_track_is_rejected_after_extraction(tmp_path: Path) -> None:
    # 30 lines over 20 minutes (1.5 a minute): a signs track under an innocent name.
    cues = [(10.0 + i * 40.0, 12.0 + i * 40.0, f"Sign number {i}") for i in range(30)]
    output, result = syncing.extract_embedded_timing_reference(_mkv(tmp_path, cues), tmp_path / "cache")
    assert output is None and result["reason"] == "timing_reference_too_sparse", result


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_a_cached_reference_keeps_its_key_and_loses_its_fragments(tmp_path: Path) -> None:
    # Review: no new cache key (re-extraction takes minutes on a NAS); a reference cached
    # with its fragments is cleaned when it is read.
    noisy = sorted(_lines() + [cue for at in range(20, 600, 6) for cue in _letter_sign(float(at) + 0.7)])
    video = _mkv(tmp_path, _lines())
    first, result = syncing.extract_embedded_timing_reference(video, tmp_path / "cache")
    assert first is not None and result["reason"] == "applied", result
    write_exact_srt(noisy, first)  # as a version without the filter left it
    again, cached = syncing.extract_embedded_timing_reference(video, tmp_path / "cache")
    assert again == first and cached["reason"] == "cached"
    assert cached["typesetting_fragments_removed"] == len(noisy) - 150
    assert len(parse_srt(first)) == 150


def test_an_untagged_reference_is_not_given_to_alass_when_the_timelines_disagree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pudge.config import SyncConfig

    video, source, reference = tmp_path / "v.mkv", tmp_path / "s.srt", tmp_path / "r.srt"
    for path in (video, source, reference):
        path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(syncing, "probe_container_edit_points", lambda *a, **k: [])
    monkeypatch.setattr(
        syncing,
        "extract_embedded_timing_reference",
        lambda *a, **k: (reference, {"reason": "applied", "timing_reference_other_language": True}),
    )
    monkeypatch.setattr(syncing, "_exact_release_zero_offset_result", lambda *a, **k: None)
    monkeypatch.setattr(
        syncing,
        "align_subtitle_timelines",
        lambda *a, **k: (source, {"reason": "timeline_unstable_segments"}),
    )
    seen = []

    def alass(reference_or_video, subtitle, *a, **k):
        seen.append(reference_or_video)
        return subtitle, {"reason": "alass_missing", "sync_was_successful": False}

    monkeypatch.setattr(syncing, "synchronize_with_alass", alass)
    syncing._optimize_subtitle_unguarded(video, source, tmp_path / "cache", SyncConfig(engine="alass"))
    assert seen == [video]
