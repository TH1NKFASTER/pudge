from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import audiobook_torrent_pack_plan


def test_volume_title_beats_nested_chapter_directories() -> None:
    files = [
        {"index": 0, "name": "[支倉 凍砂]/狼と香辛料/狼と香辛料II/01/001.mp3", "size": 100},
        {"index": 1, "name": "[支倉 凍砂]/狼と香辛料/狼と香辛料II/02/002.mp3", "size": 200},
        {"index": 2, "name": "[支倉 凍砂]/狼と香辛料/狼と香辛料III/01/001.mp3", "size": 300},
    ]
    plan = audiobook_torrent_pack_plan(files)
    assert [row["volume"] for row in plan["volumes"]] == [2, 3]
    assert plan["volumes"][0]["file_ids"] == [0, 1]
    assert plan["volumes"][1]["file_ids"] == [2]


def test_first_numeric_volume_directory_beats_deeper_chapter_number() -> None:
    files = [
        {"index": 0, "name": "Series/02/01/track-01.mp3", "size": 100},
        {"index": 1, "name": "Series/02/02/track-02.mp3", "size": 200},
        {"index": 2, "name": "Series/03/01/track-01.mp3", "size": 300},
    ]
    plan = audiobook_torrent_pack_plan(files)
    assert [row["volume"] for row in plan["volumes"]] == [2, 3]
    assert plan["volumes"][0]["file_ids"] == [0, 1]


def test_download_completion_imports_chapter_subfolders_as_one_named_volume(monkeypatch, tmp_path: Path) -> None:
    import pudge.web_app as web_app

    destination = tmp_path / "download"
    p1 = destination / "pack" / "狼と香辛料II" / "01" / "001.mp3"
    p2 = destination / "pack" / "狼と香辛料II" / "02" / "002.mp3"
    p1.parent.mkdir(parents=True)
    p2.parent.mkdir(parents=True)
    p1.write_bytes(b"a")
    p2.write_bytes(b"b")

    class FakeQbt:
        def __init__(self, *args, **kwargs):
            pass

        def files(self, torrent_hash: str):
            return [
                {"index": 4, "name": str(p1.relative_to(destination)), "progress": 1.0},
                {"index": 5, "name": str(p2.relative_to(destination)), "progress": 1.0},
            ]

        def delete(self, *args, **kwargs):
            pass

        def close(self):
            pass

    imported: list[tuple[Path, bool, str | None]] = []
    linked: list[tuple[int, int, bool]] = []

    class FakeAudiobooks:
        def import_folder(self, folder: Path, *, auto_link=True, prepare_transcription=True, title_override=None):
            imported.append((Path(folder), bool(auto_link), title_override))
            return {"id": 77}

        def import_file(self, *args, **kwargs):
            raise AssertionError("chapter tracks must be imported as one folder")

        def link_light_novel(self, book_id: int, audiobook_id: int, *, prepare_alignment=True):
            linked.append((book_id, audiobook_id, prepare_alignment))

        def auto_link_light_novel(self, book_id: int):
            raise AssertionError("explicit Nyaa import should not fall back to chapter-title auto-linking")

    class FakeLightNovels:
        @staticmethod
        def book(book_id: int):
            return {"id": book_id, "title": "狼と香辛料II", "volume": 2}

    monkeypatch.setattr(web_app, "QBittorrentClient", FakeQbt)
    api = object.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(qbittorrent=SimpleNamespace(
        base_url="http://127.0.0.1:8080", username="", password="", api_key="",
        verify_tls=True, pre_download_command="", auto_start_app=False,
    ))
    api.audiobooks = FakeAudiobooks()
    api.light_novels = FakeLightNovels()
    api.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)

    api._finish_light_novel_audiobook_torrent("hash", 180, destination, {4, 5})

    assert imported == [(destination / "pack" / "狼と香辛料II", False, "狼と香辛料II")]
    assert linked == [(180, 77, True)]
