from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.debug_snapshot import DebugSnapshotService
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.subtitles.timeline_alignment import _WindowMatch, _best_path, _segments
from pudge.web_app import WebAppApi


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    return AnimeManager(cfg, log=lambda _message: None)


def test_completed_download_rewrites_stale_subtitle_job_identity(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=196187, title="Super no Ura de Yani Suu Futari", episodes=12))
    folder = manager.config.library.root_dir / "Super no Ura de Yani Suu Futari"
    folder.mkdir()
    video = folder / "[SubsPlease] Super no Ura de Yani Suu Futari - 07 (1080p) [608320F6].mkv"
    video.write_bytes(b"video")
    torrent_hash = "f7486e1d3e1fd3c0ca21f37f46ff295b0995f8a6"
    manager.db.upsert_episode(LibraryEpisode(
        media_id=196187, title="Super no Ura de Yani Suu Futari", episode=7, media_episode=7,
        release_episode=7, video_path=video.resolve(), state="waiting_subtitles", torrent_hash=torrent_hash,
    ))
    manager.db.queue_subtitle_job(video.resolve(), 196187, 6, delay_seconds=3600, error="legacy wrong episode")
    old_job = manager.db.subtitle_jobs()[0]
    manager.db.upsert_download(DownloadItem(
        torrent_hash=torrent_hash, name=video.name, state="complete", progress=1.0,
        save_path=str(folder), content_path=str(video.resolve()), media_id=196187,
        episode=7, media_episode=7, release_episode=7,
    ))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))

    assert manager.reconcile_completed_download_rows(196187, 7) == 0
    job = manager.db.subtitle_jobs()[0]
    assert job["media_id"] == 196187
    assert job["episode"] == 7
    assert float(job["next_check"]) == float(old_job["next_check"])
    assert int(job["attempts"]) == int(old_job["attempts"])


def test_debug_snapshot_repairs_super_identity_before_capturing_episode_list(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(
        media_id=196187, title="Super no Ura de Yani Suu Futari", status="CURRENT", progress=6, episodes=12,
    ))
    folder = manager.config.library.root_dir / "Super no Ura de Yani Suu Futari"
    folder.mkdir()
    video = folder / "[SubsPlease] Super no Ura de Yani Suu Futari - 07 (1080p) [608320F6].mkv"
    video.write_bytes(b"video")
    torrent_hash = "f7486e1d3e1fd3c0ca21f37f46ff295b0995f8a6"
    manager.db.upsert_episode(LibraryEpisode(
        media_id=196187, title="Super no Ura de Yani Suu Futari", episode=6, media_episode=6,
        release_episode=7, video_path=video.resolve(), state="waiting_subtitles", torrent_hash=torrent_hash,
    ))
    manager.db.upsert_download(DownloadItem(
        torrent_hash=torrent_hash, name=video.name, state="complete", progress=1.0,
        save_path=str(folder), content_path=str(video.resolve()), media_id=196187,
        episode=7, media_episode=7, release_episode=7,
    ))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))

    snapshot = DebugSnapshotService(manager, cache_dir=tmp_path / "cache", runtime_log_path=tmp_path / "runtime.log").snapshot(196187)
    assert [(row["episode"], row["video_path"]) for row in snapshot["available_episodes"]] == [(7, str(video.resolve()))]


def test_movie_payload_and_diagnosis_use_episode_none_for_local_movie(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    anime = LibraryAnime(media_id=178788, title="Mugenjou-hen Movie 1 - Akaza Sairai", format="MOVIE", status="CURRENT")
    api.manager.db.upsert_anime(anime)
    video = cfg.library.root_dir / "movie.mkv"
    video.write_bytes(b"video")
    sup = tmp_path / "movie.sup"
    sup.write_bytes(b"PG")
    api.manager.db.upsert_episode(LibraryEpisode(
        media_id=178788, title=anime.title, episode=None, media_episode=None, release_episode=None,
        video_path=video, subtitle_path=sup, subtitle_origin="bitmap", state="waiting_text_subtitles",
    ))

    payload = api._anime_payload(anime)
    assert payload["local"] is not None
    assert payload["local"]["video_path"] == str(video)
    diagnosis = api.manager.diagnose_episode(178788, 1)
    assert diagnosis["video_path"] == str(video)
    assert diagnosis["checks"][1]["ok"] is True
    assert diagnosis["checks"][2]["ok"] is True



def test_movie_job_identity_can_repair_legacy_episode_one(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "movie.mkv"
    video.write_bytes(b"video")
    manager.db.queue_subtitle_job(video.resolve(), 178788, 1, delay_seconds=3600, error="legacy movie episode")

    manager.db.ensure_subtitle_job(video.resolve(), 178788, None)

    job = manager.db.subtitle_jobs()[0]
    assert job["media_id"] == 178788
    assert job["episode"] is None


def test_debug_snapshot_normalizes_explicit_movie_episode_one(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    anime = LibraryAnime(media_id=178788, title="Mugenjou-hen Movie 1 - Akaza Sairai", format="MOVIE")
    manager.db.upsert_anime(anime)
    video = manager.config.library.root_dir / "movie.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(LibraryEpisode(
        media_id=178788, title=anime.title, episode=None, media_episode=None, release_episode=None,
        video_path=video.resolve(), state="waiting_text_subtitles",
    ))

    snapshot = DebugSnapshotService(
        manager, cache_dir=tmp_path / "cache", runtime_log_path=tmp_path / "runtime.log"
    ).snapshot(178788, 1)

    assert snapshot["selected_episode"] is None
    assert snapshot["selected_local_episode"]["video_path"] == str(video.resolve())
    assert snapshot["summary"]["diagnosis"]["checks"][1]["ok"] is True

def _match(center: float, offset: float, score: float) -> _WindowMatch:
    return _WindowMatch(
        center=center, offset=offset, score=score, matched=12, source_count=15, reference_count=15,
        onset_coverage=0.8, onset_f1=0.8, activity_f1=0.8, mean_error=0.4,
        rank_delta=0.1, gap_fingerprint=0.7, edge_hint_distance=None,
    )


def test_timeline_path_ignores_isolated_large_offset_peaks_but_keeps_sustained_edit() -> None:
    windows = []
    centers = [72.0 * i for i in range(1, 13)]
    for index, center in enumerate(centers):
        if index < 7:
            stable = 84.0
            local = 98.0 if index == 3 else 117.0 if index == 6 else stable
            windows.append((center, [_match(center, local, 3.0), _match(center, stable, 2.55)]))
        else:
            stable = -46.0
            local = -89.0 if index == 9 else -97.0 if index == 11 else stable
            windows.append((center, [_match(center, local, 3.0), _match(center, stable, 2.55)]))

    path = _best_path(windows)
    segments = _segments(path)
    assert len(segments) == 2
    assert [round(float(row["offset_seconds"])) for row in segments] == [84, -46]


def test_prepared_text_repair_does_not_promote_ocr_when_policy_is_disabled(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config.matching.ocr_counts_as_ready = False
    video = manager.config.library.root_dir / "movie.mkv"
    video.write_bytes(b"video")
    subtitle = manager.config.paths.cache_dir / "playback-srt" / "v14-ocr.srt"
    subtitle.parent.mkdir(parents=True, exist_ok=True)
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n鬼\n", encoding="utf-8")
    manager.db.upsert_episode(LibraryEpisode(
        media_id=178788,
        title="Mugenjou-hen Movie 1 - Akaza Sairai",
        episode=None,
        media_episode=None,
        release_episode=None,
        video_path=video.resolve(),
        subtitle_path=subtitle.resolve(),
        subtitle_origin="ocr",
        state="waiting_text_subtitles",
    ))

    assert manager.reconcile_prepared_subtitle_rows(178788) == 0
    row = manager.db.episode_by_path(video.resolve())
    assert row is not None
    assert row.state == "waiting_text_subtitles"
    assert row.subtitle_origin == "ocr"


def test_movie_ocr_fallback_stays_not_ready_in_primary_anime_payload(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.matching.ocr_counts_as_ready = False
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    anime = LibraryAnime(
        media_id=178788,
        title="Mugenjou-hen Movie 1 - Akaza Sairai",
        format="MOVIE",
        status="CURRENT",
    )
    api.manager.db.upsert_anime(anime)
    video = cfg.library.root_dir / "movie.mkv"
    video.write_bytes(b"video")
    ocr = tmp_path / "ocr.srt"
    ocr.write_text("1\n00:00:01,000 --> 00:00:02,000\n鬼\n", encoding="utf-8")
    api.manager.db.upsert_episode(LibraryEpisode(
        media_id=178788,
        title=anime.title,
        episode=None,
        media_episode=None,
        release_episode=None,
        video_path=video,
        subtitle_path=ocr,
        subtitle_origin="ocr",
        state="ready",
    ))

    payload = api._anime_payload(anime)

    assert payload["local"] is not None
    assert payload["local"]["state"] == "waiting_text_subtitles"
    assert payload["local"]["subtitle_origin"] == "ocr"
    assert payload["local"]["subtitle_source"] == "image"
