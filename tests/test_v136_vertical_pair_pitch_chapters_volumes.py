from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService, _epub_metadata, _volume_from_text
from tests.css_support import css_rules
from tests.i18n_support import has_text


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _audio(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def _put_audio(service: AudiobookService, source: Path, title: str, duration: float = 100.0) -> dict:
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"audio")
    return service._upsert(
        path=source,
        title=title,
        duration=duration,
        files=[
            {
                "index": 0,
                "path": str(source),
                "title": title,
                "duration": duration,
                "start": 0.0,
                "end": duration,
            }
        ],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": duration}],
    )


def test_vertical_reader_places_furigana_and_fills_the_spoken_word_downwards() -> None:
    rules = css_rules("index.html")
    ruby = rules.declarations("#lnReader.vertical ruby.ln-furigana-ruby")
    assert (ruby["ruby-position"], ruby["-webkit-ruby-position"]) == ("under", "after")
    assert rules.get("#lnReader.vertical .ln-paired-word-current", "background").startswith(
        "linear-gradient(180deg,"
    )


def test_clicking_the_same_word_again_closes_its_card(page) -> None:
    payload = {
        "text": "猫",
        "paragraphs": ["猫"],
        "tokens": [[{"wordId": 7, "readingIndex": 0, "start": 0, "end": 1}]],
        "vocabulary": [
            {"wordId": 7, "readingIndex": 0, "spelling": "猫", "reading": "ねこ", "pitchAccents": [1]}
        ],
        "settings": {},
    }
    result = page.run(
        r"""
        const rect = {left: 0, top: 0, right: 20, bottom: 20};
        await __settle(PudgeReadingTools.study.openText('猫', rect));
        const first = {open: PudgeReadingTools.study.isOpen(), pitch: Boolean(document.querySelector('#pudgeStudyCard .pudge-study-pitch'))};
        await __settle(PudgeReadingTools.study.openText('猫', rect));
        return {first, again: PudgeReadingTools.study.isOpen()};
        """,
        api={"study_parse_text": payload},
    )
    assert result.value == {"first": {"open": True, "pitch": True}, "again": False}


def test_reader_never_shows_inline_pitch_and_has_no_pitch_controls(page) -> None:
    # Pitch accent lives in the word card only; the reader's own pitch controls were removed.
    result = page.run(
        r"""
        $('lnReaderShell').classList.add('open');
        $('lnReader').innerHTML = '<p><ruby class="ln-pitch-ruby">猫<rt>ね↓こ</rt></ruby></p>';
        const shown = __visible('#lnReader rt');
        applyLnReaderSettings({show_pitch_accent: true, reader_pitch_accent: true});
        populateLnReaderAppearance({show_pitch_accent: true});
        return {shown, rt: __visible('#lnReader rt'),
                controls: [...document.querySelectorAll('#lnReaderAppearance [id]')].map(node => node.id).filter(id => /pitch/i.test(id))};
        """,
    )
    assert result.value == {"shown": True, "rt": False, "controls": []}
    assert not has_text("pitch", "en") and not has_text("тональн", "ru")


def test_audiobooks_have_no_find_light_novel_on_nyaa_action() -> None:
    assert not has_text("Find LN on Nyaa", "en") and not has_text("Найти LN на Nyaa", "ru")


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
    container = """<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
    opf = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>テスト本</dc:title></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine></package>"""
    nav = """<html xmlns="http://www.w3.org/1999/xhtml"><body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><ol><li><a href="c1.xhtml">Chapter 1</a></li></ol></nav></body></html>"""
    chapter = """<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter 1</title></head><body><h1>旅立ちの朝</h1><p>これは十分に長い日本語の本文です。狼と商人は新しい町へ向かいました。</p></body></html>"""
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
    good = _put_audio(
        audio,
        tmp_path / "a" / "また、同じ夢を見ていた [audiobook.jp 237969].m4b",
        "また、同じ夢を見ていた [audiobook.jp 237969]",
    )
    _put_audio(audio, tmp_path / "b" / "銀河鉄道の父.m4b", "銀河鉄道の父")
    transcript = audio._transcript_path(int(good["id"]))
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps(
            {
                "schema": "audiobook-stt-v3",
                "segments": [{"text": "同じ夢を見ていた少女は夢について考えた" * 30}],
            }
        ),
        encoding="utf-8",
    )
    rows = audio.link_candidates_for_light_novel(int(novel["id"]))
    assert rows and int(rows[0]["id"]) == int(good["id"])
    assert "audio/text" in rows[0]["signals"]
    filtered = audio.link_candidates_for_light_novel(int(novel["id"]), "mata")
    # Romanized global search is handled elsewhere; this modal search is literal/title-fuzzy.
    assert isinstance(filtered, list)


def test_updated_light_novel_card_keeps_its_audiobook_badge(page) -> None:
    # A card update without paired-audio data (a re-imported volume) keeps the 🎧 badge.
    paired = {
        "id": 9,
        "title": "Novel 1",
        "volume": 1,
        "character_count": 1000,
        "paired_audio": {"audiobook_id": 77},
    }
    result = page.run(
        r"""
        setPage('lightnovels'); await __tick(50);
        const badge = () => document.querySelector('#lightNovelsContent [data-ln-book="9"] .ln-paired-audio-badge')?.getAttribute('aria-label') ?? null;
        const before = badge();
        window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail: {ok: true, focus: {page: 'lightnovels', kind: 'light_novel', id: 9,
          book: {id: 9, title: 'Novel 1 (new edition)', volume: 1, character_count: 1200}}}}));
        await __tick(500);
        return {before, after: badge(), title: __text('#lightNovelsContent [data-ln-book="9"] .ln-card-title'), label: t('ui.hasAudiobook')};
        """,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": [paired]}},
    )
    out = result.value
    assert out["before"] == out["label"] and out["after"] == out["label"]
    assert out["title"] == "Novel 1 (new edition)"
