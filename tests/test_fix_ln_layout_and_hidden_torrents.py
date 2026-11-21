"""Light novel downloads get a folder per title, like anime; aria2's .torrent is hidden in Finder.

A: aria2 saves ``<dir>/<info hash>.torrent`` next to the download and reads it back from
there, so the file is never moved: it gets macOS's hidden flag (Finder does not show it).
B: a novel release goes to ``<Light Novels>/<title>`` (``extra_files.title_folder_name``,
the anime folder name); a volume download registered by an older version (no folder)
still finishes from the root.
"""

from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path

import pytest

import pudge.fsutil as fsutil
from pudge.extra_files import title_folder_name
from pudge.light_novels import LightNovelService
from pudge.providers.aria2 import Aria2Client
from tests.test_fix_ln_nyaa_existing_torrent import (  # noqa: F401 - fixtures
    NEW_HASH,
    _cfg,
    _row,
    _write_epub,
    no_threads,
    qbt,
)

HASH = "c" * 40
UF_HIDDEN = 0x8000


@pytest.fixture
def flags(monkeypatch) -> dict[str, int]:
    """macOS's file flags, on any system: path -> st_flags."""
    store: dict[str, int] = {}
    real_lstat = os.lstat

    class Result:
        def __init__(self, path):
            self._real = real_lstat(path)
            self.st_flags = store.get(str(path), 0)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def chflags(path, value, *, follow_symlinks=True):
        store[str(path)] = int(value)

    monkeypatch.setattr(stat, "UF_HIDDEN", UF_HIDDEN, raising=False)
    monkeypatch.setattr(os, "chflags", chflags, raising=False)
    monkeypatch.setattr(fsutil.os, "lstat", Result)
    return store


def test_hide_from_finder_sets_the_flag_once_and_keeps_the_file(tmp_path, flags) -> None:
    path = tmp_path / f"{HASH}.torrent"
    path.write_bytes(b"d4:infod4:name1:aee")
    flags[str(path)] = 0x1  # another flag stays
    assert fsutil.hide_from_finder(path) is True
    assert flags[str(path)] == 0x1 | UF_HIDDEN
    assert fsutil.hide_from_finder(path) is False  # already hidden
    assert path.read_bytes() == b"d4:infod4:name1:aee"


def test_hide_from_finder_is_a_no_op_without_the_flag(tmp_path, monkeypatch) -> None:
    path = tmp_path / f"{HASH}.torrent"
    path.write_bytes(b"x")
    monkeypatch.delattr(os, "chflags", raising=False)
    assert fsutil.hide_from_finder(path) is False
    assert path.exists()


def test_hide_saved_torrent_files_hides_only_aria2_metadata(tmp_path, flags) -> None:
    show = tmp_path / "Show"
    show.mkdir()
    saved = show / f"{HASH}.torrent"
    own = show / "my download.torrent"
    episode = show / "Show - 01.mkv"
    for path in (saved, own, episode):
        path.write_bytes(b"x")
    assert fsutil.hide_saved_torrent_files(tmp_path) == [saved]
    assert flags == {str(saved): UF_HIDDEN}
    assert saved.exists() and own.exists()


def _aria2(tmp_path: Path) -> Aria2Client:
    client = Aria2Client.__new__(Aria2Client)
    client.state_dir = tmp_path / "state"
    client.state_dir.mkdir()
    client.seed_mode = "off"
    return client


def test_aria2_listing_hides_the_saved_metadata_in_the_library_folder(tmp_path, flags, monkeypatch) -> None:
    folder = tmp_path / "library" / "Show"
    folder.mkdir(parents=True)
    saved = folder / f"{HASH}.torrent"
    saved.write_bytes(b"x")
    client = _aria2(tmp_path)
    monkeypatch.setattr(
        client,
        "_all_statuses",
        lambda: [
            # The magnet's metadata task (followed by the payload task) and the payload.
            {"gid": "1", "status": "complete", "dir": str(folder), "infoHash": HASH, "followedBy": ["2"]},
            {"gid": "2", "status": "active", "dir": str(folder), "infoHash": HASH, "following": "1"},
        ],
    )
    monkeypatch.setattr(client, "_load_metadata", lambda: {})
    client.torrents()
    assert flags[str(saved)] & UF_HIDDEN
    assert saved.exists()  # aria2 reads it back from exactly here


def test_aria2_hide_ignores_anything_but_an_info_hash(tmp_path, flags) -> None:
    (tmp_path / "x.torrent").write_bytes(b"x")
    assert Aria2Client._hide_saved_metadata(tmp_path, "x") is False
    assert Aria2Client._hide_saved_metadata(tmp_path, "g" * 40) is False
    assert Aria2Client._hide_saved_metadata("", HASH) is False
    assert Aria2Client._hide_saved_metadata(tmp_path, HASH) is False  # no such file
    assert flags == {}


def test_novel_release_goes_into_a_folder_named_like_an_anime_folder(tmp_path, qbt, no_threads) -> None:
    qbt.new_files = [{"index": 0, "name": "Pack/Series 12.epub", "priority": 1, "progress": 0.0}]
    service = LightNovelService(_cfg(tmp_path))
    service._anilist_cache = (0.0, [{"media_id": 5, "title": "Series: Part/Two", "format": "NOVEL"}])
    service.download_nyaa_release(_row(NEW_HASH, media_id=5, series_title="searched"))
    [(_path, add)] = [(p, d) for p, d in qbt.posts if p == "/api/v2/torrents/add"]
    folder = service.root / title_folder_name("Series: Part/Two")
    assert Path(add["savepath"]) == folder.resolve()
    assert folder.is_dir()
    [row] = service._volume_downloads()
    assert Path(row["folder"]) == folder.resolve()


def test_download_folder_title_sources(tmp_path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    root = service.root.resolve()
    assert service.download_folder({"series_title": "Searched Title"}) == root / "Searched Title"
    assert service.download_folder({"title": "Some Series Vol 3"}) == root / "Some Series"
    # Nothing usable, or a name leaving the folder: the old place, the root.
    assert service.download_folder({}) == service.root
    assert service.download_folder({"series_title": ".."}) == service.root
    assert service.download_folder({"series_title": ".hidden"}) == root / "hidden"
    # A book bound to AniList names it by the AniList title.
    epub = tmp_path / "in" / "Book 1.epub"
    _write_epub(epub, "Book 1")
    book = service.import_file(epub)
    with service._connection() as conn:
        conn.execute("UPDATE ln_books SET anilist_id=7 WHERE id=?", (book["id"],))
    service._anilist_cache = (0.0, [{"media_id": 7, "title": "AniList Name"}])
    assert service.download_folder({"book_id": book["id"]}) == root / "AniList Name"


def test_finished_volume_is_extracted_into_its_title_folder(tmp_path, qbt) -> None:
    service = LightNovelService(_cfg(tmp_path))
    folder = service.root / "Series"
    archive = folder / "Pack" / "Series 10-12.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Series 12.epub", "x")
    qbt.add_existing(
        NEW_HASH,
        [{"index": 0, "name": "Pack/Series 10-12.zip", "priority": 6, "progress": 1.0}],
        save_path=str(folder),
    )
    service._register_volume_download(NEW_HASH, 12, title="Series", folder=str(folder))
    service._finish_volume_torrent(NEW_HASH, 12)
    assert (folder / "Series 12.epub").exists()
    assert not (service.root / "Series 12.epub").exists()
    assert service.volume_downloads() == []


def test_volume_download_of_an_older_version_still_finishes_from_the_root(tmp_path, qbt) -> None:
    service = LightNovelService(_cfg(tmp_path))
    archive = service.root / "Pack" / "Series 10-12.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Series 12.epub", "x")
    qbt.add_existing(
        NEW_HASH,
        [{"index": 0, "name": "Pack/Series 10-12.zip", "priority": 6, "progress": 1.0}],
        save_path=str(service.root),
    )
    service._register_volume_download(NEW_HASH, 12, title="Series")  # no folder: an old row
    service._finish_volume_torrent(NEW_HASH, 12)
    assert (service.root / "Series 12.epub").exists()


def test_folder_outside_the_library_falls_back_to_the_root(tmp_path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    assert service._volume_download_folder({"folder": str(tmp_path / "elsewhere")}) == service.root
    assert service._volume_download_folder({"folder": ""}) == service.root
