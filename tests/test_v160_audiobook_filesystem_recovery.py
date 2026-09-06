from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace


def _api():
    from pudge.web_app import WebAppApi

    api = object.__new__(WebAppApi)
    api.logger = logging.getLogger("pudge-test-v160")
    return api


def _touch(path: Path, size: int = 8, *, old: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if old:
        stamp = time.time() - 120.0
        os.utime(path, (stamp, stamp))
    return path


def test_pre_v160_collection_recovery_selects_only_spice_and_wolf_volume_2(tmp_path: Path) -> None:
    api = _api()
    api.light_novels = SimpleNamespace(
        _release_volume_match=lambda title, target: (False, None, False),
    )
    destination = tmp_path / "Pudge Audiobooks" / "狼と香辛料" / "Volume 02"
    correct = _touch(
        destination
        / "Audiobook Collection"
        / "[支倉 凍砂] 狼と香辛料"
        / "[02] 狼と香辛料II [B0C58KN4W9].m4b",
        123,
    )
    _touch(
        destination
        / "Audiobook Collection"
        / "[支倉 凍砂] 狼と香辛料"
        / "[03] 狼と香辛料III [B0C58KN4XX].m4b",
        456,
    )
    _touch(
        destination
        / "Audiobook Collection"
        / "[支倉 凍砂] 新説 狼と香辛料"
        / "[02] 狼と羊皮紙II [B0DLNRM887].m4b",
        999,
    )
    _touch(
        destination
        / "Audiobook Collection"
        / "[丸山 くがね] オーバーロード"
        / "02"
        / "book.m4b",
        777,
    )

    paths = api._recoverable_light_novel_audiobook_paths(
        {"id": 77, "title": "狼と香辛料II", "series_title": "狼と香辛料", "volume": 2},
        destination,
    )

    assert paths == [correct.resolve()]


def test_v160_manifest_recovers_opaque_direct_release_after_restart(tmp_path: Path) -> None:
    api = _api()
    destination = tmp_path / "Volume 02"
    opaque = _touch(destination / "release-root" / "audio-0001.m4b", 50)
    book = {"id": 9, "title": "狼と香辛料II", "series_title": "狼と香辛料", "volume": 2}
    api._write_ln_audiobook_selection_manifest(
        destination,
        book=book,
        torrent_hash="abc",
        selected=[{"name": "release-root/audio-0001.m4b"}],
        collection=False,
    )

    assert api._recoverable_light_novel_audiobook_paths(book, destination) == [opaque.resolve()]


def test_recovered_multifile_volume_never_imports_neighbor_audio(tmp_path: Path) -> None:
    api = _api()
    destination = tmp_path / "Volume 02"
    selected_a = _touch(destination / "collection" / "series" / "disc1" / "01.mp3", 10)
    selected_b = _touch(destination / "collection" / "series" / "disc2" / "02.mp3", 20)
    neighbor = _touch(destination / "collection" / "series" / "volume3" / "bad.mp3", 30)
    calls = []

    class FakeAudiobooks:
        def import_folder(self, folder, **kwargs):
            files = sorted(path for path in Path(folder).rglob("*") if path.is_file() and path.suffix == ".mp3")
            calls.append((Path(folder), files, kwargs))
            return {"id": 12}

        def import_file(self, *args, **kwargs):
            raise AssertionError("multifile recovery should import one folder view")

    api.audiobooks = FakeAudiobooks()
    result = api._import_recovered_light_novel_audiobook(
        {"title": "狼と香辛料II", "volume": 2},
        destination,
        [selected_a, selected_b],
    )

    assert result == {"id": 12}
    root, files, kwargs = calls[0]
    assert root == destination / ".pudge-volume-import"
    assert len(files) == 2
    assert neighbor.resolve() not in {path.resolve() for path in files}
    assert {path.stat().st_ino for path in files} == {selected_a.stat().st_ino, selected_b.stat().st_ino}
    assert kwargs["title_override"] == "狼と香辛料II"
    assert kwargs["prepare_transcription"] is False


def test_recovery_links_completed_disk_download_and_starts_alignment(tmp_path: Path) -> None:
    api = _api()
    book = {"id": 42, "title": "狼と香辛料II", "series_title": "狼と香辛料", "volume": 2}
    destination = tmp_path / "Pudge Audiobooks" / "狼と香辛料" / "Volume 02"
    audio = _touch(destination / "Audiobook Collection" / "[支倉 凍砂] 狼と香辛料" / "狼と香辛料II [B0C58KN4W9].m4b")
    api.config = SimpleNamespace(
        paths=SimpleNamespace(download_dirs=[str(tmp_path)]),
        library=SimpleNamespace(root_dir=str(tmp_path)),
    )
    api.light_novels = SimpleNamespace(state=lambda: {"books": [book]})
    linked = []

    class FakeAudiobooks:
        def link_for_light_novel(self, *args, **kwargs):
            return None

        def import_file(self, path, **kwargs):
            assert Path(path).resolve() == audio.resolve()
            assert kwargs["prepare_transcription"] is False
            return {"id": 88}

        def import_folder(self, *args, **kwargs):
            raise AssertionError("single m4b must stay a single audiobook")

        def link_light_novel(self, ln_book_id, audiobook_id, *, prepare_alignment):
            linked.append((ln_book_id, audiobook_id, prepare_alignment))
            return {"ok": True}

    api.audiobooks = FakeAudiobooks()
    import threading
    api._ln_audiobook_recovery_lock = threading.Lock()

    result = api._recover_light_novel_audiobook_downloads()

    assert result["recovered"] == 1
    assert linked == [(42, 88, True)]


def test_collection_volume_parser_does_not_read_subtitle_roman_suffix_as_volume() -> None:
    from pudge.audiobooks import audiobook_torrent_pack_plan

    rows = [
        {"index": 2, "name": "[支倉 凍砂] 狼と香辛料/[02] 狼と香辛料II [B0C58KN4W9].m4b", "size": 200},
        {"index": 11, "name": "[支倉 凍砂] 狼と香辛料/[11] 狼と香辛料XI Side ColorsII [B0CB3348SF].m4b", "size": 1100},
        {"index": 19, "name": "[支倉 凍砂] 狼と香辛料/[19] 狼と香辛料XIX Spring LogII [B0D2Z6Z8HR].m4b", "size": 1900},
    ]

    plan = audiobook_torrent_pack_plan(rows, series_title="狼と香辛料")
    volumes = {int(row["volume"]): row for row in plan["volumes"]}

    assert sorted(volumes) == [2, 11, 19]
    assert volumes[2]["file_ids"] == [2]
    assert volumes[11]["file_ids"] == [11]
    assert volumes[19]["file_ids"] == [19]
