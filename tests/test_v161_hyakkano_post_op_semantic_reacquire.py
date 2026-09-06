from pathlib import Path

from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import (
    _local_stt_text_shift_estimate,
    _refine_stt_opening_plateaus,
)


def _post_cues():
    starts = [129.329, 134.8, 140.2, 146.0, 152.3, 159.1, 166.0, 173.5]
    texts = [
        "やれやれ", "どういうことだ", "本当に大丈夫", "そんなわけない",
        "ちょっと待って", "分かったよ", "今行くから", "ありがとう",
    ]
    return [(start, start + 1.6, text) for start, text in zip(starts, texts)]


def test_semantic_local_clock_can_reacquire_minus_eight_seconds(tmp_path: Path) -> None:
    reference = tmp_path / "reference.srt"
    post = _post_cues()

    # Dense unrelated STT onsets create a convincing -2.7s timing alias.
    # The actual spoken Japanese for these subtitle lines is 8s earlier.
    rows = []
    for index, (start, _end, text) in enumerate(post):
        rows.append((start - 2.7, start - 1.9, f"雑音{index}"))
        if index < 6:
            rows.append((start - 8.0, start - 7.2, text))
    write_srt(sorted(rows), reference)

    result = _local_stt_text_shift_estimate(
        post,
        reference,
        max_shift_seconds=10.0,
    )

    assert result["accepted"] is True
    assert int(result["cluster_support"]) >= 5
    assert abs(float(result["shift_seconds"]) + 8.0) <= 0.15


def test_opening_plateau_uses_semantic_post_clock_over_onset_alias(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    reference = tmp_path / "reference.srt"

    pre_starts = [2.0, 5.0, 8.0, 11.0, 14.0, 17.0, 20.0, 23.0]
    pre = [(start, start + 1.2, f"前{index}") for index, start in enumerate(pre_starts)]
    post = _post_cues()
    source_cues = pre + post
    write_srt(source_cues, source)
    write_srt(source_cues, aligned)

    rows = []
    # Keep the cold-open clock stable.
    rows.extend((start, end, text) for start, end, text in pre)
    # Every post cue gets an unrelated -2.7s onset, so onset-only matching wins there.
    # Six real Japanese lines at -8s provide a stronger semantic clock.
    for index, (start, _end, text) in enumerate(post):
        rows.append((start - 2.7, start - 1.9, f"雑音{index}"))
        if index < 6:
            rows.append((start - 8.0, start - 7.2, text))
    write_srt(sorted(rows), reference)

    output, result = _refine_stt_opening_plateaus(
        source,
        aligned,
        reference,
        tmp_path / "cache",
    )

    assert result["applied"] is True
    assert result["post_choice"]["mode"] == "semantic_override"
    assert abs(float(result["post_shift_seconds"]) + 8.0) <= 0.15
    cues = parse_srt(output)
    assert abs(float(cues[len(pre)][0]) - (post[0][0] - 8.0)) <= 0.15


def test_upgrade_requeues_old_onset_only_opening_plateau(tmp_path: Path) -> None:
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies" / "pudge"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)

    video = cfg.library.root_dir / "Hyakkano - 32.mkv"
    video.write_bytes(b"video")
    subtitle = cfg.paths.cache_dir / "playback-srt" / "old.srt"
    subtitle.parent.mkdir(parents=True, exist_ok=True)
    subtitle.write_text("1\n00:02:09,000 --> 00:02:11,000\nやれやれ\n", encoding="utf-8")
    raw = cfg.paths.cache_dir / "jimaku" / "candidate.srt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("dummy", encoding="utf-8")

    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo 3rd Season",
            episode=8,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=200637,
        episode=8,
        source="jimaku",
        candidate_name=raw.name,
        candidate_path=raw,
        status="selected",
        reason="Preparation completed",
        details={
            "alignment": {
                "engine": "japanese-stt+alass+opening-plateau",
                "stt_opening_plateau_refinement": {
                    "applied": True,
                    "gap_seconds": 104.004,
                    "pre_shift_seconds": 4.1,
                    "post_shift_seconds": -2.7,
                },
            }
        },
    )

    assert manager._requeue_stt_opening_plateau_semantic_upgrade() == 1
    repaired = manager.db.episode_by_path(video)
    assert repaired is not None
    assert repaired.subtitle_path is None
    assert repaired.state == "waiting_subtitles"
    assert manager.db.get_state("subtitle_stt_plateau_semantic_generation", "") == "1"
    assert manager._requeue_stt_opening_plateau_semantic_upgrade() == 0
