from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig, SyncConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService, _epub_metadata
from pudge.reading_audio_alignment import normalize_reading_text

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
SYNCING = ROOT / "pudge" / "syncing.py"
COMPARE = ROOT / "scripts" / "compare_subtitle_alignment.py"


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _plain_contents_wolf(path: Path) -> None:
    container = '''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
    labels = ["序幕", "第一幕", "第二幕", "第三幕", "第四幕", "第五幕", "第六幕", "終幕", "あとがき"]
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>', '<item id="x0" href="x0.xhtml" media-type="application/xhtml+xml"/>']
    spine = ['<itemref idref="x0"/>']
    nav_rows = []
    for index, label in enumerate(labels, 1):
        manifest.append(f'<item id="x{index}" href="x{index}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="x{index}"/>')
        # Broken package TOC from the real failure: every story row is the book title.
        nav_rows.append(f'<li><a href="x{index}.xhtml">狼と香辛料</a></li>')
    manifest.append('<item id="author" href="author.xhtml" media-type="application/xhtml+xml"/>')
    spine.append('<itemref idref="author"/>')
    opf = f'''<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>狼と香辛料</dc:title></metadata><manifest>{''.join(manifest)}</manifest><spine>{''.join(spine)}</spine></package>'''
    nav = f'''<html xmlns="http://www.w3.org/1999/xhtml"><body><nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>{''.join(nav_rows)}</ol></nav></body></html>'''
    contents = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Contents</p>' + ''.join(f'<p>{label}</p>' for label in labels) + '</body></html>'
    story = "狼と商人は街道を歩き、市場へ向かいました。"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", nav)
        zf.writestr("OEBPS/x0.xhtml", contents)
        for index, label in enumerate(labels, 1):
            heading = f"<h2>{label}</h2>" if label == "あとがき" else ""
            zf.writestr("OEBPS/x%d.xhtml" % index, f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{heading}<p>{story * (18 + index)}{index}</p></body></html>')
        zf.writestr("OEBPS/author.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2>支倉凍砂</h2><p>著者プロフィールです。</p></body></html>')


def test_plain_paragraph_contents_repairs_wolf_titles_and_is_not_a_chapter(tmp_path: Path) -> None:
    epub = tmp_path / "wolf.epub"
    _plain_contents_wolf(epub)
    title, chapters, _cover = _epub_metadata(epub)
    assert title == "狼と香辛料"
    assert [name for name, text in chapters if "街道" in text] == [
        "序幕", "第一幕", "第二幕", "第三幕", "第四幕", "第五幕", "第六幕", "終幕", "あとがき"
    ]
    assert not any(text.startswith("Contents\n序幕") for _name, text in chapters)
    assert LightNovelService.CONTENT_SCHEMA >= 9


def test_blur_reveal_is_instant_without_scale_or_filter_transition() -> None:
    html = INDEX.read_text(encoding="utf-8")
    rule = html.split(".ln-inline-image img{", 1)[1].split("}", 1)[0]
    blurred = html.split(".ln-reader.blur-images .ln-inline-image:not(.revealed) img{", 1)[1].split("}", 1)[0]
    assert "transition:filter" not in rule
    assert "transform" not in blurred
    assert "filter:blur(44px)" in blurred


def test_headphone_badge_hugs_title_and_is_vertically_centered() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert '.ln-card h3{line-height:1.25;display:flex;align-items:center;gap:1px' in html
    assert '.ln-paired-audio-badge{display:inline-flex;align-items:center;justify-content:center;margin-left:0' in html
    assert 'transform:translateY(.5px)' in html
    assert '.ln-card-title{min-width:0;width:fit-content;max-width:calc(100% - 16px);flex:0 1 auto' in html


def test_alignment_fingerprint_ignores_chapter_titles(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    ln = LightNovelService(cfg)
    epub = tmp_path / "wolf.epub"
    _plain_contents_wolf(epub)
    book = ln.import_file(epub)
    audio_file = tmp_path / "audio.m4b"
    audio_file.write_bytes(b"audio")
    audio = AudiobookService(Database(cfg.library.database_path), ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv", cache_dir=cfg.paths.cache_dir)
    a = audio._upsert(
        path=audio_file,
        title="狼と香辛料 Volume 1",
        duration=100.0,
        files=[{"index": 0, "path": str(audio_file), "title": "wolf", "duration": 100.0, "start": 0.0, "end": 100.0}],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 100.0}],
    )
    before = audio._alignment_fingerprint(int(book["id"]), int(a["id"]))
    with Database(cfg.library.database_path).connect() as conn:
        conn.execute("UPDATE ln_chapters SET title='Completely different display title' WHERE book_id=?", (int(book["id"]),))
    after = audio._alignment_fingerprint(int(book["id"]), int(a["id"]))
    assert before == after


def test_pre_v139_alignment_cache_is_migrated_without_realigning(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    ln = LightNovelService(cfg)
    epub = tmp_path / "wolf.epub"
    _plain_contents_wolf(epub)
    book = ln.import_file(epub)
    audio_file = tmp_path / "audio.m4b"
    audio_file.write_bytes(b"audio")
    audio = AudiobookService(Database(cfg.library.database_path), ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv", cache_dir=cfg.paths.cache_dir)
    a = audio._upsert(
        path=audio_file,
        title="狼と香辛料 Volume 1",
        duration=100.0,
        files=[{"index": 0, "path": str(audio_file), "title": "wolf", "duration": 100.0, "start": 0.0, "end": 100.0}],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 100.0}],
    )
    aid, bid = int(a["id"]), int(book["id"])
    with Database(cfg.library.database_path).connect() as conn:
        source = [dict(row) for row in conn.execute("SELECT chapter_index,title,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index", (bid,))]
    aligned = []
    for row in source[:3]:
        length = len(normalize_reading_text(row["text"]))
        aligned.append({"chapter_index": row["chapter_index"], "title": row["title"], "normalized_length": length, "confidence": 0.9, "anchors": [{"offset": 0, "time": 0.0}, {"offset": max(1, length), "time": 10.0}]})
    old = cfg.paths.cache_dir / "reading-audio-alignment" / "old-title-sensitive-cache.json"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({
        "schema": "reading-audio-v3", "model": audio.stt_model, "confidence": 0.9,
        "chapters": aligned, "anchor_count": 6,
        "processing": {"transcript_fingerprint": audio._transcript_fingerprint(aid)},
    }, ensure_ascii=False), encoding="utf-8")
    destination = audio._alignment_path(bid, aid)
    assert not destination.exists()
    loaded = audio._load_alignment(bid, aid)
    assert loaded is not None
    assert destination.is_file()
    assert loaded["processing"]["migrated_from_fingerprint"] == old.stem



def test_pre_v139_alignment_cache_migrates_after_contents_chapter_index_shift(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    ln = LightNovelService(cfg)
    epub = tmp_path / "wolf-shift.epub"
    _plain_contents_wolf(epub)
    book = ln.import_file(epub)
    audio_file = tmp_path / "audio-shift.m4b"
    audio_file.write_bytes(b"audio")
    audio = AudiobookService(Database(cfg.library.database_path), ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv", cache_dir=cfg.paths.cache_dir)
    a = audio._upsert(
        path=audio_file, title="狼と香辛料 Volume 1", duration=100.0,
        files=[{"index": 0, "path": str(audio_file), "title": "wolf", "duration": 100.0, "start": 0.0, "end": 100.0}],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 100.0}],
    )
    aid, bid = int(a["id"]), int(book["id"])
    with Database(cfg.library.database_path).connect() as conn:
        source = [dict(row) for row in conn.execute("SELECT chapter_index,title,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index", (bid,))]
    aligned = []
    for row in source[:4]:
        length = len(normalize_reading_text(row["text"]))
        # Simulate v138: a now-removed Contents page occupied index 0.
        aligned.append({"chapter_index": int(row["chapter_index"]) + 1, "title": f"Chapter {int(row['chapter_index']) + 2}", "normalized_length": length, "confidence": 0.9, "anchors": [{"offset": 0, "time": 0.0}, {"offset": max(1, length), "time": 10.0}]})
    old = cfg.paths.cache_dir / "reading-audio-alignment" / "old-shifted-cache.json"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({
        "schema": "reading-audio-v3", "model": audio.stt_model, "confidence": 0.9,
        "chapters": aligned, "anchor_count": 8,
        "processing": {"transcript_fingerprint": audio._transcript_fingerprint(aid)},
    }, ensure_ascii=False), encoding="utf-8")
    loaded = audio._load_alignment(bid, aid)
    assert loaded is not None
    assert [row["chapter_index"] for row in loaded["chapters"]] == [0, 1, 2, 3]
    assert [row["title"] for row in loaded["chapters"]] == [row["title"] for row in source[:4]]

def test_ab_comparator_has_pre_v138_toggle_and_downloads_output() -> None:
    script = COMPARE.read_text(encoding="utf-8")
    syncing = SYNCING.read_text(encoding="utf-8")
    assert SyncConfig().japanese_stt_text_clock is True
    assert "if config.japanese_stt_text_clock:" in syncing
    assert "text_clock_disabled" in syncing
    assert "japanese_stt_text_clock=bool(text_clock)" in script
    assert "text_clock=False" in script
    assert "text_clock=True" in script
    assert 'Path.home() / "Downloads" / f"pudge-subtitle-alignment-ab-' in script
    assert '"differences.json"' in script and '"differences.csv"' in script
    assert '["open", "-R", str(log_path)]' in script
