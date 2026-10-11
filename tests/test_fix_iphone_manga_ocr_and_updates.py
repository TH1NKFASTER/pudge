"""iPhone manga OCR on request, and updates of downloaded volumes.

A downloaded manga volume carries OCR only when the Mac's OCR of the whole volume is
complete (never part of it). "Get OCR" on the phone starts the Mac's volume OCR, follows
its progress, and the finished OCR comes as a small package of its own. The phone checks
its downloaded volumes now and then: the Mac answers their current revisions in one request.
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
from pudge.iphone_reading import read_package


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _image() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 12), "white").save(output, "PNG")
    return output.getvalue()


def _call(service, method, route, body=None):
    return service.media.handle(method, "/api/v2/media/" + route, {}, lambda: body or {})


def _wait(service, route):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = _call(service, "GET", route)
        if state["state"] != "preparing":
            return state
        time.sleep(0.02)
    raise AssertionError("package not prepared")


def _files(service, state):
    package = state["package"]
    return read_package(service.resource(package["resourceId"], package["sha256"]))


@pytest.fixture
def volume(service, tmp_path):
    archive = tmp_path / "vol.cbz"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("001.png", _image())
        handle.writestr("002.png", _image())
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(id,path,title,page_count,created_at,updated_at) VALUES(4,?,'よつばと 1',2,0,0)",
            (str(archive),),
        )
    ocr = {"complete": False, "running": False, "state": "idle", "total_pages": 2, "completed_pages": 1}
    started = []

    def start(book):
        started.append(book)
        ocr.update(running=True, state="running")

    regions = {0: [{"x": 0.1, "y": 0.7, "width": 0.2, "height": 0.2, "text": "何だ"}], 1: []}
    service.api = SimpleNamespace(
        light_novels=SimpleNamespace(
            settings=lambda: SimpleNamespace(study_backend="jiten", review_mode="binary"),
            parse_study_text=lambda text: {
                "tokens": [
                    [{"start": 0, "end": 1, "wordId": 9, "readingIndex": 0}] for _ in text.split("\n")
                ],
                "vocabulary": [],
            },
        ),
        manga=SimpleNamespace(
            _book=lambda book: {"path": str(archive)},
            _pages=lambda path: ["001.png", "002.png"],
            text_regions=lambda book, page, cached_only: {
                "page_index": page,
                "status": "ready",
                "regions": regions[page],
            },
            ocr_cache_status=lambda book: dict(ocr),
            _ocr_context=lambda book: ("fp", 1, ocr.get("revision", 1)),
        ),
        manga_ocr_book_status=lambda book: dict(ocr),
        start_manga_ocr_book=start,
        logger=None,
    )
    return SimpleNamespace(ocr=ocr, started=started, archive=archive)


def test_volume_without_complete_ocr_comes_with_pages_only(service, volume):
    _call(service, "POST", "manga/4/package")
    state = _wait(service, "manga/4/package")
    assert state["state"] == "ready" and state["ocrComplete"] is False and state["ocrRevision"] == ""
    files = _files(service, state)
    description = json.loads(files["package.json"])
    assert description["ocr"] is False and description["pages"] == ["page-0.png", "page-1.png"]
    # Half of the volume has OCR on the Mac: none of it goes to the phone.
    assert not [name for name in files if name.startswith("text-") or name == "parsed.json"]
    # The OCR-only package is refused until the volume is complete.
    from pudge.iphone_media import MediaError

    with pytest.raises(MediaError):
        _call(service, "POST", "manga/4/ocr-package")


def test_get_ocr_starts_the_volume_and_the_finished_ocr_comes_alone(service, volume):
    _call(service, "POST", "manga/4/package")
    pages = _wait(service, "manga/4/package")
    status = _call(service, "POST", "manga/4/ocr")
    assert volume.started == [4]
    assert status["running"] is True and status["complete"] is False and status["progress"] == 0.5
    assert status["ocrRevision"] == "" and "fingerprint" not in status
    # Complete pages but still running (words being parsed): not complete yet.
    volume.ocr.update(complete=True, completed_pages=2)
    assert _call(service, "GET", "manga/4/ocr")["complete"] is False
    volume.ocr.update(running=False, state="ready")
    finished = _call(service, "GET", "manga/4/ocr")
    assert finished["complete"] is True and finished["ocrRevision"] and finished["progress"] == 1.0
    # Asking again does not restart a finished OCR.
    _call(service, "POST", "manga/4/ocr")
    assert volume.started == [4]
    _call(service, "POST", "manga/4/ocr-package")
    state = _wait(service, "manga/4/ocr-package")
    assert state["state"] == "ready" and state["revision"] == finished["ocrRevision"]
    files = _files(service, state)
    assert sorted(files) == ["package.json", "parsed.json", "text-0.json", "text-1.json"]  # no pages again
    assert json.loads(files["text-0.json"])["regions"][0]["text"] == "何だ"
    assert json.loads(files["parsed.json"])["items"][0]["paragraphs"][0]["tokens"][0][:4] == [0, 1, 9, 0]
    # The update check: same pages, new OCR.
    [checked] = _call(service, "POST", "downloads/check", {"items": [{"kind": "manga", "id": 4}]})["items"]
    assert (
        checked["pagesRevision"] == pages["pagesRevision"]
        and checked["ocrRevision"] == finished["ocrRevision"]
    )
    assert checked["revision"] != pages["revision"]
    # Corrected OCR on the Mac is a new OCR revision.
    volume.ocr["revision"] = 2
    assert _call(service, "GET", "manga/4/ocr")["ocrRevision"] != finished["ocrRevision"]


def test_get_ocr_explains_when_the_mac_cannot_run_it(service, volume):
    def unavailable(book):
        raise RuntimeError("Mokuro is not installed")

    service.api.start_manga_ocr_book = unavailable
    from pudge.iphone_media import MediaError

    with pytest.raises(MediaError, match="Mokuro is not installed"):
        _call(service, "POST", "manga/4/ocr")


def test_update_check_answers_many_volumes_and_marks_removed_ones(service, volume):
    with service.db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_books(id INTEGER PRIMARY KEY,title TEXT,volume INT,current_chapter INT,"
            "current_offset REAL,finished INT,cover_url TEXT,updated_at REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_chapters(id INTEGER PRIMARY KEY AUTOINCREMENT,book_id INT,chapter_index INT,"
            "title TEXT,text TEXT,text_hash TEXT)"
        )
        conn.execute("INSERT INTO ln_books VALUES(3,'本',1,0,0,0,'',1)")
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(3,0,'一','猫','h0')"
        )
    body = {
        "items": [
            {"kind": "ln", "id": 3},
            {"kind": "manga", "id": 4},
            {"kind": "ln", "id": 77},
            {"kind": "x", "id": 1},
        ]
    }
    first = _call(service, "POST", "downloads/check", body)["items"]
    assert [(row["kind"], row["id"]) for row in first] == [("ln", 3), ("manga", 4), ("ln", 77)]
    assert {k: v for k, v in first[2].items() if k != "deletedAt"} == {
        "kind": "ln",
        "id": 77,
        "missing": True,
    }
    assert first[2]["deletedAt"] > 0  # when it was deleted (tests/test_fix_iphone_multi_device.py)
    assert first[1]["ocrComplete"] is False and first[1]["pagesRevision"]
    with service.db.connect() as conn:
        conn.execute("UPDATE ln_chapters SET text='犬',text_hash='h1'")
    again = _call(service, "POST", "downloads/check", body)["items"]
    assert again[0]["revision"] != first[0]["revision"] and again[1] == first[1]
    # A new archive file is new pages.
    with zipfile.ZipFile(volume.archive, "a") as handle:
        handle.writestr("003.png", _image())
    assert (
        _call(service, "POST", "downloads/check", body)["items"][1]["pagesRevision"]
        != first[1]["pagesRevision"]
    )
