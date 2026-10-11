import json
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def _api_with_unreadable_release(tmp_path: Path) -> WebAppApi:
    api = make_api(tmp_path)
    anime = LibraryAnime(media_id=1111, title="Readable Anime", status="CURRENT", episodes=12)
    video = tmp_path / "[Group] unreadable.release.name.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_anime(anime)
    api.manager.db.upsert_episode(
        LibraryEpisode(anime.media_id, anime.title, 5, video, state="waiting_subtitles")
    )
    api.manager.db.queue_subtitle_job(video, anime.media_id, 5)
    api.manager.db.upsert_download(
        DownloadItem(
            torrent_hash="abc123",
            name="[Group] unreadable.release.name",
            state="downloading",
            progress=0.5,
            save_path=str(tmp_path),
            content_path=str(video),
            media_id=anime.media_id,
            episode=5,
            is_batch=False,
        )
    )
    return api


def test_activity_payload_exposes_anime_titles(tmp_path: Path) -> None:
    state = _api_with_unreadable_release(tmp_path).get_state_fast()

    assert state["subtitle_jobs"][0]["anime_title"] == "Readable Anime"
    assert state["downloads"][0]["anime_title"] == "Readable Anime"


def test_downloads_show_the_anime_title_not_an_anilist_id(tmp_path: Path, page) -> None:
    # The Downloads window (the backend's real list) names the anime; the release name is the
    # detail line. It used to fall back to "AniList 1111".
    downloads = json.loads(
        json.dumps(_api_with_unreadable_release(tmp_path).torrent_downloads(None, False), default=str)
    )
    result = page.run(
        r"""
        await openDownloadCenter(null); await __tick(20);
        const card = document.querySelector('#modalBody .download-card');
        return {title: card.querySelector('.download-card-title strong').textContent, detail: card.querySelector('.download-card-title span').textContent,
                text: __text('#modalBody')};
        """,
        api={"torrent_downloads": downloads},
    )
    out = result.value
    assert out["title"] == "Readable Anime"
    assert "[Group] unreadable.release.name" in out["detail"]
    assert "AniList 1111" not in out["text"]
