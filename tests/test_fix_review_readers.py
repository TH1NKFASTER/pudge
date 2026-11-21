"""Code review fixes for the readers: light novels, manga, audiobooks, Jimaku archives.

What went wrong before, and what happens now:

1. Zip bombs. EPUB, CBZ and Jimaku subtitle archive members were read whole with no
   size limit (a 0.18 MB EPUB took the parser to 1.3 GB of memory); 7z/rar subtitle
   archives were unpacked whole. Now members are read with a per-member cap (declared
   size and bytes read) and a per-archive total; 7-Zip is stopped once its folder
   outgrows the limit. The import fails with a clear error.
2. One manga page over Pillow's pixel limit raised DecompressionBombError out of the
   cover code: import failed after the row was written and every state() call raised,
   so the whole manga library vanished. Now that book has no cover and its page opens.
3. EPUB text before the first TOC fragment anchor (a prologue) was dropped. Now it is
   a leading section named by the TOC entry for the file, its heading or "Beginning";
   untitled text that follows another file continues that file's chapter.
4. A re-index kept the raw chapter index, so 6 chapters at 4/0.5 re-split into 3
   showed 100%. Now the reading place and the bookmark move with the text.
5. ffmpeg's stderr went to a pipe read only after exit; damaged audio wrote ~100 KB
   of errors, the pipe filled and the STT chunk stalled until the timeout. Now
   stderr goes to a file and the end of it is still in the error message.
6. TOC links with "../" were not normalized: chapter titles fell back to
   "Chapter N" and a cover href with "../" was missing. Now they are normalized and
   can never leave the archive.
7. An EPUB titled ".." was copied next to, not into, the Light Novels folder.
8. A byte-order mark in a TXT (or U+FEFF in an EPUB) stayed in the text and shifted
   the paired-audio highlighting by one character. Now it is removed.
9. A symlinked audio file aborted a whole folder import (relative_to on a resolved
   path). Now the folder imports.
10. A book paragraph "[[PUDGE_LN_IMAGE_URL:https://...]]" was shown as an image loaded
   from that address. Now book text cannot form Pudge markers, and the reader shows
   only Pudge's own stored covers/ images; anything else is plain text.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge import i18n
from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import (
    LightNovelService,
    _epub_cover_blob,
    _epub_metadata,
    _is_ln_image_paragraph,
    _safe_name,
)
from pudge.manga import MangaService
from pudge.models import JimakuFile, VideoIdentity
from pudge.providers.jimaku import materialize_jimaku_files

MB = 1024 * 1024
TOO_LARGE = "unpacks to more than"
CONTAINER = (
    '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
    '<rootfiles><rootfile full-path="{opf}" media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _xhtml(body: str, head: str = "<title>t</title>") -> str:
    return f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head>{head}</head><body>{body}</body></html>'


def _write_epub(
    path: Path,
    files: dict[str, str | bytes],
    *,
    manifest: list[tuple[str, str, str, str]],
    spine: list[str],
    opf: str = "OEBPS/content.opf",
    title: str = "本",
    meta: str = "",
) -> Path:
    items = "".join(
        f'<item id="{item_id}" href="{href}" media-type="{media}"'
        + (f' properties="{props}"' if props else "")
        + "/>"
        for item_id, href, media, props in manifest
    )
    refs = "".join(f'<itemref idref="{item_id}"/>' for item_id in spine)
    package = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title>{meta}</metadata>'
        f"<manifest>{items}</manifest><spine>{refs}</spine></package>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER.format(opf=opf))
        zf.writestr(opf, package)
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _simple_epub(path: Path, bodies: list[str], *, title: str = "本") -> Path:
    files = {f"OEBPS/c{i}.xhtml": _xhtml(f"<h1>第{i + 1}章</h1>{body}") for i, body in enumerate(bodies)}
    manifest = [(f"c{i}", f"c{i}.xhtml", "application/xhtml+xml", "") for i in range(len(bodies))]
    return _write_epub(
        path, files, manifest=manifest, spine=[f"c{i}" for i in range(len(bodies))], title=title
    )


def _ln_service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "ln.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    service = LightNovelService(cfg)
    service.queue_auto_bind_anilist = lambda *_a, **_k: None  # type: ignore[method-assign]
    service._inherit_series_anilist = lambda *_a, **_k: None  # type: ignore[method-assign]
    return service


def _chapters(service: LightNovelService, book_id: int) -> list[tuple[str, str]]:
    with service._connection() as conn:
        rows = conn.execute(
            "SELECT title,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index", (book_id,)
        ).fetchall()
    return [(str(row["title"]), str(row["text"])) for row in rows]


def _png(size: tuple[int, int], *, noise: bool = False) -> bytes:
    buffer = io.BytesIO()
    image = Image.frombytes("L", size, os.urandom(size[0] * size[1])) if noise else Image.new("L", size, 255)
    image.save(buffer, "PNG")
    return buffer.getvalue()


# 1. Zip bombs ------------------------------------------------------------------------


def test_epub_bomb_chapter_is_refused_before_it_is_unpacked(tmp_path: Path) -> None:
    manifest = [("c0", "c0.xhtml", "application/xhtml+xml", "")]
    bomb = _write_epub(tmp_path / "bomb.epub", {}, manifest=manifest, spine=["c0"])
    with zipfile.ZipFile(bomb, "a", zipfile.ZIP_DEFLATED) as zf:
        # 65 MB of text that deflates to well under a megabyte, written in chunks.
        with zf.open("OEBPS/c0.xhtml", "w") as handle:
            handle.write(b"<html><body><p>")
            for _ in range(65):
                handle.write("あ".encode() * (MB // 3))
            handle.write(b"</p></body></html>")
    assert bomb.stat().st_size < 2 * MB

    with pytest.raises(Exception, match=TOO_LARGE):
        _epub_metadata(bomb)
    service = _ln_service(tmp_path)
    with pytest.raises(Exception, match=TOO_LARGE):
        service.import_file(bomb)
    # The library scan treats it as a rejected file instead of retrying it forever.
    assert service.is_probably_japanese_source(bomb) is False


def test_cbz_page_over_the_member_limit_is_refused(tmp_path: Path, monkeypatch) -> None:
    cbz = tmp_path / "Manga 01.cbz"
    with zipfile.ZipFile(cbz, "w") as zf:
        zf.writestr("001.png", _png((60, 60), noise=True))
    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    book = service.import_file(cbz)
    monkeypatch.setattr("pudge.manga.IMAGE_MEMBER_LIMIT", 1000, raising=False)
    with pytest.raises(Exception, match=TOO_LARGE):
        service.page(int(book["id"]), 0)
    with pytest.raises(Exception, match=TOO_LARGE):
        service.page_asset(int(book["id"]), 0, expected_revision=service._page_revision(cbz, "001.png"))


class _FakeJimakuClient:
    def __init__(self, archive: Path) -> None:
        self.archive = archive

    def download(self, item: JimakuFile, cache_dir: Path) -> Path:
        return self.archive


def _jimaku(archive: Path) -> list:
    item = JimakuFile(
        url="https://example.test/" + archive.name, name=archive.name, size=1, last_modified="", score=70.0
    )
    return materialize_jimaku_files(
        _FakeJimakuClient(archive),  # type: ignore[arg-type]
        item,
        VideoIdentity(title="Show"),
        archive.parent / "Show.mkv",
        archive.parent / "cache",
    )


JAPANESE_ASS = (
    "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    + "".join(
        f"Dialogue: 0,0:00:{i % 60:02d}.00,0:00:{i % 60:02d}.50,Default,,0,0,0,,今日はいい天気ですね。\n"
        for i in range(40)
    )
)


def test_jimaku_zip_member_bomb_is_not_written_to_disk(tmp_path: Path, capsys) -> None:
    archive = tmp_path / "subs.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("Show - 01.ass", "w") as handle:
            handle.write(JAPANESE_ASS.encode())
            for _ in range(65):
                handle.write(b";" * MB)
    assert _jimaku(archive) == []
    assert not [path for path in tmp_path.iterdir() if path.suffix == ".ass"]
    assert TOO_LARGE in capsys.readouterr().out


def test_jimaku_7z_extraction_is_stopped_and_cleaned_up(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "subs.7z"
    archive.write_bytes(b"fake archive")
    payload = tmp_path / "payload.ass"
    payload.write_text(JAPANESE_ASS + ";" * 200_000, encoding="utf-8")
    tool = tmp_path / "7zz"
    tool.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do case "$arg" in -o*) out=${arg#-o} ;; esac; done\n'
        'cp "$FAKE_7Z_PAYLOAD" "$out/Show - 01.ass"\n'
        "sleep 5\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    monkeypatch.setenv("FAKE_7Z_PAYLOAD", str(payload))
    monkeypatch.setenv("PUDGE_7ZIP", str(tool))
    monkeypatch.setattr("pudge.providers.jimaku.SUBTITLE_ARCHIVE_LIMIT", 64 * 1024, raising=False)
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))

    started = time.monotonic()
    assert _jimaku(archive) == []
    assert time.monotonic() - started < 4.0
    assert list(temp_root.iterdir()) == []


# 2. Oversized manga page ---------------------------------------------------------------


def test_one_decompression_bomb_page_does_not_hide_the_manga_library(tmp_path: Path, monkeypatch) -> None:
    # A low pixel limit stands in for a 14000x13000 page: Pillow raises the same error.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    ok = tmp_path / "Fine 01.cbz"
    with zipfile.ZipFile(ok, "w") as zf:
        zf.writestr("001.png", _png((10, 10)))
    bomb = tmp_path / "Huge 01.cbz"
    with zipfile.ZipFile(bomb, "w") as zf:
        zf.writestr("001.png", _png((60, 60)))
    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    service.import_file(ok)
    huge = service.import_file(bomb)

    books = service.state()["books"]
    assert sorted(book["title"] for book in books) == ["Fine 01", "Huge 01"]
    page = service.page(int(huge["id"]), 0)
    assert page["spread"] is False and page["data_uri"].startswith("data:image/png;base64,")


# 3. Text before the first TOC anchor ---------------------------------------------------


def _fragment_epub(path: Path, nav_links: str, body: str, *, extra_spine: bool = False) -> Path:
    nav = _xhtml(f'<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>{nav_links}</ol></nav>')
    files: dict[str, str | bytes] = {"OEBPS/nav.xhtml": nav, "OEBPS/text.xhtml": _xhtml(body)}
    manifest = [
        ("nav", "nav.xhtml", "application/xhtml+xml", "nav"),
        ("t", "text.xhtml", "application/xhtml+xml", ""),
    ]
    spine = ["t"]
    if extra_spine:
        files["OEBPS/first.xhtml"] = _xhtml("<h1>序章</h1><p>序章の本文はここから始まる。</p>")
        manifest.append(("f", "first.xhtml", "application/xhtml+xml", ""))
        spine = ["f", "t"]
    return _write_epub(path, files, manifest=manifest, spine=spine)


def test_epub_prologue_before_the_first_anchor_is_kept_with_its_toc_label(tmp_path: Path) -> None:
    path = _fragment_epub(
        tmp_path / "b.epub",
        '<li><a href="text.xhtml">プロローグ</a></li><li><a href="text.xhtml#c1">第一章</a></li>'
        '<li><a href="text.xhtml#c2">第二章</a></li>',
        "<h1>プロローグ</h1><p>プロローグの本文はここにある大切な文章です。</p>"
        '<h1 id="c1">第一章</h1><p>第一章の本文。</p><h1 id="c2">第二章</h1><p>第二章の本文。</p>',
    )
    _title, chapters, _cover = _epub_metadata(path)
    assert [title for title, _text in chapters] == ["プロローグ", "第一章", "第二章"]
    assert "プロローグの本文" in chapters[0][1]


def test_untitled_text_before_the_first_anchor(tmp_path: Path) -> None:
    links = '<li><a href="text.xhtml#c1">第一章</a></li><li><a href="text.xhtml#c2">第二章</a></li>'
    body = '<p>名もない冒頭の文章。</p><h1 id="c1">第一章</h1><p>一の本文。</p><h1 id="c2">第二章</h1><p>二の本文。</p>'
    # At the start of the book: its own "Beginning" section.
    _title, chapters, _cover = _epub_metadata(_fragment_epub(tmp_path / "a.epub", links, body))
    assert [title for title, _text in chapters] == [i18n.t("ln.chapterBeginning"), "第一章", "第二章"]
    assert chapters[0][1] == "名もない冒頭の文章。"
    # After another file: it continues that file's chapter.
    _title, chapters, _cover = _epub_metadata(
        _fragment_epub(tmp_path / "b.epub", links, body, extra_spine=True)
    )
    assert [title for title, _text in chapters] == ["序章", "第一章", "第二章"]
    assert chapters[0][1].endswith("名もない冒頭の文章。")


# 4. Re-index keeps the reading place ---------------------------------------------------


def test_reindex_moves_reading_place_and_bookmark_with_the_text(tmp_path: Path) -> None:
    service = _ln_service(tmp_path)
    body = "<p>" + "これは本文です。" * 50 + "</p>"
    source = tmp_path / "in" / "book.epub"
    source.parent.mkdir()
    book_id = int(service.import_file(_simple_epub(source, [body] * 6))["id"])
    service.save_bookmark(book_id, 4, 0.5)
    assert service.progress_summary(book_id)["reading_progress_percent"] == 75.0

    stored = Path(service.book(book_id)["file_path"])
    _simple_epub(stored, [body] * 3)
    with service._connection() as conn:
        conn.execute("UPDATE ln_books SET content_schema=1")
    assert service.reindex_outdated_sources() == 1
    summary = service.progress_summary(book_id)
    assert (summary["current_chapter"], summary["current_offset"]) == (2, pytest.approx(0.25))
    assert summary["reading_progress_percent"] == pytest.approx(75.0)
    with service._connection() as conn:
        mark = conn.execute(
            "SELECT chapter_index,offset FROM ln_bookmarks WHERE book_id=?", (book_id,)
        ).fetchone()
    assert (mark["chapter_index"], mark["offset"]) == (2, pytest.approx(0.25))

    # Same chapters again: the position stays exactly where it was.
    service.save_bookmark(book_id, 1, 0.123)
    with service._connection() as conn:
        conn.execute("UPDATE ln_books SET content_schema=1")
    service.reindex_outdated_sources()
    summary = service.progress_summary(book_id)
    assert (summary["current_chapter"], summary["current_offset"]) == (1, 0.123)


def test_reading_place_follows_a_chapter_whose_text_survived() -> None:
    from pudge.light_novels import _remap_reading_position

    old = [("a", 100), ("b", 200), ("c", 300)]
    new = [("prologue", 50), ("a", 100), ("b", 200), ("c", 300)]
    assert _remap_reading_position(old, new, 1, 0.3) == (2, 0.3)
    assert _remap_reading_position(old, old, 2, 0.5) == (2, 0.5)
    assert _remap_reading_position(old, [("x", 600)], 9, 0.5) == (0, 1.0)


# 5. ffmpeg stderr --------------------------------------------------------------------


def _noisy_ffmpeg(tmp_path: Path, *, code: int) -> Path:
    tool = tmp_path / "ffmpeg"
    tool.write_text(
        "#!/bin/sh\n"
        "for last; do :; done\n"
        "head -c 300000 /dev/zero | tr '\\0' 'x' 1>&2\n"
        "echo ' the real error' 1>&2\n" + (': > "$last"\n' if code == 0 else "") + f"exit {code}\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return tool


@pytest.mark.parametrize("code", [0, 1])
def test_noisy_ffmpeg_does_not_stall_the_stt_chunk(tmp_path: Path, code: int) -> None:
    fake = SimpleNamespace(_resolved_ffmpeg=lambda: str(_noisy_ffmpeg(tmp_path, code=code)))
    cancel = threading.Event()
    result: dict[str, object] = {}

    def run() -> None:
        try:
            AudiobookService._extract_stt_chunk(
                fake,
                tmp_path / "in.mp3",
                tmp_path / "out.flac",
                start=0.0,
                duration=300.0,
                cancel_event=cancel,
            )
            result["value"] = "ok"
        except BaseException as exc:  # noqa: BLE001 - the outcome is checked below
            result["value"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(15)
    finished = not thread.is_alive()
    cancel.set()  # lets the old code give up so the test does not hang
    thread.join(10)
    assert finished
    if code == 0:
        assert result["value"] == "ok"
    else:
        assert isinstance(result["value"], RuntimeError)
        assert str(result["value"]).endswith("the real error")


# 6. "../" TOC links --------------------------------------------------------------------


def test_toc_and_cover_links_with_dotdot_are_normalized(tmp_path: Path) -> None:
    nav = _xhtml(
        '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>'
        + "".join(f'<li><a href="../Text/c{i}.xhtml">見出し{i}</a></li>' for i in range(3))
        + '<li><a href="../../../etc/c0.xhtml">外</a></li></ol></nav>'
    )
    files: dict[str, str | bytes] = {"OEBPS/Text/nav.xhtml": nav, "Images/cover.png": _png((8, 8))}
    for i in range(3):
        files[f"OEBPS/Text/c{i}.xhtml"] = _xhtml(f"<p>本文その{i}。" + "あ" * 200 + "</p>")
    manifest = [("nav", "Text/nav.xhtml", "application/xhtml+xml", "nav")]
    manifest += [(f"c{i}", f"Text/c{i}.xhtml", "application/xhtml+xml", "") for i in range(3)]
    manifest.append(("cover", "../Images/cover.png", "image/png", "cover-image"))
    path = _write_epub(tmp_path / "b.epub", files, manifest=manifest, spine=["c0", "c1", "c2"])

    _title, chapters, cover = _epub_metadata(path)
    assert [title for title, _text in chapters] == ["見出し0", "見出し1", "見出し2"]
    assert cover is not None and cover[1] == ".png"
    assert _epub_cover_blob(path) == cover


# 7. A book titled ".." ------------------------------------------------------------------


def test_book_titled_dotdot_stays_inside_the_light_novels_folder(tmp_path: Path) -> None:
    assert _safe_name("..") == "Light Novel" and _safe_name(" . ") == "Light Novel"
    service = _ln_service(tmp_path)
    source = tmp_path / "dl" / "notes.epub"
    source.parent.mkdir()
    _simple_epub(source, ["<p>" + "本文。" * 50 + "</p>"], title="..")
    book = service.import_file(source)
    stored = Path(book["file_path"])
    assert stored.is_relative_to(service.root.resolve())
    assert not (service.root.parent / "notes.epub").exists()


# 8. Byte-order marks ---------------------------------------------------------------------


def test_bom_is_not_part_of_the_book_text(tmp_path: Path) -> None:
    service = _ln_service(tmp_path)
    txt = tmp_path / "in" / "memo.txt"
    txt.parent.mkdir()
    txt.write_bytes("﻿吾輩は猫である。\n名前はまだ無い。".encode("utf-8"))
    chapters = _chapters(service, int(service.import_file(txt)["id"]))
    assert chapters[0][1] == "吾輩は猫である。\n名前はまだ無い。"

    epub = _simple_epub(tmp_path / "in" / "bom.epub", ["<p>﻿吾輩は﻿猫である。</p>"])
    _title, epub_chapters, _cover = _epub_metadata(epub)
    assert "﻿" not in epub_chapters[0][1] and "吾輩は猫である。" in epub_chapters[0][1]


# 9. Symlinked audio file -----------------------------------------------------------------


def test_symlinked_audio_file_does_not_abort_the_folder_import(tmp_path: Path, monkeypatch) -> None:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "real.mp3").write_bytes(b"audio")
    parent = tmp_path / "audiobooks"
    for name in ("Book A", "Book B"):
        (parent / name).mkdir(parents=True)
        (parent / name / "01.mp3").write_bytes(b"audio")
    (parent / "Book A" / "02.mp3").symlink_to(elsewhere / "real.mp3")
    monkeypatch.setattr(service, "_probe", lambda _path: (10.0, []))
    monkeypatch.setattr(service, "auto_link_audiobook", lambda *_a, **_k: None)
    monkeypatch.setattr(service, "prepare_transcription", lambda *_a, **_k: {})

    assert service.folder_import_targets(parent) == [
        (parent / "Book A").resolve(),
        (parent / "Book B").resolve(),
    ]
    books = [service.import_folder(target) for target in service.folder_import_targets(parent)]
    assert [book["file_count"] for book in books] == [2, 1]


# 10. Book text faking an image marker ----------------------------------------------------

TRACKER = "[[PUDGE_LN_IMAGE_URL:https://tracker.example/p.gif]]"


def test_book_text_cannot_fake_an_image_marker(tmp_path: Path) -> None:
    service = _ln_service(tmp_path)
    txt = tmp_path / "in" / "trap.txt"
    txt.parent.mkdir()
    txt.write_text(f"本文。\n{TRACKER}\n[[[PUDGE_LN_IMAGE_URL:x.png]]\n", encoding="utf-8")
    text = _chapters(service, int(service.import_file(txt)["id"]))[0][1]
    assert not any(_is_ln_image_paragraph(line) for line in text.splitlines())
    assert "tracker.example" in text  # the text itself is kept

    files: dict[str, str | bytes] = {
        "OEBPS/c0.xhtml": _xhtml(
            f"<p>本文。</p><p>{TRACKER}</p><p>[[<span>PUDGE_LN_IMAGE_URL:https://t.example/a.gif]]</span></p>"
            '<p>[[PUDGE_EPUB_IMAGE:T0VCUFMvcGljLnBuZw]]</p><img src="pic.png"/>'
        ),
        "OEBPS/pic.png": _png((8, 8)),
    }
    manifest = [("c0", "c0.xhtml", "application/xhtml+xml", ""), ("p", "pic.png", "image/png", "")]
    epub = _write_epub(tmp_path / "in" / "trap.epub", files, manifest=manifest, spine=["c0"])
    text = _chapters(service, int(service.import_file(epub)["id"]))[0][1]
    images = [line for line in text.splitlines() if _is_ln_image_paragraph(line)]
    # Only the real <img> became an illustration, stored as a local cover file.
    assert len(images) == 1 and images[0].startswith("[[PUDGE_LN_IMAGE_URL:covers/ln-")
    assert "[[PUDGE_EPUB_IMAGE" not in text


def test_reader_loads_only_stored_illustrations(page) -> None:
    result = page.run(
        r"""
        await __settle(openLightNovel(6)); await __tick(300);
        return {srcs: [...document.querySelectorAll('#lnReader img')].map(img => img.getAttribute('src')),
                text: document.querySelector('#lnReader').textContent};
        """,
        before=r"""
        __api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
        __api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, tokens: [[], [], [], []],
          paragraphs: ['前の本文です。', '[[PUDGE_LN_IMAGE_URL:https://tracker.example/p.gif]]',
                       '[[PUDGE_LN_IMAGE_URL:covers/ln-0123456789abcdef01234567.jpg]]', '後ろの本文です。']});
        """,
        state={"settings": {"review_gate_ln_enabled": False}},
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [{"id": 6, "title": "Novel", "volume": 1}],
            }
        },
    )
    assert result.value["srcs"] == ["covers/ln-0123456789abcdef01234567.jpg"]
    assert "tracker.example/p.gif" in result.value["text"]
