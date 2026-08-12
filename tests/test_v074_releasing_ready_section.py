from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime, LibraryEpisode
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


def test_releasing_planning_title_with_multiple_ready_episodes_is_new_ready(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=74001,
            title="Still Airing",
            status="PLANNING",
            media_status="RELEASING",
            progress=0,
            episodes=12,
        )
    )

    for episode in (1, 2):
        video = tmp_path / f"Still Airing - {episode:02d}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=74001,
                title="Still Airing",
                episode=episode,
                video_path=video,
                state="ready",
            )
        )

    home = api.get_state()["home"]

    assert any(item.get("media_id") == 74001 for item in home["new_ready"])
    assert not any(item.get("media_id") == 74001 for item in home["completed_ready"])


def test_finished_planning_title_with_ready_episode_stays_completed_ready(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=74002,
            title="Finished Show",
            status="PLANNING",
            media_status="FINISHED",
            progress=0,
            episodes=2,
        )
    )
    video = tmp_path / "Finished Show - 01.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=74002,
            title="Finished Show",
            episode=1,
            video_path=video,
            state="ready",
        )
    )

    home = api.get_state()["home"]

    assert any(item.get("media_id") == 74002 for item in home["completed_ready"])
