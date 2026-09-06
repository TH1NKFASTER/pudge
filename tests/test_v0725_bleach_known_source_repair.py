from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.subtitle_formats import parse_srt
from pudge.subtitles.source_repairs import apply_known_source_repair


def _write_reference(path: Path) -> None:
    path.write_text(
        """1
00:00:17,590 --> 00:00:21,770
A fusion of Getsugatensho and Gran Rey Cero...

2
00:00:21,770 --> 00:00:27,360
A fitting power for one born from a fusion of so many things.

3
00:00:28,010 --> 00:00:31,360
And yet...

4
00:00:31,360 --> 00:00:33,450
it still falls short!

5
00:02:27,000 --> 00:02:29,000
Later dialogue
""",
        encoding="utf-8",
    )


def test_bleach_known_incomplete_source_restores_web_opening(tmp_path: Path) -> None:
    source = tmp_path / "aligned.srt"
    source.write_text(
        """1
00:00:33,082 --> 00:00:35,985
ふさわしい力だ。
だが　それでもなお➨

2
00:00:36,085 --> 00:00:38,053
私には届かぬ！

3
00:02:27,162 --> 00:02:29,665
悪魔の左腕！
""",
        encoding="utf-8",
    )
    reference = tmp_path / "english.srt"
    _write_reference(reference)

    repaired, result = apply_known_source_repair(
        source,
        tmp_path / "cache",
        media_id=185874,
        episode=5,
        candidate_name="[NanakoRaws] Bleach Sennen Kessen-hen S01E45 (TVA TV 1080p HEVC AAC).srt",
        timing_reference=reference,
    )

    assert result["applied"] is True
    cues = parse_srt(repaired)
    assert len(cues) == 5
    assert cues[0][0] == 17.59
    assert "月牙天衝と王虚の閃光の融合" in cues[0][2]
    assert cues[1][0] == 21.77
    assert "あらゆるものの融合" in cues[1][2]
    assert "ふさわしい力だ" in cues[1][2]
    assert cues[2][0] == 28.01
    assert "それでもなお" in cues[2][2]
    assert cues[3][0] == 31.36
    assert "私には届かぬ" in cues[3][2]
    assert cues[4][0] == 147.162
    assert "悪魔の左腕" in cues[4][2]


def test_known_source_repair_requires_exact_source_identity(tmp_path: Path) -> None:
    source = tmp_path / "aligned.srt"
    source.write_text(
        "1\n00:00:33,082 --> 00:00:38,053\nふさわしい力だ。 私には届かぬ！\n",
        encoding="utf-8",
    )
    reference = tmp_path / "english.srt"
    _write_reference(reference)

    repaired, result = apply_known_source_repair(
        source,
        tmp_path / "cache",
        media_id=185874,
        episode=5,
        candidate_name="Some other Bleach subtitle.srt",
        timing_reference=reference,
    )

    assert repaired == source
    assert result == {"applied": False, "reason": "no_matching_rule"}


def test_selected_v63_bleach_nanako_is_requeued_once_for_source_repair(
    tmp_path: Path,
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)

    video = cfg.library.root_dir / "Bleach E45.mkv"
    video.write_bytes(b"video")
    subtitle = cfg.paths.cache_dir / "playback-srt" / "bleach-v63.srt"
    subtitle.parent.mkdir(parents=True, exist_ok=True)
    subtitle.write_text(
        "1\n00:00:33,082 --> 00:00:38,053\nふさわしい力だ。\n",
        encoding="utf-8",
    )
    raw = cfg.paths.cache_dir / "jimaku" / (
        "[NanakoRaws] Bleach Sennen Kessen-hen S01E45 "
        "(TVA TV 1080p HEVC AAC).srt"
    )
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=185874,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            episode=5,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=185874,
        episode=5,
        source="jimaku",
        candidate_name=raw.name,
        candidate_path=raw,
        status="selected",
        reason="Preparation completed",
        details={"alignment": {"timeline_algorithm": "timeline-v6.3-onset-flip-boundary"}},
    )

    assert manager._requeue_known_source_repairs() == 1
    repaired = manager.db.episode_by_path(video)
    assert repaired is not None
    assert repaired.subtitle_path is None
    assert repaired.state == "waiting_subtitles"
    assert manager._requeue_known_source_repairs() == 0
