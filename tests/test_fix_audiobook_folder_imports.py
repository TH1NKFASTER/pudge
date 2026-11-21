"""Audiobook folders: junk files, one bad file, multi-disc drops, watched folders (behaviour).

* An AppleDouble ``._01.mp3`` (exFAT drive, NAS share) next to ``01.mp3`` made the
  whole folder fail ("Failed to read frame size"); one damaged file did the same.
* Dropping ``Book/CD1``, ``Book/CD2`` and ``cover.jpg`` imported no audiobook and
  turned the cover into a one-page manga; a collection folder imported nothing.
* A watched folder's BD batch with ``CDs/OST/*.flac`` became the audiobook "OST"
  and queued hours of speech-to-text; folder scans no longer transcribe anything.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import types
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.web_app import WebAppApi

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg/ffprobe"
)


def _mp3(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=440:d={seconds}", str(path)],
        check=True,
    )
    return path


def _appledouble(path: Path) -> Path:
    path.write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00" + b"Mac OS X        " + b"\0" * 4000)
    return path


def _service(tmp_path: Path) -> AudiobookService:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )
    service.transcriptions: list[int] = []  # type: ignore[misc]
    service.prepare_transcription = lambda book_id, **_kw: service.transcriptions.append(int(book_id))  # type: ignore[method-assign]
    service.auto_link_audiobook = lambda book_id: None  # type: ignore[method-assign]
    return service


@needs_ffmpeg
def test_appledouble_companion_does_not_break_folder_import(tmp_path: Path) -> None:
    folder = tmp_path / "Book"
    _mp3(folder / "01.mp3")
    _appledouble(folder / "._01.mp3")
    (folder / "__MACOSX").mkdir()
    _appledouble(folder / "__MACOSX" / "._01.mp3")
    service = _service(tmp_path)
    assert [path.name for path in service._folder_files(folder)] == ["01.mp3"]
    book = service.import_folder(folder, auto_link=False, prepare_transcription=False)
    assert [Path(item["path"]).name for item in service._file_rows(book["id"])] == ["01.mp3"]


@needs_ffmpeg
def test_one_unreadable_file_is_left_out_of_the_book(tmp_path: Path) -> None:
    folder = tmp_path / "Book"
    _mp3(folder / "01.mp3")
    (folder / "02.mp3").write_bytes(b"not audio at all" * 100)
    _mp3(folder / "03.mp3")
    service = _service(tmp_path)
    book = service.import_folder(folder, auto_link=False, prepare_transcription=False)
    rows = service._file_rows(book["id"])
    assert [Path(item["path"]).name for item in rows] == ["01.mp3", "03.mp3"]
    assert [item["file_index"] for item in rows] == [0, 1]
    assert rows[1]["start"] == pytest.approx(rows[0]["end"])


@needs_ffmpeg
def test_folder_of_only_unreadable_files_still_fails(tmp_path: Path) -> None:
    folder = tmp_path / "Book"
    folder.mkdir()
    (folder / "01.mp3").write_bytes(b"garbage" * 100)
    with pytest.raises(ValueError):
        _service(tmp_path).import_folder(folder, auto_link=False, prepare_transcription=False)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str):
        def call(*args: Any, **_kwargs: Any) -> Any:
            self.calls.append((name, args))
            if name == "import_image_groups":
                return [{"id": 1, "title": "cover"}]
            if name == "discover_image_files":
                from pudge.manga import MangaService

                return MangaService.discover_image_files(*args)
            if name == "shelf_keys":
                return {}
            if name == "import_dropped_media_folder":
                return []
            return {"id": 7, "title": "x"}

        return call


def _drop_api(tmp_path: Path) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.audiobooks = _service(tmp_path)
    api.manga = _Recorder()
    api.light_novels = _Recorder()
    api.manager = _Recorder()
    api.logger = logging.getLogger("test")
    api._drop_shelf_snapshot = lambda: {}
    api._dispatch_drop_event = lambda *args, **kwargs: None
    return api


@needs_ffmpeg
def test_dropped_multi_disc_folder_is_one_audiobook_and_no_manga(tmp_path: Path) -> None:
    book = tmp_path / "Some Audiobook"
    _mp3(book / "CD1" / "01.mp3")
    _mp3(book / "CD2" / "01.mp3")
    _appledouble(book / "CD2" / "._01.mp3")
    Image.new("RGB", (10, 10)).save(book / "cover.jpg")
    api = _drop_api(tmp_path)
    result = WebAppApi.import_dropped_paths(api, [str(book)])
    assert result["failed"] == []
    assert [(item["kind"], Path(item["source"]).name) for item in result["imports"]] == [
        ("audiobook", "Some Audiobook")
    ]
    audiobook_id = result["imports"][0]["id"]
    assert len(api.audiobooks._file_rows(audiobook_id)) == 2
    assert not any(name in {"import_image_groups", "import_images"} for name, _ in api.manga.calls)


@needs_ffmpeg
def test_dropped_collection_folder_imports_each_book(tmp_path: Path) -> None:
    shelf = tmp_path / "Audiobooks"
    _mp3(shelf / "Book A" / "01.mp3")
    _mp3(shelf / "Book B" / "01.mp3")
    _mp3(shelf / "Book B" / "02.mp3")
    api = _drop_api(tmp_path)
    result = WebAppApi.import_dropped_paths(api, [str(shelf)])
    assert sorted(item["title"] for item in result["imports"] if item["kind"] == "audiobook") == [
        "Book A",
        "Book B",
    ]
    assert result["failed"] == []


@needs_ffmpeg
def test_dropped_anime_folder_with_soundtrack_stays_anime(tmp_path: Path) -> None:
    release = tmp_path / "[Group] Show"
    release.mkdir()
    (release / "Show - 01.mkv").write_bytes(b"x")
    _mp3(release / "OST" / "01.mp3")
    api = _drop_api(tmp_path)
    WebAppApi.import_dropped_paths(api, [str(release)])
    assert [name for name, _ in api.manager.calls] == ["import_dropped_media_folder"]
    with api.audiobooks.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audiobooks").fetchone()[0] == 0


class _WatchedRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.state: dict[str, str] = {}

    def import_folder(self, target: Path, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("import_folder", target.name, kwargs))
        return {"id": 1}

    def import_file(self, target: Path, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("import_file", target.name, kwargs))
        return {"id": 1}

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value


def _watched_api(root: Path) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.config = types.SimpleNamespace(
        paths=types.SimpleNamespace(download_dirs=[root], max_scanned_files=8000),
        library=types.SimpleNamespace(recursive=True),
    )
    api.audiobooks = _WatchedRecorder()
    api.manga = _WatchedRecorder()
    api.light_novels = _WatchedRecorder()
    api.manager = types.SimpleNamespace(db=_WatchedRecorder())
    api.logger = logging.getLogger("test")
    return api


def test_watched_folder_skips_a_video_releases_soundtrack(tmp_path: Path) -> None:
    root = tmp_path / "Anime"
    batch = root / "[Group] Show BD-BOX"
    (batch / "CDs" / "OST").mkdir(parents=True)
    for index in range(1, 4):
        (batch / f"Show - {index:02d}.mkv").write_bytes(b"x")
        (batch / "CDs" / "OST" / f"{index:02d} Theme.flac").write_bytes(b"x")
    side = root / "[Other] Movie"
    side.mkdir()
    (side / "Movie.mkv").write_bytes(b"x")
    (side / "Bonus Track.flac").write_bytes(b"x")
    api = _watched_api(root)
    WebAppApi.scan_watched_media_folders(api)
    assert api.audiobooks.calls == []


def test_watched_folder_audiobooks_are_not_transcribed_and_junk_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "Downloads"
    book = root / "Some Audiobook"
    book.mkdir(parents=True)
    for index in range(1, 3):
        (book / f"{index:02d}.mp3").write_bytes(b"x")
        _appledouble(book / f"._{index:02d}.mp3")
    (root / "Single.m4b").write_bytes(b"x")
    (root / "Show - 01.mkv").write_bytes(b"x")  # loose videos in the watched folder itself
    _appledouble(root / "._Single.m4b")
    api = _watched_api(root)
    WebAppApi.scan_watched_media_folders(api)
    assert sorted(api.audiobooks.calls) == [
        ("import_file", "Single.m4b", {"prepare_transcription": False}),
        ("import_folder", "Some Audiobook", {"prepare_transcription": False}),
    ]
