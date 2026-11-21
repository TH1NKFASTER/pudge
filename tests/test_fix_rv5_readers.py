"""Reading stack review (rv5): light novels and manga. Invented text only.

1. A Windows-made Japanese book (cp932: ①, Ⅱ, ㈱, 髙 ...) declared "Shift_JIS", or an
   EPUB document with no or an unknown declaration (x-sjis) in Shift_JIS/EUC-JP, was read
   as UTF-8: the whole chapter became U+FFFD. TXT books with such characters were refused.
2. An EPUB illustration that could not be read left its internal marker
   ``[[PUDGE_EPUB_IMAGE:...]]`` in the chapter: shown as text and sent to Jiten.
3. Token offsets are UTF-16 (Jiten, jpdb, the reader): a character outside the BMP (𠮟)
   shifted every later word position of a chapter in Python (statistics, read-along).
4. A long Japanese title made a folder name over 255 bytes: the import failed.
5. A copy cut off half-way replaced the library's copy of a book.
6. Parse-ahead read every later chapter's text and unpacked each stored parse on every
   chapter opened; now it only checks the cache keys and reads text when it parses.
7. A manga archive imported again with fewer pages kept a place past its last page.
"""

from __future__ import annotations

import io
import shutil
import threading
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pudge import light_novels
from pudge.archive_limits import ArchiveTooLargeError
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import (
    LightNovelService,
    _decode_markup,
    _epub_metadata,
    _safe_name,
    _txt_metadata,
    chapter_word_positions_from,
)
from pudge.manga import MangaService

_CONTAINER = (
    '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/c.opf"/></rootfiles></container>'
)
_BODY = "春の朝、少女は丘の上で風を待っていた。遠くの町から鐘の音が聞こえる。" * 8
_WINDOWS = "第Ⅱ章　髙村は①番目の扉を開けた。㈱山野の看板が見える。"


def _opf(items: str, spine: str) -> str:
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>架空の本</dc:title>'
        f"</metadata><manifest>{items}</manifest><spine>{spine}</spine></package>"
    )


def _epub(path: Path, files: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", _CONTAINER)
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def _one_document(path: Path, document: bytes, extra: dict[str, bytes] | None = None) -> Path:
    item = '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
    return _epub(
        path, {"OEBPS/c.opf": _opf(item, '<itemref idref="a"/>'), "OEBPS/a.xhtml": document, **(extra or {})}
    )


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


# ---------------------------------------------------------------------------- 1. encodings
def test_windows_characters_in_a_shift_jis_declared_document(tmp_path: Path) -> None:
    document = (
        '<?xml version="1.0" encoding="Shift_JIS"?><html xmlns="http://www.w3.org/1999/xhtml">'
        f"<body><p>{_BODY}</p><p>{_WINDOWS}</p></body></html>"
    ).encode("cp932")
    _title, chapters, _cover = _epub_metadata(_one_document(tmp_path / "w.epub", document))
    text = chapters[0][1]
    # (the extractor turns the ideographic space into a plain one)
    assert _WINDOWS.replace("\u3000", " ") in text and "\ufffd" not in text


@pytest.mark.parametrize("encoding,declaration", [("cp932", ""), ("euc_jp", ' encoding="x-euc-jp"')])
def test_undeclared_or_unknown_legacy_encoding(encoding: str, declaration: str) -> None:
    body = _BODY if encoding == "euc_jp" else _BODY + _WINDOWS
    raw = f"<?xml version='1.0'{declaration}?><html><body><p>{body}</p></body></html>".encode(encoding)
    assert body in _decode_markup(raw)


def test_damaged_utf8_stays_utf8() -> None:
    raw = bytearray(f"<html><body><p>{_BODY}</p></body></html>".encode("utf-8"))
    raw[60] = 0xFF
    text = _decode_markup(bytes(raw))
    assert 0 < text.count("\ufffd") <= 3 and text.count("遠くの町から鐘の音") == 8


def test_windows_made_txt_book(tmp_path: Path) -> None:
    source = tmp_path / "本.txt"
    source.write_bytes((_BODY + "\r\n" + _WINDOWS).encode("cp932"))
    _title, chapters = _txt_metadata(source)
    assert chapters[0][1] == _BODY + "\n" + _WINDOWS


# ------------------------------------------------------------- 2. unreadable illustrations
def test_unreadable_illustration_leaves_no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, "PNG")
    document = (
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f'<p>{_BODY}</p><img src="good.png"/><p>{_BODY}</p><img src="bad.png"/><img src="gone.png"/>'
        "</body></html>"
    ).encode("utf-8")
    path = _one_document(
        tmp_path / "絵.epub",
        document,
        {"OEBPS/good.png": buffer.getvalue(), "OEBPS/bad.png": buffer.getvalue()},
    )
    real = light_novels.read_member

    def read_member(archive, name, **kwargs):
        if str(name).endswith("bad.png"):
            raise ArchiveTooLargeError("too large")
        return real(archive, name, **kwargs)

    monkeypatch.setattr(light_novels, "read_member", read_member)
    service = LightNovelService(_cfg(tmp_path))
    try:
        book = service.import_file(path)
        text = str(service.chapter_fast(int(book["id"]), 0)["text"])
    finally:
        service.close()
    assert "PUDGE_EPUB_IMAGE" not in text
    assert text.count("[[PUDGE_LN_IMAGE_URL:covers/") == 1


# -------------------------------------------------------------- 3. UTF-16 token offsets
def test_word_positions_count_a_surrogate_pair_once() -> None:
    paragraph = "𠮟られた猫が走る"
    # JavaScript offsets: 𠮟 is two units, so 猫 starts at 5 (Python index 4).
    parsed = {
        "paragraphs": [paragraph],
        "tokens": [
            [
                {"start": 0, "end": 4, "wordId": 1, "readingIndex": 0},
                {"start": 5, "end": 6, "wordId": 2, "readingIndex": 0},
                {"start": 7, "end": 9, "wordId": 3, "readingIndex": 0},
            ]
        ],
    }
    text = "前書き\n" + paragraph
    positions = chapter_word_positions_from(text, parsed)
    assert [(text[start], word) for start, word, _reading in positions] == [("𠮟", 1), ("猫", 2), ("走", 3)]


def test_parse_coverage_counts_utf16_offsets() -> None:
    paragraph = "𠮟" + "あいうえおかきくけこさしすせそたちつてと"
    tokens = [{"start": 0, "end": len(paragraph) + 1}]  # the whole paragraph in UTF-16 units
    letters, coverage = LightNovelService._parse_coverage(paragraph, tokens)
    assert letters == 20 and coverage == 1.0


# ------------------------------------------------------------------ 4. long titles
def test_long_japanese_title_fits_a_folder_name(tmp_path: Path) -> None:
    title = "長い題名の物語" * 30
    name = _safe_name(title)
    assert len(name.encode("utf-8")) <= 255 and title.startswith(name)
    assert _safe_name("短い題名") == "短い題名"
    (tmp_path / name).mkdir()


# --------------------------------------------------------- 5. a copy cut off half-way
def test_failed_copy_keeps_the_library_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def book(path: Path, body: str) -> Path:
        return _one_document(path, f"<html><body><p>{body}</p></body></html>".encode("utf-8"))

    service = LightNovelService(_cfg(tmp_path))
    try:
        (tmp_path / "one").mkdir()
        first = service.import_file(book(tmp_path / "one" / "架空の本.epub", _BODY))
        library_copy = Path(first["file_path"])
        before = library_copy.read_bytes()
        (tmp_path / "two").mkdir()
        updated = book(tmp_path / "two" / "架空の本.epub", _BODY + "続き。")

        def broken_copy(source, target, *args, **kwargs):
            Path(target).write_bytes(b"PK half")
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(light_novels.shutil, "copy2", broken_copy)
        with pytest.raises(OSError):
            service.import_file(updated, on_conflict="replace")
    finally:
        service.close()
    assert library_copy.read_bytes() == before
    assert not [path for path in library_copy.parent.iterdir() if path.name.endswith(".tmp")]


# ------------------------------------------------------------------ 6. parse-ahead
def test_parse_ahead_reads_only_the_chapter_it_parses(tmp_path: Path) -> None:
    source = tmp_path / "本.txt"
    source.write_text("\n".join(["あ" * 100 + "。"] * 1000), encoding="utf-8")  # several parts
    service = LightNovelService(_cfg(tmp_path))
    try:
        book = service.import_file(source)
        service.save_settings({"parse_ahead": "next"})
        parsed: list[str] = []
        done = threading.Event()

        def parse(text, digest, **_kwargs):
            parsed.append(digest)
            done.set()
            return {"tokens": [], "vocabulary": [], "paragraphs": []}

        service._parse_text = parse  # type: ignore[method-assign]
        service._schedule_parse_ahead(int(book["id"]), 0)
        assert done.wait(10)
        chapters = service.book(int(book["id"]))["chapters"]
        assert parsed == [chapters[1]["text_hash"]]
    finally:
        service.close()


# ----------------------------------------------------------- 7. manga imported again
def _cbz(path: Path, pages: int) -> Path:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), (200, 200, 200)).save(buffer, "JPEG")
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(pages):
            archive.writestr(f"{index:03d}.jpg", buffer.getvalue())
    return path


def test_manga_imported_again_with_fewer_pages_keeps_its_place_inside(tmp_path: Path) -> None:
    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    path = _cbz(tmp_path / "架空 01.cbz", 10)
    book = service.import_file(path)
    service.set_position(int(book["id"]), 8)
    service.mark_read(int(book["id"]), 8)
    time.sleep(0.01)
    _cbz(path, 4)
    again = service.import_file(path)
    assert again["id"] == book["id"]
    assert again["page_count"] == 4 and again["position"] == 3 and again["read_pages"] == 4
    shutil.rmtree(tmp_path / "cache", ignore_errors=True)


# ------------------------------------------- 8. a Mac reader left open behind the phone
def _two_part_book(tmp_path: Path) -> tuple[LightNovelService, int]:
    source = tmp_path / "置き場.txt"
    source.write_text("\n".join(["い" * 100 + "。"] * 400), encoding="utf-8")  # two parts
    service = LightNovelService(_cfg(tmp_path))
    return service, int(service.import_file(source)["id"])


def test_an_automatic_save_behind_a_newer_place_elsewhere_is_kept_out(tmp_path: Path) -> None:
    service, book = _two_part_book(tmp_path)
    try:
        assert len(service.book(book)["chapters"]) >= 2
        service.save_bookmark(book, 0, 0.5, source="auto", known_at=0.0)
        mac_knows = service.progress_summary(book)["position_updated_at"]
        time.sleep(0.01)
        service.set_position(book, 1, 0.5)  # the phone read on (iphone_media -> light_novel_position)
        time.sleep(0.01)
        # The Mac reader, still open on part 1, saves after its 20 s dwell: kept out.
        stale = service.save_bookmark(book, 0, 0.6, source="auto", known_at=mac_knows)
        place = service.progress_summary(book)
        assert stale["kept"] == "newer" and (place["current_chapter"], place["current_offset"]) == (1, 0.5)
        # Further ahead counts; so does "Read up to here" and a save without a version (old pages).
        assert "kept" not in service.save_bookmark(book, 1, 0.7, source="auto", known_at=mac_knows)
        assert "kept" not in service.save_bookmark(book, 0, 0.1, source="manual", known_at=mac_knows)
        assert service.progress_summary(book)["current_chapter"] == 0
        assert "kept" not in service.save_bookmark(book, 0, 0.0, source="auto")
    finally:
        service.close()


READER = r"""
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, position_updated_at: 123.5,
  chapters: [{chapter_index: 0, title: 'One'}, {chapter_index: 1, title: 'Two'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, text: `章${index}`,
  paragraphs: [`章${index}`], tokens: [[]], vocabulary: []});
__api.light_novel_bookmark = async (id, chapter, offset) => ({ok: true, current_chapter: chapter, current_offset: offset,
  position_updated_at: 200.25});
"""


def test_mac_reader_sends_the_place_version_it_knows(page) -> None:
    result = page.run(
        r"""
        const saves = () => __apiCalls.filter(call => call.name === 'light_novel_bookmark').map(call => call.args);
        await __settle(openLightNovel(6)); await __tick(21000);
        const first = saves().at(-1);
        await __tick(100);
        await __settle(loadLightNovelChapter(1, 0)); await __tick(21000);   // opened by hand here
        return {first, after: saves().at(-1), version: ui.lnBook.position_updated_at};
        """,
        before=READER,
    )
    first, after = result.value["first"], result.value["after"]
    assert first[:2] == [6, 0] and first[3] == "auto" and first[5] == 123.5
    assert after[:2] == [6, 1] and after[3] == "auto" and len(after) <= 5  # its own place: no version
    assert result.value["version"] == 200.25  # a save's answer is the version it knows next


# ----------------------------------------- 9. finishing a volume while AniList is down
def test_finishing_a_volume_does_not_wait_for_anilist(tmp_path: Path) -> None:
    service, book = _two_part_book(tmp_path)
    service.config.anilist.enabled = True
    service.config.anilist.access_token = "invented-token"
    with service._connection() as conn:
        conn.execute("UPDATE ln_books SET anilist_id=77,anilist_total_volumes=3 WHERE id=?", (book,))
    release = threading.Event()
    sent: list[tuple[int, int, str]] = []

    def offline(media_id, progress, status=None):
        release.wait(5)
        raise light_novels.httpx.ConnectError("offline")

    service._save_anilist_volume = offline  # type: ignore[method-assign]

    def pending() -> list[tuple]:
        with service._connection() as conn:
            return [
                tuple(row)
                for row in conn.execute(
                    "SELECT media_id,progress_volumes,status,attempts FROM ln_anilist_pending"
                )
            ]

    try:
        started = time.monotonic()
        finished = service.finish_volume(book)
        assert time.monotonic() - started < 2 and finished["finished"] == 1  # not waiting on AniList
        release.set()
        deadline = time.monotonic() + 5
        while not (pending() and pending()[0][3] >= 1) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pending()[0][:3] == (77, 1, "CURRENT")
        service._save_anilist_volume = lambda media_id, progress, status=None: (  # type: ignore[method-assign]
            sent.append((media_id, progress, status)) or {"status": "CURRENT"}
        )
        deadline = time.monotonic() + 5
        while service.__dict__.get("_anilist_volume_running") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert service.flush_anilist_volumes() == 1  # the next library refresh sends it
        assert sent == [(77, 1, "CURRENT")] and pending() == []
    finally:
        release.set()
        service.close()


# -------------------------------------------------------- 10. oversized illustrations
def test_oversized_illustration_is_left_out_at_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def png(size: tuple[int, int]) -> bytes:
        buffer = io.BytesIO()
        Image.new("L", size, 128).save(buffer, "PNG")
        return buffer.getvalue()

    monkeypatch.setattr(light_novels, "LN_ILLUSTRATION_MAX_PIXELS", 1000)
    document = (
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f'<p>{_BODY}</p><img src="small.png"/><p>{_BODY}</p><img src="vast.png"/></body></html>'
    ).encode("utf-8")
    path = _one_document(
        tmp_path / "絵2.epub", document, {"OEBPS/small.png": png((8, 8)), "OEBPS/vast.png": png((100, 100))}
    )
    service = LightNovelService(_cfg(tmp_path))
    try:
        book = service.import_file(path)
        text = str(service.chapter_fast(int(book["id"]), 0)["text"])
    finally:
        service.close()
    assert text.count("[[PUDGE_LN_IMAGE_URL:covers/") == 1 and "PUDGE_EPUB_IMAGE" not in text


def test_phone_illustration_over_the_pixel_limit_is_a_media_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from PIL import Image as PILImage

    from pudge.iphone_media import IPhoneMedia, MediaError

    name = "ln-" + "a" * 24 + ".png"
    Image.new("L", (100, 100), 0).save(tmp_path / name, "PNG")
    monkeypatch.setattr(PILImage, "MAX_IMAGE_PIXELS", 1000)  # 100 x 100 is a "bomb" now
    media = SimpleNamespace(
        service=SimpleNamespace(api=SimpleNamespace(light_novels=SimpleNamespace(cover_cache_dir=tmp_path))),
        _thumbnail=IPhoneMedia._thumbnail,
    )
    with pytest.raises(MediaError):
        IPhoneMedia.ln_image(media, name)


# ------------------------------------------------------- 11. manga page list cache
def test_archive_page_list_is_read_once_until_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from pudge import manga

    path = _cbz(tmp_path / "一覧 01.cbz", 5)
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert len(MangaService._pages(path)) == 5
    opened: list[str] = []
    real = manga.zipfile.ZipFile

    class Counting(real):  # type: ignore[misc, valid-type]
        def __init__(self, file, *args, **kwargs):
            opened.append(str(file))
            super().__init__(file, *args, **kwargs)

    monkeypatch.setattr(manga.zipfile, "ZipFile", Counting)
    pages = MangaService._pages(path)
    pages.append("changed by a caller.jpg")
    assert MangaService._pages(path) == [f"{index:03d}.jpg" for index in range(5)] and opened == []
    monkeypatch.setattr(manga.zipfile, "ZipFile", real)
    _cbz(path, 3)
    os.utime(path, (old + 10, old + 10))
    assert len(MangaService._pages(path)) == 3  # another size and date: listed again
