"""The last two minutes of Dorohedoro E05 came 4-5 s early.

User: "at the end the subtitles are bad" (stopped at 20:38). The Japanese subtitles run 1 s
behind the embedded English ones for the whole episode. One alignment window over the ending
song and the preview matched at -4 s, and a -5 s bridge was built towards it through the dense
dialogue before the song. The guard against one-window tails then compared the tail with the
bridge (a 1 s jump, kept) instead of with the -1 s clock both replaced.

Fixture: cue timings only (start, end) of the two subtitle files.
"""

from __future__ import annotations

import json
from pathlib import Path

from pudge.subtitles.timeline_alignment import align_subtitle_timelines
from subtitle_rebuild_support import assert_rebuilding_with_subtitles_kept

FIXTURE = Path(__file__).parent / "fixtures" / "timeline_dorohedoro_e05_timings.json"


def _srt(path: Path, cues: list[list[float]], prefix: str) -> Path:
    def stamp(value: float) -> str:
        ms = int(round(value * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    blocks = [f"{i}\n{stamp(s)} --> {stamp(e)}\n{prefix} {i}\n" for i, (s, e) in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def test_the_whole_episode_keeps_its_clock_through_the_ending(tmp_path: Path) -> None:
    data = json.loads(FIXTURE.read_text())
    source = _srt(tmp_path / "source.srt", data["source"], "ja")
    reference = _srt(tmp_path / "reference.srt", data["reference"], "en")
    _out, result = align_subtitle_timelines(source, reference, tmp_path / "cache", force=True)
    assert result["accepted"] is True
    assert [segment["offset_seconds"] for segment in result["timeline_segments"]] == [-1.0]
    guard = result["timeline_weak_tail_guard"]
    assert guard["applied"] is True and guard["reason"] == "weak_bridged_singleton_tail_clock"
    assert result["timeline_validation"]["after"]["mean_error_seconds"] < 0.30


def _bridge_segments(tail_offset: float) -> list[dict[str, object]]:
    return [
        {"source_start": 0.0, "source_end": 1283.9, "offset_seconds": -1.0, "support": 23, "kind": "stable"},
        {
            "source_start": 1283.9,
            "source_end": 1317.0,
            "offset_seconds": -5.0,
            "support": 21,
            "kind": "transition_bridge",
        },
        {
            "source_start": 1317.0,
            "source_end": None,
            "offset_seconds": tail_offset,
            "support": 1,
            "kind": "stable",
        },
    ]


def test_a_real_late_clock_change_reached_through_a_bridge_is_kept() -> None:
    from pudge.subtitles.timeline_alignment import _suppress_weak_singleton_tail_transition

    # Dialogue every 3.1 s until the end; the reference really is 4 s earlier from 1284 s on.
    source = [(float(t), float(t) + 1.5, "ja") for t in [i * 3.1 for i in range(1, 460)]]
    reference = [
        (s - 1.0 if s < 1284 else s - 4.0, e - 1.0 if s < 1284 else e - 4.0, "en") for s, e, _t in source
    ]
    segments, boundaries, diagnostics = _suppress_weak_singleton_tail_transition(
        source, _bridge_segments(-4.0), [1283.9, 1317.0], reference
    )
    assert len(segments) == 3 and boundaries == [1283.9, 1317.0]
    assert diagnostics["bridged_tail"]["applied"] is False


def test_old_fits_with_a_bridged_one_window_tail_are_prepared_again_once(tmp_path: Path) -> None:
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies"
    cfg.library.root_dir.mkdir()
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)

    def episode(name: str, state: str, algorithm: str, segments, stopped_at: float | None = None) -> Path:
        video = cfg.library.root_dir / name
        video.write_bytes(b"video")
        subtitle = tmp_path / f"{name}.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=105228,
                title="Dorohedoro",
                episode=len(name),
                video_path=video,
                subtitle_path=subtitle,
                subtitle_origin="jimaku",
                state=state,
            )
        )
        if stopped_at is not None:
            manager.db.record_playback(video, stopped_at, 1420.0)
        manager.db.record_subtitle_history(
            video_path=video,
            media_id=105228,
            episode=len(name),
            source="jimaku",
            candidate_name="source.srt",
            candidate_path=tmp_path / "source.srt",
            status="selected",
            reason="prepared",
            details={
                "alignment": {
                    "engine": "embedded-reference+timeline",
                    "timeline_algorithm": algorithm,
                    "timeline_segments": segments,
                }
            },
        )
        return video

    old = "timeline-v6.21-agreed-crossover-fine-offset"
    affected = episode("a.mkv", "ready", old, _bridge_segments(-4.0))
    stopped = episode(  # stopped at 20:38
        "bb.mkv", "ready", "timeline-v6.20-shared-opening-anchors", _bridge_segments(-4.0), 1238.0
    )
    episode("ccc.mkv", "watched", old, _bridge_segments(-4.0))  # already watched
    episode("dddd.mkv", "ready", old, _bridge_segments(-4.0)[:1])  # one clock
    episode("eeeee.mkv", "ready", "timeline-v6.22-bridged-singleton-tail", _bridge_segments(-4.0))
    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 2
    # Kept while they are prepared again.
    assert_rebuilding_with_subtitles_kept(manager, affected)
    assert_rebuilding_with_subtitles_kept(manager, stopped)
    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 0
