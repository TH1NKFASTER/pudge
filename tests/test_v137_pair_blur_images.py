from __future__ import annotations

import zipfile
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService, _epub_metadata, _is_epub_image_only_text

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
    return AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def _put_audio(service: AudiobookService, source: Path, title: str) -> dict:
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"audio")
    return service._upsert(
        path=source,
        title=title,
        duration=100.0,
        files=[{"index": 0, "path": str(source), "title": title, "duration": 100.0, "start": 0.0, "end": 100.0}],
        chapters=[{"index": 0, "title": title, "start": 0.0, "end": 100.0}],
    )


def test_jiten_pitch_stays_in_card_but_reader_pitch_controls_stay_removed() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    study = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/reading_tools.css").read_text(encoding="utf-8")

    assert "${renderPitchAccent(card)}" in study
    assert "lnrPitchAccent" not in html and "lnrPitchColor" not in html
    assert "root.classList.add('hide-pitch-accent')" in html
    assert ".pudge-study-pitch,.pudge-study-pitch *{-webkit-user-select:none!important;user-select:none!important}" in css
    assert "padding:9px 14px 14px" in css
    assert "min-height:44px" in css


def test_pair_is_context_menu_only_and_candidate_percent_is_hidden() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")

    assert "data-ln-pair-audio" not in html
    assert 'data-ln-context-action="pair-audiobook"' in html
    assert "if(action==='pair-audiobook'){await showLnAudiobookLink(book);return;}" in html
    assert "Math.round(Number(row.probability" not in html
    assert 'id="lnAudioLinkResults" class="ln-audio-link-results"' in html
    assert "padding:15px 17px;font-size:17px;outline:none" in html
    assert "Has audiobook" in html
    assert "Has local audiobook" not in html
    assert "Paired</button>" not in html


def test_explicit_volume_mismatch_is_ranked_below_matching_volume(tmp_path: Path) -> None:
    ln = LightNovelService(_cfg(tmp_path))
    source = tmp_path / "library" / "狼と香辛料2.txt"
    source.write_text("狼と香辛料。商人と賢狼の旅の続きです。" * 50, encoding="utf-8")
    novel = ln.import_file(source)
    assert int(novel["volume"]) == 2

    audio = _audio(tmp_path)
    first = _put_audio(audio, tmp_path / "audio" / "狼と香辛料1.m4b", "狼と香辛料1")
    second = _put_audio(audio, tmp_path / "audio" / "狼と香辛料2.m4b", "狼と香辛料2")
    rows = audio.link_candidates_for_light_novel(int(novel["id"]))
    ids = [int(row["id"]) for row in rows]
    assert ids.index(int(second["id"])) < ids.index(int(first["id"]))
    first_row = next(row for row in rows if int(row["id"]) == int(first["id"]))
    assert "volume mismatch 1/2" in first_row["signals"]


def test_reader_blur_setting_and_click_to_reveal_then_zoom_contract(tmp_path: Path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    assert service.settings_payload()["blur_images"] is False
    assert service.save_settings({"blur_images": True})["blur_images"] is True
    assert LightNovelService(service.config).settings_payload()["blur_images"] is True

    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert 'id="lnrBlurImages"' in html
    assert "'lnrBlurImages'" in html[html.index("const LN_READER_CONTROL_IDS"):]
    assert "root.classList.toggle('blur-images',!!st.blur_images)" in html
    assert ".ln-reader.blur-images .ln-inline-image:not(.revealed) img" in html
    assert "figure?.classList.add('revealed');return;" in html
    assert "window.PudgeCoverPreview?.open?.(image);" in html
    preview = (ROOT / "pudge/web/cover_preview.js").read_text(encoding="utf-8")
    assert "drag.image?.matches?.('.ln-inline-image img')" in preview
    assert "reader?.classList.contains('blur-images')" in preview


def test_image_only_spine_item_stays_inside_logical_chapter(tmp_path: Path) -> None:
    epub = tmp_path / "illustrated.epub"
    container = '''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
    opf = '''<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>狼と香辛料</dc:title></metadata><manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="ill" href="illustration.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/><item id="pic" href="images/pic.jpg" media-type="image/jpeg"/></manifest><spine><itemref idref="c1"/><itemref idref="ill"/><itemref idref="c2"/></spine></package>'''
    c1 = '''<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>第一幕</h1><p>第一幕の本文です。商人と賢狼は旅を続けます。</p></body></html>'''
    ill = '''<html xmlns="http://www.w3.org/1999/xhtml"><body><figure><img src="images/pic.jpg" alt=""/></figure></body></html>'''
    c2 = '''<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>第二幕</h1><p>第二幕の本文です。新しい町へ向かいます。</p></body></html>'''
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/c1.xhtml", c1)
        zf.writestr("OEBPS/illustration.xhtml", ill)
        zf.writestr("OEBPS/c2.xhtml", c2)
        zf.writestr("OEBPS/images/pic.jpg", b"jpeg")

    _title, chapters, _cover = _epub_metadata(epub)
    assert len(chapters) == 2
    assert [title for title, _text in chapters] == ["第一幕", "第二幕"]
    assert "[[PUDGE_EPUB_IMAGE:" in chapters[0][1]
    assert not any(_is_epub_image_only_text(text) for _title, text in chapters)
    assert LightNovelService.CONTENT_SCHEMA >= 7


def test_cmd_a_does_not_select_manga_volumes_inside_reader() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    block = html[html.index("document.addEventListener('keydown',event=>{\n  const editable="):]
    assert "if(ui.page==='manga'&&$('mangaReaderV2')?.classList.contains('open'))return;" in block
