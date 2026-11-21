"""A batch that finished right before aria2 stopped is not lost.

User (Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen): "only one episode is shown, and it
has no subtitles". The 12-episode BD batch finished at 23:55 (every piece checks against the
torrent), Pudge was quit a minute later and opened again with Torrents off. aria2 does not keep a
finished task in its session, so the task was gone: the row stayed "downloading 99.8%", the folder
was left out of the Library scan as an unfinished download, and when aria2 ran again the forgotten
task was recovered only for single-file torrents. The episodes never reached the library.

aria2 keeps "<info hash>.torrent" beside the download and deletes "<name>.aria2" only when it is
complete: no control file and every file with its exact length is a finished payload.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime
from pudge.providers.aria2 import Aria2Client, Aria2Error, torrent_layout

MEDIA = 21733
TITLE = "Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen"
NAME = "[SCY-kBaraka] Shouwa Genroku Rakugo Shinjuu S2 [BD 1080p FLAC]"
EPISODES = [
    f"[SCY-kBaraka] Shouwa Genroku Rakugo Shinjuu S2 - {n:02d} [BD 1080p FLAC].mkv" for n in (1, 2, 3)
]
EXTRA = "NC/[SCY-kBaraka] Shouwa Genroku Rakugo Shinjuu S2 - NCED [BD 1080p FLAC].mkv"
FILES = [(EXTRA, 7), *[(name, 10 + index) for index, name in enumerate(EPISODES)]]


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    return b"d" + b"".join(_bencode(key) + _bencode(value[key]) for key in sorted(value)) + b"e"


def _torrent(files=FILES) -> tuple[bytes, str]:
    info = {
        "name": NAME,
        "piece length": 16384,
        "pieces": b"\0" * 20,
        "files": [{"length": length, "path": relative.split("/")} for relative, length in files],
    }
    return _bencode({"info": info}), hashlib.sha1(_bencode(info)).hexdigest()


def _download(folder: Path, files=FILES) -> str:
    payload, info_hash = _torrent(files)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{info_hash}.torrent").write_bytes(payload)
    for relative, length in FILES:
        path = folder / NAME / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v" * length)
    return info_hash


def _client(tmp_path: Path, folder: Path, info_hash: str) -> Aria2Client:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    client._save_metadata(
        {
            info_hash[:16]: {
                "info_hash": info_hash,
                "title": NAME,
                "save_path": str(folder),
                "media_id": MEDIA,
                "episode": None,
                "is_batch": True,
                "video_count": 4,
                "video_episodes": [1, 2, 3],
                "anime_title": TITLE,
                "completed_on": 0,
            }
        }
    )
    return client


def test_the_saved_torrent_tells_a_finished_payload(tmp_path: Path) -> None:
    folder = tmp_path / "Rakugo S2"
    info_hash = _download(folder)
    name, files, multi = torrent_layout((folder / f"{info_hash}.torrent").read_bytes())
    assert (name, multi, files[0]) == (NAME, True, (EXTRA, 7))
    found = Aria2Client._completed_payload_on_disk(folder, info_hash)
    assert found == (folder / NAME, sum(length for _path, length in FILES))

    (folder / NAME / EXTRA).unlink()  # deselected or deleted by the person: still finished
    assert Aria2Client._completed_payload_on_disk(folder, info_hash) is not None
    control = folder / f"{NAME}.aria2"
    control.write_bytes(b"control")  # still downloading
    assert Aria2Client._completed_payload_on_disk(folder, info_hash) is None
    control.unlink()
    (folder / NAME / EPISODES[1]).write_bytes(b"short")  # not the length of the torrent
    assert Aria2Client._completed_payload_on_disk(folder, info_hash) is None

    payload, _hash = _torrent([("../outside.mkv", 3)])
    assert torrent_layout(payload) is None


def test_a_forgotten_batch_task_comes_back_from_its_saved_torrent(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "library" / TITLE
    info_hash = _download(folder)
    client = _client(tmp_path, folder, info_hash)
    monkeypatch.setattr(client, "_all_statuses", lambda: [])  # aria2 runs, the task is gone
    try:
        [item] = client.torrents()
    finally:
        client.close()
    assert item.torrent_hash == info_hash and item.state == "complete" and item.progress == 1.0
    assert item.is_batch and item.media_id == MEDIA and item.release_episode is None
    assert Path(item.content_path) == (folder / NAME).resolve()


def _manager(tmp_path: Path) -> AnimeManager:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    config.qbittorrent.enabled = False
    config.aria2.enabled = True
    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(MEDIA, TITLE, episodes=12, format="TV", status="PLANNING", media_status="FINISHED")
    )
    return manager


def test_with_torrents_off_the_finished_batch_reaches_the_library(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / TITLE
    info_hash = _download(folder)
    # What the database held: the row last seen at 99.8%.
    manager.db.upsert_download(
        DownloadItem(
            info_hash,
            NAME,
            "active",
            0.998,
            str(folder),
            str(folder / NAME),
            media_id=MEDIA,
            is_batch=True,
            raw={"backend": "aria2", "total_size": 100, "downloaded": 99},
        )
    )
    assert manager._path_within(folder / NAME / EPISODES[0], manager.incomplete_download_paths())
    client = _client(tmp_path, folder, info_hash)

    def stopped(**_kwargs):
        raise Aria2Error("aria2 RPC is not running")

    monkeypatch.setattr(client, "torrents", stopped)
    monkeypatch.setattr(manager, "torrent_clients", lambda: [("aria2", client)])
    monkeypatch.setattr(manager, "downloads_enabled", lambda: False)
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None))
    try:
        manager.sync_downloads()
    except Exception:
        pass  # aria2 itself is still unavailable; that is reported as before

    episodes = sorted(
        (row.episode, row.state) for row in manager.db.episodes(MEDIA) if row.episode is not None
    )
    assert episodes == [(1, "waiting_subtitles"), (2, "waiting_subtitles"), (3, "waiting_subtitles")]
    assert manager.db.subtitle_jobs()  # subtitles are prepared for them
    row = manager.db.download_by_hash(info_hash)
    assert row.state == "complete" and manager._download_is_complete(row)
    assert not manager.incomplete_download_paths()

    # The next poll does not register it again.
    calls: list[str] = []
    monkeypatch.setattr(
        manager, "_register_completed_download", lambda item, **_k: calls.append(item.name) or 0
    )
    try:
        manager.sync_downloads()
    except Exception:
        pass
    assert calls == []
