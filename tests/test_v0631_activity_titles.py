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


def test_activity_payload_exposes_anime_titles(tmp_path: Path) -> None:
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

    state = api.get_state_fast()

    assert state["subtitle_jobs"][0]["anime_title"] == "Readable Anime"
    assert state["downloads"][0]["anime_title"] == "Readable Anime"


def test_activity_html_never_uses_anilist_id_as_display_title() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("function renderDownloads()")
    end = html.index("function input(", start)
    activity = html[start:end]

    assert "`AniList ${job.media_id}`" not in activity
    assert "`AniList ${item.media_id}`" not in activity
    assert "job.anime_title||job.video" in activity
    assert "d.anime_title||d.name" in activity
