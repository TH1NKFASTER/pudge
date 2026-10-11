"""Light novels and manga volumes downloaded to the iPhone for reading without the Mac.

The Mac packs a volume into one file in the background (chapters with Jiten words, the
linked audiobook, illustrations; manga pages, OCR text boxes and their words) and serves it
as a verified resource, like a prepared episode.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.database import Database
from pudge.iphone_reading import PackageWriter, read_package


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _image(kind="PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 12), "white").save(output, kind)
    return output.getvalue()


def _settings():
    return SimpleNamespace(study_backend="jiten", review_mode="binary")


def _wait(service, kind, item):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = service.media.handle("GET", f"/api/v2/media/{kind}/{item}/package", {}, dict)
        if state["state"] != "preparing":
            return state
        time.sleep(0.02)
    raise AssertionError("package not prepared")


def _download(service, state):
    package = state["package"]
    path = service.resource(package["resourceId"], package["sha256"])
    assert path.stat().st_size == package["sizeBytes"]
    return read_package(path)


def test_package_holds_named_files_and_rejects_unsafe_names(tmp_path):
    writer = PackageWriter(tmp_path / "work")
    writer.add("package.json", b'{"a":1}')
    writer.add("page-0.jpg", b"\xff\xd8 bytes")
    writer.add("empty.json", b"")
    with pytest.raises(ValueError):
        writer.add("../escape", b"x")
    with pytest.raises(ValueError):
        writer.add("page-0.jpg", b"again")
    target = tmp_path / "one.pudgepkg"
    writer.finish(target)
    assert read_package(target) == {
        "package.json": b'{"a":1}',
        "page-0.jpg": b"\xff\xd8 bytes",
        "empty.json": b"",
    }
    assert target.read_bytes()[:8] == b"PUDGEPK1"
    assert [p.name for p in (tmp_path / "work").iterdir()] == []  # no leftovers


def _light_novel(service, tmp_path):
    covers = tmp_path / "covers"
    covers.mkdir()
    image = "ln-" + "a" * 24 + ".png"
    (covers / image).write_bytes(_image())
    with service.db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_books(id INTEGER PRIMARY KEY,title TEXT,volume INT,current_chapter INT,"
            "current_offset REAL,finished INT,cover_url TEXT,updated_at REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_chapters(id INTEGER PRIMARY KEY AUTOINCREMENT,book_id INT,chapter_index INT,"
            "title TEXT,text TEXT,text_hash TEXT)"
        )
        conn.execute("INSERT INTO ln_books VALUES(3,'狼と香辛料 1',1,1,0.5,0,'',1)")
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(3,0,'一','猫だ','h0')"
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(3,1,'二','犬','h1')"
        )
    parsed = []

    def chapter(book, index):
        if index == 1:
            raise RuntimeError("Jiten offline")
        parsed.append((book, index))
        return {
            "chapter_index": index,
            "title": "一",
            "paragraphs": ["猫だ", f"[[PUDGE_LN_IMAGE_URL:covers/{image}]]"],
            "tokens": [[{"start": 0, "end": 1, "wordId": 5, "readingIndex": 0}], []],
            "vocabulary": [],
            "parsing": False,
        }

    service.api = SimpleNamespace(
        light_novels=SimpleNamespace(
            settings=_settings,
            state=lambda: {"books": []},
            chapter=chapter,
            chapter_fast=lambda book, index: {
                "chapter_index": index,
                "title": "二",
                "paragraphs": ["犬"],
                "tokens": [],
                "vocabulary": [],
                "parsing": True,
            },
            cover_cache_dir=str(covers),
        ),
        logger=None,
    )
    return image, parsed


def test_light_novel_volume_is_packed_with_words_illustrations_and_its_audiobook(service, tmp_path):
    image, parsed = _light_novel(service, tmp_path)
    first = service.media.handle("POST", "/api/v2/media/ln/3/package", {}, dict)
    assert first["state"] in {"preparing", "ready"} and first["kind"] == "ln"
    state = _wait(service, "ln", 3)
    assert state["state"] == "ready" and state["progress"] == 1.0, state
    files = _download(service, state)
    description = json.loads(files["package.json"])
    assert description["kind"] == "ln" and description["revision"] == state["revision"]
    assert description["book"]["chapters"] == [
        {"index": 0, "title": "一", "characters": 2},
        {"index": 1, "title": "二", "characters": 1},
    ]
    assert description["book"]["chapter"] == 1 and description["item"]["title"] == "狼と香辛料 1"
    assert description["images"] == [image]
    one = json.loads(files["chapter-0.json"])
    assert one["parsing"] is False and one["paragraphs"][0]["tokens"][0][:4] == [0, 1, 5, 0]
    assert one["paragraphs"][1] == {"text": "", "image": True, "tokens": [], "ruby": [], "imageId": image}
    # Jiten could not parse chapter 2: its plain text is still in the volume.
    two = json.loads(files["chapter-1.json"])
    assert two["parsing"] is False and two["paragraphs"][0]["text"] == "犬"
    assert Image.open(io.BytesIO(files[f"image-{image}"])).size == (8, 12)
    assert parsed == [(3, 0)]
    # Ready packages are not prepared again; changed text needs a new one.
    assert service.media.reading.prepare("ln", 3)["state"] == "ready"
    with service.db.connect() as conn:
        conn.execute("UPDATE ln_chapters SET text='犬だ',text_hash='h1b' WHERE chapter_index=1")
    assert service.media.reading.state("ln", 3)["state"] == "idle"
    service.media.reading.prepare("ln", 3)
    assert _wait(service, "ln", 3)["revision"] != state["revision"]


def test_linked_audiobook_comes_with_the_light_novel(service, tmp_path):
    _light_novel(service, tmp_path)
    with service.db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES('/a.m4b','Book',4,0,0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at) VALUES(3,?,'stt',1,1)",
            (book,),
        )
    service.asset_for_book = lambda audiobook: "asset-" + str(audiobook)
    service.media.reading.prepare("ln", 3)
    files = _download(service, _wait(service, "ln", 3))
    description = json.loads(files["package.json"])
    assert description["book"]["audioAssetId"] == f"asset-{book}" and description["item"]["hasAudio"] is True


def test_manga_volume_is_packed_with_pages_text_boxes_and_their_words(service, tmp_path):
    archive = tmp_path / "vol.cbz"
    pages = ["001.jpg", "002.png"]
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("001.jpg", _image("JPEG"))
        handle.writestr("002.png", _image())
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(id,path,title,page_count,created_at,updated_at) VALUES(4,?,'よつばと 1',2,0,0)",
            (str(archive),),
        )
    parses = []

    def parse(text):
        parses.append(text)
        lines = text.split("\n")
        return {
            "tokens": [[{"start": 0, "end": 1, "wordId": 9, "readingIndex": 0}] for _ in lines],
            "vocabulary": [],
        }

    regions = {0: [{"x": 0.1, "y": 0.7, "width": 0.2, "height": 0.2, "text": "何\nだ"}], 1: []}
    service.api = SimpleNamespace(
        light_novels=SimpleNamespace(settings=_settings, parse_study_text=parse),
        manga=SimpleNamespace(
            _book=lambda book: {"path": str(archive)},
            _pages=lambda path: pages,
            text_regions=lambda book, page, cached_only: {
                "page_index": page,
                "status": "ready" if regions[page] else "missing",
                "regions": regions[page],
            },
            ocr_cache_status=lambda book: {"complete": True, "total_pages": 2, "completed_pages": 2},
        ),
        logger=None,
    )
    service.media.reading.prepare("manga", 4)
    state = _wait(service, "manga", 4)
    assert state["state"] == "ready", state
    files = _download(service, state)
    description = json.loads(files["package.json"])
    assert description["pages"] == ["page-0.jpg", "page-1.png"] and description["item"]["pages"] == 2
    assert description["ocr"] is True and description["ocrRevision"]  # the whole volume has OCR
    assert files["page-1.png"] == _image()
    text = json.loads(files["text-0.json"])
    assert text["regions"][0]["text"] == "何\nだ" and text["regions"][0]["y"] == pytest.approx(0.1)
    assert json.loads(files["text-1.json"])["regions"] == []
    # One Jiten parse for all text boxes; a line break inside a box is parsed as a space.
    assert parses == ["何 だ"]
    [item] = json.loads(files["parsed.json"])["items"]
    assert item["text"] == "何\nだ" and item["paragraphs"][0]["tokens"][0][:4] == [0, 1, 9, 0]
    assert "cover.jpg" in files


def test_unknown_or_missing_volumes_are_refused(service):
    from pudge.iphone_media import MediaError

    service.api = SimpleNamespace(light_novels=SimpleNamespace(settings=_settings), logger=None)
    with pytest.raises(KeyError):
        service.media.handle("POST", "/api/v2/media/anime/1/package", {}, dict)
    with pytest.raises(MediaError):
        service.media.handle("POST", "/api/v2/media/manga/99/package", {}, dict)
