from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from pudge.config import AppConfig, SyncConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles.timeline_alignment import (
    _early_edit_audio_verification_risk,
    _shared_opening_anchors_resolve_path,
    _shared_opening_text_anchors,
    _suppress_weak_singleton_tail_transition,
    _WindowMatch,
    align_subtitle_timelines,
)
from pudge.subtitles.validation import quality_from_result
from pudge.syncing import _optimize_subtitle_unguarded, subtitle_quality_accepted


def timing():
    return json.loads((Path(__file__).parent / "fixtures/rezero_ep19_timing_v17.json").read_text())


def test_real_episode_clock_switches_in_opening_gap_and_stays_through_final_dialogue(tmp_path):
    data = timing()
    source, reference = tmp_path / "source.srt", tmp_path / "reference.srt"
    write_srt(data["source"], source, preserve_order=True)
    write_srt(data["reference"], reference, preserve_order=True)
    output, result = align_subtitle_timelines(source, reference, tmp_path, force=True)
    assert result["accepted"] and result["timeline_alignment_reliable"]
    assert [(s["offset_seconds"], s["source_end"]) for s in result["timeline_segments"]] == [
        (-31.5, 253.935),
        (-41.75, None),
    ]
    assert result["timeline_weak_tail_guard"]["reason"] == "weak_singleton_tail_contradicted_by_reference"
    risk = result["timeline_early_edit_audio_verification"]
    assert not risk["required"] and risk["resolved_by"] == "shared_opening_text_anchors"
    cues = parse_srt(output)
    assert len(cues) == len(data["source"])
    assert all(a[0] <= b[0] for a, b in itertools.pairwise(cues))
    assert all(end > start for start, end, _text in cues)
    anchors = {text: start for start, _end, text in cues if text in {"ＥＭＭ！", "ＥＭＴ！"}}
    assert abs(anchors["ＥＭＭ！"] - 289.97) <= 0.35
    assert abs(anchors["ＥＭＴ！"] - 322.84) <= 0.35
    final_source_index = next(i for i, cue in enumerate(data["source"]) if cue[0] == 2319.18)
    assert cues[final_source_index][0] == pytest.approx(2277.43, abs=0.001)
    # Held-out reference error remains visible; this must not get grade A.
    accepted, reason = subtitle_quality_accepted(result)
    quality = quality_from_result(result, accepted=accepted, reason=reason)
    assert accepted and quality.confidence.value != "A"
    assert quality.holdout_p95_seconds == pytest.approx(1.18)


@pytest.mark.parametrize(
    "change", ["none", "single", "repeated", "wrong_time", "reversed", "lowercase", "embedded"]
)
def test_shared_acronyms_require_two_unique_ordered_consistent_identities(change):
    source = [(332.03, 334.03, "ＥＭＭ！"), (364.73, 366.73, "ＥＭＴ！")]
    reference = [(289.97, 291.50, "E-M-M!"), (322.84, 324.42, "E-M-T!")]
    if change == "single":
        reference.pop()
    elif change == "repeated":
        source.append((370.0, 372.0, "ＥＭＭ！"))
    elif change == "wrong_time":
        reference[1] = (328.84, 330.42, "E-M-T!")
    elif change == "reversed":
        reference = [(322.84, 324.42, "E-M-M!"), (289.97, 291.50, "E-M-T!")]
    elif change == "lowercase":
        reference[0] = (289.97, 291.50, "e-m-m!")
    elif change == "embedded":
        source[0] = (332.03, 334.03, "説明 ＥＭＭ！")
    evidence = _shared_opening_text_anchors(source, reference, 304.4, -31.5, -41.75)
    assert evidence["supported"] == (change == "none")


def window(center, offset):
    return _WindowMatch(center, offset, 3.0, 8, 8, 8, 1.0, 1.0, 0.95, 0.2, 0.0, 0.0, None)


def test_text_proof_does_not_clear_unrelated_early_audio_risk():
    diagnostics = {
        "applied": True,
        "shared_text_anchors": {"supported": True},
        "first_post_gap_source_time": 304.4,
        "gap_seconds": 100.93,
        "current_offset_seconds": -31.5,
        "candidate_offset_seconds": -41.75,
    }
    path = [window(74.74, -31), window(182.74, -31.5), window(290.74, -3.25)]
    assert _shared_opening_anchors_resolve_path(path, diagnostics)
    assert not _shared_opening_anchors_resolve_path(path + [window(350, -25)], diagnostics)
    risk = _early_edit_audio_verification_risk(
        path,
        {"gap_seconds": 90, "delta_seconds": 6, "boundary_source_time": 180},
        [],
        resolved_by="shared_opening_text_anchors",
    )
    assert risk["required"] and "opening_gap_clock_ambiguity" in risk["reasons"]


@pytest.mark.parametrize("change", ["none", "supported", "no_reference", "real_edit"])
def test_small_tail_jump_needs_independent_reference_contradiction(change):
    data = timing()
    segments = [
        {"offset_seconds": -41.75, "support": 34, "kind": "stable"},
        {"offset_seconds": -38.0, "support": 1, "kind": "stable"},
    ]
    reference = data["reference"]
    if change == "supported":
        segments[-1]["support"] = 2
    elif change == "no_reference":
        reference = None
    elif change == "real_edit":
        reference = [
            (s + 3.75 if s >= 2200.75 else s, e + 3.75 if s >= 2200.75 else e, t) for s, e, t in reference
        ]
    mapped, bounds, report = _suppress_weak_singleton_tail_transition(
        data["source"], segments, [2242.5], reference
    )
    assert report["applied"] == (change == "none")
    assert len(mapped) == (1 if change == "none" else 2)
    assert bounds == ([] if change == "none" else [2242.5])


def test_real_clock_is_usable_by_pipeline_without_unnecessary_unavailable_stt(tmp_path, monkeypatch):
    data = timing()
    source, reference = tmp_path / "source.srt", tmp_path / "embedded.srt"
    write_srt(data["source"], source, preserve_order=True)
    write_srt(data["reference"], reference, preserve_order=True)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        "pudge.syncing.extract_embedded_timing_reference", lambda *a, **k: (reference, {"language": "eng"})
    )
    monkeypatch.setattr("pudge.syncing._exact_release_zero_offset_result", lambda *a: None)

    def no_stt(*a, **k):
        pytest.fail("The opening alias has independent text proof; no STT escalation is needed")

    monkeypatch.setattr("pudge.syncing._try_japanese_stt_fallback", no_stt)
    path, result = _optimize_subtitle_unguarded(
        video, source, tmp_path / "cache", SyncConfig(engine="alass", use_container_chapters=False)
    )
    assert path != source and result["sync_was_successful"]
    assert subtitle_quality_accepted(result)[0]


def test_upgrade_requeues_only_old_weak_opening_signature_once(tmp_path):
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies"
    cfg.library.root_dir.mkdir()
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)
    video = cfg.library.root_dir / "episode.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "playback.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Episode",
            episode=1,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    alignment = {
        "engine": "embedded-reference+timeline",
        "timeline_algorithm": "timeline-v6.19-edge-zones-audio-verify",
        "timeline_opening_gap_reacquire": {
            "applied": False,
            "reason": "later_clock_not_clearly_better",
            "gap_seconds": 100.93,
            "current_offset_seconds": -31.5,
            "candidate_offset_seconds": -41.75,
            "candidate_support": 34,
        },
    }
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=1,
        episode=1,
        source="jimaku",
        candidate_name="source.srt",
        candidate_path=tmp_path / "source.srt",
        status="selected",
        reason="prepared",
        details={"alignment": alignment},
    )
    assert manager._requeue_shared_opening_timeline_upgrade() == 1
    row = manager.db.episode_by_path(video)
    assert row.subtitle_path is None and row.state == "waiting_subtitles"
    assert video.read_bytes() == b"video" and subtitle.exists()
    assert manager._requeue_shared_opening_timeline_upgrade() == 0
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Episode",
            episode=1,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    alignment["timeline_algorithm"] = "timeline-v6.20-shared-opening-anchors"
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=1,
        episode=1,
        source="jimaku",
        candidate_name="source.srt",
        candidate_path=tmp_path / "source.srt",
        status="selected",
        reason="prepared",
        details={"alignment": alignment},
    )
    assert manager._requeue_shared_opening_timeline_upgrade() == 0
    assert manager.db.episode_by_path(video).state == "ready"
