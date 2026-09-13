from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.anilist.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.agent.delete_only_managed_files = True
    cfg.agent.keep_batch_until_completed = True
    return AnimeManager(cfg, log=lambda _message: None)


def test_cleanup_never_drops_remaining_batch_rows_when_download_metadata_is_missing(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    media_id = 151379
    torrent_hash = "akiba-batch"
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=media_id,
            title="Akiba Meido Sensou",
            status="CURRENT",
            progress=4,
            episodes=12,
        )
    )

    paths: list[Path] = []
    for episode in range(1, 13):
        video = manager.config.library.root_dir / f"Akiba Maid Sensou - {episode:02d}.mkv"
        video.write_bytes(b"video")
        paths.append(video)
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=media_id,
                title="Akiba Meido Sensou",
                episode=episode,
                video_path=video,
                state="watched" if episode <= 4 else "ready",
                torrent_hash=torrent_hash,
            ),
            downloaded_at=1.0,
        )

    # Simulate old/stale qBittorrent metadata: all episode rows still prove that
    # this hash is a batch even though there is no downloads row anymore.
    manager.db.schedule_cleanup(paths[0], 0.0)

    assert manager.db.episode_count_for_torrent(torrent_hash) == 12
    assert manager.db.download_by_hash(torrent_hash) is None
    assert manager.cleanup() == 0
    assert manager.db.episode_count_for_torrent(torrent_hash) == 12
    assert all(path.exists() for path in paths)


def test_manga_page_picker_is_scrollable_dropdown_under_page_number() -> None:
    root = Path(__file__).parents[1]
    js = (root / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    css = (root / "pudge" / "web" / "manga_reader_v2.css").read_text(encoding="utf-8")

    assert 'class="manga-v2-page-picker-shell"' in js
    assert 'aria-haspopup="listbox"' in js
    assert 'data-manga-v2-page-option' in js
    assert 'mangaV2PageInput' not in js
    assert '.manga-v2-page-picker-shell{position:relative' in css
    assert 'top:calc(100% + 5px)' in css
    assert 'max-height:min(340px,52vh)' in css
    assert 'overflow-y:auto' in css


def test_torrent_toggle_waiting_count_uses_real_backend_jobs_not_stale_intents() -> None:
    import logging
    import threading
    from types import SimpleNamespace

    from pudge import web_app

    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=False),
        qbittorrent=SimpleNamespace(category="pudge"),
    )
    api.manager = SimpleNamespace(
        config=SimpleNamespace(nyaa=SimpleNamespace(torrents_enabled=False)),
        db=SimpleNamespace(downloads=lambda: []),
        download_intents=SimpleNamespace(waiting_count=lambda: 2),
        torrent_clients=lambda: [],
    )
    api.logger = logging.getLogger("test-v212-no-ghost-torrent-waiting")
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = False
    api._torrent_session_authoritative = True
    api._torrent_traffic_lock = threading.Lock()
    api._last_torrent_traffic = {}
    api._downloads_configured = lambda: True

    off = api.torrent_traffic_status()
    assert off["waiting"] == 0
    assert off["active"] == 0
    assert off["download_speed"] == 0
    assert off["upload_speed"] == 0

    api._torrent_session_enabled = True
    on = api.torrent_traffic_status()
    assert on["waiting"] == 0
    assert on["active"] == 0
    assert on["download_speed"] == 0
    assert on["upload_speed"] == 0


def test_torrent_toggle_frontend_trusts_fresh_live_waiting_count() -> None:
    root = Path(__file__).parents[1]
    html = (root / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    toggle = html.split("function refreshTorrentToggle(){", 1)[1].split(
        "function scheduleTorrentTrafficPoll", 1
    )[0]

    assert "const waiting=liveFresh?Math.max(0,Number(live.waiting||0)):fallbackWaiting;" in toggle
    assert "Math.max(fallbackWaiting,Number(live.waiting||0))" not in toggle
