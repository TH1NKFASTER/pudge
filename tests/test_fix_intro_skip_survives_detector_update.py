"""A detector update keeps the previous detector's Skip segments until the file is recomputed."""

from __future__ import annotations

from pathlib import Path

from pudge import intro_skipper
from pudge.intro_skipper import IntroSkipperCache, identity_of, segments_for_video


def _result(algorithm: str, start: float) -> dict:
    return {
        "algorithm": algorithm,
        "status": "found",
        "segments": [
            {"kind": "intro", "start_seconds": start, "end_seconds": start + 89.0, "confidence": 0.9}
        ],
    }


def _video(tmp_path: Path) -> Path:
    video = tmp_path / "Show - 11.mkv"
    video.write_bytes(b"x" * 64)
    return video


def test_previous_detector_results_still_give_skip_segments(tmp_path) -> None:
    video = _video(tmp_path)
    old = intro_skipper.ALGORITHM_VERSION.replace(intro_skipper.DETECTOR_VERSION, "intro-2")
    assert old in intro_skipper.PLAYBACK_FALLBACK_VERSIONS
    IntroSkipperCache(tmp_path / "cache").write_result(identity_of(video), _result(old, 88.2))
    assert [(s.kind, s.start_seconds) for s in segments_for_video(tmp_path / "cache", video)] == [
        ("intro", 88.2)
    ]


def test_recomputed_result_wins_and_unknown_versions_are_ignored(tmp_path) -> None:
    video = _video(tmp_path)
    cache = IntroSkipperCache(tmp_path / "cache")
    cache.write_result(identity_of(video), _result("pudge-hk-0+intro-1", 10.0))
    assert segments_for_video(tmp_path / "cache", video) == []
    cache.write_result(identity_of(video), _result(intro_skipper.ALGORITHM_VERSION, 80.0))
    assert [s.start_seconds for s in segments_for_video(tmp_path / "cache", video)] == [80.0]


def test_old_results_still_count_as_needing_analysis(tmp_path) -> None:
    old = intro_skipper.ALGORITHM_VERSION.replace(intro_skipper.DETECTOR_VERSION, "intro-2")
    assert intro_skipper._needs_analysis(_result(old, 1.0), set(), 0.0)
