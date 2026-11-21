"""Core review rv5: parser crashes on unusual names and dates, one title or file ending a
pass, state rows that were never removed, and repeated work that cost energy (searches of
episodes that keep missing, ffprobe on every download check, title normalisation, the
disk limit). Invented titles only."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import pudge.manager as manager_module
from pudge.config import AppConfig
from pudge.database import Database
from pudge.filename import parse_anime_filename
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.providers import nyaa
from pudge.release_parser import double_episode_span, parse_release_name


# --- one unusual name or date used to raise out of every parser and scorer -------------

CIRCLED_NAMES = [
    "[Grp] Hoshi no Umi - ② [1080p].mkv",
    "[Grp] Hoshi no Umi ② [1080p].mkv",
    "Hoshi no Umi ①.mkv",
    "[Grp] Hoshi no Umi - 05² [1080p].mkv",
    "[Grp] Hoshi no Umi - ¹ [1080p].mkv",
    "[Grp] Hoshi no Umi - Episode ③ [1080p].mkv",
    "Hoshi no Umi Season ① E 05",
]


@pytest.mark.parametrize("name", CIRCLED_NAMES)
def test_circled_and_superscript_numbers_never_raise(name: str) -> None:
    # A library file named like this aborted every library scan (parse_anime_filename in
    # the scan loop), and a feed item like this every release search.
    parsed = parse_release_name.__wrapped__(name)
    assert parsed.episode is None or isinstance(parsed.episode, int)
    parse_anime_filename(name)
    nyaa.release_episode(name)
    nyaa.release_episode_range(name)
    nyaa.release_title_is_pack(name)
    nyaa.release_group(name)
    double_episode_span.__wrapped__(name)


def test_circled_episode_is_no_regular_episode_and_plain_numbers_still_parse() -> None:
    assert parse_release_name.__wrapped__("[Grp] Hoshi no Umi - ② [1080p].mkv").episode is None
    assert parse_release_name.__wrapped__("[Grp] Hoshi no Umi - 05 [1080p].mkv").episode == 5
    assert parse_anime_filename("[Grp] Hoshi no Umi - 05v2 [1080p].mkv").episode == 5


def _release(title: str, *, published: str = "", info_hash: str = "a" * 40, seeders: int = 20) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link="https://example.invalid/view/1",
        torrent_url=f"magnet:?xt=urn:btih:{info_hash}",
        info_hash=info_hash,
        size_text="1.0 GiB",
        size_bytes=1024**3,
        seeders=seeders,
        leechers=1,
        downloads=5,
        trusted=True,
        remake=False,
        published=published,
        is_batch=nyaa.release_title_is_pack(title),
        group=nyaa.release_group(title),
    )


_SCORE_KW = dict(
    trusted_groups=["Grp"],
    preferred_groups=[],
    blocked_groups=[],
    preferred_resolution="1080p",
    min_seeders=1,
    target_episode_min_bytes=200 * 1024**2,
    target_episode_max_bytes=2500 * 1024**2,
)

_ANIME = LibraryAnime(
    media_id=1,
    title="Hoshi no Umi",
    titles=["Hoshi no Umi"],
    episodes=12,
    format="TV",
    media_status="RELEASING",
)

# Past the last representable date once converted to UTC.
FAR_FUTURE = "Fri, 31 Dec 9999 23:59:59 -1400"


def test_far_future_publish_date_does_not_break_scoring() -> None:
    release = _release("[Grp] Hoshi no Umi - 05 [1080p]", published=FAR_FUTURE, seeders=0)
    scored = nyaa.score_release(release, _ANIME, episode=5, batch=False, **_SCORE_KW)
    assert "ep=5" in scored.reasons
    assert not any(reason.startswith("age=") for reason in scored.reasons)
    assert nyaa.fresh_trusted_zero_seeders_allowed(release, ["Grp"]) is False


def test_one_bad_feed_date_does_not_hide_the_good_release() -> None:
    good = _release("[Grp] Hoshi no Umi - 05 [1080p]", info_hash="b" * 40)
    odd = _release("[Grp] Hoshi no Umi - 05 [720p]", published=FAR_FUTURE, info_hash="c" * 40)

    class Feed:
        def releases(self, *_args):
            return [odd, good]

    ranked = nyaa.search_subsplease_ranked(Feed(), _ANIME, episode=5, batch=False, **_SCORE_KW)
    assert {item.info_hash for item in ranked} == {"b" * 40, "c" * 40}
    ranked = nyaa.search_shana_ranked(Feed(), _ANIME, episode=5, batch=False, **_SCORE_KW)
    assert "b" * 40 in {item.info_hash for item in ranked}


# --- one title's unexpected error used to end the automatic pass -----------------------


def _manager(tmp_path: Path) -> AnimeManager:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    config.nyaa.enabled = True
    config.nyaa.auto_download_current = True
    config.qbittorrent.enabled = False
    config.aria2.enabled = True
    return AnimeManager(config, log=lambda message: None)


def test_one_failing_title_does_not_end_the_automatic_pass(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    for media_id, title in ((101, "Alpha Hoshi"), (202, "Beta Umi")):
        manager.db.upsert_anime(
            LibraryAnime(
                media_id,
                title,
                status="CURRENT",
                media_status="RELEASING",
                progress=1,
                episodes=12,
                next_airing_episode=3,
            )
        )
    for name in (
        "replace_stalled_aria2_downloads",
        "cleanup_aria2_leftovers",
        "cleanup_orphan_aria2_controls",
        "start_queued_releases",
        "prioritize_nearest_episodes",
    ):
        monkeypatch.setattr(manager, name, lambda *args, **kwargs: 0)
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)
    monkeypatch.setattr(manager, "_storage_can_accept", lambda size: True)
    monkeypatch.setattr(
        manager,
        "search_releases",
        lambda media_id, *, episode, batch, automatic=False: [
            _release(f"[Grp] Show {media_id} - 02 [1080p]", info_hash=f"{media_id:040d}")
        ],
    )
    monkeypatch.setattr(manager, "automatic_release_candidates", lambda releases: list(releases))
    added: list[tuple[int, int]] = []

    def add_release(media_id, release, *, episode, batch, **kwargs):
        if media_id == 101:
            raise PermissionError("library disk is not mounted")
        added.append((media_id, episode))
        return True

    monkeypatch.setattr(manager, "add_release", add_release)
    assert manager.auto_search_current() == 1
    assert added == [(202, 2)], "the title after the failing one is still searched and added"
    assert manager.download_intents.get(202, 2, False)["state"] == "downloading"


# --- state rows that stayed forever ----------------------------------------------------


def _state_keys(db: Database, prefix: str) -> set[str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT key FROM state WHERE key LIKE ?", (prefix + "%",)).fetchall()
    return {str(row["key"]) for row in rows}


def test_prune_history_drops_old_double_episode_markers_and_empty_stall_rows(tmp_path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    now = time.time()
    db.set_state("double_episode_seen:old", "1.0")
    db.set_state("double_episode_seen:new", str(now))
    db.set_state("aria2_stall:" + "1" * 40, "")
    db.set_state("aria2_stall:" + "2" * 40, '{"downloaded":0}')
    with db.connect() as conn:
        conn.execute(
            "UPDATE state SET updated_at=? WHERE key=?", (now - 10 * 86400, "double_episode_seen:old")
        )
    removed = db.prune_history(now=now)
    assert removed["double_episode_seen"] == 1 and removed["aria2_stall_markers"] == 1
    assert _state_keys(db, "double_episode_seen:") == {"double_episode_seen:new"}
    assert _state_keys(db, "aria2_stall:") == {"aria2_stall:" + "2" * 40}


def test_stall_marker_is_deleted_once_the_download_moves(tmp_path) -> None:
    manager = _manager(tmp_path)
    torrent_hash = "d" * 40
    manager.db.set_state(f"aria2_stall:{torrent_hash}", '{"downloaded":0,"stalled_since":1}')

    class Client:
        def reconnect(self, _hash):
            raise AssertionError("a moving download is not reconnected")

    moving = DownloadItem(
        torrent_hash,
        "[Grp] Hoshi no Umi - 05 [1080p].mkv",
        "active",
        0.4,
        str(tmp_path),
        "",
        raw={"downloaded": 400, "total_size": 1000, "download_speed": 50_000, "num_connections": 4},
    )
    assert manager._recover_stalled_aria2_downloads(Client(), [moving], now=time.time()) == 0
    assert _state_keys(manager.db, "aria2_stall:") == set()


def test_forgetting_a_deleted_video_drops_its_subtitle_probe_cache(tmp_path) -> None:
    manager = _manager(tmp_path)
    video = tmp_path / "library" / "Hoshi no Umi" / "[Grp] Hoshi no Umi - 05 [1080p].mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"\0" * 64)
    key = manager._embedded_subtitle_probe_state_key(video.resolve())
    manager.db.set_state(key, '{"version":1}')
    video.unlink()
    assert manager._forget_video_state([video]) >= 1
    assert manager.db.get_state(key, "") == ""


# --- energy: older episodes that keep missing back off ----------------------------------

BACKOFF_MEDIA = 303


def _backoff_pass(manager: AnimeManager, monkeypatch) -> list[int]:
    searched: list[int] = []
    monkeypatch.setattr(
        manager,
        "search_releases",
        lambda media_id, *, episode, batch, automatic=False: searched.append(episode) or [],
    )
    manager.auto_search_current()
    return searched


def _backoff_manager(tmp_path, monkeypatch) -> tuple[AnimeManager, list[str]]:
    manager = _manager(tmp_path)
    manager.config.nyaa.max_auto_download_per_anime = 10
    manager.db.upsert_anime(
        LibraryAnime(
            BACKOFF_MEDIA,
            "Gamma Sora",
            status="CURRENT",
            media_status="RELEASING",
            progress=0,
            episodes=12,
            # Episodes 1-3 aired; 3 is the newest, 4 airs tomorrow.
            next_airing_episode=4,
            next_airing_at=int(time.time()) + 86400,
        )
    )
    for name in (
        "replace_stalled_aria2_downloads",
        "cleanup_aria2_leftovers",
        "cleanup_orphan_aria2_controls",
        "start_queued_releases",
        "prioritize_nearest_episodes",
    ):
        monkeypatch.setattr(manager, name, lambda *args, **kwargs: 0)
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)
    monkeypatch.setattr(manager, "_storage_can_accept", lambda size: True)
    monkeypatch.setattr(manager, "automatic_release_candidates", lambda releases: list(releases))
    logged: list[str] = []
    real_info = manager.logger.info

    def info(message, *args, **kwargs):
        logged.append(message % args if args else message)
        return real_info(message, *args, **kwargs)

    monkeypatch.setattr(manager.logger, "info", info)
    return manager, logged


def _miss(manager: AnimeManager, episode: int) -> dict:
    raw = manager.db.get_state(manager._auto_search_miss_key(BACKOFF_MEDIA, episode), "")
    return json.loads(raw) if raw else {}


def test_older_missed_episodes_back_off_and_the_newest_is_always_searched(tmp_path, monkeypatch) -> None:
    manager, logged = _backoff_manager(tmp_path, monkeypatch)
    for _ in range(AnimeManager.AUTO_SEARCH_BACKOFF_AFTER_MISSES):
        assert _backoff_pass(manager, monkeypatch) == [1, 2, 3]
    entered = [line for line in logged if line.startswith("RESULT step=nyaa.auto_search_backoff")]
    assert len(entered) == 2 and "episode=1 " in entered[0] and "episode=2 " in entered[1]
    assert "retry_in_s=1200" in entered[0]
    assert _miss(manager, 3) == {}, "the newest aired episode never counts misses"

    # Backing off: only the newest is searched (the budget is not used by the others).
    assert _backoff_pass(manager, monkeypatch) == [3]
    assert _backoff_pass(manager, monkeypatch) == [3]

    # Refresh / "search new releases" / Torrents On search everything.
    with manager.ignore_auto_search_backoff():
        assert _backoff_pass(manager, monkeypatch) == [1, 2, 3]
    assert _miss(manager, 1)["misses"] == 7
    assert 2400 - 5 <= _miss(manager, 1)["next_at"] - time.time() <= 2400 + 5, "doubles: 20 -> 40 min"

    # The wait is over for episode 1 only.
    key = manager._auto_search_miss_key(BACKOFF_MEDIA, 1)
    manager.db.set_state(key, json.dumps({"misses": 7, "next_at": time.time() - 1}))
    assert _backoff_pass(manager, monkeypatch) == [1, 3]


def test_backoff_is_capped_and_resets_on_a_manual_pick_or_a_file(tmp_path, monkeypatch) -> None:
    manager, _logged = _backoff_manager(tmp_path, monkeypatch)
    now = time.time()
    misses: dict = {}
    manager._note_auto_search_miss(misses, BACKOFF_MEDIA, 1, now=now)
    for _ in range(30):
        manager._note_auto_search_miss(misses, BACKOFF_MEDIA, 1, now=now)
    assert _miss(manager, 1)["next_at"] - now == pytest.approx(3 * 3600)
    for episode in (1, 2):
        manager.db.set_state(
            manager._auto_search_miss_key(BACKOFF_MEDIA, episode),
            json.dumps({"misses": 9, "next_at": time.time() + 3600}),
        )
    # The user picks a release (Find episode, a manual download): its misses are gone.
    manager._allow_redownload(BACKOFF_MEDIA, 2)
    assert _miss(manager, 2) == {} and _miss(manager, 1)
    # Episode 1 turns up in the library: forgotten on the next pass.
    manager.db.upsert_episode(
        LibraryEpisode(
            BACKOFF_MEDIA,
            "Gamma Sora",
            1,
            tmp_path / "library" / "Gamma Sora" / "Gamma Sora - 01.mkv",
        )
    )
    assert _backoff_pass(manager, monkeypatch) == [2, 3]
    assert _miss(manager, 1) == {}
    # A whole-title pick forgets every episode; a title no longer followed forgets too.
    manager.db.set_state(manager._auto_search_miss_key(BACKOFF_MEDIA, 2), json.dumps({"misses": 9}))
    manager._allow_redownload(BACKOFF_MEDIA, None)
    assert _miss(manager, 2) == {}
    manager.db.set_state(manager._auto_search_miss_key(999, 2), json.dumps({"misses": 9}))
    _backoff_pass(manager, monkeypatch)
    assert manager.db.get_state(manager._auto_search_miss_key(999, 2), "") == ""


def test_web_search_new_releases_bypasses_the_backoff(tmp_path, monkeypatch) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    seen: list[bool] = []
    monkeypatch.setattr(
        api.manager,
        "auto_search_current",
        lambda: seen.append(bool(getattr(manager_module._AUTO_SEARCH_BYPASS, "active", False))) or 0,
    )
    api.search_new_releases()
    assert seen == [True]


# --- energy: the embedded-subtitle probe of a completed download is reused --------------


def test_completed_download_probe_is_reused_while_the_file_is_unchanged(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    video = tmp_path / "library" / "Gamma Sora" / "[Grp] Gamma Sora - 05 [1080p].mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"\0" * 128)
    probes: list[Path] = []

    def details(path, **kwargs):
        probes.append(Path(path))
        return "embedded", None, 3

    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", details)
    assert manager._completed_subtitle_details(video) == ("embedded", None, 3)
    assert manager._completed_subtitle_details(video) == ("embedded", None, 3)
    assert len(probes) == 1, "unchanged file: no second ffprobe"

    video.write_bytes(b"\0" * 256)
    stat = video.stat()
    os.utime(video, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    manager._completed_subtitle_details(video)
    assert len(probes) == 2, "a changed file is probed again"

    sidecar = video.with_name(video.stem + ".ja.srt")
    sidecar.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nきょうは とても いい てんき ですね。\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\nそうですね、さんぽに いきましょう。\n",
        encoding="utf-8",
    )
    source, path, sid = manager._completed_subtitle_details(video)
    assert (source, sid) == ("external", None) and path == sidecar.resolve()
    assert len(probes) == 2, "a subtitle file beside the video needs no probe"


def test_failed_probe_is_not_cached(tmp_path, monkeypatch) -> None:
    from pudge.media import MediaProbeError

    manager = _manager(tmp_path)
    video = tmp_path / "library" / "Gamma Sora" / "[Grp] Gamma Sora - 06 [1080p].mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"\0" * 128)
    attempts: list[str] = []

    def details(path, **kwargs):
        attempts.append("details")
        raise MediaProbeError("ffprobe is not installed")

    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", details)
    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_source", lambda *a, **k: attempts.append("source") or ("none", None)
    )
    assert manager._completed_subtitle_details(video) == ("none", None, None)
    assert manager._completed_subtitle_details(video) == ("none", None, None)
    assert attempts == ["details", "source", "details", "source"]


def test_registering_a_completed_download_twice_probes_once(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(BACKOFF_MEDIA, "Gamma Sora", status="CURRENT", episodes=12, format="TV")
    )
    video = tmp_path / "library" / "Gamma Sora" / "[Grp] Gamma Sora - 05 [1080p].mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"\0" * 128)
    probes: list[Path] = []
    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_details",
        lambda path, **kwargs: probes.append(Path(path)) or ("none", None, None),
    )
    item = DownloadItem(
        "e" * 40,
        video.name,
        "uploading",
        1.0,
        str(video.parent),
        str(video),
        media_id=BACKOFF_MEDIA,
        episode=5,
        completed_on=1,
    )
    assert manager._register_completed_download(item) == 1
    manager._register_completed_download(item)
    manager._register_completed_download(item)
    assert len(probes) == 1
    assert manager.db.episode_by_path(video.resolve()).state == "waiting_subtitles"


# --- energy: the disk limit goes on past a failed delete, schedules read once ------------


def _disk_limited(tmp_path, monkeypatch) -> tuple[AnimeManager, list[Path]]:
    manager = _manager(tmp_path)
    manager.config.library.disk_limit_enabled = True
    manager.config.library.disk_limit_gb = 25 / 1024**3  # 25 bytes: two of three files must go
    manager.db.upsert_anime(LibraryAnime(BACKOFF_MEDIA, "Gamma Sora", status="CURRENT", episodes=12))
    folder = tmp_path / "library" / "Gamma Sora"
    folder.mkdir(parents=True)
    videos = []
    for episode in (1, 2, 3):
        video = folder / f"Gamma Sora - {episode:02d}.mkv"
        video.write_bytes(b"x" * 10)
        manager.db.upsert_episode(
            LibraryEpisode(
                BACKOFF_MEDIA,
                "Gamma Sora",
                episode,
                video,
                state="watched",
                torrent_hash=f"{episode}" * 40,
                watched_at=float(episode),
            )
        )
        videos.append(video)
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)

    def no_per_episode_reads(*_args):
        raise AssertionError("the personal schedule is read once per pass")

    monkeypatch.setattr(manager.db, "personal_release_item_retained", no_per_episode_reads)
    return manager, videos


def test_disk_limit_goes_on_past_a_failed_delete(tmp_path, monkeypatch) -> None:
    from pudge.manager import ManagerError

    manager, videos = _disk_limited(tmp_path, monkeypatch)

    def delete_owned(torrent_hash, download, *, deleting):
        if torrent_hash == "1" * 40:
            raise ManagerError("the torrent client refused")
        return True

    monkeypatch.setattr(manager, "_delete_owned_torrent", delete_owned)
    assert manager.enforce_disk_limit() == 1
    assert [video.exists() for video in videos] == [True, False, True]


def test_disk_limit_keeps_scheduled_episodes_from_one_read(tmp_path, monkeypatch) -> None:
    manager, videos = _disk_limited(tmp_path, monkeypatch)
    monkeypatch.setattr(manager.db, "personal_release_retained_items", lambda: {(BACKOFF_MEDIA, 1)})
    monkeypatch.setattr(manager, "_delete_owned_torrent", lambda *args, **kwargs: True)
    assert manager.enforce_disk_limit() == 1
    assert [video.exists() for video in videos] == [True, False, True]


def test_personal_release_retained_items_matches_the_single_check(tmp_path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(BACKOFF_MEDIA, "Gamma Sora", status="CURRENT", episodes=12))
    now = time.time()
    with db.connect() as conn:
        for enabled, anime_id in ((1, BACKOFF_MEDIA),):
            conn.execute(
                "INSERT INTO personal_release_schedules(anime_id,enabled,start_local_datetime,timezone_iana,"
                "first_episode_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (anime_id, enabled, "2026-11-01T20:00", "UTC", 1, now, now),
            )
        schedule_id = conn.execute("SELECT schedule_id FROM personal_release_schedules").fetchone()[0]
        for episode, watched in ((1, now), (2, None), (3, None)):
            conn.execute(
                "INSERT INTO personal_release_items(schedule_id,logical_episode_id,ordinal,unlock_at_utc,"
                "nominal_local_datetime,schedule_revision,watched_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (schedule_id, episode, episode, now, "2026-11-01T20:00", 1, watched, now),
            )
    retained = db.personal_release_retained_items()
    assert retained == {(BACKOFF_MEDIA, 2), (BACKOFF_MEDIA, 3)}
    for episode in (1, 2, 3, 4):
        assert ((BACKOFF_MEDIA, episode) in retained) == db.personal_release_item_retained(
            BACKOFF_MEDIA, episode
        )


# --- one file never ends the library scan ------------------------------------------------


def test_one_failing_file_does_not_end_the_library_scan(tmp_path, monkeypatch, caplog) -> None:
    import pudge.library as library

    db = Database(tmp_path / "library.sqlite3")
    root = tmp_path / "library"
    (root / "Gamma Sora").mkdir(parents=True)
    good = root / "Gamma Sora" / "Gamma Sora - 01.mkv"
    bad = root / "Gamma Sora" / "Gamma Sora - 02.mkv"
    for video in (good, bad):
        video.write_bytes(b"\0" * 64)
    real_parse = library.parse_anime_filename

    def parse(path):
        if Path(path).name == bad.name:
            raise ValueError("a name no parser expected")
        return real_parse(path)

    monkeypatch.setattr(library, "parse_anime_filename", parse)
    monkeypatch.setattr(library, "japanese_subtitle_details", lambda *a, **k: ("none", None, None))
    with caplog.at_level("WARNING", logger="pudge.library"):
        rows = library.scan_library(root, db)
    assert [row.video_path.name for row in rows] == [good.name]
    assert any("SKIP step=library.scan_file" in record.getMessage() for record in caplog.records)


def test_a_database_error_still_ends_the_library_scan(tmp_path, monkeypatch) -> None:
    import pudge.library as library

    db = Database(tmp_path / "library.sqlite3")
    root = tmp_path / "library"
    root.mkdir()
    (root / "Gamma Sora - 01.mkv").write_bytes(b"\0" * 64)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "episode_by_path", locked)
    monkeypatch.setattr(library, "japanese_subtitle_details", lambda *a, **k: ("none", None, None))
    with pytest.raises(sqlite3.OperationalError):
        library.scan_library(root, db)


# --- energy: each release title is normalised once per search ----------------------------


def test_feed_search_normalises_each_title_once(monkeypatch) -> None:
    import pudge.filename as filename

    folds: list[str] = []
    real_fold = filename.fold_search_title
    monkeypatch.setattr(filename, "fold_search_title", lambda value: folds.append(value) or real_fold(value))
    items = [
        _release(f"[Grp] Other Show {number} - 05 [1080p]", info_hash=f"{number:040x}")
        for number in range(40)
    ]
    items.append(_release("[Grp] Hoshi no Umi - 05 [1080p]", info_hash="b" * 40))

    class Feed:
        def releases(self, *_args):
            return list(items)

    ranked = nyaa.search_subsplease_ranked(Feed(), _ANIME, episode=5, batch=False, **_SCORE_KW)
    assert [item.info_hash for item in ranked] == ["b" * 40]
    assert len(folds) == len(set(folds)), "no title is normalised twice within one search"
    assert filename._TITLE_MEMO.get() is None, "the memo ends with the search"


def test_newest_aired_episode_is_searched_when_older_misses_use_the_budget(tmp_path, monkeypatch) -> None:
    manager, _logged = _backoff_manager(tmp_path, monkeypatch)
    manager.config.nyaa.max_auto_download_per_anime = 1
    assert _backoff_pass(manager, monkeypatch) == [1, 3]
