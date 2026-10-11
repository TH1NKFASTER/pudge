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
    save_final_pipeline_result(
        video, cfg, subtitle=subtitle, subtitle_id=None, dependency=subtitle, source="ocr"
    )
    assert load_final_pipeline_result(video, cfg) is not None

    cfg.matching.ocr_image_subtitles = False
    assert load_final_pipeline_result(video, cfg) is None


def test_save_settings_returns_reconciled_state_and_requests_immediate_recheck(
    tmp_path: Path, monkeypatch
) -> None:
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
    episode = next(
        item
        for group in result["state"]["library"]
        for item in group["episodes"]
        if item["video_path"] == str(video)
    )
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


# A manual subtitle check (priority 200) is due now; window.__blocked makes the backend say
# it waits until playback ends (heavy work is not run while mpv plays).
PRIORITY_JOB = {
    "video": "Show - 03.mkv",
    "video_path": "/v/Show - 03.mkv",
    "state": "pending",
    "priority": 200,
    "next_check": 1790856000,
    "attempts": 0,
}  # the harness clock's start


def test_window_checks_a_due_priority_subtitle_job_every_second(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const polls = after => __apiCalls.filter(c => c.name === 'poll_downloads_and_subtitles' && c.at > after).map(c => c.at);
            await __tick(5500);
            const front = {polls: polls(0), status: __text('#status')};
            window.__blocked = true;
            await __tick(15000);
            const blocked = {polls: polls(5500), status: __text('#status')};
            await __setHidden(true);
            await __tick(200000);
            return {front, blocked, hidden: polls(20500),
                    texts: [t('status.subtitleCheckingBackground'), t('status.subtitleWaitingPlayback')]};
            """,
            lang=lang,
            state={"subtitle_jobs": [PRIORITY_JOB]},
            before="__api.poll_downloads_and_subtitles = async () => ({stats: window.__blocked ? {subtitle_waiting_for_foreground: 1} : {}});",
        )
        out = result.value
        checking, after_playback = out["texts"]
        assert out["front"] == {"polls": [1000, 2000, 3000, 4000, 5000], "status": checking}
        # Waiting for the player: every 5 s; in the background once a minute.
        assert out["blocked"] == {"polls": [6000, 11000, 16000], "status": after_playback}
        assert out["hidden"] == [80500, 140500, 200500]


def test_saving_settings_that_need_a_subtitle_recheck_polls_at_once(page) -> None:
    # Nothing to do: the window stopped polling. Settings that change subtitle readiness
    # (OCR off, a new Jimaku key, ...) make the backend ask for a recheck; the window polls
    # right away instead of waiting for the next check or a Refresh.
    for recheck in (True, False):
        result = page.run(
            r"""
            await __tick(30000);
            setPage('settings', true); await __tick(20);
            await __click('#saveSettings'); await __tick(5);
            return __apiCalls.filter(c => c.name === 'poll_downloads_and_subtitles').map(c => c.at);
            """,
            before=f"__api.save_settings = async values => ({{ok: true, settings: {{...values, light_novels: {{}}}}, recheck_subtitles: {str(recheck).lower()}}});",
        )
        assert result.value == ([1000, 16000, 30020] if recheck else [1000, 16000])
