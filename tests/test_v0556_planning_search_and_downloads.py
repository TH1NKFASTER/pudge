from __future__ import annotations

from pathlib import Path

from anime_mpv.config import AppConfig, write_config
from anime_mpv.manager_models import LibraryAnime
from anime_mpv.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "anime_mpv" / "web" / "index.html"


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_planning_search_supports_cmd_f_titles_aliases_and_anilist_id(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=151807,
            title="Mushoku Tensei III",
            titles=["Mushoku Tensei: Jobless Reincarnation Season 3"],
            synonyms=["無職転生 III"],
            status="PLANNING",
            media_status="NOT_YET_RELEASED",
        )
    )

    planned = api.get_state()["planned"][0]
    assert planned["media_id"] == 151807
    assert planned["titles"] == ["Mushoku Tensei: Jobless Reincarnation Season 3"]
    assert planned["synonyms"] == ["無職転生 III"]

    html = HTML.read_text(encoding="utf-8")
    assert 'id="plannedSearch" type="search"' in html
    assert "String(a.media_id)===query" in html
    assert "...(a.titles||[])" in html
    assert "...(a.synonyms||[])" in html
    assert "shortcut_app_planning_search" not in html
    assert "String(event.key||'').toLowerCase()==='f'" in html
    assert "if(ui.page!=='planned')setPage('planned')" not in html
    assert "ui.page==='settings'" in html
    assert "search.focus();search.select()" in html


def test_finished_watching_title_without_ready_files_is_available_to_download(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=42,
            title="Finished backlog anime",
            status="CURRENT",
            progress=2,
            episodes=12,
            media_status="FINISHED",
        )
    )

    home = api.get_state()["home"]

    assert [item["media_id"] for item in home["download_available"]] == [42]
    assert home["caught_up"] == []
    assert home["completed_ready"] == []


def test_unreleased_watching_title_is_not_offered_for_download(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=43,
            title="Future anime",
            status="CURRENT",
            progress=0,
            episodes=12,
            media_status="NOT_YET_RELEASED",
        )
    )

    assert api.get_state()["home"]["download_available"] == []


def test_download_group_and_planning_download_only_action_use_batch_search() -> None:
    html = HTML.read_text(encoding="utf-8")

    assert "'section.availableDownload':'Available to download'" in html
    assert "'section.availableDownload':'Можно скачать'" in html
    assert "+homeSection('section.availableDownload',home.download_available||[],downloadAvailableHomeCard)" in html
    assert 'data-action="release" data-id="${a.media_id}" data-batch="1"' in html
    assert 'data-context-action="download-only"' in html
    assert "if(action==='download-only'){await openRelease(a.media_id,null,true);return;}" in html
    assert "download_available" in html
