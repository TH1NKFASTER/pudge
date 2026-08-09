from __future__ import annotations

import hashlib
import json
from pathlib import Path

from anime_mpv.cli import _find_online_subtitles
from anime_mpv.config import AppConfig, write_config
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryAnime, LibraryEpisode
from anime_mpv.models import AniListAnime, JimakuEntry, JimakuFile, SubtitleCandidate, VideoIdentity
from anime_mpv.providers.jimaku import JimakuClient
from anime_mpv.web_app import WebAppApi


def config_for(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    return cfg


def test_deleted_torrent_and_video_no_longer_block_missing_episode_search(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = config_for(tmp_path)
    cfg.qbittorrent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=77,
            title="Kimi ga Shinu made Koi wo Shitai",
            status="CURRENT",
            progress=4,
            next_airing_episode=6,
            episodes=12,
        )
    )
    missing_video = tmp_path / "downloads" / "Kimi ga Shinu made Koi wo Shitai - 01.mkv"
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=77,
            title="Kimi ga Shinu made Koi wo Shitai",
            episode=5,
            video_path=missing_video,
            state="waiting_subtitles",
            torrent_hash="oldhash",
        )
    )
    manager.db.queue_subtitle_job(missing_video, 77, 5)

    class FakeQbt:
        def torrents(self, *, category: str = ""):
            return []

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "qbt_client", lambda: FakeQbt())

    manager.sync_downloads()

    assert manager._last_missing_episode_rows == 1
    assert manager.db.has_episode(77, 5) is False
    assert manager.db.episode_by_path(missing_video) is None
    assert manager.db.subtitle_jobs() == []


def test_foreground_poll_restarts_auto_search_only_after_missing_path_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = config_for(tmp_path)
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    calls: list[str] = []

    def sync_downloads() -> int:
        calls.append("sync")
        api.manager._last_missing_episode_rows = 1 if calls.count("sync") == 1 else 0
        return 0

    monkeypatch.setattr(api.manager, "sync_downloads", sync_downloads)
    monkeypatch.setattr(
        api.manager,
        "auto_search_current",
        lambda: calls.append("auto") or 1,
    )

    result = api.poll_downloads_and_subtitles()

    assert calls == ["sync", "auto", "sync"]
    assert result["stats"]["auto"] == 1
    assert result["stats"]["downloads_after_auto"] == 0


def test_cloudy_beach_exact_title_overrides_conflicting_parent_anilist_entry(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = config_for(tmp_path)
    cfg.jimaku.api_key = "token"
    video = (
        tmp_path
        / "[Erai-raws] Shibou Yuugi de Meshi wo Kuu - 44-Cloudy Beach "
        "[1080p NF WEB-DL AVC AAC][JPN][E88A3655].mkv"
    )
    video.write_bytes(b"video")
    subtitle = tmp_path / "cloudy-beach.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nこれはクラウディビーチです。\n",
        encoding="utf-8",
    )
    requested_entries: list[int] = []
    queries: list[str] = []

    parent = JimakuEntry(
        id=12000,
        name="Shibou Yuugi de Meshi wo Kuu.",
        english_name=None,
        japanese_name=None,
        anilist_id=180746,
        flags={},
    )
    movie = JimakuEntry(
        id=12261,
        name="Shibou Yuugi de Meshi wo Kuu. 44:CLOUDY BEACH",
        english_name=None,
        japanese_name="死亡遊戯で飯を食う。44:CLOUDY BEACH",
        anilist_id=209961,
        flags={"movie": True},
    )
    movie_file = JimakuFile(
        url="https://example.test/cloudy.srt",
        name="死亡遊戯で飯を食う。.44：CLOUDY.BEACH.WEBRip.Netflix.ja[cc].srt",
        size=subtitle.stat().st_size,
        last_modified="",
        score=90.0,
        details={"episode_match": "unknown"},
    )

    class FakeJimaku:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_entries(self, *, anilist_id=None, query=None):
            if anilist_id is not None:
                assert anilist_id == 180746
                return [parent]
            queries.append(str(query))
            return [movie] if "cloudy" in str(query).casefold() else []

        def rank_entries(self, entries, _identity, _anilist_id):
            return list(entries)

        def files_for_episode(self, entry_id, episode, alternative_episodes=()):
            requested_entries.append(entry_id)
            assert episode is None
            return [movie_file] if entry_id == 12261 else []

        def rank_files(self, files, *_args, **_kwargs):
            return files

        def close(self) -> None:
            pass

    def fake_materialize(_client, item, _identity, *_args, **_kwargs):
        return [
            SubtitleCandidate(
                subtitle,
                "jimaku",
                item.score,
                item.name,
                verified_japanese=True,
            )
        ]

    monkeypatch.setattr("anime_mpv.cli.JimakuClient", FakeJimaku)
    monkeypatch.setattr("anime_mpv.cli.materialize_jimaku_files", fake_materialize)

    stale_parent_hint = AniListAnime(
        id=180746,
        titles=["Shibou Yuugi de Meshi wo Kuu."],
        synonyms=[],
        season_year=2026,
        episodes=11,
        format="TV",
    )
    result = _find_online_subtitles(
        video,
        VideoIdentity(
            title="Shibou Yuugi de Meshi wo Kuu - 44-Cloudy Beach",
            episode=None,
        ),
        cfg,
        None,
        False,
        anime_hint=stale_parent_hint,
        skip_airing_lookup=True,
    )

    assert any("cloudy" in query.casefold() for query in queries)
    assert requested_entries == [12261]
    assert len(result) == 1
    assert result[0].details["entry_id"] == 12261
    assert result[0].details["entry_identity_exact_title_match"] is True
    assert result[0].details["entry_anilist_match"] is True


def test_empty_jimaku_search_cache_is_revalidated(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    client = JimakuClient(
        "https://jimaku.cc",
        "token",
        cache_dir=cache_dir,
        cache_ttl_seconds=120,
    )
    params = {"anime": "true", "anilist_id": 209961}
    raw = json.dumps(
        {"base_url": "https://jimaku.cc", "path": "/api/entries/search", "params": params},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    cache_path = cache_dir / "jimaku-api" / f"{digest}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("[]", encoding="utf-8")
    calls: list[dict[str, object]] = []

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return [
                {
                    "id": 12261,
                    "name": "Shibou Yuugi de Meshi wo Kuu. 44:CLOUDY BEACH",
                    "anilist_id": 209961,
                    "flags": {"movie": True},
                }
            ]

    class FakeHttp:
        def get(self, _url, *, params=None):
            calls.append(dict(params or {}))
            return Response()

        def close(self) -> None:
            pass

    client.client.close()
    client.client = FakeHttp()
    try:
        result = client.search_entries(anilist_id=209961)
    finally:
        client.close()

    assert calls == [params]
    assert [entry.id for entry in result] == [12261]
    assert json.loads(cache_path.read_text(encoding="utf-8"))[0]["id"] == 12261
