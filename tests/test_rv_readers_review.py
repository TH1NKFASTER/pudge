"""Readers review (fx196): legacy-encoded EPUB/TXT, per-image EPUB failures,
reduced-scale cover thumbnails, no .tmp left by a failed write. Invented data only."""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pudge import light_novels, manga, manga_ocr_artifact
from pudge.archive_limits import ArchiveTooLargeError
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService, _epub_metadata, _txt_metadata
from pudge.manga import MangaService

_CONTAINER = (
    '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/c.opf"/></rootfiles></container>'
)
_BODY = "吾輩は猫である。名前はまだ無い。" * 12


def _opf(items: str, spine: str, extra: str = "") -> str:
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>試験の本</dc:title>'
        f"{extra}</metadata><manifest>{items}</manifest><spine>{spine}</spine></package>"
    )


def _epub(path: Path, files: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", _CONTAINER)
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def _one_document_epub(path: Path, document: bytes) -> Path:
    item = '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
    return _epub(path, {"OEBPS/c.opf": _opf(item, '<itemref idref="a"/>'), "OEBPS/a.xhtml": document})


def test_shift_jis_epub_document_imports_with_its_text(tmp_path: Path) -> None:
    # expat refuses multi-byte legacy encodings ("multi-byte encodings are not
    # supported" ValueError): the whole book failed to import.
    document = (
        '<?xml version="1.0" encoding="Shift_JIS"?><html xmlns="http://www.w3.org/1999/xhtml">'
        f"<head><title>本</title></head><body><h1>第一章</h1><p>{_BODY}</p></body></html>"
    ).encode("shift_jis")
    title, chapters, _cover = _epub_metadata(_one_document_epub(tmp_path / "sjis.epub", document))
    assert title == "試験の本"
    assert [name for name, _text in chapters] == ["第一章"]
    assert _BODY in chapters[0][1] and "�" not in chapters[0][1]


def test_utf16_epub_document_is_not_read_as_utf8(tmp_path: Path) -> None:
    document = (
        '<?xml version="1.0" encoding="UTF-16"?><html xmlns="http://www.w3.org/1999/xhtml">'
        f"<body><p>{_BODY}</p></body></html>"
    ).encode("utf-16")
    _title, chapters, _cover = _epub_metadata(_one_document_epub(tmp_path / "u16.epub", document))
    assert _BODY in chapters[0][1] and "�" not in chapters[0][1]


def test_shift_jis_document_split_at_toc_fragments(tmp_path: Path) -> None:
    # Sections of one spine document were re-encoded as UTF-8 with the Shift_JIS
    # declaration still inside: each slice is now parsed as already-decoded text.
    document = (
        '<?xml version="1.0" encoding="Shift_JIS"?><html xmlns="http://www.w3.org/1999/xhtml">'
        '<head><meta charset="Shift_JIS"/></head><body>'
        f'<h2 id="c1">一</h2><p>{_BODY}</p><h2 id="c2">二</h2><p>{_BODY}おわり</p></body></html>'
    ).encode("shift_jis")
    nav = (
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
        '<li><a href="a.xhtml#c1">始まり</a></li><li><a href="a.xhtml#c2">終わり</a></li>'
        "</ol></nav></body></html>"
    )
    items = (
        '<item id="n" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
    )
    path = _epub(
        tmp_path / "split.epub",
        {
            "OEBPS/c.opf": _opf(items, '<itemref idref="a"/>'),
            "OEBPS/nav.xhtml": nav,
            "OEBPS/a.xhtml": document,
        },
    )
    _title, chapters, _cover = _epub_metadata(path)
    assert [name for name, _text in chapters] == ["始まり", "終わり"]
    assert chapters[1][1].rstrip().endswith("おわり")
    assert all("�" not in text for _name, text in chapters)


def test_wrongly_declared_encoding_falls_back_to_utf8(tmp_path: Path) -> None:
    document = (f'<?xml version="1.0" encoding="EUC-JP"?><html><body><p>{_BODY}</p></body></html>').encode(
        "utf-8"
    )
    assert light_novels._decode_markup(document).count("吾輩") == 12


def test_utf16_txt_book_is_decoded(tmp_path: Path) -> None:
    source = tmp_path / "本.txt"
    source.write_bytes(_BODY.encode("utf-16"))
    _title, chapters = _txt_metadata(source)
    assert chapters == [("Part 1", _BODY)]


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_one_unreadable_illustration_does_not_fail_the_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, "PNG")
    document = (
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f'<p>{_BODY}</p><img src="good.png"/><p>{_BODY}</p><img src="huge.png"/></body></html>'
    )
    path = _epub(
        tmp_path / "絵.epub",
        {
            "OEBPS/c.opf": _opf(
                '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>', '<itemref idref="a"/>'
            ),
            "OEBPS/a.xhtml": document,
            "OEBPS/good.png": buffer.getvalue(),
            "OEBPS/huge.png": buffer.getvalue(),
        },
    )
    real_read_member = light_novels.read_member

    def read_member(archive, name, **kwargs):
        if str(name).endswith("huge.png"):
            raise ArchiveTooLargeError("too large")
        return real_read_member(archive, name, **kwargs)

    monkeypatch.setattr(light_novels, "read_member", read_member)
    service = LightNovelService(_cfg(tmp_path))
    try:
        book = service.import_file(path)
        chapter = service.chapter_fast(int(book["id"]), 0)
    finally:
        service.close()
    text = str(chapter.get("text") or "")
    assert text.count("[[PUDGE_LN_IMAGE_URL:covers/") == 1


def _jpeg(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def test_manga_cover_thumbnail_decodes_jpeg_at_reduced_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Series Vol.01"
    folder.mkdir()
    for index in range(2):
        (folder / f"{index:03d}.jpg").write_bytes(_jpeg((2400, 3400)))
    service = MangaService(Database(tmp_path / "data" / "library.sqlite3"), cache_dir=tmp_path / "cache")
    [book] = service.import_image_groups(service.discover_image_files(folder))
    shutil.rmtree(tmp_path / "cache" / "manga-covers", ignore_errors=True)  # made again below
    decoded: list[tuple[int, int]] = []
    real_convert = Image.Image.convert

    def convert(self, *args, **kwargs):
        decoded.append(self.size)
        return real_convert(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", convert)
    target = service._local_cover_file(service._book(int(book["id"])))
    assert target is not None
    with Image.open(target) as thumb:
        assert max(thumb.size) <= 480 and thumb.size[0] <= 320
        assert thumb.getpixel((5, 5))[0] > 150
    # Decoded at 1/4 scale (600x850), not the page's full 2400x3400.
    assert decoded and max(decoded[0]) < 1000


def test_lightnovel_cover_thumbnail_uses_reduced_decode(tmp_path: Path) -> None:
    source = tmp_path / "cover.jpg"
    source.write_bytes(_jpeg((2000, 3000)))
    thumb = tmp_path / "thumb.jpg"
    service = LightNovelService(_cfg(tmp_path))
    try:
        assert service._make_cover_thumbnail(source, thumb)
    finally:
        service.close()
    with Image.open(thumb) as image:
        assert image.size == (320, 480)


_REAL_NAMED_TEMPORARY_FILE = tempfile.NamedTemporaryFile


class _FailingTemporary:
    def __init__(self, **kwargs):
        self._inner = _REAL_NAMED_TEMPORARY_FILE(**kwargs)
        self.name = self._inner.name

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def write(self, _data):
        raise OSError(28, "No space left on device")


def test_failed_ocr_artifact_write_leaves_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manga_ocr_artifact.tempfile, "NamedTemporaryFile", _FailingTemporary)
    target = tmp_path / "ocr" / "artifact.json.gz"
    with pytest.raises(OSError):
        manga_ocr_artifact.write_artifact(target, {"pages": []})
    assert list(target.parent.iterdir()) == []


def test_failed_cover_preview_write_leaves_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Series Vol.02"
    folder.mkdir()
    (folder / "001.jpg").write_bytes(_jpeg((40, 60)))
    service = MangaService(Database(tmp_path / "data" / "library.sqlite3"), cache_dir=tmp_path / "cache")
    [book] = service.import_image_groups(service.discover_image_files(folder))
    monkeypatch.setattr(manga.tempfile, "NamedTemporaryFile", _FailingTemporary)
    with pytest.raises(OSError):
        service.cover_preview_asset(int(book["id"]))
    assert list((tmp_path / "cache" / "manga-cover-previews").iterdir()) == []
