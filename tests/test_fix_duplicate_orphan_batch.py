"""A finished single-episode torrent must not block the next episode (Kusuriya 3rd Season ep 2).

The user's aria2 kept a finished "The Apothecary Diaries S03E49" (season episode 1) as an orphan
metadata row flagged "series pack"; every candidate for episode 2 was refused as
"already downloading" and the episode never arrived.
"""

from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.providers.aria2 import Aria2Client
from test_v0730_acquisition_safety import Backend

MEDIA = 195516
TITLE = "Kusuriya no Hitorigoto 3rd Season"
RELEASE = "The Apothecary Diaries S03E49 1080p CR WEB-DL AAC2.0 H.264-VARYG (Kusuriya no Hitorigoto 3rd Season, Multi-Subs)"
FILE = "The.Apothecary.Diaries.S03E49.1080p.CR.WEB-DL.AAC2.0.H.264-VARYG.mkv"
HASH = "b0822659d5dfd1771a296fc3291357f6f0e34a6e"


def _manager(tmp_path: Path, *, backend: str = "off") -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    cfg.qbittorrent.enabled = backend == "qbittorrent"
    cfg.aria2.enabled = backend == "aria2"
    cfg.nyaa.torrents_enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=MEDIA, title=TITLE, episodes=12, format="TV", status="CURRENT", progress=1)
    )
    # The third season continues the absolute numbering: episode 1 is release 49.
    cache = cfg.paths.cache_dir / "anilist-release-numbering" / f"{MEDIA}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"offset":48,"resolver_version":2,"prequel_titles":[]}', encoding="utf-8")
    return manager


def _season_folder(manager: AnimeManager) -> Path:
    folder = manager.config.library.root_dir / TITLE
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _orphan_client(tmp_path: Path, folder: Path, **meta) -> Aria2Client:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    client._save_metadata(
        {
            "gid-kusuriya": {
                "info_hash": HASH,
                "title": RELEASE,
                "save_path": str(folder),
                "media_id": MEDIA,
                "episode": None,
                "is_batch": True,
                "anime_title": TITLE,
                "completed_on": 10,
                **meta,
            }
        }
    )
    client._all_statuses = lambda: []  # aria2 forgot the finished task
    return client


def _anime(manager: AnimeManager) -> LibraryAnime:
    anime = manager.db.get_anime(MEDIA)
    assert anime is not None
    return anime


def test_single_file_series_pack_orphan_is_one_episode(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    folder = _season_folder(manager)
    (folder / FILE).write_bytes(b"v" * 64)
    client = _orphan_client(tmp_path, folder)
    try:
        (orphan,) = client.torrents()
    finally:
        client.close()
    assert orphan.is_batch is False
    assert orphan.release_episode == 49


def test_finished_episode_one_does_not_block_episode_two(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    folder = _season_folder(manager)
    video = folder / FILE
    video.write_bytes(b"v" * 64)
    # The library knows the file as season episode 1 (absolute 49).
    manager.db.upsert_episode(
        LibraryEpisode(
            MEDIA, TITLE, 1, video.resolve(), release_episode=49, state="watched", torrent_hash=HASH
        )
    )
    manager.db.upsert_download(
        DownloadItem(
            HASH, RELEASE, "complete", 1.0, str(folder), str(video), media_id=MEDIA, episode=1, is_batch=True
        )
    )
    client = _orphan_client(tmp_path, folder)
    try:
        anime = _anime(manager)
        assert (
            manager._existing_download_for_request(client, anime, episode=2, batch=False, release_hash="new")
            is None
        )
        # Episode 1 itself is still covered by its file.
        held = manager._existing_download_for_request(
            client, anime, episode=1, batch=False, release_hash="new"
        )
        assert held is not None and held.torrent_hash == HASH
    finally:
        client.close()
    # The wrong "series pack" flag is corrected in the library too (cleanup keeps no watched file for it).
    for item in client.torrents():
        manager.db.upsert_download(item)
    assert manager.db.download_by_hash(HASH).is_batch is False


def test_orphan_of_a_real_batch_blocks_only_episodes_on_disk(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    folder = _season_folder(manager)
    (folder / "[Group] Kusuriya no Hitorigoto S03E49 [1080p].mkv").write_bytes(b"v" * 64)
    client = _orphan_client(
        tmp_path,
        folder,
        title="[Group] Kusuriya no Hitorigoto 3rd Season (01-12) [1080p]",
        release_episode=49,
        episode=1,
        video_count=12,
        video_episodes=list(range(49, 61)),
    )
    try:
        anime = _anime(manager)
        (orphan,) = client.torrents()
        assert orphan.is_batch is True
        # Episode 2's file is not on this Mac: the forgotten pack must not hold the download back.
        assert (
            manager._existing_download_for_request(client, anime, episode=2, batch=False, release_hash="new")
            is None
        )
        second = folder / "[Group] Kusuriya no Hitorigoto S03E50 [1080p].mkv"
        second.write_bytes(b"v" * 64)
        manager.db.upsert_episode(
            LibraryEpisode(MEDIA, TITLE, 2, second.resolve(), release_episode=50, state="ready")
        )
        assert manager._existing_download_for_request(
            client, anime, episode=2, batch=False, release_hash="new"
        )
    finally:
        client.close()


def _live_pack(files: list[str] | None, *, name: str = "[Group] Kusuriya no Hitorigoto 3rd Season [1080p]"):
    raw = {"backend": "aria2", "_media_id_source": "aria2_metadata"}
    if files is not None:
        raw["video_episodes"] = [int(value) for value in files]
    return DownloadItem("pack", name, "downloading", 0.3, "/x", "/x", media_id=MEDIA, is_batch=True, raw=raw)


class _Client:
    def __init__(self, item: DownloadItem) -> None:
        self.item = item

    def torrents(self, *, category: str = ""):
        return [self.item]


def test_series_pack_blocks_only_the_episodes_it_holds(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    anime = _anime(manager)
    # Files S03E49..S03E54 (absolute) are episodes 1..6 of the season.
    holds_six = _Client(_live_pack([str(value) for value in range(49, 55)]))
    assert manager._existing_download_for_request(
        holds_six, anime, episode=2, batch=False, release_hash="new"
    )
    assert (
        manager._existing_download_for_request(holds_six, anime, episode=8, batch=False, release_hash="new")
        is None
    )
    # Season-local file numbers work as well.
    local = _Client(_live_pack(["1", "2", "3"]))
    assert manager._existing_download_for_request(local, anime, episode=3, batch=False, release_hash="new")
    assert (
        manager._existing_download_for_request(local, anime, episode=4, batch=False, release_hash="new")
        is None
    )
    # Unknown contents: still treated as holding every episode.
    unknown = _Client(_live_pack(None))
    assert manager._existing_download_for_request(unknown, anime, episode=9, batch=False, release_hash="new")


def test_live_aria2_single_video_series_pack_is_not_a_batch(tmp_path: Path) -> None:
    folder = tmp_path / "dl"
    folder.mkdir()
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    client._save_metadata(
        {
            "gid1": {
                "info_hash": "a" * 40,
                "title": "One",
                "save_path": str(folder),
                "media_id": MEDIA,
                "is_batch": True,
            },
            "gid2": {
                "info_hash": "b" * 40,
                "title": "Pack",
                "save_path": str(folder),
                "media_id": MEDIA,
                "is_batch": True,
            },
        }
    )

    def status(gid: str, names: list[str]) -> dict:
        return {
            "gid": gid,
            "status": "active",
            "totalLength": "100",
            "completedLength": "10",
            "dir": str(folder),
            "infoHash": ("a" if gid == "gid1" else "b") * 40,
            "files": [{"path": str(folder / name)} for name in names],
        }

    client._all_statuses = lambda: [
        status("gid1", [FILE, "info.nfo"]),
        status("gid2", ["Show - 01.mkv", "Show - 02.mkv", "Show - 03.mkv"]),
    ]
    try:
        rows = {row.torrent_hash: row for row in client.torrents()}
        metadata = client._load_metadata()
    finally:
        client.close()
    assert rows["a" * 40].is_batch is False
    assert rows["b" * 40].is_batch is True
    assert rows["b" * 40].raw["video_episodes"] == [1, 2, 3]
    assert metadata["gid1"]["video_count"] == 1


def test_race_stops_and_says_which_torrent_covers_the_episode(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, backend="aria2")
    backend = Backend("aria2")
    backend.items["pack"] = _live_pack(None)
    monkeypatch.setattr(manager, "qbt_client", lambda: backend)
    releases = [
        NyaaRelease(f"[Group] {TITLE} - 02 [1080p]", "", "", value, "1 GiB", 1024**3, 300, 10, 0, True, False)
        for value in ("one", "two")
    ]
    manager._race_add_candidates(
        MEDIA, releases, episode=2, batch=False, fast_seconds=0, total_seconds=0, poll_seconds=0.01
    )
    intent = manager.download_intents.get(MEDIA, 2, False)
    assert "Covered by an existing torrent" in str(intent.get("detail"))
    assert intent["owned_candidates"] == []
    assert backend.added == []
    assert manager.db.download_by_hash("one") is None
