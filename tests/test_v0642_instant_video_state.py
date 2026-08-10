from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.pipeline_cache import load_final_pipeline_result, save_final_pipeline_result
from pudge.subtitle_formats import clean_srt_for_playback
from pudge.web_app import WebAppApi


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.download_dirs = []
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.nyaa.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    return cfg


def test_ocr_origin_is_persisted_on_episode_rows(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    manager = AnimeManager(cfg)
    video = tmp_path / "ep.mkv"
    subtitle = tmp_path / "ocr.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nJP\n", encoding="utf-8")
    manager.db.upsert_episode(LibraryEpisode(None, "Anime", 1, video, state="waiting_subtitles"))
    manager.db.set_subtitle_ready(video, subtitle, origin="ocr")

    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "ready"
    assert row.subtitle_origin == "ocr"


def test_disabling_ocr_immediately_moves_ready_episode_to_waiting_text(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    cfg.matching.ocr_image_subtitles = False
    manager = AnimeManager(cfg)
    video = tmp_path / "ep.mkv"
    ocr_srt = tmp_path / "cache" / "playback-srt" / "v12-test.srt"
    bitmap = tmp_path / "ep.sup"
    video.write_bytes(b"video")
    ocr_srt.parent.mkdir(parents=True)
    ocr_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nJP\n", encoding="utf-8")
    bitmap.write_bytes(b"PG")
    manager.db.upsert_episode(
        LibraryEpisode(None, "Anime", 1, video, subtitle_path=ocr_srt, state="ready", subtitle_origin="ocr")
    )

    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_details",
        lambda *args, **kwargs: ("external_bitmap", bitmap, None),
    )
    changed = manager.invalidate_disabled_ocr_subtitles()

    assert changed == [video.resolve()]
    row = manager.db.episode_by_path(video.resolve())
    assert row is not None
    assert row.state == "waiting_text_subtitles"
    assert row.subtitle_path == bitmap
    assert row.subtitle_origin == "bitmap"
    jobs = manager.db.subtitle_jobs()
    assert len(jobs) == 1
    assert int(jobs[0]["priority"]) >= 200
    assert "OCR disabled" in str(jobs[0]["last_error"])


def test_final_ocr_pipeline_cache_is_invalid_when_ocr_setting_is_off(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.matching.ocr_image_subtitles = True
    video = tmp_path / "ep.mkv"
    subtitle = tmp_path / "ocr.srt"
    video.write_bytes(b"video")
    subtitle.write_text("subtitle", encoding="utf-8")
    save_final_pipeline_result(video, cfg, subtitle=subtitle, subtitle_id=None, dependency=subtitle, source="ocr")
    assert load_final_pipeline_result(video, cfg) is not None

    cfg.matching.ocr_image_subtitles = False
    assert load_final_pipeline_result(video, cfg) is None


def test_save_settings_returns_reconciled_state_and_requests_immediate_recheck(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    cfg.matching.ocr_image_subtitles = True
    config_path = tmp_path / "config.toml"
    write_config(cfg, config_path)
    api = WebAppApi(config_path)
    video = tmp_path / "ep.mkv"
    ocr_srt = tmp_path / "ocr.srt"
    bitmap = tmp_path / "ep.sup"
    video.write_bytes(b"video")
    ocr_srt.write_text("subtitle", encoding="utf-8")
    bitmap.write_bytes(b"PG")
    api.manager.db.upsert_episode(
        LibraryEpisode(None, "Anime", 1, video, subtitle_path=ocr_srt, state="ready", subtitle_origin="ocr")
    )
    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_details",
        lambda *args, **kwargs: ("external_bitmap", bitmap, None),
    )
    monkeypatch.setattr("pudge.web_app.request_folder_access", lambda paths: {})

    result = api.save_settings({"ocr_image_subtitles": False})

    assert result["recheck_subtitles"] is True
    assert result["reconcile"]["ocr_invalidated"] == 1
    episode = next(item for item in result["state"]["episodes"] if item["video_path"] == str(video))
    assert episode["state"] == "waiting_text_subtitles"



def test_legacy_cleaned_ocr_cache_is_recognized_without_db_origin(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.matching.ocr_image_subtitles = False
    manager = AnimeManager(cfg)
    video = tmp_path / "ep.mkv"
    video.write_bytes(b"video")
    ocr_srt = cfg.paths.cache_dir / "ocr" / "legacy.srt"
    ocr_srt.parent.mkdir(parents=True, exist_ok=True)
    ocr_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nJP\n", encoding="utf-8")
    cleaned, _ = clean_srt_for_playback(ocr_srt, cfg.paths.cache_dir)
    item = LibraryEpisode(None, "Anime", 1, video, subtitle_path=cleaned, state="ready")
    manager.db.upsert_episode(item)

    persisted = manager.db.episode_by_path(video)
    assert persisted is not None
    assert persisted.subtitle_origin == ""
    assert manager._is_legacy_ocr_prepared_subtitle(persisted) is True

def test_web_ui_applies_settings_state_and_polls_priority_jobs_immediately() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "duePrioritySubtitleJobs" in html
    assert "document.hidden||!ui.windowActive?5000:1000" in html
    assert "if(r.state)ui.state=r.state" in html
    assert "if(r.recheck_subtitles)" in html
    assert "setTimeout(pollForegroundWork,0)" in html
    assert "if(progress>=.95)return 2000" in html
