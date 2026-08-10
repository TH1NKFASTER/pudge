from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.database import Database
from pudge.library import scan_library
from pudge.manager_models import LibraryEpisode
from pudge.pipeline_cache import save_final_pipeline_result


def test_library_scan_does_not_enqueue_valid_ready_prepared_subtitle(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    video = root / "Odd Taxi - 01.mkv"
    subtitle = tmp_path / "cache" / "playback-srt" / "odd-taxi-01.srt"
    video.write_bytes(b"video")
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")

    db = Database(tmp_path / "library.sqlite3")
    db.upsert_episode(
        LibraryEpisode(
            media_id=128547,
            title="Odd Taxi",
            episode=1,
            video_path=video.resolve(),
            subtitle_path=subtitle.resolve(),
            state="ready",
        )
    )

    monkeypatch.setattr(
        "pudge.library.japanese_subtitle_details",
        lambda *_args, **_kwargs: ("none", None, None),
    )

    items = scan_library(root, db, ffprobe="ffprobe", ffmpeg="ffmpeg")

    assert len(items) == 1
    assert items[0].state == "ready"
    assert items[0].subtitle_path == subtitle.resolve()
    assert db.subtitle_jobs() == []


def test_library_scan_restores_reset_row_from_valid_final_pipeline_cache(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"

    video = cfg.library.root_dir / "Odd Taxi - 02.mkv"
    subtitle = cfg.paths.cache_dir / "playback-srt" / "odd-taxi-02.srt"
    video.write_bytes(b"video")
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    save_final_pipeline_result(
        video.resolve(),
        cfg,
        subtitle=subtitle.resolve(),
        subtitle_id=None,
        dependency=subtitle.resolve(),
        source="jimaku",
    )

    db = Database(cfg.library.database_path)
    db.upsert_episode(
        LibraryEpisode(
            media_id=128547,
            title="Odd Taxi",
            episode=2,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )
    db.queue_subtitle_job(video.resolve(), 128547, 2)

    monkeypatch.setattr(
        "pudge.library.japanese_subtitle_details",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("valid final cache must avoid subtitle reprobe")
        ),
    )

    items = scan_library(
        cfg.library.root_dir,
        db,
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        pipeline_cache_config=cfg,
    )

    assert len(items) == 1
    stored = db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.state == "ready"
    assert stored.subtitle_path == subtitle.resolve()
    assert db.subtitle_jobs() == []


def test_spurious_ready_job_is_removed_before_stale_selection_repair(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Odd Taxi - 03.mkv"
    subtitle = tmp_path / "playback-srt" / "odd-taxi-03.srt"
    video.write_bytes(b"video")
    subtitle.parent.mkdir()
    subtitle.write_text("subtitle", encoding="utf-8")

    db.upsert_episode(
        LibraryEpisode(
            media_id=128547,
            title="Odd Taxi",
            episode=3,
            video_path=video.resolve(),
            subtitle_path=subtitle.resolve(),
            state="ready",
        )
    )
    db.queue_subtitle_job(video.resolve(), 128547, 3)

    assert db.repair_spurious_ready_subtitle_jobs() == 1
    assert db.repair_stale_subtitle_selections() == 0
    stored = db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.state == "ready"
    assert stored.subtitle_path == subtitle.resolve()
    assert db.subtitle_jobs() == []


def test_library_scan_preserves_bitmap_fallback_while_text_job_remains(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    video = root / "Movie.mkv"
    bitmap = tmp_path / "cache" / "pgs-onset" / "movie.sup"
    video.write_bytes(b"video")
    bitmap.parent.mkdir(parents=True)
    bitmap.write_bytes(b"bitmap")

    db = Database(tmp_path / "library.sqlite3")
    db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Movie",
            episode=None,
            video_path=video.resolve(),
            subtitle_path=bitmap.resolve(),
            state="waiting_text_subtitles",
        )
    )
    db.queue_subtitle_job(video.resolve(), 1, None)

    monkeypatch.setattr(
        "pudge.library.japanese_subtitle_details",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("valid bitmap fallback must be preserved")
        ),
    )

    items = scan_library(root, db, ffprobe="ffprobe", ffmpeg="ffmpeg")

    assert items[0].state == "waiting_text_subtitles"
    assert items[0].subtitle_path == bitmap.resolve()
    assert len(db.subtitle_jobs()) == 1
