from pathlib import Path

from anime_mpv.config import AppConfig, write_config
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryEpisode
from anime_mpv.pipeline_cache import (
    final_pipeline_cache_available,
    save_final_pipeline_result,
)
from anime_mpv.subtitle_formats import clean_srt_for_playback
from anime_mpv.web_app import WebAppApi


def _srt(text: str = "日本語") -> str:
    return f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n"


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    return cfg


def test_alignment_generation_requeues_only_playback_copies_from_alignment_cache(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    manager = AnimeManager(cfg, log=lambda _message: None)
    video = cfg.library.root_dir / "Bleach.2004.S17E43.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")

    aligned = cfg.paths.cache_dir / "synced" / "old-bad-sync.srt"
    aligned.parent.mkdir(parents=True, exist_ok=True)
    aligned.write_text(_srt(), encoding="utf-8")
    playback, _ = clean_srt_for_playback(aligned, cfg.paths.cache_dir)

    # A normal playback-cleaned subtitle that did not come from an alignment
    # cache must not be rebuilt just because the sync algorithm changed.
    raw = cfg.paths.cache_dir / "jimaku" / "already-good.srt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(_srt("正常"), encoding="utf-8")
    unaffected_playback, _ = clean_srt_for_playback(raw, cfg.paths.cache_dir)
    unaffected_video = cfg.library.root_dir / "Other - 01.mkv"
    unaffected_video.write_bytes(b"video")

    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=185874,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            episode=43,
            video_path=video.resolve(),
            subtitle_path=playback.resolve(),
            state="ready",
        )
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=2,
            title="Other",
            episode=1,
            video_path=unaffected_video.resolve(),
            subtitle_path=unaffected_playback.resolve(),
            state="ready",
        )
    )
    manager.db.set_state("subtitle_validation_generation", "14")
    save_final_pipeline_result(
        video,
        cfg,
        subtitle=playback,
        subtitle_id=None,
        dependency=playback,
        source="external",
    )
    assert final_pipeline_cache_available(video, cfg)

    assert manager._requeue_legacy_generated_subtitles() == 1

    bleach = manager.db.episode_by_path(video.resolve())
    unaffected = manager.db.episode_by_path(unaffected_video.resolve())
    assert bleach is not None
    assert bleach.subtitle_path is None
    assert bleach.state == "waiting_subtitles"
    assert unaffected is not None
    assert unaffected.subtitle_path == unaffected_playback.resolve()
    assert final_pipeline_cache_available(video, cfg) is False
    assert manager.db.get_state("subtitle_validation_generation", "") == "16"


def test_qbittorrent_failure_does_not_block_due_subtitle_jobs(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path)
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    video = cfg.library.root_dir / "Anime - 01.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    api.manager.db.queue_subtitle_job(video, 1, 1, priority=200)

    calls: list[tuple[str, int]] = []

    def unavailable() -> int:
        raise RuntimeError("qBittorrent connection refused")

    def process(limit: int = 4, **_kwargs) -> int:
        calls.append(("subs", limit))
        return 1

    monkeypatch.setattr(api.manager, "sync_downloads", unavailable)
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", process)
    monkeypatch.setattr(api.manager, "cleanup_qbittorrent_tags", lambda: {})

    result = api.poll_downloads_and_subtitles()

    assert calls == [("subs", 1)]
    assert result["stats"]["subs"] == 1


def test_random_score_wheel_explanation_is_removed() -> None:
    html = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")
    assert "The wheel chooses one score; middle values have larger sectors." not in html
    assert "Колесо выбирает одну оценку; у средних значений сектора больше." not in html
    assert "const introHtml=intro?" in html
