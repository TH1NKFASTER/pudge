from __future__ import annotations

import json
from pathlib import Path

from css_support import css_rules

from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
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


def partial_download(tmp_path: Path, *, media_id: int | None = 42) -> DownloadItem:
    video = tmp_path / "library" / "Anime" / "movie.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"partial")
    return DownloadItem(
        torrent_hash="abc123",
        name="Anime movie",
        state="downloading",
        progress=0.35,
        save_path=str(video.parent),
        content_path=str(video),
        media_id=media_id,
        episode=None,
        is_batch=True,
        added_on=1,
        completed_on=0,
        raw={},
    )


def test_download_available_card_contains_active_download_state(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=42,
            title="Backlog anime",
            status="CURRENT",
            progress=0,
            episodes=12,
            media_status="FINISHED",
        )
    )
    api.manager.db.upsert_download(partial_download(tmp_path))

    card = api.get_state()["home"]["download_available"][0]

    assert card["download"]["progress"] == 0.35
    assert card["download"]["state"] == "downloading"


def test_partial_qbittorrent_file_is_hidden_from_library_and_ready_sections(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    item = partial_download(tmp_path)
    video = Path(item.content_path)
    api.manager.db.upsert_anime(LibraryAnime(media_id=42, title="Anime"))
    api.manager.db.upsert_download(item)
    # Simulate the stale false-positive row produced by an older version.
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=42,
            title="Anime",
            episode=None,
            video_path=video,
            state="ready",
        )
    )

    state = api.get_state()

    assert state["library"] == []
    anime_by_id = {anime.media_id: anime for anime in api.manager.db.anime_list()}
    assert api._downloaded_payloads(anime_by_id) == []
    assert state["home"]["completed_ready"] == []


def test_scan_library_removes_stale_partial_episode_row(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    item = partial_download(tmp_path)
    video = Path(item.content_path)
    api.manager.db.upsert_download(item)
    api.manager.db.upsert_episode(LibraryEpisode(None, "Anime", None, video, state="ready"))
    api.manager.db.queue_subtitle_job(video, None, None)

    api.manager.scan_library()

    assert api.manager.db.episode_by_path(video) is None
    assert api.manager.db.subtitle_jobs() == []


def test_existing_old_torrent_for_same_anime_blocks_second_batch(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.qbittorrent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=119321,
            title="Mahou Shoujo Madoka☆Magica: Hangyaku no Monogatari",
            format="MOVIE",
        )
    )
    existing = DownloadItem(
        torrent_hash="oldhash",
        name="[Group] Mahou Shoujo Madoka Magica Hangyaku no Monogatari [1080p]",
        state="downloading",
        progress=0.55,
        save_path=str(tmp_path / "library"),
        content_path=str(tmp_path / "library" / "Madoka.mkv"),
        media_id=None,
        episode=None,
        is_batch=False,
        raw={},
    )
    added = []

    class FakeClient:
        def torrents(self, *, category=""):
            return [existing]

        def add_release(self, release, **kwargs):
            added.append(release)

        def close(self):
            pass

    monkeypatch.setattr(manager, "qbt_client", lambda: FakeClient())
    result = manager.add_release(
        119321,
        NyaaRelease(
            title="Another Madoka torrent",
            link="",
            torrent_url="",
            info_hash="newhash",
            size_text="2 GiB",
            size_bytes=2 * 1024**3,
            seeders=10,
            leechers=0,
            downloads=10,
            trusted=True,
            remake=False,
        ),
        episode=None,
        batch=True,
    )

    assert result is False
    assert added == []
    assert manager.db.download_by_hash("oldhash").media_id == 119321


def test_download_card_keeps_normal_height_and_hides_button_while_active(tmp_path: Path, page) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=42, title="Backlog anime", status="CURRENT", episodes=12, media_status="FINISHED"
        )
    )
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=43, title="Other backlog", status="CURRENT", episodes=12, media_status="FINISHED"
        )
    )
    api.manager.db.upsert_download(partial_download(tmp_path))
    state = json.loads(json.dumps(api.get_state(), default=str))
    state["settings"]["torrents_enabled"] = True
    release = {
        "title": "[Group] Other backlog (BD 1080p)",
        "seeders": 9,
        "downloads": 1,
        "size_text": "9 GiB",
        "group": "Group",
    }
    for lang in ("en", "ru"):
        result = page.run(
            """
            const card = id => document.querySelector(`[data-home-section="section.availableDownload"] [data-media-id="${id}"]`);
            const look = id => ({button: card(id).querySelector('button.download-card-button')?.innerText ?? null,
                                 state: card(id).querySelector('.download-card-state')?.innerText ?? null});
            const download = ui.state.home.download_available.find(a => a.media_id === 42).download;
            const out = {stale: look(42), staleExpected: compactDownloadStatus(download), idle: look(43),
                         downloadAnime: t('action.downloadAnime')};
            // Torrent traffic is fresh: the card says how far the download is.
            ui.torrentTraffic = {enabled: true, stale: false, updated_at: Date.now() / 1000, transition: 'on'};
            renderCurrent();
            out.running = look(42);
            out.runningExpected = t('label.downloading', {progress: 35});
            // Downloading the other one: the backend says it is already being downloaded.
            await __click('[data-home-section="section.availableDownload"] [data-media-id="43"] button.download-card-button');
            await __tick(10);
            await __click('#modalBody [data-action="download-release"]');
            await __tick(10);
            out.toast = __toasts().at(-1);
            out.already = t('toast.alreadyDownloading');
            return out;
            """,
            lang=lang,
            state=state,
            api={"search_releases": [release], "add_release": {"already_downloading": True}},
        )
        out = result.value
        assert out["idle"] == {"button": out["downloadAnime"], "state": None}
        assert out["stale"] == {"button": None, "state": out["staleExpected"]}
        assert out["running"] == {"button": None, "state": out["runningExpected"]}
        assert result.calls_of("add_release") == [[43, None, True, release]]
        assert out["toast"] == out["already"]
    # The state label replaces the button without changing the card's height.
    rules = css_rules("index.html")
    assert rules.get(".download-available-card .airing-meta", "height") == "68px"
    assert rules.get(".download-available-card .airing-meta", "min-height") == "68px"
    assert rules.get(".download-available-card .cover-action", "display") == "block"
    assert rules.get(".download-available-card .cover-action", "width") == "100%"
