"""Quality upgrades really download; watched folders import whole series.

1. An upgrade (or a release the user picked for an owned episode) is added even
   though the old copy is still listed by the torrent client; a job is recorded
   only when something was added, expires after a week, and the old file is
   replaced only once the new copy is Ready with Japanese subtitles.
2. A watched-folder scan looks a series up once (not once per episode), reuses
   known/local matches, does not charge ffprobe time to the AniList budget and
   never forgets rows when the budget runs out.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pudge.manager as manager_module
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease

# --------------------------------------------------------------------------- upgrades


def _release(info_hash: str, score: float) -> NyaaRelease:
    return NyaaRelease(
        title=f"[Group] Upgrade Show - 05 [1080p] [{info_hash[:8]}]",
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


def _old_download(tmp_path: Path) -> DownloadItem:
    return DownloadItem(
        torrent_hash="oldhash",
        name="[Old] Upgrade Show - 05 [720p]",
        state="uploading",
        progress=1.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "Upgrade Show - 05.mkv"),
        media_id=700,
        episode=5,
        raw={},
    )


class FakeTorrentClient:
    """A client that still lists the old, seeding copy of episode 5."""

    def __init__(self, listed: list[DownloadItem]) -> None:
        self.listed = listed
        self.added: list[str] = []
        self.deleted: list[str] = []

    def torrents(self, *, category: str = "") -> list[DownloadItem]:
        return list(self.listed)

    def add_release(self, release, **_kwargs) -> None:
        self.added.append(release.info_hash)
        self.listed.append(
            DownloadItem(
                torrent_hash=release.info_hash,
                name=release.title,
                state="downloading",
                progress=0.0,
                save_path="",
                content_path="",
                media_id=700,
                episode=5,
                raw={},
            )
        )

    def start(self, _torrent_hash: str) -> None:
        pass

    def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        self.deleted.append(torrent_hash)

    def close(self) -> None:
        pass


def _upgrade_manager(tmp_path: Path, monkeypatch, listed: list[DownloadItem] | None = None):
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_upgrade_downloaded = True
    cfg.nyaa.auto_require_trusted = False
    cfg.nyaa.upgrade_check_hours = 0
    cfg.nyaa.max_upgrade_checks_per_run = 2
    cfg.nyaa.upgrade_min_score_gain = 30
    cfg.qbittorrent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=700, title="Upgrade Show", status="CURRENT", progress=4, episodes=12)
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
    manager.db.upsert_download(_old_download(tmp_path))
    manager.db.record_release("oldhash", 700, 5, "old", 80.0)
    client = FakeTorrentClient(listed if listed is not None else [_old_download(tmp_path)])
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    monkeypatch.setattr(manager, "_torrent_admission", lambda: None)
    return manager, client


def test_upgrade_is_added_although_the_old_copy_is_still_seeding(tmp_path: Path, monkeypatch) -> None:
    manager, client = _upgrade_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, "search_releases", lambda *a, **k: [_release("newhash", 115.0)])

    assert manager.auto_upgrade_downloaded() == 1
    assert client.added == ["newhash"]
    assert manager.db.has_pending_upgrade(700, 5)


def test_no_upgrade_job_when_nothing_was_added(tmp_path: Path, monkeypatch) -> None:
    manager, client = _upgrade_manager(tmp_path, monkeypatch)
    # The upgrade torrent itself is already listed: add_release adds nothing.
    client.add_release(_release("newhash", 115.0))
    client.added.clear()
    monkeypatch.setattr(manager, "search_releases", lambda *a, **k: [_release("newhash", 115.0)])

    assert manager.auto_upgrade_downloaded() == 0
    assert client.added == []
    assert not manager.db.has_pending_upgrade(700, 5)


def test_stale_upgrade_job_expires_and_episode_is_checked_again(tmp_path: Path, monkeypatch) -> None:
    manager, _client = _upgrade_manager(tmp_path, monkeypatch)
    manager.db.record_upgrade(
        new_info_hash="deadhash",
        old_torrent_hash="oldhash",
        media_id=700,
        episode=5,
        old_score=80.0,
        new_score=115.0,
    )
    eight_days_ago = time.time() - 8 * 24 * 3600
    with sqlite3.connect(manager.db.path) as conn:
        conn.execute("UPDATE upgrade_jobs SET created_at=?,updated_at=?", (eight_days_ago, eight_days_ago))
    searched: list[int] = []
    monkeypatch.setattr(
        manager, "search_releases", lambda *_a, episode=None, **_k: searched.append(episode) or []
    )

    manager.auto_upgrade_downloaded()

    assert searched == [5]
    jobs = {job["new_info_hash"]: job["state"] for job in manager.db.upgrade_jobs()}
    assert jobs["deadhash"] == "expired"
    assert (tmp_path / "Upgrade Show - 05.mkv").is_file()


def test_fresh_upgrade_job_still_blocks_rechecks(tmp_path: Path, monkeypatch) -> None:
    manager, _client = _upgrade_manager(tmp_path, monkeypatch)
    manager.db.record_upgrade(
        new_info_hash="newhash",
        old_torrent_hash="oldhash",
        media_id=700,
        episode=5,
        old_score=80.0,
        new_score=115.0,
    )
    monkeypatch.setattr(
        manager,
        "search_releases",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("pending upgrade must not be re-checked")),
    )

    assert manager.auto_upgrade_downloaded() == 0
    assert manager.db.has_pending_upgrade(700, 5)


def _new_copy(manager, tmp_path: Path, *, state: str, subtitle: bool) -> Path:
    new_video = tmp_path / "Upgrade Show - 05 new.mkv"
    new_video.write_bytes(b"new")
    subtitle_path = None
    if subtitle:
        subtitle_path = tmp_path / "Upgrade Show - 05 new.ja.srt"
        subtitle_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=700,
            title="Upgrade Show",
            episode=5,
            video_path=new_video,
            subtitle_path=subtitle_path,
            state=state,
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
    return new_video


def test_old_ready_file_is_kept_until_the_upgrade_has_japanese_subtitles(tmp_path: Path, monkeypatch) -> None:
    for state in ("local", "waiting_subtitles"):
        case = tmp_path / state
        case.mkdir()
        manager, client = _upgrade_manager(case, monkeypatch)
        _new_copy(manager, case, state=state, subtitle=False)

        assert manager.finalize_ready_upgrades() == 0
        assert manager.reconcile_duplicate_versions() == 0
        assert client.deleted == []
        assert (case / "Upgrade Show - 05.mkv").is_file()
        assert manager.db.has_pending_upgrade(700, 5)


def test_ready_upgrade_replaces_the_old_file(tmp_path: Path, monkeypatch) -> None:
    manager, client = _upgrade_manager(tmp_path, monkeypatch)
    _new_copy(manager, tmp_path, state="ready", subtitle=True)

    assert manager.finalize_ready_upgrades() == 1
    assert client.deleted == ["oldhash"]
    assert not (tmp_path / "Upgrade Show - 05.mkv").exists()
    assert manager.db.episode_for_torrent(700, 5, "newhash") is not None
    assert not manager.db.has_pending_upgrade(700, 5)


def test_manual_pick_for_an_owned_episode_is_downloaded_and_replaces_it(tmp_path: Path, monkeypatch) -> None:
    manager, client = _upgrade_manager(tmp_path, monkeypatch)

    assert manager.add_release(700, _release("pickedhash", 60.0), episode=5, batch=False, manual=True)
    assert client.added == ["pickedhash"]
    jobs = {job["new_info_hash"]: job for job in manager.db.upgrade_jobs()}
    assert jobs["pickedhash"]["old_torrent_hash"] == "oldhash"
    assert jobs["pickedhash"]["state"] == "downloading"


def test_automatic_download_still_refuses_a_second_copy(tmp_path: Path, monkeypatch) -> None:
    manager, client = _upgrade_manager(tmp_path, monkeypatch)

    assert manager.add_release(700, _release("otherhash", 60.0), episode=5, batch=False) is False
    assert client.added == []


# ------------------------------------------------------------------ watched folders


def _watched_config(tmp_path: Path) -> tuple[AppConfig, Path]:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.root_dir = tmp_path / "Library"
    config.library.root_dir.mkdir(parents=True)
    config.library.database_path = tmp_path / "library.sqlite3"
    watched = tmp_path / "Downloads"
    watched.mkdir()
    config.paths.download_dirs = [watched]
    config.anilist.enabled = True
    config.anilist.access_token = "token"
    return config, watched


def _candidate(media_id: int, title: str, episodes: int = 24) -> SimpleNamespace:
    return SimpleNamespace(
        id=media_id,
        titles=[title],
        synonyms=[],
        score=95.0,
        episodes=episodes,
        format="TV",
        season_year=2020,
    )


def _fake_anilist(monkeypatch, answers: dict[str, SimpleNamespace]) -> list[str]:
    searched: list[str] = []

    class Client:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def search(self, identity):
            searched.append(identity.title)
            found = answers.get(identity.title)
            return [found] if found is not None else []

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager_module, "AniListClient", Client)
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))
    return searched


def test_watched_folder_imports_a_whole_series_with_one_lookup(tmp_path: Path, monkeypatch) -> None:
    config, watched = _watched_config(tmp_path)
    videos = [watched / f"[Group] Long Show - {number:02d} [1080p].mkv" for number in range(1, 21)]
    for video in videos:
        video.write_bytes(b"video")
    searched = _fake_anilist(monkeypatch, {"Long Show": _candidate(900, "Long Show")})

    manager = AnimeManager(config, log=lambda _message: None)
    manager.scan_library()

    assert searched == ["Long Show"]
    assert all(manager.db.episode_by_path(video) is not None for video in videos)
    assert {manager.db.episode_by_path(video).media_id for video in videos} == {900}

    # A later scan (even in a new process) maps the imported series locally.
    AnimeManager(config, log=lambda _message: None).scan_library()
    assert searched == ["Long Show"]
    assert all(manager.db.episode_by_path(video) is not None for video in videos)


def test_series_from_the_users_list_is_matched_without_anilist(tmp_path: Path, monkeypatch) -> None:
    config, watched = _watched_config(tmp_path)
    videos = [watched / f"Delta Show - {number:02d}.mkv" for number in range(1, 4)]
    for video in videos:
        video.write_bytes(b"video")
    searched = _fake_anilist(monkeypatch, {"Delta Show": _candidate(901, "Delta Show")})
    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(media_id=901, title="Delta Show", status="CURRENT", progress=0))

    manager.scan_library()

    assert searched == []
    assert {manager.db.episode_by_path(video).media_id for video in videos} == {901}


def test_similar_or_sequel_list_entries_are_not_guessed(tmp_path: Path, monkeypatch) -> None:
    config, watched = _watched_config(tmp_path)
    golf = watched / "Golf Show - 01.mkv"
    hero = watched / "Boku no Hero Academia - 05.mkv"
    for video in (golf, hero):
        video.write_bytes(b"video")
    searched = _fake_anilist(
        monkeypatch,
        {
            "Golf Show": _candidate(920, "Golf Show"),
            "Boku no Hero Academia": _candidate(921, "Boku no Hero Academia"),
        },
    )
    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(media_id=930, title="Hotel Show", status="CURRENT", progress=0))
    manager.db.upsert_anime(
        LibraryAnime(media_id=931, title="Boku no Hero Academia 2", status="CURRENT", progress=0)
    )

    manager.scan_library()

    assert sorted(searched) == ["Boku no Hero Academia", "Golf Show"]
    assert manager.db.episode_by_path(golf).media_id == 920
    assert manager.db.episode_by_path(hero).media_id == 921


def test_ffprobe_time_does_not_use_up_the_anilist_budget(tmp_path: Path, monkeypatch) -> None:
    config, watched = _watched_config(tmp_path)
    names = ["Alpha Show", "Bravo Show", "Charlie Show"]
    videos = [watched / f"{name} - 01.mkv" for name in names]
    for video in videos:
        video.write_bytes(b"video")
    searched = _fake_anilist(
        monkeypatch, {name: _candidate(910 + index, name) for index, name in enumerate(names)}
    )

    class SlowClock:
        """The manager's clock; probing each file takes six "seconds"."""

        offset = 0.0

        def __getattr__(self, name):
            return getattr(time, name)

        def monotonic(self) -> float:
            return time.monotonic() + self.offset

    clock = SlowClock()
    monkeypatch.setattr(manager_module, "time", clock)

    def slow_probe(*_args, **_kwargs):
        clock.offset += 6.0
        return ("none", None, None)

    monkeypatch.setattr("pudge.library.japanese_subtitle_details", slow_probe)

    manager = AnimeManager(config, log=lambda _message: None)
    manager.scan_library()

    assert sorted(searched) == sorted(names)
    assert all(manager.db.episode_by_path(video) is not None for video in videos)


def test_rows_waiting_for_the_lookup_budget_are_kept_and_matched_next_scan(
    tmp_path: Path, monkeypatch
) -> None:
    config, watched = _watched_config(tmp_path)
    names = [
        "Alpha Show",
        "Bravo Show",
        "Charlie Show",
        "Delta Show",
        "Echo Show",
        "Foxtrot Show",
        "Golf Show",
        "Hotel Show",
        "India Show",
        "Juliet Show",
    ]
    answers = {f"{name} S2": _candidate(2000 + index, f"{name} S2") for index, name in enumerate(names)}
    searched = _fake_anilist(monkeypatch, answers)
    manager = AnimeManager(config, log=lambda _message: None)
    videos = []
    for index, name in enumerate(names):
        # Imported by an older version against the Season 1 entry, already Ready.
        video = watched / f"{name} S2 - 01.mkv"
        video.write_bytes(b"video")
        subtitle = watched / f"{name} S2 - 01.ja.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
        manager.db.upsert_anime(LibraryAnime(media_id=1000 + index, title=name, progress=0))
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=1000 + index,
                title=name,
                episode=1,
                video_path=video,
                subtitle_path=subtitle,
                state="ready",
            )
        )
        videos.append(video)

    manager.scan_library()

    # Eight lookups per scan; the files not looked up yet keep their rows.
    assert len(searched) == 8
    assert all(manager.db.episode_by_path(video) is not None for video in videos)

    manager.scan_library()

    assert sorted(searched) == sorted(answers)
    assert sorted(manager.db.episode_by_path(video).media_id for video in videos) == sorted(
        candidate.id for candidate in answers.values()
    )
