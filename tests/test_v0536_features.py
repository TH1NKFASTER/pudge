from __future__ import annotations

from pathlib import Path

from anime_mpv.config import AppConfig, write_config
from anime_mpv.database import Database
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryEpisode, NyaaRelease
from anime_mpv.models import AniListAnime, JimakuFile, VideoIdentity
from anime_mpv.providers.anilist import AniListClient
from anime_mpv.providers.jimaku import JimakuClient, materialize_jimaku_files
from anime_mpv.providers.nyaa import score_release
from anime_mpv.manager_models import LibraryAnime


def _anime(media_id: int, title: str, episodes: int) -> AniListAnime:
    return AniListAnime(
        id=media_id,
        titles=[title],
        synonyms=[],
        season_year=2026,
        episodes=episodes,
        format="TV",
    )


def test_relative_episode_gets_absolute_alias_from_prequel_chain() -> None:
    season_one = _anime(1, "Seihantai na Kimi to Boku", 12)
    season_two = _anime(2, "Seihantai na Kimi to Boku 2nd Season", 12)

    client = object.__new__(AniListClient)

    def relations(media_id: int):
        if media_id == 2:
            return season_two, [("PREQUEL", season_one)]
        return season_one, []

    client.get_anime_with_relations = relations  # type: ignore[method-assign]
    absolute, chain = AniListClient.absolute_episode_number(client, season_two, 5)

    assert absolute == 17
    assert [item.id for item in chain] == [1, 2]


def test_jimaku_accepts_absolute_episode_alias_and_normalizes_candidate(tmp_path: Path) -> None:
    subtitle = tmp_path / "[NanakoRaws] Seihantai na Kimi to Boku S02E17.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nこれは日本語の字幕です。" * 8,
        encoding="utf-8",
    )
    item = JimakuFile(
        url="https://example.test/s02e17.srt",
        name=subtitle.name,
        size=subtitle.stat().st_size,
        last_modified="",
    )

    class FilesClient:
        def __init__(self):
            self.calls: list[int | None] = []

        def files(self, _entry_id: int, episode: int | None):
            self.calls.append(episode)
            return [item] if episode == 17 else []

    files_client = FilesClient()
    found = JimakuClient.files_for_episode(
        files_client,  # type: ignore[arg-type]
        42,
        5,
        alternative_episodes=(17,),
    )
    assert found == [item]
    assert files_client.calls[:2] == [5, 17]

    rank_client = object.__new__(JimakuClient)
    ranked = JimakuClient.rank_files(
        rank_client,
        found,
        VideoIdentity(title="Seihantai na Kimi to Boku 2nd Season", episode=5),
        tmp_path / "[Erai-raws] Seihantai na Kimi to Boku 2nd Season - 05.mkv",
        alternative_episodes=(17,),
    )
    assert ranked[0].details["episode_match"] == "absolute"

    class DownloadClient:
        def download(self, _item, _cache_dir):
            return subtitle

    candidates = materialize_jimaku_files(
        DownloadClient(),  # type: ignore[arg-type]
        ranked[0],
        VideoIdentity(title="Seihantai na Kimi to Boku 2nd Season", episode=5),
        tmp_path / "video.mkv",
        tmp_path / "cache",
        allowed_episodes=(17,),
    )
    assert candidates[0].episode == 5
    assert candidates[0].details["source_episode_number"] == 17


def test_continue_watching_records_position_and_rewinds_in_web_payload(tmp_path: Path) -> None:
    from anime_mpv.web_app import WebAppApi

    cfg = AppConfig()
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.playback.rewind_seconds = 15
    video = tmp_path / "Anime - 03.mkv"
    video.write_bytes(b"video")

    cfg.config_path = tmp_path / "config.toml"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    api.manager.db.upsert_anime(LibraryAnime(media_id=7, title="Anime", status="CURRENT"))
    api.manager.db.upsert_episode(
        LibraryEpisode(7, "Anime", 3, video, state="ready")
    )
    api.manager.db.record_playback(video, 125, 1400)

    item = api.get_state()["home"]["continue_watching"][0]
    assert item["episode"] == 3
    assert item["position"] == 125
    assert item["resume_start"] == 110


def test_disk_limit_defaults_to_500_gb_and_reports_usage(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.disk_limit_enabled = True
    cfg.library.disk_limit_gb = 500
    (tmp_path / "episode.mkv").write_bytes(b"12345")

    manager = AnimeManager(cfg)
    status = manager.storage_status()
    assert status["limit_gb"] == 500
    assert status["used_bytes"] == 5
    assert status["over_limit"] is False


def test_video_quality_policy_blocks_explicit_bad_releases() -> None:
    anime = LibraryAnime(media_id=1, title="Anime", titles=["Anime"])
    release = NyaaRelease(
        title="[Group] Anime - 01 [1080p AI Upscaled English Dub]",
        link="",
        torrent_url="https://example.test/test.torrent",
        size_text="700 MiB",
        size_bytes=700 * 1024**2,
        seeders=20,
        leechers=0,
        downloads=0,
        info_hash="test",
        trusted=True,
        remake=False,
    )
    scored = score_release(
        release,
        anime,
        episode=1,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        preferred_video_codecs=["HEVC", "AVC"],
        preferred_sources=["BluRay", "WEB-DL"],
        require_japanese_audio=True,
        avoid_upscaled=True,
    )
    assert "blocked-upscale" in scored.reasons
    assert "english-dub-only" in scored.reasons


def test_continue_watching_marks_movie_for_web_label(tmp_path: Path) -> None:
    from anime_mpv.web_app import WebAppApi

    cfg = AppConfig()
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.config_path = tmp_path / "config.toml"
    write_config(cfg, cfg.config_path)

    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    api = WebAppApi(cfg.config_path)
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=8, title="Movie", status="CURRENT", format="MOVIE", episodes=1)
    )
    api.manager.db.upsert_episode(
        LibraryEpisode(8, "Movie", None, video, state="ready")
    )
    api.manager.db.record_playback(video, 600, 7200)

    item = api.get_state()["home"]["continue_watching"][0]
    assert item["episode"] is None
    assert item["is_movie"] is True


def test_continue_watching_movie_uses_movie_label() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text()
    assert "'label.continueMovie':'Movie • resume at {time}'" in html
    assert "a.is_movie?t('label.continueMovie'" in html
