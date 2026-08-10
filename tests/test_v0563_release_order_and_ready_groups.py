from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def relation(
    media_id: int,
    title: str,
    start_date: str,
    relation_type: str = "SIDE_STORY",
    *,
    children: list[dict] | None = None,
) -> dict:
    return {
        "relation_type": relation_type,
        "media_id": media_id,
        "title": title,
        "site_url": f"https://anilist.co/anime/{media_id}",
        "format": "SPECIAL",
        "season_year": int(start_date[:4]),
        "start_date": start_date,
        "episodes": 1,
        "relations": children or [],
    }


def test_release_date_setting_persists_and_orders_all_anime_relations(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    assert api.config.anilist.relations_by_release_date is True

    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=100,
            title="Current",
            status="PLANNING",
            episodes=1,
            season_year=2020,
            start_date="2020-06-01",
            relations=[
                relation(80, "Older side story", "2018-01-01"),
                relation(90, "Previous release", "2019-12-01", "SEQUEL"),
                relation(
                    110,
                    "Later release",
                    "2021-01-01",
                    "PREQUEL",
                    children=[relation(120, "Latest release", "2022-01-01", "OTHER")],
                ),
            ],
        )
    )

    graph = api.get_state()["planned"][0]["relations"]
    assert graph["order_mode"] == "release"
    assert [item["media_id"] for level in graph["prequel_levels"] for item in level] == [80, 90]
    assert [item["media_id"] for level in graph["sequel_levels"] for item in level] == [110, 120]

    api.save_settings({"relations_by_release_date": False})
    assert load_config(api.config_path).anilist.relations_by_release_date is False


def test_completed_ready_single_episode_chain_is_grouped_in_release_order(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    entries = [
        (1, "Part C", "2022-01-01", 2),
        (2, "Part A", "2020-01-01", 3),
        (3, "Part B", "2021-01-01", 1),
    ]
    for media_id, title, start_date, related_id in entries:
        api.manager.db.upsert_anime(
            LibraryAnime(
                media_id=media_id,
                title=title,
                status="PLANNING",
                media_status="FINISHED",
                episodes=1,
                start_date=start_date,
                season_year=int(start_date[:4]),
                relations=[relation(related_id, f"Related {related_id}", "2020-01-01")],
            )
        )
        video = tmp_path / f"{title}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=media_id,
                title=title,
                episode=1,
                video_path=video,
                state="ready",
            )
        )

    completed = api.get_state()["home"]["completed_ready"]
    assert len(completed) == 1
    assert completed[0]["kind"] == "watch_sequence"
    assert [item["media_id"] for item in completed[0]["items"]] == [2, 3, 1]

    html = HTML.read_text(encoding="utf-8")
    assert "readySequenceCard" in html
    assert "Порядок просмотра" in html


def test_downloaded_planning_title_waiting_for_subtitles_is_visible_on_home(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=999,
            title="Boku no Hero Academia: I am a hero too",
            status="PLANNING",
            media_status="FINISHED",
            episodes=1,
        )
    )
    video = tmp_path / "Boku no Hero Academia - I am a hero too.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=999,
            title="Boku no Hero Academia: I am a hero too",
            episode=1,
            video_path=video,
            state="waiting_subtitles",
        )
    )

    home = api.get_state()["home"]
    assert [item["media_id"] for item in home["waiting"]] == [999]
    assert home["waiting"][0]["local"]["state"] == "waiting_subtitles"
