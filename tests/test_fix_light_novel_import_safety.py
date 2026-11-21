"""Light novel imports: same-named volumes, oversized EPUBs, one parse, illustrations (behaviour).

* ``Vol1/book.epub`` and ``Vol2/book.epub`` of one series both went to
  ``Light Novels/<series>/book.epub``: the second copy overwrote the first and took
  over its row and reading place. It now gets its own name.
* One XHTML listed many times in the spine was parsed and stored once per listing
  (a 20 KB EPUB grew the library by 67 MB); a book's text is also capped.
* The library and watched-folder scans parsed every new book twice (language
  check, then import).
* Inline illustrations live only in the cover cache: once it is emptied they are
  read again from the EPUB; they get no library-list thumbnail.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

import pytest
from PIL import Image

import pudge.light_novels as light_novels
from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService

BODY = "第一章の本文です。彼は静かに窓の外を見ていた。" * 20


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.library.cover_cache_dir = tmp_path / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    for folder in (config.library.root_dir, config.paths.cache_dir, config.library.cover_cache_dir):
        folder.mkdir(parents=True, exist_ok=True)
    return LightNovelService(config)


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), color).save(buffer, "PNG")
    return buffer.getvalue()


def _epub(
    path: Path,
    title: str,
    documents: list[str],
    *,
    spine: list[int] | None = None,
    image: bytes | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = "".join(
        f'<item id="d{index}" href="d{index}.xhtml" media-type="application/xhtml+xml"/>'
        for index in range(len(documents))
    )
    if image is not None:
        items += '<item id="pic" href="images/pic.png" media-type="image/png"/>'
    refs = "".join(f'<itemref idref="d{index}"/>' for index in (spine or range(len(documents))))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "c.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title></metadata><manifest>{items}</manifest><spine>{refs}</spine></package>",
        )
        for index, body in enumerate(documents):
            picture = '<p><img src="images/pic.png"/></p>' if image is not None and index == 0 else ""
            archive.writestr(
                f"d{index}.xhtml",
                f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{body}</p>{picture}</body></html>',
            )
        if image is not None:
            archive.writestr("images/pic.png", image)
    return path


def test_second_volume_with_the_same_file_name_gets_its_own_copy(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = _epub(tmp_path / "dl1" / "book.epub", "Mahou no Hon Vol. 1", ["第一巻の本文です。" * 20])
    second = _epub(tmp_path / "dl2" / "book.epub", "Mahou no Hon Vol. 2", ["第二巻の本文です。" * 20])
    one = service.import_file(first)
    service.save_bookmark(one["id"], 0, 0.7)
    two = service.import_file(second)
    again = service.import_file(second)
    assert one["id"] != two["id"] and again["id"] == two["id"]
    rows = (
        sqlite3.connect(tmp_path / "db.sqlite3")
        .execute("SELECT title,volume,file_path,current_offset FROM ln_books ORDER BY id")
        .fetchall()
    )
    assert [(title, volume, Path(file).name) for title, volume, file, _offset in rows] == [
        ("Mahou no Hon Vol. 1", 1, "book.epub"),
        ("Mahou no Hon Vol. 2", 2, "book (2).epub"),
    ]
    assert rows[0][3] == pytest.approx(0.7)
    assert Path(rows[0][2]).read_bytes() == first.read_bytes()


def test_volumes_named_only_by_their_folder_get_their_own_copies(tmp_path: Path) -> None:
    service = _service(tmp_path)
    one = service.import_file(_epub(tmp_path / "Vol 1" / "book.epub", "Mahou no Hon", ["一巻。" * 50]))
    two = service.import_file(_epub(tmp_path / "Vol 2" / "book.epub", "Mahou no Hon", ["二巻。" * 50]))
    assert one["id"] != two["id"]
    assert Path(one["file_path"]).read_bytes() != Path(two["file_path"]).read_bytes()


def test_an_updated_copy_of_the_same_volume_still_replaces_it(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = _epub(tmp_path / "dl" / "book.epub", "Mahou no Hon Vol. 1", ["古い本文です。" * 20])
    one = service.import_file(source)
    _epub(source, "Mahou no Hon Vol. 1", ["新しい本文です。" * 20])
    updated = service.import_file(source)
    assert updated["id"] == one["id"]
    assert Path(updated["file_path"]).name == "book.epub"
    assert Path(updated["file_path"]).read_bytes() == source.read_bytes()


def test_a_document_repeated_in_the_spine_is_read_once(tmp_path: Path) -> None:
    source = _epub(tmp_path / "rep.epub", "繰り返し", [BODY], spine=[0] * 30)
    _title, chapters, _cover = light_novels._epub_metadata(source)
    assert sum(text.count("第一章の本文です。") for _name, text in chapters) == 20


def test_book_text_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(light_novels, "EPUB_TEXT_LIMIT", 1000)
    source = _epub(tmp_path / "long.epub", "長い本", [BODY, BODY.replace("第一章", "第二章"), BODY])
    with pytest.raises(LightNovelError, match="million characters"):
        light_novels._epub_metadata(source)
    text = tmp_path / "long.txt"
    text.write_text(BODY * 3, encoding="utf-8")
    with pytest.raises(LightNovelError, match="million characters"):
        light_novels._txt_metadata(text)
    service = _service(tmp_path)
    assert service.is_probably_japanese_source(source) is False


def test_language_check_and_import_parse_a_new_book_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = light_novels._epub_metadata

    def counted(path: Path):
        calls.append(Path(path).name)
        return original(path)

    monkeypatch.setattr(light_novels, "_epub_metadata", counted)
    service = _service(tmp_path)
    source = _epub(service.root / "Series" / "vol1.epub", "シリーズ 1", [BODY])
    assert service.scan_downloaded() == 1
    assert calls == ["vol1.epub"]

    outside = _epub(tmp_path / "watched" / "vol2.epub", "シリーズ 2", [BODY])
    assert service.is_probably_japanese_source(outside)
    service.import_file(outside, explicit=False)
    assert calls == ["vol1.epub", "vol2.epub"]
    # The kept result is used once: a later import of the same file parses it again.
    service.import_file(outside, explicit=False)
    assert calls == ["vol1.epub", "vol2.epub", "vol2.epub"]
    assert source.is_file()


def test_missing_illustration_is_read_again_from_the_epub(tmp_path: Path) -> None:
    service = _service(tmp_path)
    picture = _png((10, 120, 200))
    book = service.import_file(_epub(tmp_path / "dl" / "illustrated.epub", "挿絵の本", [BODY], image=picture))
    with service._connection() as conn:
        text = conn.execute(
            "SELECT text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index", (book["id"],)
        ).fetchone()[0]
    names = [name for name in (tmp_path / "covers").iterdir() if name.suffix == ".png"]
    assert len(names) == 1 and f"covers/{names[0].name}" in text
    # An illustration is not a library cover: no list thumbnail for it.
    assert not names[0].with_name(names[0].name + ".list.jpg").exists()

    names[0].unlink()
    service._parse_text = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]
    service._schedule_parse_ahead = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    service.chapter_fast(book["id"], 0)
    assert names[0].read_bytes() == picture
    assert not names[0].with_name(names[0].name + ".list.jpg").exists()
