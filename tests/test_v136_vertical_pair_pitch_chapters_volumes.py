from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService, _epub_metadata, _volume_from_text

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _audio(tmp_path: Path) -> AudiobookService:
    return AudiobookService(Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv", cache_dir=tmp_path / "cache")


def _put_audio(service: AudiobookService, source: Path, title: str, duration: float = 100.0) -> dict:
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"audio")
    return service._upsert(
        path=source,
        title=title,
        duration=duration,
        files=[{"index": 0, "path": str(source), "title": title, "duration": duration, "start": 0.0, "end": duration}],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": duration}],
    )


def test_vertical_reader_and_study_card_contracts() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    study = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")

    assert "ruby-position:under" in html and "-webkit-ruby-position:after" in html
    assert "#lnReader.vertical .ln-paired-word-current{background:linear-gradient(180deg" in html
    assert "activeToken?.identity === identity" in study and "closeStudyCard();" in study
    assert "${renderPitchAccent(card)}" in study
    assert "lnrPitchAccent" not in html and "lnrPitchColor" not in html
    assert "Find LN on Nyaa" not in media and "Найти LN на Nyaa" not in media
    assert "data-ln-pair-audio" not in html and "pair-audiobook" in html and "light_novel_audiobook_candidates" in html


def test_ascii_japanese_suffix_volume_and_duplicate_volume_repair(tmp_path: Path) -> None:
    assert _volume_from_text("狼と香辛料2") == 2
    assert _volume_from_text("狼と香辛料 12") == 12
    service = LightNovelService(_cfg(tmp_path))
    parent = tmp_path / "sources" / "Spice"
    parent.mkdir(parents=True)
    now = time.time()
    with service._connect() as conn:
        for i in range(5):
            path = parent / f"wolf-{i}.epub"
            conn.execute(
                "INSERT INTO ln_books(title,file_path,file_type,volume,cover_url,content_schema,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("狼と香辛料", str(path), "epub", 1, "", service.CONTENT_SCHEMA, now + i, now + i),
            )
    books = sorted(service.books(), key=lambda row: int(row["id"]))
    assert [int(book["volume"]) for book in books] == [1, 2, 3, 4, 5]


def test_epub_generic_nav_title_prefers_real_heading(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    container = '''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
    opf = '''<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>テスト本</dc:title></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine></package>'''
    nav = '''<html xmlns="http://www.w3.org/1999/xhtml"><body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><ol><li><a href="c1.xhtml">Chapter 1</a></li></ol></nav></body></html>'''
    chapter = '''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter 1</title></head><body><h1>旅立ちの朝</h1><p>これは十分に長い日本語の本文です。狼と商人は新しい町へ向かいました。</p></body></html>'''
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", nav)
        zf.writestr("OEBPS/c1.xhtml", chapter)
    title, chapters, _cover = _epub_metadata(epub)
    assert title == "テスト本"
    assert chapters[0][0] == "旅立ちの朝"


def test_pair_candidates_rank_title_volume_and_existing_audio_text(tmp_path: Path) -> None:
    ln = LightNovelService(_cfg(tmp_path))
    source = tmp_path / "library" / "また、同じ夢を見ていた.txt"
    source.write_text("同じ夢を見ていた。少女は夢について考えた。" * 80, encoding="utf-8")
    novel = ln.import_file(source)
    audio = _audio(tmp_path)
    good = _put_audio(audio, tmp_path / "a" / "また、同じ夢を見ていた [audiobook.jp 237969].m4b", "また、同じ夢を見ていた [audiobook.jp 237969]")
    _put_audio(audio, tmp_path / "b" / "銀河鉄道の父.m4b", "銀河鉄道の父")
    transcript = audio._transcript_path(int(good["id"]))
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({"schema": "audiobook-stt-v3", "segments": [{"text": "同じ夢を見ていた少女は夢について考えた" * 30}]}), encoding="utf-8")
    rows = audio.link_candidates_for_light_novel(int(novel["id"]))
    assert rows and int(rows[0]["id"]) == int(good["id"])
    assert "audio/text" in rows[0]["signals"]
    filtered = audio.link_candidates_for_light_novel(int(novel["id"]), "mata")
    # Romanized global search is handled elsewhere; this modal search is literal/title-fuzzy.
    assert isinstance(filtered, list)


def test_incremental_ln_card_preserves_paired_audio() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "paired_audio:book.paired_audio===undefined?previous.paired_audio:book.paired_audio" in html
    assert "await loadLightNovels(true);syncLnPairedTray" in html
