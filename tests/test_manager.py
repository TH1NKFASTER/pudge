from __future__ import annotations

from pathlib import Path

from anime_mpv.database import Database
from anime_mpv.manager_models import LibraryAnime, LibraryEpisode
from anime_mpv.providers.nyaa import (
    parse_rss,
    release_episode,
    release_episode_range,
    score_release,
)


RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">
<channel>
  <item>
    <title>[Erai-raws] Example Anime - 06 [1080p][Multiple Subtitle]</title>
    <link>https://nyaa.si/download/1.torrent</link>
    <guid>https://nyaa.si/view/1</guid>
    <pubDate>Sun, 02 Aug 2026 12:00:00 +0000</pubDate>
    <nyaa:seeders>81</nyaa:seeders>
    <nyaa:leechers>12</nyaa:leechers>
    <nyaa:downloads>441</nyaa:downloads>
    <nyaa:infoHash>ABCDEF0123456789</nyaa:infoHash>
    <nyaa:categoryId>1_2</nyaa:categoryId>
    <nyaa:size>1.2 GiB</nyaa:size>
    <nyaa:trusted>Yes</nyaa:trusted>
    <nyaa:remake>No</nyaa:remake>
  </item>
</channel>
</rss>"""


def test_nyaa_rss_and_scoring() -> None:
    releases = parse_rss(RSS)
    assert len(releases) == 1
    release = releases[0]
    assert release.seeders == 81
    assert release.info_hash == "abcdef0123456789"
    assert release_episode(release.title) == 6

    anime = LibraryAnime(media_id=1, title="Example Anime", titles=["Example Anime"])
    scored = score_release(
        release,
        anime,
        episode=6,
        batch=False,
        trusted_groups=["Erai-raws"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )
    assert scored.score > 90
    assert "trusted" in scored.reasons
    assert "ep=6" in scored.reasons


def test_database_library_and_cleanup(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    anime = LibraryAnime(media_id=10, title="Test", status="CURRENT", progress=2, episodes=12)
    db.upsert_anime(anime)
    video = tmp_path / "Test - 03.mkv"
    video.write_bytes(b"video")
    episode = LibraryEpisode(
        media_id=10,
        title="Test",
        episode=3,
        video_path=video,
        state="waiting_subtitles",
        torrent_hash="hash",
    )
    db.upsert_episode(episode)
    assert db.has_episode(10, 3)
    assert db.ready_episode(10, 3) is not None

    subtitle = tmp_path / "Test - 03.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nテスト\n", encoding="utf-8")
    db.set_subtitle_ready(video, subtitle)
    ready = db.ready_episode(10, 3)
    assert ready is not None
    assert ready.subtitle_path == subtitle

    db.schedule_cleanup(video, 0)
    assert len(db.due_cleanup()) == 1


def test_qbittorrent_52_adds_with_form_and_json_response(tmp_path: Path) -> None:
    import httpx

    from anime_mpv.manager_models import NyaaRelease
    from anime_mpv.providers.qbittorrent import QBittorrentClient

    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append((request.url.path, body))
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/createCategory"):
            assert b"category=anime-mpv" in body
            return httpx.Response(200, text="")
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/add"):
            assert request.headers.get("content-type", "").startswith(
                "application/x-www-form-urlencoded"
            )
            assert b"urls=magnet%3A%3Fxt%3Durn%3Abtih%3Aabcdef" in body
            assert b"stopped=false" in body
            assert b"contentLayout=Original" in body
            return httpx.Response(
                200,
                json={
                    "success_count": 1,
                    "failure_count": 0,
                    "pending_count": 0,
                    "added_torrent_ids": ["abcdef"],
                },
            )
        if request.url.path.endswith("/setCategory"):
            assert b"category=anime-mpv" in body
            return httpx.Response(200, text="")
        if request.url.path.endswith("/createTags"):
            return httpx.Response(200, text="")
        if request.url.path.endswith("/addTags"):
            assert b"anime%3A+Example%2Cepisode%3A+1" in body
            return httpx.Response(200, text="")
        raise AssertionError(request.url.path)

    client = QBittorrentClient("http://qbt.local", api_key="test")
    client.client.close()
    client.client = httpx.Client(
        base_url="http://qbt.local",
        transport=httpx.MockTransport(handler),
        headers={"Referer": "http://qbt.local"},
    )
    release = NyaaRelease(
        title="[Erai-raws] Example - 01",
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash="abcdef",
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=10,
        leechers=1,
        downloads=100,
        trusted=True,
        remake=False,
    )
    client.add_release(
        release,
        save_path=tmp_path / "Example",
        category="anime-mpv",
        tags=["anime: Example", "episode: 1"],
    )
    assert any(path.endswith("/add") for path, _ in seen)
    client.close()


def test_qbittorrent_409_duplicate_is_idempotent(tmp_path: Path) -> None:
    import httpx

    from anime_mpv.manager_models import NyaaRelease
    from anime_mpv.providers.qbittorrent import QBittorrentClient

    info_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal info_calls
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json={"anime-mpv": {}})
        if request.url.path.endswith("/info"):
            info_calls += 1
            if info_calls == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"hash": "abcdef"}])
        if request.url.path.endswith("/add"):
            return httpx.Response(409, text="Conflict")
        if request.url.path.endswith(("/setCategory", "/createTags", "/addTags")):
            return httpx.Response(200, text="")
        raise AssertionError(request.url.path)

    client = QBittorrentClient("http://qbt.local", api_key="test")
    client.client.close()
    client.client = httpx.Client(
        base_url="http://qbt.local",
        transport=httpx.MockTransport(handler),
    )
    release = NyaaRelease(
        title="Example",
        link="",
        torrent_url="",
        info_hash="abcdef",
        size_text="",
        size_bytes=0,
        seeders=1,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
    )
    client.add_release(
        release,
        save_path=tmp_path,
        category="anime-mpv",
        tags=["anilist-1"],
    )
    assert info_calls >= 2
    client.close()


def test_database_caches_planned_metadata(tmp_path: Path) -> None:
    db = Database(tmp_path / "planned.sqlite3")
    anime = LibraryAnime(
        media_id=42,
        title="Cached Planned",
        status="PLANNING",
        episodes=24,
        media_status="FINISHED",
        end_date="2026-08-01",
        mean_score=87,
    )
    db.upsert_anime(anime)
    cached = db.get_anime(42)
    assert cached is not None
    assert cached.media_status == "FINISHED"
    assert cached.end_date == "2026-08-01"
    assert cached.mean_score == 87
    assert cached.episodes == 24


def test_database_migrates_old_anime_columns(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE anime (
            media_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            titles_json TEXT NOT NULL DEFAULT '[]',
            synonyms_json TEXT NOT NULL DEFAULT '[]',
            cover_url TEXT NOT NULL DEFAULT '',
            site_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            progress INTEGER NOT NULL DEFAULT 0,
            episodes INTEGER,
            format TEXT,
            next_airing_episode INTEGER,
            next_airing_at INTEGER,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    Database(path)
    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(anime)")}
    conn.close()
    assert "media_status" in columns
    assert "end_date" in columns
    assert "mean_score" in columns


def test_qbittorrent_api_key_uses_bearer_header() -> None:
    import httpx

    from anime_mpv.providers.qbittorrent import QBittorrentClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-key"
        assert request.url.path == "/api/v2/app/version"
        return httpx.Response(200, text="v5.2.0")

    client = QBittorrentClient("http://127.0.0.1:8080", api_key="secret-key")
    client.client.close()
    client.client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        headers=client._headers(client.base_url),
    )
    try:
        assert client.version() == "v5.2.0"
    finally:
        client.close()


def test_nyaa_proxy_url_normalization() -> None:
    from anime_mpv.providers.nyaa import NyaaClient

    assert NyaaClient(proxy_url="[::1]:1080").proxy_url == "socks5://[::1]:1080"
    assert NyaaClient(proxy_url="socks://127.0.0.1:1080").proxy_url == "socks5://127.0.0.1:1080"


def test_library_scan_marks_embedded_japanese_subtitles_ready(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.library import scan_library

    db = Database(tmp_path / "embedded.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=77, title="Eureka Evrika", status="CURRENT"))
    root = tmp_path / "library"
    root.mkdir()
    video = root / "Eureka Evrika - 05.mkv"
    video.write_bytes(b"video")

    from anime_mpv.models import EmbeddedSubtitle

    monkeypatch.setattr(
        "anime_mpv.library.find_embedded_japanese_subtitles",
        lambda *args, **kwargs: [
            EmbeddedSubtitle(
                stream_index=2,
                subtitle_id=1,
                codec="ass",
                language="ja",
                score=120.0,
            )
        ],
    )

    items = scan_library(root, db, ffprobe="ffprobe", ffmpeg="ffmpeg")

    assert len(items) == 1
    assert items[0].state == "ready"
    assert items[0].subtitle_path is None
    stored = db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.state == "ready"


def test_nyaa_prefers_correct_recent_1080p_season_over_seeded_480p_wrong_season() -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from anime_mpv.manager_models import NyaaRelease

    anime = LibraryAnime(
        media_id=3,
        title="Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo 3rd Season",
        titles=["Hyakkano 3rd Season"],
        synonyms=["Hyakkano"],
    )
    now = datetime.now(timezone.utc)
    common = dict(
        link="",
        torrent_url="",
        size_text="1 GiB",
        size_bytes=1024**3,
        leechers=1,
        downloads=500,
        trusted=True,
        remake=False,
    )
    wrong = NyaaRelease(
        title="[SubsPlease] Hyakkano - 05 (480p) [78DF34C7]",
        info_hash="wrong",
        seeders=900,
        group="SubsPlease",
        published=format_datetime(now - timedelta(days=1)),
        **common,
    )
    right = NyaaRelease(
        title="[Erai-raws] Hyakkano S03 - 05 [1080p][Multiple Subtitle]",
        info_hash="right",
        seeders=35,
        group="Erai-raws",
        published=format_datetime(now - timedelta(days=2)),
        **common,
    )

    kwargs = dict(
        anime=anime,
        episode=5,
        batch=False,
        trusted_groups=["SubsPlease", "Erai-raws"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )
    wrong_scored = score_release(wrong, **kwargs)
    right_scored = score_release(right, **kwargs)

    assert right_scored.score > wrong_scored.score + 100
    assert "season=3" in right_scored.reasons
    assert "season-not-specified" in wrong_scored.reasons
    assert "very-low-resolution" in wrong_scored.reasons


def test_nyaa_explicit_wrong_season_is_hard_penalized() -> None:
    from anime_mpv.manager_models import NyaaRelease

    anime = LibraryAnime(media_id=3, title="Example 3rd Season")
    release = NyaaRelease(
        title="[Group] Example S01E05 [1080p]",
        link="",
        torrent_url="",
        info_hash="wrong-season",
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=500,
        leechers=1,
        downloads=1000,
        trusted=True,
        remake=False,
        group="Group",
    )
    scored = score_release(
        release,
        anime,
        episode=5,
        batch=False,
        trusted_groups=["Group"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )
    assert "wrong-season=1" in scored.reasons
    assert scored.score < 80


def test_nyaa_batch_scoring_strongly_prefers_full_large_pack() -> None:
    from anime_mpv.manager_models import NyaaRelease

    anime = LibraryAnime(
        media_id=88,
        title="Example 2nd Season",
        episodes=12,
    )
    common = dict(
        link="",
        torrent_url="",
        leechers=0,
        downloads=100,
        trusted=True,
        remake=False,
        group="Erai-raws",
    )
    full = NyaaRelease(
        title="[Erai-raws] Example 2nd Season S02 01-12 Batch [1080p]",
        info_hash="full",
        size_text="12 GiB",
        size_bytes=12 * 1024**3,
        seeders=35,
        is_batch=True,
        **common,
    )
    single = NyaaRelease(
        title="[Erai-raws] Example 2nd Season S02 - 01 [1080p]",
        info_hash="single",
        size_text="1.1 GiB",
        size_bytes=int(1.1 * 1024**3),
        seeders=900,
        is_batch=False,
        **common,
    )
    kwargs = dict(
        anime=anime,
        episode=None,
        batch=True,
        trusted_groups=["Erai-raws"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )

    full_scored = score_release(full, **kwargs)
    single_scored = score_release(single, **kwargs)

    assert release_episode_range(full.title) == (1, 12)
    assert "full-series-range" in full_scored.reasons
    assert "large-series-size" in full_scored.reasons
    assert "single-episode=1" in single_scored.reasons
    assert full_scored.score > single_scored.score + 150


def test_manager_uses_readable_qbittorrent_tags(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import NyaaRelease

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.qbittorrent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(media_id=42, title="Readable, Anime Title"))
    captured = {}

    class FakeClient:
        def add_release(self, release, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    monkeypatch.setattr(manager, "qbt_client", lambda: FakeClient())
    manager.add_release(
        42,
        NyaaRelease(
            title="Example pack",
            link="",
            torrent_url="",
            info_hash="abc",
            size_text="8 GiB",
            size_bytes=8 * 1024**3,
            seeders=10,
            leechers=0,
            downloads=10,
            trusted=True,
            remake=False,
        ),
        episode=None,
        batch=True,
    )

    assert captured["tags"] == ["pudge", "anime: Readable Anime Title", "anilist: 42", "series pack"]


def test_manager_requeues_old_generated_subtitle_once(tmp_path: Path) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)

    video = cfg.library.root_dir / "Anime - 05.mkv"
    subtitle = cfg.paths.cache_dir / "aligned" / "Anime - 05.srt"
    video.parent.mkdir(parents=True)
    subtitle.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=None,
            title="Anime",
            episode=5,
            video_path=video,
            subtitle_path=subtitle,
            state="ready",
        )
    )

    assert manager._requeue_legacy_generated_subtitles() == 1
    refreshed = manager.db.episode_by_path(video)
    assert refreshed is not None
    assert refreshed.subtitle_path is None
    assert refreshed.state == "waiting_subtitles"
    assert len(manager.db.subtitle_jobs()) == 1
    assert manager._requeue_legacy_generated_subtitles() == 0


def test_qbittorrent_reads_new_readable_tags() -> None:
    import httpx

    from anime_mpv.providers.qbittorrent import QBittorrentClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/torrents/info"
        return httpx.Response(
            200,
            json=[
                {
                    "hash": "abc",
                    "name": "Pack",
                    "state": "downloading",
                    "progress": 0.5,
                    "save_path": "/tmp/anime",
                    "content_path": "/tmp/anime/Pack",
                    "tags": "anime: Readable Anime,series pack",
                }
            ],
        )

    client = QBittorrentClient("http://qbt.local", api_key="key")
    client.client.close()
    client.client = httpx.Client(
        base_url="http://qbt.local",
        transport=httpx.MockTransport(handler),
    )
    try:
        item = client.torrents()[0]
    finally:
        client.close()

    assert item.is_batch is True
    assert item.raw["_anime_title_tag"] == "Readable Anime"


def test_completed_series_pack_registers_all_video_files(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import DownloadItem

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(media_id=91, title="Pack Anime", episodes=2))

    pack = cfg.library.root_dir / "Pack Anime" / "Season"
    pack.mkdir(parents=True)
    (pack / "Pack Anime - 01.mkv").write_bytes(b"one")
    (pack / "Pack Anime - 02.mkv").write_bytes(b"two")
    monkeypatch.setattr(
        "anime_mpv.manager.japanese_subtitle_source",
        lambda *args, **kwargs: ("embedded", None),
    )

    count = manager._register_completed_download(
        DownloadItem(
            torrent_hash="pack-hash",
            name="Pack Anime",
            state="uploading",
            progress=1.0,
            save_path=str(pack.parent),
            content_path=str(pack),
            media_id=91,
            is_batch=True,
        )
    )

    assert count == 2
    episodes = manager.db.episodes(91)
    assert {item.episode for item in episodes} == {1, 2}
    assert all(item.state == "ready" for item in episodes)


def test_nyaa_short_title_requires_whole_token_match() -> None:
    from anime_mpv.manager_models import NyaaRelease

    anime = LibraryAnime(media_id=1, title="Akira", titles=["Akira"], duration=124)
    wrong = NyaaRelease(
        title="[SubsPlease] Ansatsusha de Aru Ore no Status ga Yuusha yori mo Akiraka ni Tsuyoi no da ga - 01 (1080p)",
        link="",
        torrent_url="",
        info_hash="wrong",
        size_text="1.4 GiB",
        size_bytes=1400 * 1024 * 1024,
        seeders=500,
        leechers=0,
        downloads=5000,
        trusted=True,
        remake=False,
        group="SubsPlease",
    )
    right = NyaaRelease(
        title="[Anime Time] Akira (1988) [1080p]",
        link="",
        torrent_url="",
        info_hash="right",
        size_text="5 GiB",
        size_bytes=5 * 1024**3,
        seeders=20,
        leechers=0,
        downloads=100,
        trusted=True,
        remake=False,
        group="Anime Time",
    )
    kwargs = dict(
        anime=anime,
        episode=None,
        batch=False,
        trusted_groups=["SubsPlease", "Anime Time"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=8000 * 1024 * 1024,
    )

    wrong_scored = score_release(wrong, **kwargs)
    right_scored = score_release(right, **kwargs)

    assert "short-title-token-mismatch" in wrong_scored.reasons
    assert "exact-title-phrase" in right_scored.reasons
    assert right_scored.score > wrong_scored.score + 150


def test_nyaa_penalizes_episode_below_800_mib() -> None:
    from anime_mpv.manager_models import NyaaRelease

    anime = LibraryAnime(media_id=2, title="Example Anime", titles=["Example Anime"], duration=24)

    def release(size_mib: int) -> NyaaRelease:
        return NyaaRelease(
            title="[SubsPlease] Example Anime - 01 (1080p)",
            link="",
            torrent_url="",
            info_hash=str(size_mib),
            size_text=f"{size_mib} MiB",
            size_bytes=size_mib * 1024 * 1024,
            seeders=100,
            leechers=0,
            downloads=1000,
            trusted=True,
            remake=False,
            group="SubsPlease",
        )

    kwargs = dict(
        anime=anime,
        episode=1,
        batch=False,
        trusted_groups=["SubsPlease"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )
    small = score_release(release(600), **kwargs)
    normal = score_release(release(1000), **kwargs)

    assert "low-bitrate-size" in small.reasons
    assert "size-floor-ok" in normal.reasons
    assert normal.score > small.score + 60


def test_nyaa_uses_thirty_mib_per_minute_for_nonstandard_duration() -> None:
    from anime_mpv.manager_models import NyaaRelease

    anime = LibraryAnime(media_id=3, title="Short Anime", titles=["Short Anime"], duration=12)

    def score(size_mib: int):
        release = NyaaRelease(
            title="[SubsPlease] Short Anime - 01 (1080p)",
            link="",
            torrent_url="",
            info_hash=str(size_mib),
            size_text=f"{size_mib} MiB",
            size_bytes=size_mib * 1024 * 1024,
            seeders=20,
            leechers=0,
            downloads=100,
            trusted=True,
            remake=False,
            group="SubsPlease",
        )
        return score_release(
            release,
            anime,
            episode=1,
            batch=False,
            trusted_groups=["SubsPlease"],
            preferred_groups=[],
            blocked_groups=[],
            preferred_resolution="1080p",
            min_seeders=1,
            target_episode_min_bytes=250 * 1024 * 1024,
            target_episode_max_bytes=3500 * 1024 * 1024,
        )

    below = score(250)
    enough = score(360)
    assert "duration=12m" in below.reasons
    assert "low-bitrate-size" in below.reasons
    assert "size-floor-ok" in enough.reasons


def test_auto_search_skips_fully_watched_current_anime(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.qbittorrent.enabled = True

    messages: list[str] = []
    manager = AnimeManager(cfg, log=messages.append)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=501,
            title="Fully Watched",
            status="CURRENT",
            progress=12,
            episodes=12,
            next_airing_episode=None,
        )
    )

    def fail_search(*args, **kwargs):
        raise AssertionError("Nyaa must not be queried for a fully watched title")

    monkeypatch.setattr(manager, "search_releases", fail_search)

    assert manager.auto_search_current() == 0
    assert any("progress 12 >= released 12" in message for message in messages)


def test_auto_search_skips_when_release_boundary_is_unknown(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.qbittorrent.enabled = True

    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=502,
            title="Unknown Boundary",
            status="CURRENT",
            progress=5,
            episodes=None,
            next_airing_episode=None,
        )
    )

    monkeypatch.setattr(
        manager,
        "search_releases",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Nyaa must not be queried without a release boundary")
        ),
    )

    assert manager.auto_search_current() == 0


def _upgrade_release(info_hash: str, score: float):
    from anime_mpv.manager_models import NyaaRelease

    return NyaaRelease(
        title=f"[Group] Upgrade - 05 [1080p] [{info_hash[:8]}]",
        link="",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash=info_hash,
        size_text="1.4 GiB",
        size_bytes=1400 * 1024 * 1024,
        seeders=50,
        leechers=0,
        downloads=100,
        trusted=True,
        remake=False,
        score=score,
        group="Group",
    )


def _upgrade_manager(tmp_path: Path):
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import DownloadItem

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.nyaa.auto_upgrade_downloaded = True
    cfg.nyaa.auto_require_trusted = False
    cfg.nyaa.upgrade_check_hours = 0
    cfg.nyaa.max_upgrade_checks_per_run = 2
    cfg.nyaa.upgrade_min_score_gain = 30
    cfg.qbittorrent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=700,
            title="Upgrade Show",
            status="CURRENT",
            progress=4,
            episodes=12,
            next_airing_episode=6,
        )
    )
    video = tmp_path / "Upgrade Show - 05.mkv"
    video.write_bytes(b"old")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=700,
            title="Upgrade Show",
            episode=5,
            video_path=video,
            state="ready",
            torrent_hash="oldhash",
        )
    )
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash="oldhash",
            name="old",
            state="uploading",
            progress=1.0,
            save_path=str(tmp_path),
            content_path=str(video),
            media_id=700,
            episode=5,
        )
    )
    manager.db.record_release("oldhash", 700, 5, "old", 80.0)
    return manager


def test_downloaded_episode_upgrade_requires_large_score_gain(tmp_path: Path, monkeypatch) -> None:
    manager = _upgrade_manager(tmp_path)
    added: list[str] = []
    monkeypatch.setattr(manager, "search_releases", lambda *a, **k: [_upgrade_release("newhash", 115.0)])
    monkeypatch.setattr(
        manager,
        "add_release",
        lambda _media_id, release, **_kwargs: added.append(release.info_hash),
    )

    assert manager.auto_upgrade_downloaded() == 1
    assert added == ["newhash"]
    assert manager.db.has_pending_upgrade(700, 5)


def test_downloaded_episode_upgrade_skips_small_gain(tmp_path: Path, monkeypatch) -> None:
    manager = _upgrade_manager(tmp_path)
    monkeypatch.setattr(manager, "search_releases", lambda *a, **k: [_upgrade_release("newhash", 100.0)])
    monkeypatch.setattr(
        manager,
        "add_release",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not download")),
    )

    assert manager.auto_upgrade_downloaded() == 0
    assert not manager.db.has_pending_upgrade(700, 5)


def test_upgrade_replaces_old_file_even_while_new_subtitles_are_pending(
    tmp_path: Path, monkeypatch
) -> None:
    from anime_mpv.manager_models import DownloadItem, LibraryEpisode

    manager = _upgrade_manager(tmp_path)
    new_video = tmp_path / "Upgrade Show - 05 new.mkv"
    new_video.write_bytes(b"new")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=700,
            title="Upgrade Show",
            episode=5,
            video_path=new_video,
            state="waiting_subtitles",
            torrent_hash="newhash",
        )
    )
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash="newhash",
            name="new",
            state="uploading",
            progress=1.0,
            save_path=str(tmp_path),
            content_path=str(new_video),
            media_id=700,
            episode=5,
        )
    )
    manager.db.record_release("newhash", 700, 5, "new", 115.0)
    manager.db.record_upgrade(
        new_info_hash="newhash",
        old_torrent_hash="oldhash",
        media_id=700,
        episode=5,
        old_score=80.0,
        new_score=115.0,
    )

    deleted: list[str] = []

    class FakeQbt:
        def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
            assert delete_files is True
            deleted.append(torrent_hash)

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "qbt_client", lambda: FakeQbt())

    assert manager.finalize_ready_upgrades() == 1
    assert deleted == ["oldhash"]
    assert manager.db.episode_for_torrent(700, 5, "oldhash") is None
    assert manager.db.episode_for_torrent(700, 5, "newhash") is not None


def test_reconcile_orphaned_scored_duplicate_versions(
    tmp_path: Path, monkeypatch
) -> None:
    from anime_mpv.manager_models import DownloadItem, LibraryEpisode

    manager = _upgrade_manager(tmp_path)
    new_video = tmp_path / "Upgrade Show - 05 better.mkv"
    new_video.write_bytes(b"new")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=700,
            title="Upgrade Show",
            episode=5,
            video_path=new_video,
            state="waiting_subtitles",
            torrent_hash="newhash",
        )
    )
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash="newhash",
            name="new",
            state="uploading",
            progress=1.0,
            save_path=str(tmp_path),
            content_path=str(new_video),
            media_id=700,
            episode=5,
        )
    )
    manager.db.record_release("newhash", 700, 5, "new", 115.0)

    deleted: list[str] = []

    class FakeQbt:
        def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
            deleted.append(torrent_hash)

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "qbt_client", lambda: FakeQbt())

    assert manager.reconcile_duplicate_versions() == 1
    assert deleted == ["oldhash"]
    assert manager.db.episode_for_torrent(700, 5, "oldhash") is None
    assert manager.db.episode_for_torrent(700, 5, "newhash") is not None


def test_regular_maintenance_searches_missing_before_upgrades(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    order: list[str] = []

    monkeypatch.setattr(manager, "_requeue_legacy_generated_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda: [])
    monkeypatch.setattr(manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: order.append("missing") or 0)
    monkeypatch.setattr(manager, "auto_upgrade_downloaded", lambda: order.append("upgrade") or 0)
    monkeypatch.setattr(manager, "process_subtitle_jobs", lambda *a, **k: order.append("subtitles") or 0)
    monkeypatch.setattr(manager, "finalize_ready_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "cleanup", lambda: 0)

    manager.run_once()
    assert order == ["subtitles", "missing", "upgrade"]



def test_startup_maintenance_runs_subtitle_jobs_even_with_background_agent(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    order: list[str] = []

    monkeypatch.setattr(manager, "_requeue_legacy_generated_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda: order.append("scan") or [])
    monkeypatch.setattr(manager, "sync_downloads", lambda: order.append("downloads") or 0)
    monkeypatch.setattr(manager, "process_subtitle_jobs", lambda *a, **k: order.append("subtitles") or 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: order.append("missing") or 0)
    monkeypatch.setattr(manager, "auto_upgrade_downloaded", lambda: order.append("upgrade") or 0)
    monkeypatch.setattr(manager, "finalize_ready_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "cleanup", lambda: 0)

    manager.run_startup_once()
    # Startup must not block the UI on ALASS/LLM subtitle preparation. Missing
    # releases are discovered immediately; queued subtitle jobs are handled by
    # the foreground worker/agent afterwards.
    assert order == ["downloads", "scan", "missing", "upgrade"]

def test_nyaa_search_uses_ascii_folded_title_variant() -> None:
    from anime_mpv.manager_models import LibraryAnime
    from anime_mpv.providers.nyaa import search_ranked

    class FakeClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str):
            self.queries.append(query)
            return []

    anime = LibraryAnime(
        media_id=901,
        title="Otome Kaijuu Caraméliser",
        titles=["Otome Kaijuu Caraméliser"],
        progress=0,
        episodes=12,
    )
    client = FakeClient()
    search_ranked(
        client,
        anime,
        episode=1,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=800 * 1024 * 1024,
        target_episode_max_bytes=4 * 1024 * 1024 * 1024,
    )
    assert any("Carameliser" in query for query in client.queries)


def test_subtitle_jobs_are_claimed_once(tmp_path: Path) -> None:
    from anime_mpv.database import Database

    db = Database(tmp_path / "claim.sqlite3")
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    db.queue_subtitle_job(video, 1, 5)

    first = db.claim_due_subtitle_jobs(10)
    second = db.claim_due_subtitle_jobs(10)

    assert len(first) == 1
    assert second == []


def test_process_subtitle_job_passes_cached_anilist_identity(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import LibraryAnime, LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    video = tmp_path / "Otome Kaijuu Carameliser - 05.mkv"
    prepared = tmp_path / "prepared.srt"
    video.write_bytes(b"video")
    prepared.write_text("subtitle", encoding="utf-8")
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=204466,
            title="Otome Kaijuu Caraméliser",
            titles=["Otome Kaijuu Caraméliser"],
            synonyms=["Otome Kaijuu Carameliser"],
            episodes=12,
            format="TV",
        )
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=204466,
            title="Otome Kaijuu Caraméliser",
            episode=5,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )
    manager.db.queue_subtitle_job(video.resolve(), 204466, 5)
    calls: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            calls.append(command)
            self.command = command
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return (
                f"PREPARED_SUBTITLE={prepared}\nPREPARE_STATUS=ready\n",
                "",
            )

    monkeypatch.setattr("anime_mpv.manager.subprocess.Popen", FakeProcess)

    assert manager.process_subtitle_jobs(limit=1) == 1
    command = calls[0]
    assert command[command.index("--media-id") + 1] == "204466"
    assert "--skip-airing-lookup" in command
    assert command[command.index("--episode-hint") + 1] == "5"
    stored = manager.db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.subtitle_path == prepared
    assert stored.state == "ready"


def test_global_local_subtitle_with_wrong_title_is_not_a_candidate(tmp_path: Path) -> None:
    from anime_mpv.local_search import find_local_subtitles
    from anime_mpv.models import VideoIdentity

    video_dir = tmp_path / "video"
    other_dir = tmp_path / "other"
    video_dir.mkdir()
    other_dir.mkdir()
    video = video_dir / "Otome Kaijuu Carameliser - 05.mkv"
    wrong = other_dir / "Reincarnated.as.a.Sword.S01E05.ja.srt"
    video.write_bytes(b"video")
    wrong.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nこれは日本語です。\n",
        encoding="utf-8",
    )

    candidates = find_local_subtitles(
        video,
        VideoIdentity(title="Otome Kaijuu Carameliser", episode=5),
        [other_dir],
        tmp_path / "cache",
        max_files=100,
    )

    wrong_candidates = [item for item in candidates if item.path == wrong.resolve()]
    assert not wrong_candidates or wrong_candidates[0].score < 68.0


def test_manager_generation_four_from_two_requeues_only_direct_alass_outputs(tmp_path: Path) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.set_state("subtitle_validation_generation", "2")

    for folder, episode in (("alass", 5), ("synced", 6)):
        video = cfg.library.root_dir / f"Anime - {episode:02d}.mkv"
        subtitle = cfg.paths.cache_dir / folder / f"Anime - {episode:02d}.srt"
        video.parent.mkdir(parents=True, exist_ok=True)
        subtitle.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=1,
                title="Anime",
                episode=episode,
                video_path=video,
                subtitle_path=subtitle,
                state="ready",
            )
        )

    assert manager._requeue_legacy_generated_subtitles() == 1
    alass_item = manager.db.episode_by_path(cfg.library.root_dir / "Anime - 05.mkv")
    synced_item = manager.db.episode_by_path(cfg.library.root_dir / "Anime - 06.mkv")
    assert alass_item is not None and alass_item.subtitle_path is None
    assert synced_item is not None and synced_item.subtitle_path is not None



def test_failed_subtitle_validation_clears_stale_prepared_path(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)

    video = tmp_path / "Mushoku Tensei III - 06.mkv"
    stale = tmp_path / "cached-01-02.srt"
    video.write_bytes(b"video")
    stale.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n間違った字幕\n",
        encoding="utf-8",
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=178789,
            title="Mushoku Tensei III",
            episode=6,
            video_path=video.resolve(),
            subtitle_path=stale.resolve(),
            state="ready",
        )
    )
    manager.db.queue_subtitle_job(video.resolve(), 178789, 6)

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            self.command = command
            self.returncode = 4

        def poll(self):
            return self.returncode

        def communicate(self):
            return (
                "Субтитры отклонены проверкой качества\n"
                "PREPARE_STATUS=waiting_subtitles\n",
                "",
            )

    monkeypatch.setattr("anime_mpv.manager.subprocess.Popen", FakeProcess)

    assert manager.process_subtitle_jobs(limit=1) == 0
    stored = manager.db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.subtitle_path is None
    assert stored.embedded_subtitle_id is None
    assert stored.state == "waiting_subtitles"


def test_repair_stale_subtitle_selection_clears_path_with_active_job(tmp_path: Path) -> None:
    from anime_mpv.database import Database
    from anime_mpv.manager_models import LibraryEpisode

    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Anime - 06.mkv"
    subtitle = tmp_path / "stale.srt"
    video.write_bytes(b"video")
    subtitle.write_text("subtitle", encoding="utf-8")
    db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Anime",
            episode=6,
            video_path=video.resolve(),
            subtitle_path=subtitle.resolve(),
            state="ready",
        )
    )
    db.queue_subtitle_job(video.resolve(), 1, 6)

    assert db.repair_stale_subtitle_selections() == 1
    stored = db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.subtitle_path is None
    assert stored.state == "waiting_subtitles"


def test_library_refresh_preserves_valid_prepared_subtitle_state(tmp_path: Path) -> None:
    from anime_mpv.database import Database
    from anime_mpv.manager_models import LibraryEpisode

    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Anime - 06.mkv"
    subtitle = tmp_path / "prepared.srt"
    video.write_bytes(b"video")
    subtitle.write_text("subtitle", encoding="utf-8")

    db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Anime",
            episode=6,
            video_path=video.resolve(),
            subtitle_path=subtitle.resolve(),
            state="ready",
        )
    )
    db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Anime",
            episode=6,
            video_path=video.resolve(),
            subtitle_path=None,
            state="local",
        )
    )

    stored = db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.subtitle_path == subtitle.resolve()
    assert stored.state == "ready"


def test_manager_generation_five_requeues_old_piecewise_outputs(tmp_path: Path) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.set_state("subtitle_validation_generation", "3")

    for folder, episode in (("alass", 5), ("reference-piecewise", 6), ("synced", 7)):
        video = cfg.library.root_dir / f"Anime - {episode:02d}.mkv"
        subtitle = cfg.paths.cache_dir / folder / f"Anime - {episode:02d}.srt"
        video.parent.mkdir(parents=True, exist_ok=True)
        subtitle.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=1,
                title="Anime",
                episode=episode,
                video_path=video,
                subtitle_path=subtitle,
                state="ready",
            )
        )

    assert manager._requeue_legacy_generated_subtitles() == 2
    assert manager.db.episode_by_path(cfg.library.root_dir / "Anime - 05.mkv").subtitle_path is None
    assert manager.db.episode_by_path(cfg.library.root_dir / "Anime - 06.mkv").subtitle_path is None
    assert manager.db.episode_by_path(cfg.library.root_dir / "Anime - 07.mkv").subtitle_path is not None
    assert manager.db.get_state("subtitle_validation_generation", "") == "14"


def test_sync_anilist_undoes_local_watched_marker(monkeypatch, tmp_path: Path) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"

    manager = AnimeManager(cfg)
    manager.db.upsert_anime(
        LibraryAnime(media_id=77, title="Example", status="CURRENT", progress=5, episodes=12)
    )
    video = (tmp_path / "Example - 05.mkv").resolve()
    video.touch()
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=77,
            title="Example",
            episode=5,
            video_path=video,
            state="ready",
            torrent_hash="hash",
        )
    )
    manager.db.schedule_cleanup(video, 2.0)

    class FakeClient:
        def __init__(self, endpoint, access_token=""):
            pass

        def library(self):
            return [
                LibraryAnime(
                    media_id=77,
                    title="Example",
                    status="CURRENT",
                    progress=4,
                    episodes=12,
                )
            ]

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.manager.AniListClient", FakeClient)

    manager.sync_anilist()

    restored = manager.db.episode_by_path(video)
    assert restored is not None
    assert restored.state == "ready"
    assert restored.watched_at is None
    assert restored.delete_after is None
    assert manager.db.get_anime(77).progress == 4


def test_cleanup_verifies_anilist_before_deleting(monkeypatch, tmp_path: Path) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    cfg.qbittorrent.enabled = False
    cfg.agent.delete_only_managed_files = False

    manager = AnimeManager(cfg)
    manager.db.upsert_anime(
        LibraryAnime(media_id=88, title="Example", status="CURRENT", progress=5, episodes=12)
    )
    video = (tmp_path / "Example - 05.mkv").resolve()
    video.touch()
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=88,
            title="Example",
            episode=5,
            video_path=video,
            state="ready",
        )
    )
    manager.db.schedule_cleanup(video, 0.0)

    def rollback():
        manager.db.reconcile_anilist_progress(88, 4)
        return [LibraryAnime(media_id=88, title="Example", status="CURRENT", progress=4)]

    monkeypatch.setattr(manager, "sync_anilist", rollback)

    assert manager.cleanup() == 0
    assert video.exists()
    restored = manager.db.episode_by_path(video)
    assert restored is not None and restored.state == "ready"


def test_background_prepare_marker_is_set_on_manager_child_source() -> None:
    # Regression guard: manager-spawned --prepare-only workers must identify
    # themselves so a manual --prepare-only can claim foreground priority.
    from inspect import getsource
    from anime_mpv.manager import AnimeManager

    source = getsource(AnimeManager.process_subtitle_jobs)
    assert 'env["ANIME_MPV_BACKGROUND_PREPARE"] = "1"' in source


def test_same_directory_named_subtitle_for_another_show_is_not_a_candidate(tmp_path: Path) -> None:
    from anime_mpv.local_search import find_local_subtitles
    from anime_mpv.models import VideoIdentity

    video = tmp_path / "Seihantai na Kimi to Boku 2nd Season - 05.mkv"
    wrong = tmp_path / "Reincarnated.as.a.Sword.S01E05.WEBRip.Netflix.ja[cc].srt"
    video.write_bytes(b"video")
    wrong.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nこれは日本語です。\n",
        encoding="utf-8",
    )

    candidates = find_local_subtitles(
        video,
        VideoIdentity(title="Seihantai na Kimi to Boku 2nd Season", episode=5),
        [tmp_path],
        tmp_path / "cache",
        max_files=100,
    )

    wrong_candidates = [item for item in candidates if item.path == wrong.resolve()]
    assert not wrong_candidates or wrong_candidates[0].score < 68.0


def test_manager_generation_seven_requeues_generated_playback_outputs(tmp_path: Path) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.set_state("subtitle_validation_generation", "6")

    for folder, episode in (("playback-srt", 5), ("reference-piecewise", 6), ("alass", 7)):
        video = cfg.library.root_dir / f"Anime - {episode:02d}.mkv"
        subtitle = cfg.paths.cache_dir / folder / f"Anime - {episode:02d}.srt"
        video.parent.mkdir(parents=True, exist_ok=True)
        subtitle.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=1,
                title="Anime",
                episode=episode,
                video_path=video,
                subtitle_path=subtitle,
                state="ready",
            )
        )

    assert manager._requeue_legacy_generated_subtitles() == 2
    assert manager.db.episode_by_path(cfg.library.root_dir / "Anime - 05.mkv").subtitle_path is None
    assert manager.db.episode_by_path(cfg.library.root_dir / "Anime - 06.mkv").subtitle_path is None
    assert manager.db.episode_by_path(cfg.library.root_dir / "Anime - 07.mkv").subtitle_path is not None
    assert manager.db.get_state("subtitle_validation_generation", "") == "14"


def test_ready_notification_uses_episode_then_full_anime(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import LibraryAnime, LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.ui.language = "en"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=77, title="Example", status="CURRENT", episodes=2, format="TV")
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "anime_mpv.manager.send_native_notification",
        lambda subtitle, message: calls.append((subtitle, message)) or True,
    )

    first = tmp_path / "Example - 01.mkv"
    first.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=77, title="Example", episode=1, video_path=first, state="ready")
    )
    manager._notify_ready_episode(video=first, media_id=77, episode=1)
    assert calls == [("Episode ready", "Example — episode 1 is ready with subtitles.")]

    second = tmp_path / "Example - 02.mkv"
    second.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=77, title="Example", episode=2, video_path=second, state="ready")
    )
    manager._notify_ready_episode(video=second, media_id=77, episode=2)
    assert calls[-1] == ("Anime ready", "Example: all episodes are ready with subtitles.")

    manager._notify_ready_episode(video=second, media_id=77, episode=2)
    assert len(calls) == 2


def test_ready_notification_can_be_disabled_without_later_duplicate(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import LibraryAnime, LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.ui.notifications_enabled = False
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=88, title="Silent", status="CURRENT", episodes=12, format="TV")
    )
    video = tmp_path / "Silent - 01.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=88, title="Silent", episode=1, video_path=video, state="ready")
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "anime_mpv.manager.send_native_notification",
        lambda subtitle, message: calls.append((subtitle, message)) or True,
    )

    manager._notify_ready_episode(video=video, media_id=88, episode=1)
    manager.config.ui.notifications_enabled = True
    manager._notify_ready_episode(video=video, media_id=88, episode=1)

    assert calls == []



def test_startup_maintenance_runs_one_subtitle_job_when_agent_disabled(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager

    cfg = AppConfig()
    cfg.agent.enabled = False
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    calls: list[int] = []

    monkeypatch.setattr(manager, "_requeue_legacy_generated_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda: [])
    monkeypatch.setattr(manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(manager, "process_subtitle_jobs", lambda limit=4: calls.append(limit) or 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: 0)
    monkeypatch.setattr(manager, "auto_upgrade_downloaded", lambda: 0)
    monkeypatch.setattr(manager, "finalize_ready_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "cleanup", lambda: 0)

    manager.run_startup_once()
    assert calls == []


def test_library_scan_reuses_known_no_subtitle_result(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.database import Database
    from anime_mpv.library import scan_library
    from anime_mpv.manager_models import LibraryEpisode

    db = Database(tmp_path / "known-none.sqlite3")
    root = tmp_path / "library"
    root.mkdir()
    video = root / "Known None - 01.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(
        LibraryEpisode(
            media_id=None,
            title="Known None",
            episode=1,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )

    monkeypatch.setattr(
        "anime_mpv.library.japanese_subtitle_details",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("known result must not be probed again")),
    )

    items = scan_library(root, db, ffprobe="ffprobe", ffmpeg="ffmpeg")
    assert len(items) == 1
    assert items[0].state == "local"
