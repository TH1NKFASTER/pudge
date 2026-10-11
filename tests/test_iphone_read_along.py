from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from pudge.database import Database
from media_tools import requires_ffmpeg


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _setup(service, tmp_path):
    source = tmp_path / "one.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=4", str(source)], check=True)
    text = "「おはよう。」と言った。\n[[PUDGE_LN_IMAGE_URL:x.png]]\n彼は笑った！本当？"
    with service.db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,4,0,0)",
            (str(source), "Book"),
        ).lastrowid
        conn.execute(
            'INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,0,?,"Book",4,0,4)',
            (book, str(source)),
        )
        conn.execute("CREATE TABLE IF NOT EXISTS ln_books(id INTEGER PRIMARY KEY,title TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_chapters(id INTEGER PRIMARY KEY AUTOINCREMENT,book_id INT,chapter_index INT,title TEXT,text TEXT)"
        )
        conn.execute("INSERT INTO ln_books VALUES(7,'Novel')")
        conn.execute("INSERT INTO ln_chapters(book_id,chapter_index,title,text) VALUES(7,1,'One',?)", (text,))
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at) VALUES(7,?,'stt',1,1)",
            (book,),
        )
    alignment_path = tmp_path / "alignment.json"
    alignment_path.write_text("{}")
    alignment = {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "normalized_length": 15,
                "start": 0.0,
                "end": 4.0,
                "anchors": [
                    {"offset": 0, "time": 0.0},
                    {"offset": 8, "time": 2.0},
                    {"offset": 15, "time": 4.0},
                ],
            }
        ],
    }
    calls = []
    service.audiobooks = SimpleNamespace(
        book=lambda _id, **_k: {},
        _embedded_cover_paths=lambda _p: None,
        _alignment_path=lambda ln, ab: alignment_path,
        _load_alignment=lambda ln, ab: calls.append(1) or alignment,
    )
    return book, calls


@requires_ffmpeg
def test_linked_light_novel_becomes_a_read_along_resource(service, tmp_path):
    _, calls = _setup(service, tmp_path)
    service.refresh_catalog()
    item = service.catalog()["items"][0]
    read_along = item["readAlong"]
    assert read_along["fileName"] == "readalong.json"
    output = service.resource(read_along["resourceId"], read_along["sha256"])
    chapter = json.loads(output.read_text(encoding="utf-8"))["chapters"][0]
    assert [s["text"] for s in chapter["sentences"]] == [
        "「おはよう。」",
        "と言った。",
        "彼は笑った！",
        "本当？",
    ]
    assert [s["p"] for s in chapter["sentences"]] == [0, 0, 1, 1]  # image paragraphs carry no text
    assert chapter["sentences"][-1]["end"] == 15  # same normalized clock as the Mac reader
    assert chapter["anchors"][0] == [0, 0] and chapter["anchors"][-1] == [4000, 15]
    # Unchanged alignment: cached, not rebuilt on the next catalog refresh.
    service.refresh_catalog()
    assert len(calls) == 1
    assert service.catalog()["items"][0]["readAlong"] == read_along


@requires_ffmpeg
def test_read_along_is_an_archivable_resource(service, tmp_path):
    from pudge.iphone_service import resources, validate_manifest

    _setup(service, tmp_path)
    service.refresh_catalog()
    manifest = service.catalog()["items"][0]
    assert manifest["readAlong"] in resources(manifest)
    validate_manifest(manifest)
    bad = dict(manifest, readAlong=dict(manifest["readAlong"], fileName="../x.json"))
    with pytest.raises(ValueError):
        validate_manifest(bad)


@requires_ffmpeg
def test_book_without_linked_novel_has_no_read_along(service, tmp_path):
    book, _ = _setup(service, tmp_path)
    with service.db.connect() as conn:
        conn.execute("DELETE FROM reading_audio_links")
    service.refresh_catalog()
    assert "readAlong" not in service.catalog()["items"][0]
