from __future__ import annotations

import logging
from pathlib import Path

from pudge.cli import _authoritative_online_identity, _jimaku_episode_aliases
from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.models import AniListAnime, JimakuFile, VideoIdentity
from pudge.providers.jimaku import JimakuClient

ROOT = Path(__file__).parents[1]


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    return AnimeManager(cfg, log=lambda _message: None)


def test_seihantai_completed_download_repairs_wrong_episode_row_and_job(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            episodes=13,
            status="CURRENT",
        )
    )
    folder = manager.config.library.root_dir / "Seihantai na Kimi to Boku 2nd Season"
    folder.mkdir()
    video = folder / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    video.write_bytes(b"video")
    torrent_hash = "f3a099e5c9a9519b1b406a23202f94e1e9d07157"

    # Simulate the stale absolute-number row that used to make reconciliation
    # stop merely because *some* row already existed for the path.
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            episode=19,
            media_episode=19,
            release_episode=19,
            video_path=video.resolve(),
            state="waiting_subtitles",
            torrent_hash=torrent_hash,
        )
    )
    manager.db.delete_subtitle_job(video.resolve())
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash=torrent_hash,
            name=video.name,
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(video.resolve()),
            media_id=210031,
            episode=7,
            media_episode=7,
            release_episode=19,
            completed_on=10,
        )
    )
    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None)
    )
    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    repaired = manager.reconcile_completed_download_rows(210031, 7)
    row = manager.db.episode_by_path(video.resolve())
    jobs = manager.db.subtitle_jobs()

    assert repaired == 1
    assert row is not None
    assert row.media_id == 210031
    assert row.episode == 7
    assert row.media_episode == 7
    assert row.release_episode == 19
    assert row.torrent_hash == torrent_hash
    assert len(jobs) == 1
    assert jobs[0]["episode"] == 7
    assert jobs[0]["media_id"] == 210031


def test_matching_completed_row_recreates_missing_subtitle_job(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(media_id=210031, title="Seihantai na Kimi to Boku 2nd Season", episodes=13)
    )
    folder = manager.config.library.root_dir / "Seihantai na Kimi to Boku 2nd Season"
    folder.mkdir()
    video = folder / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p).mkv"
    video.write_bytes(b"video")
    torrent_hash = "abc123"
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            episode=7,
            media_episode=7,
            release_episode=19,
            video_path=video.resolve(),
            state="waiting_subtitles",
            torrent_hash=torrent_hash,
        )
    )
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash=torrent_hash,
            name=video.name,
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(video.resolve()),
            media_id=210031,
            media_episode=7,
            release_episode=19,
        )
    )
    manager.db.delete_subtitle_job(video.resolve())

    assert manager.reconcile_completed_download_rows(210031, 7) == 0
    jobs = manager.db.subtitle_jobs()
    assert len(jobs) == 1
    assert jobs[0]["episode"] == 7


def test_seihantai_online_lookup_uses_media_episode_and_absolute_alias(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    anime = AniListAnime(
        id=210031,
        titles=["Seihantai na Kimi to Boku 2nd Season", "正反対な君と僕 第2期"],
        synonyms=[],
        season_year=2026,
        episodes=13,
        format="TV",
    )
    cache = cfg.paths.cache_dir / "anilist-release-numbering" / "210031.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"offset":12,"updated_at":1787160000}', encoding="utf-8")

    filename_identity = VideoIdentity(
        title="Seihantai na Kimi to Boku",
        episode=19,
        season=2,
    )
    online_identity = _authoritative_online_identity(filename_identity, 7, anime)

    assert filename_identity.episode == 19
    assert online_identity.episode == 7
    aliases = _jimaku_episode_aliases(
        anime, online_identity.episode, cfg, logging.getLogger("test")
    )
    assert aliases == (19,)

    file = JimakuFile(
        url="https://jimaku.cc/entry/12212/download/example.srt",
        name="正反対な君と僕.S02E19.グラデーション.WEBRip.DMMTV.ja[cc].srt",
        size=1000,
        last_modified="",
    )
    ranked = JimakuClient.rank_files(
        object.__new__(JimakuClient),
        [file],
        online_identity,
        Path("[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p).mkv"),
        alternative_episodes=aliases,
    )
    assert ranked
    assert ranked[0].details["episode_match"] == "absolute"
    assert ranked[0].details["language_purity"] == "japanese_only"


def test_refresh_button_is_not_owned_by_background_subtitle_checks() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")

    refresh_line = next(line for line in html.splitlines() if line.startswith("async function refreshAll()"))
    poll_line = next(line for line in html.splitlines() if line.startswith("async function pollForegroundWork()"))
    assert "startupMaintenanceRunning||duePrioritySubtitleJobs().length" not in refresh_line
    assert "setLocalRefreshUi(subtitleChecking" not in refresh_line
    assert "setLocalRefreshUi(true,'status.subtitleCheckingBackground')" not in poll_line


def test_ln_drop_card_never_sends_embedded_cover_or_chapter_list(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    service = LightNovelService(cfg)
    source = tmp_path / "ようこそ実力至上主義の教室へ 3年生編 4.txt"
    source.write_text("これは日本語の本文です。" * 100, encoding="utf-8")
    imported = service.import_file(source)
    huge_cover = "data:image/jpeg;base64," + ("A" * 4_000_000)
    with service._connect() as conn:
        conn.execute("UPDATE ln_books SET cover_url=? WHERE id=?", (huge_cover, int(imported["id"])))

    card = service.drop_card(int(imported["id"]))

    assert card["cover_url"] == ""
    assert "chapters" not in card
    assert card["chapter_count"] == 1
    assert len(str(card)) < 20_000


def test_ln_drop_frontend_inserts_into_existing_series_without_rebuilding_siblings() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("function injectLnBook(book){")
    end = html.index("async function showLnAniListSearch", start)
    block = html[start:end]

    assert "if(oldGroup){" in block
    assert "scroller.appendChild(fresh)" in block
    assert "hydrateLnCardJiten(fresh)" in block
    assert 'data-ln-volume="${volume}"' in html
    assert "setTimeout(scheduleLnStatePoll,2500)" in html


def test_scan_library_keeps_managed_download_identity_over_wrong_parent_match(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    season1 = LibraryAnime(media_id=184951, title="Seihantai na Kimi to Boku", episodes=12)
    season2 = LibraryAnime(
        media_id=210031,
        title="Seihantai na Kimi to Boku 2nd Season",
        episodes=13,
        status="CURRENT",
    )
    manager.db.upsert_anime(season1)
    manager.db.upsert_anime(season2)
    folder = manager.config.library.root_dir / season2.title
    folder.mkdir()
    video = folder / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p).mkv"
    video.write_bytes(b"video")
    torrent_hash = "managed-season-two"
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash=torrent_hash,
            name=video.name,
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(video.resolve()),
            media_id=season2.media_id,
            episode=7,
            media_episode=7,
            release_episode=19,
        )
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=season2.media_id,
            title=season2.title,
            episode=7,
            media_episode=7,
            release_episode=19,
            video_path=video.resolve(),
            state="waiting_subtitles",
            torrent_hash=torrent_hash,
        )
    )

    # Reproduce the old failure deterministically: without managed-download
    # precedence this fuzzy parent match relabels the same path as Season 1.
    monkeypatch.setattr("pudge.library.parent_folder_anime", lambda *_a, **_k: season1)

    rows = manager.scan_library()
    row = manager.db.episode_by_path(video.resolve())

    assert rows
    assert row is not None
    assert row.media_id == season2.media_id
    assert row.media_episode == 7
    assert row.release_episode == 19
    assert row.torrent_hash == torrent_hash


def test_ln_inline_epub_covers_never_enter_state_and_migrate_to_cover_cache(
    tmp_path: Path,
) -> None:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True)
    service = LightNovelService(cfg)
    source = tmp_path / "book.txt"
    source.write_text("これは日本語です。" * 30, encoding="utf-8")
    imported = service.import_file(source)
    raw = b"fake-image-bytes" * 50_000
    inline = "data:image/png;base64," + __import__("base64").b64encode(raw).decode("ascii")
    with service._connect() as conn:
        conn.execute("UPDATE ln_books SET cover_url=? WHERE id=?", (inline, int(imported["id"])))

    # The fast state query must not fetch/serialize the legacy multi-megabyte data URL.
    before = next(book for book in service.books() if int(book["id"]) == int(imported["id"]))
    assert before["cover_url"] == ""
    assert len(str(before)) < 20_000

    assert service._migrate_inline_covers() == 1
    with service._connect() as conn:
        stored = str(conn.execute("SELECT cover_url FROM ln_books WHERE id=?", (int(imported["id"]),)).fetchone()[0])
    assert stored.startswith("covers/ln-")
    assert (cfg.library.cover_cache_dir / Path(stored).name).read_bytes() == raw
    after = next(book for book in service.books() if int(book["id"]) == int(imported["id"]))
    assert after["cover_url"] == stored


def test_ln_new_epub_cover_is_file_backed_not_base64(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True)
    service = LightNovelService(cfg)
    source = tmp_path / "volume.epub"
    source.write_bytes(b"placeholder")
    raw = b"png-cover" * 10_000
    monkeypatch.setattr(
        "pudge.light_novels._epub_metadata",
        lambda _path: ("日本語の本 1", [("1", "これは本文です。")], (raw, ".png")),
    )

    book = service.import_file(source)

    assert str(book["cover_url"]).startswith("covers/ln-")
    assert not str(book["cover_url"]).startswith("data:")
    assert (cfg.library.cover_cache_dir / Path(str(book["cover_url"])).name).read_bytes() == raw


def test_ln_rejected_sources_are_not_reparsed_until_file_changes(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True)
    service = LightNovelService(cfg)
    source = service.root / "README" / "README.txt"
    source.parent.mkdir(parents=True)
    source.write_text("plain english package metadata", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        service,
        "is_probably_japanese_source",
        lambda path: calls.append(Path(path)) or False,
    )

    assert service.scan_downloaded() == 0
    assert len(calls) == 1
    assert service.scan_downloaded() == 0
    assert len(calls) == 1

    source.write_text("changed plain english package metadata", encoding="utf-8")
    assert service.scan_downloaded() == 0
    assert len(calls) == 2


def test_completed_single_download_materializes_missing_episode_row(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=159309,
            title="Otome Game Sekai wa Mob ni Kibishii Sekai desu 2",
            episodes=12,
        )
    )
    folder = manager.config.library.root_dir / "Otome Game Sekai wa Mob ni Kibishii Sekai desu 2"
    folder.mkdir()
    video = folder / "[SubsPlease] Otome Game Sekai wa Mob ni Kibishii Sekai desu S2 - 07 (1080p).mkv"
    video.write_bytes(b"video")
    torrent_hash = "8b407280e864acc34a92e2c7f1e2334a7a8b7c7d"
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash=torrent_hash,
            name=video.name,
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(video.resolve()),
            media_id=159309,
            episode=7,
            media_episode=7,
            release_episode=7,
            completed_on=10,
        )
    )
    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None)
    )
    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    monkeypatch.setattr(manager, "_register_completed_download", lambda *_a, **_k: 0)

    repaired = manager.reconcile_completed_download_rows(159309, 7)
    row = manager.db.episode_by_path(video.resolve())

    assert repaired == 1
    assert row is not None
    assert row.media_id == 159309
    assert row.media_episode == 7
    assert row.release_episode == 7
    assert row.torrent_hash == torrent_hash
    jobs = manager.db.subtitle_jobs()
    assert len(jobs) == 1
    assert jobs[0]["episode"] == 7
