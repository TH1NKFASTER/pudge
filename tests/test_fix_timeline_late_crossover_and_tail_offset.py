"""A late picture edit and a short final clock (FX Senshi Kurumi-chan E03).

User: "the subtitles at 17:40+ are a problem". The AT-X subtitles run 5 s ahead of the web video
until a cut at 17:02 (source 1022 s) and about 1.4 s behind it afterwards. The boundary search
took the first place where the right clock was marginally better (953 s, +0.24) although a
minute of clear left-clock evidence followed: 70 s of lines came 7 s late. The final clock got
+2.0 s from four windows (+1.25/+2/+1/+2) while its 43 lines fit best at +1.5 s.

Fixture: cue timings only (start, end) of the two subtitle files.
"""

from __future__ import annotations

import json
from pathlib import Path

from pudge.subtitles.timeline_alignment import align_subtitle_timelines
from subtitle_rebuild_support import assert_rebuilding_with_subtitles_kept

FIXTURE = Path(__file__).parent / "fixtures" / "timeline_kurumi_e03_timings.json"


def _srt(path: Path, cues: list[list[float]], prefix: str) -> Path:
    def stamp(value: float) -> str:
        ms = int(round(value * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    blocks = [f"{i}\n{stamp(s)} --> {stamp(e)}\n{prefix} {i}\n" for i, (s, e) in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def test_the_cut_is_where_the_whole_scan_agrees_and_the_last_clock_fits_its_lines(tmp_path: Path) -> None:
    data = json.loads(FIXTURE.read_text())
    source = _srt(tmp_path / "source.srt", data["source"], "ja")
    reference = _srt(tmp_path / "reference.srt", data["reference"], "en")
    _out, result = align_subtitle_timelines(source, reference, tmp_path / "cache", force=True)
    assert result["accepted"] is True
    # The cold open keeps its own clock (speech: 0 s before the OP), its cut inside the OP gap
    # (changed with the leading-window clock, test_fix_cold_open_own_clock.py).
    opening, boundary = result["timeline_boundaries"]
    assert 68.5 <= opening["source_time"] <= 178.04, opening
    assert 1016.0 <= boundary["source_time"] <= 1024.0, boundary  # not 952.5
    cold, first, last = result["timeline_segments"]
    assert cold["offset_seconds"] == 0.0
    assert first["offset_seconds"] == -5.0
    assert 1.2 <= last["offset_seconds"] <= 1.6, last  # not +2.0
    assert result["timeline_validation"]["after"]["mean_error_seconds"] < 0.42


def test_old_fits_with_a_crossover_boundary_are_prepared_again_once(tmp_path: Path) -> None:
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies"
    cfg.library.root_dir.mkdir()
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)

    def episode(name: str, state: str, algorithm: str, method: str) -> Path:
        video = cfg.library.root_dir / name
        video.write_bytes(b"video")
        subtitle = tmp_path / f"{name}.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=1,
                title="Show",
                episode=len(name),
                video_path=video,
                subtitle_path=subtitle,
                subtitle_origin="jimaku",
                state=state,
            )
        )
        manager.db.record_subtitle_history(
            video_path=video,
            media_id=1,
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
                    "timeline_boundaries": [{"source_time": 952.5, "refinement": {"method": method}}],
                }
            },
        )
        return video

    old = "timeline-v6.20-shared-opening-anchors"
    affected = episode("a.mkv", "ready", old, "fixed_offset_crossover")
    episode("bb.mkv", "watched", old, "fixed_offset_crossover")  # already watched
    episode("ccc.mkv", "ready", old, "nearest_onset_fallback")  # another kind of boundary
    episode("dddd.mkv", "ready", "timeline-v6.21-agreed-crossover-fine-offset", "fixed_offset_crossover")
    assert manager._requeue_early_crossover_timeline_upgrade() == 1
    assert_rebuilding_with_subtitles_kept(manager, affected)  # kept while it is prepared again
    assert manager._requeue_early_crossover_timeline_upgrade() == 0
