from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.config_path = tmp_path / "config.toml"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_mid_episode_resume_card_wins_over_ready_section(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    anime = LibraryAnime(
        media_id=630,
        title="Resume Priority",
        status="CURRENT",
        progress=0,
        episodes=12,
        media_status="RELEASING",
        next_airing_episode=2,
        next_airing_at=2_000_000_000,
    )
    api.manager.db.upsert_anime(anime)
    video = tmp_path / "Resume Priority - 01.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=anime.media_id,
            title=anime.title,
            episode=1,
            video_path=video,
            state="ready",
        )
    )
    api.manager.db.record_playback(video, 434.643, 1440.085)

    home = api.get_state()["home"]

    assert [item["media_id"] for item in home["continue_watching"]] == [anime.media_id]
    assert home["new_ready"] == []
    assert home["completed_ready"] == []
    assert home["continue_watching"][0]["resume_start"] == 419.643


def test_player_exit_refreshes_mid_episode_state() -> None:
    html = (
        Path(__file__).parents[1] / "pudge" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    assert (
        "setPathPlayState(path,'idle');if(!watchedReported){"
        "ui.state=await pywebview.api.get_state();renderDataPages();}"
    ) in html
