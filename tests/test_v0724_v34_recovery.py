from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi

ROOT = Path(__file__).parents[1]


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = True
    return AnimeManager(cfg, log=lambda _message: None)


def test_interface_text_is_nonselectable_except_reader_debug_and_named_titles() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")

    assert "body, body * { user-select:none; -webkit-user-select:none; }" in html
    assert ".pudge-debug-overlay,.pudge-debug-overlay *" in html
    assert "#lnReader,#lnReader *" in html
    assert '.anime-card[data-planned="1"] .anime-title' in html
    assert ".audiobook-title-row strong" in html
    assert "#lnReader rt,#lnReader rp { user-select:none" in html


def test_planning_card_reports_all_episodes_ready(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=9785,
            title="Akiba Meido Sensou",
            status="PLANNING",
            episodes=2,
            media_status="FINISHED",
        )
    )
    for episode in (1, 2):
        video = cfg.library.root_dir / f"Akiba Meido Sensou - {episode:02d}.mkv"
        subtitle = cfg.library.root_dir / f"Akiba Meido Sensou - {episode:02d}.ja.srt"
        video.write_bytes(b"video")
        subtitle.write_text("日本語", encoding="utf-8")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=9785,
                title="Akiba Meido Sensou",
                episode=episode,
                video_path=video,
                subtitle_path=subtitle,
                state="ready",
            )
        )

    planned = next(item for item in api.get_state()["planned"] if item["media_id"] == 9785)

    assert planned["ready_episodes"] == [1, 2]
    assert planned["all_episodes_ready"] is True
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "planned&&a.all_episodes_ready" in html
    assert "${t('label.readyAll')}" in html


def test_stale_zero_aria2_row_cannot_overwrite_completed_local_download(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Ghost in the Shell"
    folder.mkdir()
    video = folder / "Ghost in the Shell - 01.mkv"
    video.write_bytes(b"finished-video")
    torrent_hash = "ghost-hash"
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash=torrent_hash,
            name=video.name,
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(video),
            media_id=43,
            episode=1,
            completed_on=123,
            raw={"backend": "aria2", "total_size": video.stat().st_size, "downloaded": video.stat().st_size},
        )
    )
    stale = DownloadItem(
        torrent_hash=torrent_hash,
        name=video.name,
        state="paused",
        progress=0.0,
        save_path=str(folder),
        content_path="",
        media_id=43,
        episode=1,
        raw={"total_size": 0, "downloaded": 0, "download_speed": 0},
    )

    class Client:
        def torrents(self, *, category: str = ""):
            return [stale]

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "torrent_clients", lambda: [("aria2", Client())])
    monkeypatch.setattr(manager, "_register_completed_download", lambda *_a, **_k: 0)

    assert manager.sync_downloads() == 0
    stored = manager.db.download_by_hash(torrent_hash)
    assert stored is not None
    assert stored.state == "complete"
    assert stored.progress == 1.0
    assert Path(stored.content_path).resolve() == video.resolve()


def test_single_episode_completed_download_never_claims_neighbor_episode(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Super no Ura de Yani Suu Futari"
    folder.mkdir()
    episode6 = folder / "Super no Ura de Yani Suu Futari - 06.mkv"
    episode7 = folder / "Super no Ura de Yani Suu Futari - 07.mkv"
    episode6.write_bytes(b"six")
    episode7.write_bytes(b"seven")
    item = DownloadItem(
        torrent_hash="super-6",
        name="Super no Ura de Yani Suu Futari - 06",
        state="complete",
        progress=1.0,
        save_path=str(folder),
        content_path="",
        media_id=1,
        episode=6,
        media_episode=6,
        release_episode=6,
        is_batch=False,
    )

    assert manager._completed_download_video_files(item) == [episode6.resolve()]


def test_cmd_f_routing_and_remote_anilist_suggestions_are_bounded_and_deduped() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "if($('pudgeDebugOverlay')?.classList.contains('open'))return;" in html
    assert "if(ui.page==='settings')" in html
    assert "search.focus();search.select();" in html
    assert "if(item.remote)" in html

    # Local/Planning results render first; AniList is a second phase so a slow
    # remote request never blocks the local Cmd+F search.
    local_call = html.index("global_media_search(cleaned,40)")
    local_render = html.index("renderGlobalSearchResults(localRows,cleaned)", local_call)
    remote_call = html.index("planning_search_anilist(cleaned)", local_render)
    assert local_call < local_render < remote_call
    assert "function mergeGlobalAniListSuggestions" in html
    assert "added++;if(added>=5)break;" in html
    assert "ids.has(mediaId)" in html
    assert "titles.has(titleKey)" in html

    # Backend global search remains local-only; it must not perform AniList I/O.
    source = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    block = source[source.index("    def global_media_search("):source.index("    def test_saved_credentials(")]
    assert "planning_search_anilist" not in block


def test_stale_zero_aria2_row_recovers_from_exact_completed_file_after_db_was_already_corrupted(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Ghost in the Shell"
    folder.mkdir()
    video = folder / "THE.GHOST.IN.THE.SHELL.S01E07.EPISODE.07.1080p.mkv"
    video.write_bytes(b"finished-video")
    torrent_hash = "ghost-corrupted-hash"

    # This is the state seen in the real debug snapshot: completion was already
    # lost from the DB, so the v34 "stored complete wins" guard cannot help.
    corrupted = DownloadItem(
        torrent_hash=torrent_hash,
        name=video.name,
        state="paused",
        progress=0.0,
        save_path=str(folder),
        content_path=str(video),
        media_id=177699,
        episode=7,
        media_episode=7,
        release_episode=7,
        completed_on=0,
        raw={"backend": "aria2", "total_size": 0, "downloaded": 0},
    )
    manager.db.upsert_download(corrupted)

    class Client:
        def torrents(self, *, category: str = ""):
            return [corrupted]

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "torrent_clients", lambda: [("aria2", Client())])
    monkeypatch.setattr(manager, "_register_completed_download", lambda *_a, **_k: 0)

    assert manager.sync_downloads() == 0
    stored = manager.db.download_by_hash(torrent_hash)
    assert stored is not None
    assert stored.state == "complete"
    assert stored.progress == 1.0
    assert stored.completed_on > 0
    assert stored.raw["total_size"] == video.stat().st_size
    assert stored.raw["downloaded"] == video.stat().st_size
    assert stored.raw["recovered_stale_zero_local"] is True


def test_stale_zero_aria2_row_does_not_promote_file_with_active_control_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Ghost in the Shell"
    folder.mkdir()
    video = folder / "THE.GHOST.IN.THE.SHELL.S01E07.EPISODE.07.1080p.mkv"
    video.write_bytes(b"partial-video")
    Path(str(video) + ".aria2").write_bytes(b"control")
    stale = DownloadItem(
        torrent_hash="ghost-active-hash",
        name=video.name,
        state="paused",
        progress=0.0,
        save_path=str(folder),
        content_path=str(video),
        media_id=177699,
        episode=7,
        media_episode=7,
        release_episode=7,
        raw={"backend": "aria2", "total_size": 0, "downloaded": 0},
    )

    class Client:
        def torrents(self, *, category: str = ""):
            return [stale]

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "torrent_clients", lambda: [("aria2", Client())])
    monkeypatch.setattr(manager, "_register_completed_download", lambda *_a, **_k: 0)

    assert manager.sync_downloads() == 0
    stored = manager.db.download_by_hash(stale.torrent_hash)
    assert stored is not None
    assert stored.state == "paused"
    assert stored.progress == 0.0


def test_single_episode_completed_download_does_not_claim_only_newer_sibling_after_own_file_deleted(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Super no Ura de Yani Suu Futari"
    folder.mkdir()
    episode7 = folder / "[SubsPlease] Super no Ura de Yani Suu Futari - 07 (1080p).mkv"
    episode7.write_bytes(b"seven")
    stale_episode6 = DownloadItem(
        torrent_hash="super-old-6",
        name="[SubsPlease] Super no Ura de Yani Suu Futari - 06 (1080p).mkv",
        state="complete",
        progress=1.0,
        save_path=str(folder),
        content_path=str(folder),
        media_id=196187,
        episode=6,
        media_episode=6,
        release_episode=6,
        is_batch=False,
    )

    assert manager._completed_download_video_files(stale_episode6) == []


def test_stale_zero_aria2_exact_file_recovery_recreates_library_episode(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=177699,
            title="Koukaku Kidoutai: THE GHOST IN THE SHELL",
            status="CURRENT",
            episodes=12,
        )
    )
    folder = manager.config.library.root_dir / "Koukaku Kidoutai_ THE GHOST IN THE SHELL"
    folder.mkdir()
    video = folder / "THE.GHOST.IN.THE.SHELL.S01E07.EPISODE.07.1080p.mkv"
    video.write_bytes(b"finished-video")
    stale = DownloadItem(
        torrent_hash="6b65b261986af99e1296ded462d2282b35512978",
        name=video.name,
        state="paused",
        progress=0.0,
        save_path=str(folder),
        content_path=str(video),
        media_id=177699,
        episode=7,
        media_episode=7,
        release_episode=7,
        completed_on=0,
        raw={"backend": "aria2", "total_size": 0, "downloaded": 0},
    )
    manager.db.upsert_download(stale)

    class Client:
        def torrents(self, *, category: str = ""):
            return [stale]

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "torrent_clients", lambda: [("aria2", Client())])
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    assert manager.sync_downloads() == 1
    stored = manager.db.download_by_hash(stale.torrent_hash)
    row = manager.db.episode_by_path(video.resolve())
    assert stored is not None and stored.state == "complete"
    assert row is not None
    assert row.media_id == 177699
    assert row.media_episode == 7
    assert row.release_episode == 7
    assert row.video_path == video.resolve()
    assert row.state == "waiting_subtitles"


def test_legacy_completed_episode6_row_cannot_requeue_or_relabel_existing_episode7(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(media_id=196187, title="Super no Ura de Yani Suu Futari", episodes=12)
    )
    folder = manager.config.library.root_dir / "Super no Ura de Yani Suu Futari"
    folder.mkdir()
    video7 = folder / "[SubsPlease] Super no Ura de Yani Suu Futari - 07 (1080p).mkv"
    subtitle7 = folder / "[SubsPlease] Super no Ura de Yani Suu Futari - 07 (1080p).ja.srt"
    video7.write_bytes(b"seven")
    subtitle7.write_text("日本語", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=196187,
            title="Super no Ura de Yani Suu Futari",
            episode=7,
            media_episode=7,
            release_episode=7,
            video_path=video7.resolve(),
            subtitle_path=subtitle7.resolve(),
            state="ready",
            torrent_hash="super-7",
        )
    )
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash="super-7",
            name=video7.name,
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(video7.resolve()),
            media_id=196187,
            episode=7,
            media_episode=7,
            release_episode=7,
            completed_on=20,
        )
    )
    # Episode 6 was watched/deleted earlier; the legacy row only remembers the
    # series directory. Episode 7 is now the sole video left in that directory.
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash="super-old-6",
            name="[SubsPlease] Super no Ura de Yani Suu Futari - 06 (1080p).mkv",
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(folder),
            media_id=196187,
            episode=6,
            media_episode=6,
            release_episode=6,
            completed_on=10,
        )
    )

    assert manager.reconcile_completed_download_rows(196187) == 0
    row = manager.db.episode_by_path(video7.resolve())
    assert row is not None
    assert row.media_episode == 7
    assert row.release_episode == 7
    assert row.torrent_hash == "super-7"
    assert manager.db.subtitle_jobs() == []


def test_legacy_single_episode_exact_content_path_cannot_point_to_newer_episode(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Super no Ura de Yani Suu Futari"
    folder.mkdir()
    video7 = folder / "[SubsPlease] Super no Ura de Yani Suu Futari - 07 (1080p).mkv"
    video7.write_bytes(b"seven")
    stale_episode6 = DownloadItem(
        torrent_hash="super-old-6-exact",
        name="[SubsPlease] Super no Ura de Yani Suu Futari - 06 (1080p).mkv",
        state="complete",
        progress=1.0,
        save_path=str(folder),
        content_path=str(video7),
        media_id=196187,
        episode=6,
        media_episode=6,
        release_episode=6,
        is_batch=False,
    )

    assert manager._completed_download_video_files(stale_episode6) == []


def test_paused_zero_aria2_with_local_payload_and_control_sidecar_is_reconnected_immediately(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Ghost in the Shell"
    folder.mkdir()
    video = folder / "THE.GHOST.IN.THE.SHELL.S01E07.EPISODE.07.1080p.mkv"
    video.write_bytes(b"existing-payload")
    Path(str(video) + ".aria2").write_bytes(b"control")
    item = DownloadItem(
        torrent_hash="ghost-paused-zero",
        name=video.name,
        state="paused",
        progress=0.0,
        save_path=str(folder),
        content_path=str(video),
        media_id=177699,
        episode=7,
        media_episode=7,
        release_episode=7,
        is_batch=False,
        added_on=1,
        raw={
            "backend": "aria2",
            "total_size": 0,
            "downloaded": 0,
            "download_speed": 0,
            "num_connections": 0,
        },
    )

    calls: list[str] = []

    class Client:
        def reconnect(self, torrent_hash: str) -> bool:
            calls.append(torrent_hash)
            return True

    assert manager._recover_stalled_aria2_downloads(Client(), [item], now=1000.0) == 1
    assert calls == [item.torrent_hash]
