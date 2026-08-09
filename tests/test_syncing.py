from __future__ import annotations

import logging
import sys
import types
from argparse import Namespace
from pathlib import Path

from anime_mpv.config import SyncConfig
from anime_mpv.models import SubtitleCandidate
from anime_mpv.syncing import (
    optimize_candidates,
    synchronize_subtitle,
    synchronize_with_alass,
)


class FakeParser:
    def __init__(self, seen: list[str]):
        self.seen = seen

    def parse_args(self, arguments: list[str]) -> Namespace:
        self.seen.extend(arguments)
        assert "--quality-max-offset-seconds" not in arguments
        assert "--skip-sync-on-low-quality" not in arguments
        output = arguments[arguments.index("-o") + 1]
        return Namespace(srtout=output)


def install_fake_ffsubsync(monkeypatch, *, offset: float, score: float = 1234.0) -> list[str]:
    seen: list[str] = []
    package = types.ModuleType("ffsubsync")
    implementation = types.ModuleType("ffsubsync.ffsubsync")
    implementation.logger = logging.getLogger(f"fake.ffsubsync.{id(implementation)}")  # type: ignore[attr-defined]
    implementation.logger.propagate = False  # type: ignore[attr-defined]

    def make_parser() -> FakeParser:
        return FakeParser(seen)

    def run(args: Namespace) -> dict[str, object]:
        Path(args.srtout).write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
        implementation.logger.info("score: %.3f", score)  # type: ignore[attr-defined]
        return {
            "retval": 0,
            "sync_was_successful": True,
            "offset_seconds": offset,
            "framerate_scale_factor": 1.0,
        }

    implementation.make_parser = make_parser  # type: ignore[attr-defined]
    implementation.run = run  # type: ignore[attr-defined]
    package.ffsubsync = implementation  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ffsubsync", package)
    monkeypatch.setitem(sys.modules, "ffsubsync.ffsubsync", implementation)
    return seen


def test_ffsubsync_031_compatible_arguments_and_applied_result(tmp_path: Path, monkeypatch):
    seen = install_fake_ffsubsync(monkeypatch, offset=2.5, score=777.0)
    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("source", encoding="utf-8")

    output, result = synchronize_subtitle(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="/opt/homebrew/bin/ffmpeg",
    )

    assert output != subtitle
    assert output.exists()
    assert result["reason"] == "applied"
    assert result["sync_was_successful"] is True
    assert result["alignment_score"] == 777.0
    assert "--ffmpeg-path" in seen

    cached_output, cached_result = synchronize_subtitle(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="/opt/homebrew/bin/ffmpeg",
    )
    assert cached_output == output
    assert cached_result["reason"] == "cached"
    assert cached_result["alignment_score"] == 777.0


def test_large_offset_is_rejected_by_our_quality_gate(tmp_path: Path, monkeypatch):
    install_fake_ffsubsync(monkeypatch, offset=-80.0)
    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("source", encoding="utf-8")

    output, result = synchronize_subtitle(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(quality_max_offset_seconds=45.0, skip_on_low_quality=True),
    )

    assert output == subtitle
    assert result["reason"] == "quality_offset_exceeded"
    assert result["sync_was_successful"] is False


def test_alass_uses_safe_temp_names_and_writes_output(tmp_path: Path):
    fake = tmp_path / "fake-alass"
    fake.write_text("#!/bin/sh\ncp \"$2\" \"$3\"\n", encoding="utf-8")
    fake.chmod(0o755)
    video = tmp_path / "[Group] episode.mkv"
    subtitle = tmp_path / "[Subs] episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")

    output, result = synchronize_with_alass(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(),
        alass_path=str(fake),
    )

    assert result["reason"] == "applied"
    assert result["engine"] == "alass"
    assert output.exists()
    assert "日本語" in output.read_text(encoding="utf-8")

    cached_output, cached_result = synchronize_with_alass(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(),
        alass_path=str(fake),
    )
    assert cached_output == output
    assert cached_result["reason"] == "cached"
    assert cached_result["engine"] == "alass"
    assert cached_result["reference_kind"] == "media"


def test_candidate_optimizer_prefers_best_alignment_score(tmp_path: Path, monkeypatch):
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    weak = tmp_path / "weak.srt"
    strong = tmp_path / "strong.srt"
    weak.write_text("weak", encoding="utf-8")
    strong.write_text("strong", encoding="utf-8")
    candidates = [
        SubtitleCandidate(weak, "jimaku", 100.0, "weak.srt"),
        SubtitleCandidate(strong, "jimaku", 80.0, "strong.srt"),
    ]

    def fake_prepare(video, cache_dir, config, **kwargs):
        reference = cache_dir / "speech.npz"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(b"npz")
        return reference, {"reason": "applied"}

    def fake_sync(video, subtitle, cache_dir, config, **kwargs):
        score = 100.0 if subtitle.name == "weak.srt" else 900.0
        return subtitle, {
            "reason": "applied",
            "sync_was_successful": True,
            "alignment_score": score,
            "offset_seconds": 0.0,
        }

    def fake_optimize(video, subtitle, cache_dir, config, **kwargs):
        return subtitle, {
            "reason": "applied",
            "sync_was_successful": True,
            "alignment_score": 900.0,
            "engine": "ffsubsync",
        }

    monkeypatch.setattr("anime_mpv.syncing.prepare_speech_reference", fake_prepare)
    monkeypatch.setattr("anime_mpv.syncing.synchronize_subtitle", fake_sync)
    monkeypatch.setattr("anime_mpv.syncing.optimize_subtitle", fake_optimize)
    candidate, output, result = optimize_candidates(
        video,
        candidates,
        tmp_path / "cache",
        SyncConfig(),
    )

    assert candidate is not None
    assert candidate.name == "strong.srt"
    assert output == strong
    assert result["alignment_score"] == 900.0


def test_candidate_optimizer_prefers_srt_within_alignment_tolerance(tmp_path: Path, monkeypatch):
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    native_srt = tmp_path / "episode.srt"
    styled_ass = tmp_path / "episode.ass"
    native_srt.write_text("srt", encoding="utf-8")
    styled_ass.write_text("ass", encoding="utf-8")
    candidates = [
        SubtitleCandidate(native_srt, "jimaku", 90.0, native_srt.name),
        SubtitleCandidate(styled_ass, "jimaku", 90.0, styled_ass.name),
    ]

    def fake_prepare(video, cache_dir, config, **kwargs):
        reference = cache_dir / "speech.npz"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(b"npz")
        return reference, {"reason": "applied"}

    def fake_sync(video, subtitle, cache_dir, config, **kwargs):
        score = 67557.0 if subtitle.suffix == ".srt" else 67566.0
        return subtitle, {
            "reason": "applied",
            "sync_was_successful": True,
            "alignment_score": score,
            "offset_seconds": -32.8,
        }

    selected: list[Path] = []

    def fake_optimize(video, subtitle, cache_dir, config, **kwargs):
        selected.append(subtitle)
        return subtitle, {
            "reason": "applied",
            "sync_was_successful": True,
            "alignment_score": 67557.0,
            "engine": "ffsubsync",
        }

    monkeypatch.setattr("anime_mpv.syncing.prepare_speech_reference", fake_prepare)
    monkeypatch.setattr("anime_mpv.syncing.synchronize_subtitle", fake_sync)
    monkeypatch.setattr("anime_mpv.syncing.optimize_subtitle", fake_optimize)

    candidate, _, result = optimize_candidates(
        video,
        candidates,
        tmp_path / "cache",
        SyncConfig(),
        prefer_srt=True,
        srt_tolerance_ratio=0.002,
        srt_tolerance_absolute=50.0,
    )

    assert candidate is not None
    assert candidate.path == native_srt
    assert selected == [native_srt]
    assert result["candidate_selection"]["srt_tolerance"] >= 50.0


def test_auto_engine_uses_local_segments_not_only_global_score(tmp_path: Path, monkeypatch):
    from anime_mpv.syncing import optimize_subtitle

    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "source.srt"
    ff_output = tmp_path / "ff.srt"
    alass_output = tmp_path / "alass.srt"
    for path in (video, subtitle, ff_output, alass_output):
        path.write_text("x", encoding="utf-8")

    def fake_sync(video, subtitle, cache_dir, config, **kwargs):
        if kwargs.get("tag") == "alass-finish":
            return alass_output, {
                "reason": "applied",
                "sync_was_successful": True,
                "alignment_score": 49265.0,
                "offset_seconds": 0.6,
                "framerate_scale_factor": 1.0,
            }
        return ff_output, {
            "reason": "applied",
            "sync_was_successful": True,
            "alignment_score": 67566.0,
            "offset_seconds": -32.8,
            "framerate_scale_factor": 1.0,
        }

    def fake_alass(*args, **kwargs):
        return alass_output, {
            "reason": "applied",
            "sync_was_successful": True,
            "engine": "alass",
            "alass_blocks": 3,
            "alass_distinct_shifts": 3,
        }

    def fake_segments(video, path, *args, **kwargs):
        bad = path == ff_output
        offsets = [28.0, 0.2, -12.0] if bad else [0.3, -0.1, 0.4]
        return {
            "reason": "evaluated",
            "available": True,
            "attempted_segments": 3,
            "successful_segments": 3,
            "max_abs_offset_seconds": max(abs(x) for x in offsets),
            "mean_abs_offset_seconds": sum(abs(x) for x in offsets) / 3,
            "offset_spread_seconds": max(offsets) - min(offsets),
            "segments": [],
        }

    monkeypatch.setattr("anime_mpv.syncing.synchronize_subtitle", fake_sync)
    monkeypatch.setattr("anime_mpv.syncing.synchronize_with_alass", fake_alass)
    monkeypatch.setattr("anime_mpv.syncing.evaluate_segment_alignment", fake_segments)
    monkeypatch.setattr(
        "anime_mpv.syncing._maybe_repair_piecewise",
        lambda video, path, result, *args, **kwargs: (path, result),
    )

    output, result = optimize_subtitle(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(segment_validation=True, piecewise_repair=False),
    )

    assert output == alass_output
    assert str(result["engine"]).startswith("alass")
    assert result["alignment_score"] == 49265.0


def test_piecewise_repair_uses_different_offsets_across_episode(tmp_path: Path):
    from anime_mpv.subtitle_formats import parse_srt
    from anime_mpv.syncing import apply_piecewise_repair

    subtitle = tmp_path / "episode.srt"
    subtitle.write_text(
        "1\n00:00:10,000 --> 00:00:12,000\n最初\n\n"
        "2\n00:10:00,000 --> 00:10:02,000\n最後\n",
        encoding="utf-8",
    )
    diagnostics = {
        "available": True,
        "segments": [
            {"successful": True, "center_seconds": 30.0, "offset_seconds": 2.0},
            {"successful": True, "center_seconds": 630.0, "offset_seconds": -3.0},
        ],
    }

    output, result = apply_piecewise_repair(
        subtitle,
        diagnostics,
        tmp_path / "cache",
        SyncConfig(piecewise_repair=True, piecewise_jump_threshold_seconds=2.5),
        ffmpeg_path="/missing/ffmpeg",
    )

    assert result["applied"] is True
    cues = parse_srt(output)
    assert cues[0][0] == 12.0
    assert cues[1][0] == 597.0


def test_short_segment_window_is_not_forced_to_thirty_seconds():
    from anime_mpv.syncing import _segment_windows

    windows = _segment_windows(600.0, 5, 15.0)

    assert len(windows) == 5
    assert all(length == 15.0 for _, _, length in windows)


def test_local_clip_keeps_cues_outside_nominal_window_with_padding():
    from anime_mpv.syncing import _clip_cues_for_local_alignment

    cues = [
        (35.0, 37.0, "opening cue"),
        (80.0, 82.0, "too far"),
    ]

    clipped = _clip_cues_for_local_alignment(
        cues,
        start=0.0,
        duration=15.0,
        padding_seconds=45.0,
    )

    assert clipped == [(80.0, 82.0, "opening cue")]


def test_piecewise_repair_uses_original_source_instead_of_shifted_output(tmp_path: Path, monkeypatch):
    from anime_mpv.syncing import _maybe_repair_piecewise

    video = tmp_path / "video.mkv"
    source = tmp_path / "source.srt"
    globally_shifted = tmp_path / "global.srt"
    repaired = tmp_path / "repaired.srt"
    for path in (video, source, globally_shifted, repaired):
        path.write_text("x", encoding="utf-8")

    baseline = {
        "available": True,
        "successful_segments": 3,
        "max_abs_offset_seconds": 20.0,
        "mean_abs_offset_seconds": 8.0,
        "offset_spread_seconds": 20.0,
        "segments": [],
    }
    source_diagnostics = {
        "available": True,
        "successful_segments": 3,
        "max_abs_offset_seconds": 32.0,
        "mean_abs_offset_seconds": 12.0,
        "offset_spread_seconds": 32.0,
        "segments": [],
    }
    fixed = {
        "available": True,
        "successful_segments": 3,
        "max_abs_offset_seconds": 0.4,
        "mean_abs_offset_seconds": 0.2,
        "offset_spread_seconds": 0.5,
        "segments": [],
    }

    def fake_evaluate(video, subtitle, *args, **kwargs):
        if subtitle == source:
            return source_diagnostics
        if subtitle == repaired:
            return fixed
        return baseline

    seen: list[Path] = []

    def fake_apply(subtitle, diagnostics, *args, **kwargs):
        seen.append(subtitle)
        assert diagnostics is source_diagnostics
        return repaired, {"applied": True, "reason": "applied"}

    monkeypatch.setattr("anime_mpv.syncing.evaluate_segment_alignment", fake_evaluate)
    monkeypatch.setattr("anime_mpv.syncing.apply_piecewise_repair", fake_apply)

    output, result = _maybe_repair_piecewise(
        video,
        globally_shifted,
        {
            "reason": "applied",
            "engine": "ffsubsync",
            "segment_diagnostics": baseline,
        },
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        force=False,
        verbose=False,
        source_path=source,
    )

    assert seen == [source]
    assert output == repaired
    assert result["engine"] == "ffsubsync+piecewise-source"


def test_diagnostic_rank_prioritizes_opening_and_ending_coverage():
    from anime_mpv.syncing import _diagnostic_rank

    middle_only = {
        "available": True,
        "successful_segments": 50,
        "boundary_successful_segments": 0,
        "successful_ratio": 0.9,
        "max_abs_offset_seconds": 0.1,
        "mean_abs_offset_seconds": 0.05,
        "offset_spread_seconds": 0.1,
    }
    full_coverage = {
        "available": True,
        "successful_segments": 10,
        "boundary_successful_segments": 2,
        "successful_ratio": 0.5,
        "max_abs_offset_seconds": 0.4,
        "mean_abs_offset_seconds": 0.2,
        "offset_spread_seconds": 0.5,
    }

    assert _diagnostic_rank(full_coverage) > _diagnostic_rank(middle_only)


def test_segment_reliability_rejects_boundary_oscillation():
    from anime_mpv.syncing import _segment_reliability

    offsets = [9.93, -44.99, 22.21, -43.77, 43.04, 44.92, -27.46, -43.61, 42.77, 45.0]
    result = _segment_reliability(offsets, max_offset=45.0, jump_threshold=2.5)

    assert result["reliable"] is False
    assert "boundary" in str(result["quality_reason"]) or "oscillation" in str(result["quality_reason"])


def test_piecewise_repair_refuses_unreliable_diagnostics(tmp_path: Path):
    from anime_mpv.syncing import apply_piecewise_repair

    subtitle = tmp_path / "episode.srt"
    subtitle.write_text(
        "1\n00:00:10,000 --> 00:00:12,000\n最初\n\n"
        "2\n00:10:00,000 --> 00:10:02,000\n最後\n",
        encoding="utf-8",
    )
    diagnostics = {
        "available": True,
        "reliable": False,
        "quality_reason": "full_range_oscillation",
        "segments": [
            {"successful": True, "center_seconds": 30.0, "offset_seconds": 45.0},
            {"successful": True, "center_seconds": 630.0, "offset_seconds": -45.0},
        ],
    }

    output, result = apply_piecewise_repair(
        subtitle,
        diagnostics,
        tmp_path / "cache",
        SyncConfig(piecewise_repair=True),
    )

    assert output == subtitle
    assert result["applied"] is False
    assert result["reason"] == "piecewise_unreliable_diagnostics"


def test_auto_engine_uses_global_score_when_all_local_diagnostics_are_unreliable(
    tmp_path: Path, monkeypatch
):
    from anime_mpv.syncing import optimize_subtitle

    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "source.srt"
    ff_output = tmp_path / "ff.srt"
    alass_output = tmp_path / "alass.srt"
    for path in (video, subtitle, ff_output, alass_output):
        path.write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        "anime_mpv.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (None, {"reason": "not_found"}),
    )

    def fake_sync(video, subtitle, cache_dir, config, **kwargs):
        if kwargs.get("tag") == "alass-finish":
            return alass_output, {
                "reason": "applied",
                "sync_was_successful": True,
                "alignment_score": 49406.0,
                "offset_seconds": 0.62,
                "framerate_scale_factor": 1.0,
            }
        return ff_output, {
            "reason": "applied",
            "sync_was_successful": True,
            "alignment_score": 67557.0,
            "offset_seconds": -32.81,
            "framerate_scale_factor": 1.0,
        }

    monkeypatch.setattr("anime_mpv.syncing.synchronize_subtitle", fake_sync)
    monkeypatch.setattr(
        "anime_mpv.syncing.synchronize_with_alass",
        lambda *args, **kwargs: (
            alass_output,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "engine": "alass",
                "alass_distinct_shifts": 5,
            },
        ),
    )
    unreliable = {
        "reason": "unreliable_segments",
        "available": True,
        "reliable": False,
        "quality_reason": "full_range_oscillation",
        "successful_segments": 20,
        "boundary_successful_segments": 2,
        "successful_ratio": 1.0,
        "max_abs_offset_seconds": 45.0,
        "mean_abs_offset_seconds": 35.0,
        "offset_spread_seconds": 90.0,
        "segments": [],
    }
    monkeypatch.setattr(
        "anime_mpv.syncing.evaluate_segment_alignment",
        lambda *args, **kwargs: dict(unreliable),
    )
    monkeypatch.setattr(
        "anime_mpv.syncing._maybe_repair_piecewise",
        lambda video, path, result, *args, **kwargs: (path, result),
    )

    output, result = optimize_subtitle(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(segment_validation=True, piecewise_repair=True),
    )

    assert output == ff_output
    assert result["engine"] == "ffsubsync"
    assert result["selection_reason"] == "global_score_fallback"
    assert result["local_diagnostics_ignored"] is True


def test_selects_english_default_embedded_timing_reference():
    from anime_mpv.syncing import _select_timing_reference_stream

    streams = [
        {
            "index": 11,
            "codec_type": "subtitle",
            "codec_name": "ass",
            "tags": {"language": "eng", "title": "CR"},
            "disposition": {"default": 1},
        },
        {
            "index": 12,
            "codec_type": "subtitle",
            "codec_name": "ass",
            "tags": {"language": "eng", "title": "Signs & Songs"},
            "disposition": {"default": 0},
        },
    ]

    selected = _select_timing_reference_stream(streams)

    assert selected is not None
    assert selected["index"] == 11


def test_llm_rejection_prevents_embedded_reference_alignment(tmp_path: Path, monkeypatch):
    from anime_mpv.syncing import optimize_subtitle

    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "japanese.srt"
    timing_reference = tmp_path / "english.srt"
    ff_output = tmp_path / "ff.srt"
    alass_output = tmp_path / "alass.srt"
    for path in (video, subtitle, timing_reference, ff_output, alass_output):
        path.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\ntext\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        "anime_mpv.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (
            timing_reference,
            {"reason": "applied", "language": "eng", "title": "CR"},
        ),
    )

    def fake_sync(video, subtitle, cache_dir, config, **kwargs):
        if kwargs.get("tag") == "ffsubsync":
            return ff_output, {
                "reason": "applied",
                "sync_was_successful": True,
                "alignment_score": 1000.0,
                "offset_seconds": -2.0,
                "framerate_scale_factor": 1.0,
            }
        return alass_output, {
            "reason": "applied",
            "sync_was_successful": True,
            "alignment_score": 500.0,
            "offset_seconds": 0.0,
            "framerate_scale_factor": 1.0,
        }

    seen_references: list[Path] = []

    def fake_alass(reference, *args, **kwargs):
        seen_references.append(reference)
        return alass_output, {
            "reason": "applied",
            "sync_was_successful": True,
            "engine": "alass",
        }

    class RejectingLLM:
        def compare_subtitle_semantics(self, *_args, **_kwargs):
            return {
                "accepted": False,
                "reason": "different episode",
                "similarity": 0.2,
                "matched_samples": 1,
                "total_samples": 6,
            }

    monkeypatch.setattr("anime_mpv.syncing.synchronize_subtitle", fake_sync)
    monkeypatch.setattr("anime_mpv.syncing.synchronize_with_alass", fake_alass)
    monkeypatch.setattr(
        "anime_mpv.syncing.evaluate_segment_alignment",
        lambda *args, **kwargs: {
            "available": False,
            "reliable": False,
            "quality_reason": "not_available",
        },
    )
    monkeypatch.setattr(
        "anime_mpv.syncing._maybe_repair_piecewise",
        lambda video, path, result, *args, **kwargs: (path, result),
    )

    output, result = optimize_subtitle(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(engine="auto", compare_engines=True),
        llm=RejectingLLM(),
        validate_embedded_reference_with_llm=True,
    )

    assert seen_references == [timing_reference, video]
    assert output == ff_output
    assert result["engine"] == "ffsubsync"
    assert result["timing_reference_validation"]["accepted"] is False


def test_llm_acceptance_allows_embedded_reference_alignment(tmp_path: Path, monkeypatch):
    from anime_mpv.syncing import optimize_subtitle

    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "japanese.srt"
    timing_reference = tmp_path / "english.srt"
    ff_output = tmp_path / "ff.srt"
    alass_output = tmp_path / "alass.srt"
    payload = "1\n00:00:01,000 --> 00:00:02,000\ntext\n"
    for path in (video, subtitle, timing_reference, ff_output, alass_output):
        path.write_text(payload, encoding="utf-8")

    monkeypatch.setattr(
        "anime_mpv.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (
            timing_reference,
            {"reason": "applied", "language": "eng", "title": "CR"},
        ),
    )

    def fake_sync(video, subtitle, cache_dir, config, **kwargs):
        assert kwargs.get("tag") != "embedded-reference-finish"
        return ff_output, {
            "reason": "applied",
            "sync_was_successful": True,
            "alignment_score": 1000.0,
            "offset_seconds": -2.0,
            "framerate_scale_factor": 1.0,
        }

    seen_references: list[Path] = []

    def fake_alass(reference, *args, **kwargs):
        seen_references.append(reference)
        return alass_output, {
            "reason": "applied",
            "sync_was_successful": True,
            "engine": "alass",
            "alass_blocks": 3,
            "alass_distinct_shifts": 2,
        }

    class AcceptingLLM:
        def compare_subtitle_semantics(self, *_args, **_kwargs):
            return {
                "accepted": True,
                "reason": "same episode",
                "similarity": 0.84,
                "matched_samples": 5,
                "total_samples": 6,
            }

    monkeypatch.setattr("anime_mpv.syncing.synchronize_subtitle", fake_sync)
    monkeypatch.setattr("anime_mpv.syncing.synchronize_with_alass", fake_alass)

    output, result = optimize_subtitle(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(engine="auto", compare_engines=True),
        llm=AcceptingLLM(),
        validate_embedded_reference_with_llm=True,
    )

    assert seen_references == [timing_reference]
    assert output == alass_output
    assert result["selection_reason"] == "embedded_timing_reference"
    assert result["engine"] == "embedded-reference+alass"
    assert result["timing_reference_validation"]["accepted"] is True


def test_timing_activity_detects_cold_open_improvement(tmp_path: Path):
    from anime_mpv.syncing import compare_timing_activity, _embedded_reference_is_better

    reference = tmp_path / "reference.srt"
    baseline = tmp_path / "baseline.srt"
    aligned = tmp_path / "aligned.srt"

    reference.write_text(
        "1\n00:00:10,000 --> 00:00:12,000\nA\n\n"
        "2\n00:03:30,000 --> 00:03:32,000\nB\n\n"
        "3\n00:10:00,000 --> 00:10:02,000\nC\n",
        encoding="utf-8",
    )
    # The main episode is right, but the cold open is shifted by 32 seconds.
    baseline.write_text(
        "1\n00:00:42,000 --> 00:00:44,000\nA\n\n"
        "2\n00:03:30,000 --> 00:03:32,000\nB\n\n"
        "3\n00:10:00,000 --> 00:10:02,000\nC\n",
        encoding="utf-8",
    )
    aligned.write_text(reference.read_text(encoding="utf-8"), encoding="utf-8")

    baseline_metrics = compare_timing_activity(baseline, reference)
    aligned_metrics = compare_timing_activity(aligned, reference)
    accepted, reason = _embedded_reference_is_better(baseline_metrics, aligned_metrics)

    assert aligned_metrics["start"] > baseline_metrics["start"]
    assert accepted is True
    assert reason in {"start_improved", "weighted_improved", "strong_reference_alignment"}


def test_embedded_reference_rejects_middle_degradation():
    from anime_mpv.syncing import _embedded_reference_is_better

    accepted, reason = _embedded_reference_is_better(
        {"available": True, "start": 0.2, "middle": 0.95, "weighted": 0.7, "full": 0.8},
        {"available": True, "start": 0.9, "middle": 0.7, "weighted": 0.75, "full": 0.8},
    )

    assert accepted is False
    assert reason == "middle_degraded"


def test_llm_accepted_alass_failure_is_reported_without_local_noise(tmp_path: Path, monkeypatch):
    from anime_mpv.syncing import optimize_subtitle

    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "japanese.srt"
    timing_reference = tmp_path / "english.srt"
    ff_output = tmp_path / "ff.srt"
    payload = "1\n00:00:01,000 --> 00:00:02,000\ntext\n"
    for path in (video, subtitle, timing_reference, ff_output):
        path.write_text(payload, encoding="utf-8")

    monkeypatch.setattr(
        "anime_mpv.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (
            timing_reference,
            {"reason": "applied", "language": "eng", "title": "CR"},
        ),
    )
    monkeypatch.setattr(
        "anime_mpv.syncing.synchronize_subtitle",
        lambda *args, **kwargs: (
            ff_output,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "alignment_score": 1000.0,
                "offset_seconds": -32.8,
                "framerate_scale_factor": 1.0,
            },
        ),
    )
    monkeypatch.setattr(
        "anime_mpv.syncing.synchronize_with_alass",
        lambda *args, **kwargs: (
            subtitle,
            {
                "reason": "alass_error",
                "sync_was_successful": False,
                "error": "invalid subtitle input",
            },
        ),
    )

    class AcceptingLLM:
        def compare_subtitle_semantics(self, *_args, **_kwargs):
            return {
                "accepted": True,
                "reason": "same episode",
                "similarity": 0.92,
                "matched_samples": 6,
                "total_samples": 6,
            }

    monkeypatch.setattr(
        "anime_mpv.syncing.evaluate_segment_alignment",
        lambda *args, **kwargs: {
            "available": False,
            "reliable": False,
            "quality_reason": "not_available",
        },
    )

    output, result = optimize_subtitle(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(engine="auto", compare_engines=True, segment_validation=True),
        llm=AcceptingLLM(),
        validate_embedded_reference_with_llm=True,
    )

    assert output == ff_output
    assert result["engine"] == "ffsubsync"
    assert result["embedded_reference_failure_reason"] == "alass_error"
    assert result["embedded_reference_failure_error"] == "invalid subtitle input"


def test_alass_normalizes_srt_reference_and_source(tmp_path: Path):
    fake = tmp_path / "fake-alass"
    fake.write_text(
        "#!/bin/sh\n"
        "grep -q $'\\xEF\\xBB\\xBF' \"$1\" && exit 11\n"
        "grep -q $'\\xEF\\xBB\\xBF' \"$2\" && exit 12\n"
        "cp \"$2\" \"$3\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    reference = tmp_path / "english.srt"
    source = tmp_path / "japanese.srt"
    reference.write_text("\ufeff1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
    source.write_text("\ufeff1\n00:00:02,000 --> 00:00:03,000\nこんにちは\n", encoding="utf-8")

    output, result = synchronize_with_alass(
        reference,
        source,
        tmp_path / "cache",
        SyncConfig(),
        alass_path=str(fake),
        force=True,
    )

    assert result["reason"] == "applied"
    assert result["reference_kind"] == "subtitle"
    assert output.suffix == ".srt"
    assert output.read_bytes().startswith(b"1\n")


def test_relative_semantic_samples_ignore_constant_timing_shift(tmp_path: Path):
    from anime_mpv.llm import build_subtitle_semantic_samples
    from anime_mpv.subtitle_formats import write_srt

    japanese = tmp_path / "japanese.srt"
    english = tmp_path / "english.srt"
    write_srt(
        [(10.0 + index * 5.0, 12.0 + index * 5.0, f"JA-{index}") for index in range(24)],
        japanese,
    )
    write_srt(
        [(38.0 + index * 5.0, 40.0 + index * 5.0, f"EN-{index}") for index in range(24)],
        english,
    )

    samples = build_subtitle_semantic_samples(
        japanese,
        english,
        sample_count=6,
        phrases_per_sample=3,
        alignment_mode="relative",
    )

    assert len(samples) == 6
    for sample in samples:
        ja_indexes = [int(text.split("-")[1]) for text in sample["japanese"]]
        en_indexes = [int(text.split("-")[1]) for text in sample["english"]]
        assert ja_indexes == en_indexes
        assert sample["alignment_mode"] == "relative"


def test_embedded_reference_uses_constant_offset_sampling_when_audio_sync_fails(
    tmp_path: Path, monkeypatch
):
    from anime_mpv.subtitle_formats import write_srt
    from anime_mpv.syncing import optimize_subtitle

    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "japanese.srt"
    timing_reference = tmp_path / "english.srt"
    alass_output = tmp_path / "alass.srt"
    source_cues = [
        (10.0 + index * 5.0, 12.0 + index * 5.0, f"JA-{index}")
        for index in range(24)
    ]
    aligned_cues = [
        (38.0 + index * 5.0, 40.0 + index * 5.0, f"JA-{index}")
        for index in range(24)
    ]
    reference_cues = [
        (38.0 + index * 5.0, 40.0 + index * 5.0, f"EN-{index}")
        for index in range(24)
    ]
    write_srt(source_cues, subtitle)
    write_srt(reference_cues, timing_reference)
    write_srt(aligned_cues, alass_output)

    monkeypatch.setattr(
        "anime_mpv.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (
            timing_reference,
            {"reason": "applied", "language": "eng", "title": "CR"},
        ),
    )
    monkeypatch.setattr(
        "anime_mpv.syncing.synchronize_subtitle",
        lambda *args, **kwargs: (
            subtitle,
            {
                "reason": "quality_offset_exceeded",
                "sync_was_successful": False,
                "offset_seconds": -107.12,
                "alignment_score": 34237.0,
            },
        ),
    )

    seen_references: list[Path] = []

    def fake_alass(reference, *args, **kwargs):
        seen_references.append(reference)
        return alass_output, {
            "reason": "applied",
            "sync_was_successful": True,
            "engine": "alass",
            "offset_seconds": 28.0,
            "alass_constant_shift": True,
        }

    monkeypatch.setattr("anime_mpv.syncing.synchronize_with_alass", fake_alass)

    class AcceptingLLM:
        modes: list[str] = []

        def compare_subtitle_semantics(self, *_args, **kwargs):
            self.modes.append(kwargs["alignment_mode"])
            return {
                "accepted": True,
                "reason": "same episode",
                "similarity": 0.94,
                "matched_samples": 6,
                "total_samples": 6,
            }

    llm = AcceptingLLM()
    output, result = optimize_subtitle(
        video,
        subtitle,
        tmp_path / "cache",
        SyncConfig(engine="auto", compare_engines=True),
        llm=llm,
        validate_embedded_reference_with_llm=True,
    )

    assert llm.modes == ["timestamp"]
    assert seen_references == [timing_reference]
    assert output == alass_output
    assert result["engine"] == "embedded-reference+alass"
    assert result["timing_reference_validation"]["alignment_mode"] == "alass-timestamp"
    assert result["timing_reference_validation"]["estimated_offset_seconds"] == 28.0


def test_subtitle_shift_summary_detects_constant_offset(tmp_path: Path):
    from anime_mpv.subtitle_formats import write_srt
    from anime_mpv.syncing import _subtitle_shift_summary

    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    write_srt(
        [(10.0 + index * 4.0, 12.0 + index * 4.0, str(index)) for index in range(20)],
        source,
    )
    write_srt(
        [(38.0 + index * 4.0, 40.0 + index * 4.0, str(index)) for index in range(20)],
        aligned,
    )

    summary = _subtitle_shift_summary(source, aligned)

    assert summary["offset_seconds"] == 28.0
    assert summary["alass_constant_shift"] is True
    assert summary["alass_shift_spread_seconds"] == 0.0


def test_constant_offset_estimator_handles_different_cue_splitting(tmp_path: Path):
    from anime_mpv.subtitle_formats import write_srt
    from anime_mpv.syncing import estimate_constant_subtitle_offset

    source = tmp_path / "japanese.srt"
    reference = tmp_path / "english.srt"

    source_cues: list[tuple[float, float, str]] = []
    reference_cues: list[tuple[float, float, str]] = []
    time = 12.0
    gaps = [3.1, 6.7, 2.4, 8.2, 4.6, 5.3, 9.1, 3.8]
    for index in range(48):
        time += gaps[index % len(gaps)]
        source_cues.append((time, time + 1.7, f"JA-{index}"))
        shifted = time + 28.0
        # The translation sometimes splits one Japanese cue into two English cues.
        reference_cues.append((shifted, shifted + 0.8, f"EN-{index}-a"))
        if index % 3 == 0:
            reference_cues.append((shifted + 0.95, shifted + 1.7, f"EN-{index}-b"))
        # Extra signs/accessibility cues should not destroy the onset cluster.
        if index % 7 == 0:
            reference_cues.append((shifted + 2.2, shifted + 2.8, "[sign]"))

    write_srt(source_cues, source)
    write_srt(sorted(reference_cues, key=lambda cue: cue[0]), reference)

    result = estimate_constant_subtitle_offset(source, reference)

    assert result["available"] is True
    assert result["usable_for_semantic_sampling"] is True
    assert abs(float(result["offset_seconds"]) - 28.0) <= 0.2
    assert int(result["matched_onsets"]) >= 40


def test_onset_and_semantic_reference_run_before_fft(tmp_path: Path, monkeypatch):
    from anime_mpv.subtitle_formats import write_srt
    from anime_mpv.syncing import optimize_subtitle

    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    source = tmp_path / "japanese.srt"
    reference = tmp_path / "english.srt"
    aligned = tmp_path / "aligned.srt"
    source_cues = [(10.0 + i * 5.0, 11.5 + i * 5.0, f"JA-{i}") for i in range(32)]
    aligned_cues = [(38.0 + i * 5.0, 39.5 + i * 5.0, f"JA-{i}") for i in range(32)]
    reference_cues = [(38.0 + i * 5.0, 39.5 + i * 5.0, f"EN-{i}") for i in range(32)]
    write_srt(source_cues, source)
    write_srt(reference_cues, reference)
    write_srt(aligned_cues, aligned)

    monkeypatch.setattr(
        "anime_mpv.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (reference, {"language": "eng", "title": "CR"}),
    )
    calls: list[str] = []

    class AcceptingLLM:
        def compare_subtitle_semantics(self, *_args, **_kwargs):
            calls.append("semantic")
            return {
                "accepted": True,
                "reason": "paired excerpts match",
                "similarity": 0.95,
                "matched_samples": 6,
                "total_samples": 6,
            }

    def fake_alass(reference_path, *_args, **_kwargs):
        calls.append("alass")
        assert reference_path == reference
        return aligned, {"reason": "applied", "sync_was_successful": True}

    def forbidden_fft(*_args, **_kwargs):
        calls.append("fft")
        raise AssertionError("FFT must not run after a valid embedded subtitle reference")

    monkeypatch.setattr("anime_mpv.syncing.synchronize_with_alass", fake_alass)
    monkeypatch.setattr("anime_mpv.syncing.synchronize_subtitle", forbidden_fft)

    output, result = optimize_subtitle(
        video,
        source,
        tmp_path / "cache",
        SyncConfig(engine="auto", compare_engines=True),
        llm=AcceptingLLM(),
        validate_embedded_reference_with_llm=True,
    )

    assert calls == ["alass", "semantic"]
    assert output == aligned
    assert result["engine"] == "embedded-reference+alass"
    assert result["timing_reference_validation"]["estimated_offset_seconds"] == 28.0


def test_constant_offset_estimator_rejects_dense_boundary_false_peak(tmp_path: Path):
    import random

    from anime_mpv.subtitle_formats import write_srt
    from anime_mpv.syncing import estimate_constant_subtitle_offset

    source = tmp_path / "japanese.srt"
    reference = tmp_path / "english.srt"
    rng = random.Random(7)
    starts: list[float] = []
    current = 20.0
    for _ in range(220):
        current += rng.choice([2.3, 3.7, 5.1, 7.4, 9.8])
        starts.append(current)

    source_cues = [(time, time + 1.5, f"JA-{i}") for i, time in enumerate(starts)]
    reference_starts = [
        time + 28.0 + rng.uniform(-0.12, 0.12)
        for i, time in enumerate(starts)
        if i % 3 != 0
    ]
    # Dense unrelated/sign cues create many accidental matches for wide scans.
    extra = 5.0
    while extra < starts[-1] + 60.0:
        extra += rng.uniform(2.5, 5.0)
        reference_starts.append(extra)
    reference_cues = [
        (time, time + 1.2, f"EN-{i}")
        for i, time in enumerate(sorted(reference_starts))
    ]
    write_srt(source_cues, source)
    write_srt(reference_cues, reference)

    result = estimate_constant_subtitle_offset(source, reference)

    assert result["usable_for_semantic_sampling"] is True
    assert abs(float(result["offset_seconds"]) - 28.0) <= 0.3
    assert int(result["matched_regions"]) == 8
    assert float(result["sequence_consistency"]) >= 0.8


def test_constant_offset_estimator_keeps_diverse_semantic_candidates(tmp_path: Path):
    import random

    from anime_mpv.subtitle_formats import write_srt
    from anime_mpv.syncing import estimate_constant_subtitle_offsets

    source = tmp_path / "japanese.srt"
    reference = tmp_path / "english.srt"
    rng = random.Random(1)
    current = 130.0
    starts: list[float] = []
    for _ in range(180):
        current += rng.choice([2.1, 3.4, 4.9, 7.2, 10.3]) + rng.uniform(-0.1, 0.1)
        starts.append(current)

    source_cues = [(time, time + 1.3, f"JA-{index}") for index, time in enumerate(starts)]
    reference_cues: list[tuple[float, float, str]] = []
    for index, time in enumerate(starts):
        if index % 3 != 0:
            shifted = time + 28.0 + rng.uniform(-0.08, 0.08)
            reference_cues.append((shifted, shifted + 1.2, f"TRUE-{index}"))
        if index % 2 == 0 and time - 112.0 > 0:
            shifted = time - 112.0 + rng.uniform(-0.08, 0.08)
            reference_cues.append((shifted, shifted + 1.2, f"ALIAS-{index}"))

    write_srt(source_cues, source)
    write_srt(sorted(reference_cues, key=lambda cue: cue[0]), reference)

    candidates = estimate_constant_subtitle_offsets(source, reference, maximum_results=6)
    offsets = [float(candidate["offset_seconds"]) for candidate in candidates]

    assert any(abs(offset - 28.0) <= 0.3 for offset in offsets)
    assert any(abs(offset + 112.0) <= 0.3 for offset in offsets)


def test_semantic_validation_tries_next_offset_before_fft(tmp_path: Path, monkeypatch):
    from anime_mpv.subtitle_formats import write_srt
    from anime_mpv.syncing import optimize_subtitle

    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    source = tmp_path / "japanese.srt"
    reference = tmp_path / "english.srt"
    aligned = tmp_path / "aligned.srt"
    cues = [(10.0 + index * 5.0, 11.5 + index * 5.0, str(index)) for index in range(24)]
    write_srt(cues, source)
    write_srt([(start + 28.0, end + 28.0, text) for start, end, text in cues], reference)
    write_srt([(start + 28.0, end + 28.0, text) for start, end, text in cues], aligned)

    monkeypatch.setattr(
        "anime_mpv.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (reference, {"language": "eng", "title": "CR"}),
    )
    monkeypatch.setattr(
        "anime_mpv.syncing.estimate_constant_subtitle_offsets",
        lambda *args, **kwargs: [
            {
                "available": True,
                "usable_for_semantic_sampling": True,
                "offset_seconds": -112.0,
                "source_onsets": 24,
                "reference_onsets": 24,
                "matched_onsets": 18,
                "onset_coverage": 0.75,
                "onset_z_score": 5.0,
                "activity_correlation": 0.2,
                "landmark_support": 10.0,
            },
            {
                "available": True,
                "usable_for_semantic_sampling": True,
                "offset_seconds": 28.0,
                "source_onsets": 24,
                "reference_onsets": 24,
                "matched_onsets": 16,
                "onset_coverage": 0.67,
                "onset_z_score": 4.0,
                "activity_correlation": 0.5,
                "landmark_support": 8.0,
            },
        ],
    )

    calls: list[str] = []

    class SelectiveLLM:
        def compare_subtitle_semantics(self, *_args, **_kwargs):
            calls.append("semantic")
            accepted = True
            return {
                "accepted": accepted,
                "reason": "same scenes",
                "similarity": 0.95,
                "matched_samples": 6,
                "total_samples": 6,
            }

    def fake_alass(reference_path, *_args, **_kwargs):
        calls.append("alass")
        assert reference_path == reference
        return aligned, {"reason": "applied", "sync_was_successful": True}

    def forbidden_fft(*_args, **_kwargs):
        calls.append("fft")
        raise AssertionError("FFT must not run after a later offset candidate is accepted")

    monkeypatch.setattr("anime_mpv.syncing.synchronize_with_alass", fake_alass)
    monkeypatch.setattr("anime_mpv.syncing.synchronize_subtitle", forbidden_fft)

    output, result = optimize_subtitle(
        video,
        source,
        tmp_path / "cache",
        SyncConfig(engine="auto", compare_engines=True),
        llm=SelectiveLLM(),
        validate_embedded_reference_with_llm=True,
    )

    assert calls == ["alass", "semantic"]
    assert output == aligned
    assert result["engine"] == "embedded-reference+alass"
    validation = result["timing_reference_validation"]
    assert validation["alignment_mode"] == "alass-timestamp"


def test_optimize_candidates_uses_embedded_subtitles_before_audio(tmp_path: Path, monkeypatch):
    from anime_mpv.models import SubtitleCandidate
    from anime_mpv.subtitle_formats import write_srt
    from anime_mpv.syncing import optimize_candidates

    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    source = tmp_path / "japanese.srt"
    reference = tmp_path / "english.srt"
    aligned = tmp_path / "aligned.srt"
    cues = [(10.0 + index * 4.0, 11.5 + index * 4.0, f"JA-{index}") for index in range(24)]
    write_srt(cues, source)
    write_srt([(start + 28.0, end + 28.0, f"EN-{index}") for index, (start, end, _text) in enumerate(cues)], reference)
    write_srt([(start + 28.0, end + 28.0, text) for start, end, text in cues], aligned)

    monkeypatch.setattr(
        "anime_mpv.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (reference, {"language": "eng", "title": "CR"}),
    )

    calls: list[str] = []

    def fake_alass(reference_path, subtitle_path, *_args, **_kwargs):
        calls.append("alass")
        assert reference_path == reference
        assert subtitle_path == source
        return aligned, {
            "reason": "applied",
            "sync_was_successful": True,
            "offset_seconds": 28.0,
            "alass_constant_shift": True,
        }

    class AcceptingLLM:
        def compare_subtitle_semantics(self, japanese, english, **kwargs):
            calls.append("semantic")
            assert japanese == aligned
            assert english == reference
            assert kwargs["alignment_mode"] == "timestamp"
            return {
                "accepted": True,
                "reason": "same scenes",
                "similarity": 0.96,
                "matched_samples": 6,
                "total_samples": 6,
            }

    def forbidden_audio(*_args, **_kwargs):
        raise AssertionError("audio/FFT candidate scoring must not run")

    monkeypatch.setattr("anime_mpv.syncing.synchronize_with_alass", fake_alass)
    monkeypatch.setattr("anime_mpv.syncing.prepare_speech_reference", forbidden_audio)
    monkeypatch.setattr("anime_mpv.syncing.synchronize_subtitle", forbidden_audio)

    candidate, output, result = optimize_candidates(
        video,
        [SubtitleCandidate(source, "jimaku", 100.0, source.name)],
        tmp_path / "cache",
        SyncConfig(engine="auto", compare_engines=True),
        llm=AcceptingLLM(),
        validate_embedded_reference_with_llm=True,
    )

    assert calls == ["alass", "semantic"]
    assert candidate is not None and candidate.path == source
    assert output == aligned
    assert result["engine"] == "embedded-reference+alass"
    assert result["selection_reason"] == "subtitle_reference_before_audio"
    assert result["timing_reference_validation"]["estimated_offset_seconds"] == 28.0


def test_robust_semantic_acceptance_requires_strong_activity():
    from anime_mpv.syncing import _apply_robust_semantic_activity_gate

    validation = {
        "accepted": True,
        "strict_semantic_acceptance": False,
        "robust_semantic_acceptance": True,
    }
    accepted = _apply_robust_semantic_activity_gate(
        dict(validation),
        {"available": True, "weighted": 0.82},
    )
    rejected = _apply_robust_semantic_activity_gate(
        dict(validation),
        {"available": True, "weighted": 0.41},
    )

    assert accepted["accepted"] is True
    assert rejected["accepted"] is False
    assert str(rejected["reason"]).startswith("robust_semantic_activity_too_low")


def test_subtitle_quality_gate_rejects_unreliable_segment_metrics():
    from anime_mpv.syncing import subtitle_quality_accepted

    accepted, reason = subtitle_quality_accepted(
        {
            "sync_was_successful": True,
            "segment_diagnostics": {
                "available": True,
                "reliable": False,
                "quality_reason": "no_stable_offset_cluster",
            },
        }
    )

    assert accepted is False
    assert reason == "no_stable_offset_cluster"


def test_subtitle_quality_gate_rejects_semantic_episode_mismatch():
    from anime_mpv.syncing import subtitle_quality_accepted

    accepted, reason = subtitle_quality_accepted(
        {
            "sync_was_successful": True,
            "timing_reference_validation": {
                "accepted": False,
                "total_samples": 6,
                "reason": "different episode",
            },
        }
    )

    assert accepted is False
    assert reason == "different episode"


def test_subtitle_quality_gate_accepts_exact_jimaku_boundary_only_mismatch():
    from anime_mpv.syncing import subtitle_quality_accepted

    accepted, reason = subtitle_quality_accepted(
        {
            "sync_was_successful": True,
            "segment_diagnostics": {
                "available": True,
                "reliable": False,
                "quality_reason": "too_many_boundary_hits",
            },
            "candidate_context": {
                "source": "jimaku",
                "filename_score": 107.0,
                "episode_match": "exact",
                "title_similarity": 100.0,
                "entry_anilist_match": True,
            },
        }
    )

    assert accepted is True
    assert "границы" in reason


def test_subtitle_quality_gate_still_rejects_exact_jimaku_large_jumps():
    from anime_mpv.syncing import subtitle_quality_accepted

    accepted, reason = subtitle_quality_accepted(
        {
            "sync_was_successful": True,
            "segment_diagnostics": {
                "available": True,
                "reliable": False,
                "quality_reason": "too_many_boundary_hits,too_many_large_jumps",
            },
            "candidate_context": {
                "source": "jimaku",
                "filename_score": 107.0,
                "episode_match": "exact",
                "title_similarity": 100.0,
                "entry_anilist_match": True,
            },
        }
    )

    assert accepted is False
    assert reason == "too_many_boundary_hits,too_many_large_jumps"


def test_embedded_reference_piecewise_repairs_cold_open_drift(tmp_path: Path):
    from anime_mpv.config import SyncConfig
    from anime_mpv.subtitle_formats import parse_srt, write_srt
    from anime_mpv.syncing import (
        compare_timing_activity,
        repair_with_embedded_reference_piecewise,
    )

    reference = tmp_path / "english.srt"
    aligned = tmp_path / "japanese-alass.srt"

    reference_cues: list[tuple[float, float, str]] = []
    aligned_cues: list[tuple[float, float, str]] = []
    for index, start in enumerate([20.0 + i * 7.3 for i in range(180)]):
        reference_cues.append((start, start + 1.8, f"EN-{index}"))
        if start < 180.0:
            residual = -6.0
        elif start < 620.0:
            residual = -6.0 * (620.0 - start) / 440.0
        else:
            residual = 0.0
        aligned_cues.append((start + residual, start + residual + 1.8, f"JA-{index}"))

    write_srt(reference_cues, reference)
    write_srt(aligned_cues, aligned)

    before = compare_timing_activity(aligned, reference)
    output, result = repair_with_embedded_reference_piecewise(
        aligned,
        reference,
        tmp_path / "cache",
        SyncConfig(piecewise_repair=True),
    )
    after = compare_timing_activity(output, reference)

    assert result["applied"] is True
    assert output != aligned
    assert after["start"] > before["start"]
    assert after["middle"] >= before["middle"] - 0.02
    repaired = parse_srt(output)
    assert abs(repaired[0][0] - reference_cues[0][0]) < 1.0
    middle_index = next(i for i, cue in enumerate(reference_cues) if cue[0] > 700.0)
    assert abs(repaired[middle_index][0] - reference_cues[middle_index][0]) < 0.6


def test_embedded_reference_piecewise_rejects_short_cold_open_when_it_reorders_dialogue(tmp_path: Path):
    from anime_mpv.config import SyncConfig
    from anime_mpv.subtitle_formats import parse_srt, write_srt
    from anime_mpv.syncing import repair_with_embedded_reference_piecewise

    reference = tmp_path / "english-short-open.srt"
    aligned = tmp_path / "japanese-short-open-alass.srt"

    reference_cues: list[tuple[float, float, str]] = []
    aligned_cues: list[tuple[float, float, str]] = []
    for index, start in enumerate([8.0 + i * 4.7 for i in range(285)]):
        reference_cues.append((start, start + 1.6, f"EN-{index}"))
        if start < 55.0:
            residual = -6.0
        elif start < 210.0:
            residual = -6.0 * (210.0 - start) / 155.0
        else:
            residual = 0.0
        aligned_cues.append((start + residual, start + residual + 1.6, f"JA-{index}"))

    write_srt(reference_cues, reference)
    write_srt(aligned_cues, aligned)

    output, result = repair_with_embedded_reference_piecewise(
        aligned,
        reference,
        tmp_path / "cache",
        SyncConfig(piecewise_repair=True),
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["reason"] == "reference_piecewise_unsafe_sequence"
    assert result["sequence_safety"]["reason"] == "cue_order_inversion"


def test_embedded_reference_piecewise_rejects_large_cold_open_when_it_reorders_dialogue(tmp_path: Path):
    from anime_mpv.config import SyncConfig
    from anime_mpv.subtitle_formats import parse_srt, write_srt
    from anime_mpv.syncing import repair_with_embedded_reference_piecewise

    reference = tmp_path / "english-title-card.srt"
    aligned = tmp_path / "japanese-title-card-alass.srt"

    reference_cues: list[tuple[float, float, str]] = []
    aligned_cues: list[tuple[float, float, str]] = []
    for index, start in enumerate([23.0 + i * 4.7 for i in range(280)]):
        reference_cues.append((start, start + 1.6, f"EN-{index}"))
        residual = -17.0 if start < 58.0 else 0.0
        aligned_cues.append((start + residual, start + residual + 1.6, f"JA-{index}"))

    write_srt(reference_cues, reference)
    write_srt(aligned_cues, aligned)

    output, result = repair_with_embedded_reference_piecewise(
        aligned,
        reference,
        tmp_path / "cache",
        SyncConfig(piecewise_repair=True),
    )

    assert output != aligned
    assert result["applied"] is True
    assert result["reason"] == "applied"
    assert result["sequence_safety"]["reason"] == "safe"
    assert result["edit_boundaries"]
    repaired = parse_srt(output)
    assert [text for _start, _end, text in repaired] == [
        text for _start, _end, text in aligned_cues
    ]
    assert abs(repaired[0][0] - reference_cues[0][0]) < 0.01
    first_after_edit = next(index for index, cue in enumerate(reference_cues) if cue[0] >= 58.0)
    assert abs(repaired[first_after_edit][0] - reference_cues[first_after_edit][0]) < 0.01


def test_optimize_candidates_retries_after_primary_quality_failure(tmp_path: Path, monkeypatch):
    import anime_mpv.syncing as syncing

    video = tmp_path / "Odd Taxi - 02.mkv"
    first = tmp_path / "netflix-02.srt"
    second = tmp_path / "amazon-02.srt"
    video.write_bytes(b"video")
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    candidates = [
        SubtitleCandidate(
            path=first,
            source="jimaku",
            score=100.0,
            name=first.name,
            details={"episode_match": "exact", "entry_anilist_match": True},
        ),
        SubtitleCandidate(
            path=second,
            source="jimaku",
            score=90.0,
            name=second.name,
            details={"episode_match": "exact", "entry_anilist_match": True},
        ),
    ]

    monkeypatch.setattr(
        syncing,
        "prepare_speech_reference",
        lambda *_args, **_kwargs: (None, {"reason": "test"}),
    )

    def fake_evaluate(_video, subtitle, *_args, **_kwargs):
        return subtitle, {
            "sync_was_successful": True,
            "reason": "applied",
            "alignment_score": 1000.0 if subtitle == first else 900.0,
        }

    optimized: list[Path] = []

    def fake_optimize(_video, subtitle, *_args, **_kwargs):
        optimized.append(subtitle)
        if subtitle == first:
            return subtitle, {
                "sync_was_successful": False,
                "reason": "semantic mismatch",
            }
        return subtitle, {
            "sync_was_successful": True,
            "reason": "applied",
            "engine": "test",
        }

    monkeypatch.setattr(syncing, "synchronize_subtitle", fake_evaluate)
    monkeypatch.setattr(syncing, "optimize_subtitle", fake_optimize)

    chosen, output, result = optimize_candidates(
        video,
        candidates,
        tmp_path / "cache",
        SyncConfig(),
    )

    assert chosen is candidates[1]
    assert output == second
    assert optimized == [first, second]
    assert result["selection_reason"] == "quality_fallback_candidate"
    attempts = result["quality_fallback_attempts"]
    assert len(attempts) == 2
    assert attempts[0]["accepted"] is False
    assert attempts[1]["accepted"] is True


def test_exact_jimaku_timing_consensus_accepts_three_strong_independent_files(tmp_path: Path):
    from anime_mpv.syncing import _exact_jimaku_timing_consensus

    items = []
    for index, activity in enumerate((0.93, 0.91, 0.89, 0.84), start=1):
        path = tmp_path / f"source-{index}.srt"
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
        candidate = SubtitleCandidate(
            path,
            "jimaku",
            100.0 - index,
            path.name,
            details={
                "episode_match": "exact",
                "entry_anilist_match": True,
            },
        )
        items.append(
            (
                (1.0, activity, 1.0, candidate.score),
                candidate,
                path,
                {"sync_was_successful": True},
                {"available": True, "weighted": activity},
                {"reason": "ok", "retained_ratio": 1.0},
            )
        )

    selected, payload = _exact_jimaku_timing_consensus(items)

    assert selected is not None
    assert selected[1].name == "source-1.srt"
    assert payload["accepted"] is True
    assert payload["qualified_candidates"] == 4
    assert payload["reason"] == "exact_jimaku_timing_consensus"


def test_exact_jimaku_timing_consensus_stays_strict_for_ambiguous_files(tmp_path: Path):
    from anime_mpv.syncing import _exact_jimaku_timing_consensus

    items = []
    for index, activity in enumerate((0.95, 0.94), start=1):
        path = tmp_path / f"source-{index}.srt"
        path.write_text("subtitle", encoding="utf-8")
        candidate = SubtitleCandidate(
            path,
            "jimaku",
            99.0,
            path.name,
            details={
                "episode_match": "exact",
                "entry_anilist_match": True,
            },
        )
        items.append(
            (
                (1.0, activity, 1.0, candidate.score),
                candidate,
                path,
                {},
                {"available": True, "weighted": activity},
                {"reason": "ok", "retained_ratio": 1.0},
            )
        )

    selected, payload = _exact_jimaku_timing_consensus(items)

    assert selected is None
    assert payload["accepted"] is False
    assert payload["reason"] == "insufficient_exact_timing_consensus"


def test_embedded_reference_piecewise_rejects_sparse_reference_false_cold_shift(
    tmp_path: Path,
    monkeypatch,
):
    import anime_mpv.syncing as syncing
    from anime_mpv.config import SyncConfig
    from anime_mpv.subtitle_formats import write_srt

    reference = tmp_path / "english-sparse-opening.srt"
    aligned = tmp_path / "japanese-cc-alass.srt"
    cues = [(10.0 + i * 4.5, 11.5 + i * 4.5, f"cue-{i}") for i in range(280)]
    write_srt(cues, reference)
    write_srt(cues, aligned)

    def fake_window(*_args, region_start: float, region_end: float, **_kwargs):
        is_cold = (region_start, region_end) in {(0.0, 35.0), (10.0, 50.0)}
        if is_cold:
            return {
                "available": True,
                "confident": True,
                "shift_seconds": 12.9,
                "score": 1.0,
                "matched_onsets": 4,
                "coverage": 0.18,
                "activity_overlap": 0.7,
                "activity_correlation": 0.2,
                "source_onsets": 18,
                "reference_onsets": 8,
                "first_edge_error": 9.0,
                "last_edge_error": 8.0,
                "minimum_matches": 4,
                "score_improvement": 0.2,
                "baseline": {"score": 0.8},
                "region_start": region_start,
                "region_end": region_end,
            }
        return {
            "available": True,
            "confident": True,
            "shift_seconds": 0.1,
            "score": 1.0,
            "matched_onsets": 12,
            "coverage": 0.8,
            "activity_overlap": 0.9,
            "activity_correlation": 0.8,
            "source_onsets": 14,
            "reference_onsets": 14,
            "first_edge_error": 0.1,
            "last_edge_error": 0.1,
            "minimum_matches": 4,
            "score_improvement": 0.1,
            "baseline": {"score": 0.9},
            "region_start": region_start,
            "region_end": region_end,
        }

    monkeypatch.setattr(syncing, "_windowed_reference_shift", fake_window)
    output, result = syncing.repair_with_embedded_reference_piecewise(
        aligned,
        reference,
        tmp_path / "cache",
        SyncConfig(piecewise_repair=True),
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["reason"] == "reference_piecewise_not_needed"
    assert len(result["weak_large_cold_probes"]) == 2


def test_piecewise_sequence_guard_rejects_dialogue_reordering():
    from anime_mpv.syncing import _retime_cues_without_reordering

    cues = [
        (10.0, 12.0, "first"),
        (13.0, 15.0, "second"),
        (16.0, 18.0, "third"),
    ]
    repaired, details = _retime_cues_without_reordering(
        cues,
        [10.0, 10.0, -10.0],
    )

    assert repaired is None
    assert details["reason"] == "cue_order_inversion"
    assert details["cue_index"] == 3


def test_piecewise_sequence_guard_clamps_start_and_preserves_duration():
    from anime_mpv.syncing import _retime_cues_without_reordering

    repaired, details = _retime_cues_without_reordering(
        [(0.05, 1.05, "opening")],
        [-2.0],
    )

    assert details["reason"] == "safe"
    assert repaired is not None
    assert repaired[0][0] == 0.1
    assert repaired[0][1] == 1.1
