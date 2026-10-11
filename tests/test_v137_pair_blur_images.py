from __future__ import annotations

import json
import zipfile
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService, _epub_metadata, _is_epub_image_only_text
from tests.css_support import css_rules
from tests.i18n_support import en, has_text, ru


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
        files=[
            {"index": 0, "path": str(source), "title": title, "duration": 100.0, "start": 0.0, "end": 100.0}
        ],
        chapters=[{"index": 0, "title": title, "start": 0.0, "end": 100.0}],
    )


def test_word_card_pitch_cannot_be_selected_and_the_card_header_is_compact() -> None:
    # The pitch diagram itself is drawn by the card (test_v072_jiten_word_themes_pitch); the
    # reader's own pitch controls stay removed (test_v136_vertical_pair_pitch_chapters_volumes).
    card = css_rules("reading_tools.css")
    for selector in (".pudge-study-pitch", ".pudge-study-pitch *"):
        assert card.get(selector, "user-select") in {"none", "none!important"}
    assert card.get(".pudge-study-pitch *", "user-select") == "none!important"
    assert card.get(".pudge-study-card", "padding") == "9px 14px 14px"
    assert card.get(".pudge-study-head", "min-height") == "44px"


NOVEL = {"id": 9, "title": "Novel 1", "volume": 1, "character_count": 1000}
PAIRING = r"""
let linked = false;
__api.light_novel_state = async () => ({version: linked ? 2 : 1, settings: {},
  books: [{...NOVEL, ...(linked ? {paired_audio: {audiobook_id: 77}} : {})}]});
__api.light_novel_audiobook_candidates = async () => [{id: 77, title: 'Novel 1 audiobook', signals: ['title', 'volume'], probability: 0.83}];
__api.light_novel_link_audiobook = async () => { linked = true; return {ok: true}; };
__api.light_novel_open = async id => ({...NOVEL, id, paired_audio: {audiobook_id: 77}});
""".replace("NOVEL", json.dumps(NOVEL))


def test_pairing_is_in_the_context_menu_and_candidates_show_no_percent(page) -> None:
    assert (en("ui.hasAudiobook"), ru("ui.hasAudiobook")) == ("Has audiobook", "Есть аудиокнига")
    assert not has_text("Has local audiobook")
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('lightnovels'); await __tick(50);
            const card = () => document.querySelector('#lightNovelsContent [data-ln-book="9"]');
            const cardButtons = [...card().querySelectorAll('button')].map(b => b.innerText);
            await __fire('#lightNovelsContent [data-ln-book="9"]', 'contextmenu', {clientX: 20, clientY: 20}, 'MouseEvent');
            const item = document.querySelector('#contextMenu [data-ln-context-action=pair-audiobook]');
            const menu = item?.innerText;
            item.click(); await __tick(50);
            const modal = {title: __text('#modalTitle'), results: __text('#lnAudioLinkResults')};
            await __click('#lnAudioLinkResults [data-ln-link-audio="77"]'); await __tick(100);
            return {cardButtons, menu, modal, badge: card()?.querySelector('.ln-paired-audio-badge')?.getAttribute('aria-label') ?? null,
                    toasts: __toasts(), texts: {pair: t('ui.pairAudiobook'), title: t('ui.pairWithAudiobook'), badge: t('ui.hasAudiobook'),
                    linked: t('ui.audiobookLinked')}};
            """,
            lang=lang,
            before=PAIRING,
        )
        out = result.value
        texts = out["texts"]
        # No pairing button on the card: pairing lives in the context menu.
        assert texts["pair"] not in out["cardButtons"] and out["menu"] == texts["pair"]
        assert out["modal"]["title"] == texts["title"]
        assert "Novel 1 audiobook" in out["modal"]["results"] and "title · volume" in out["modal"]["results"]
        assert "%" not in out["modal"]["results"]  # the match probability is not shown
        assert result.calls_of("light_novel_audiobook_candidates") == [[9, "", 50]]
        assert result.calls_of("light_novel_link_audiobook") == [[9, 77]]
        # The library is reloaded: the card shows the audiobook badge.
        assert out["badge"] == texts["badge"]
        assert texts["linked"] in out["toasts"]
    search = css_rules("index.html").declarations(".pudge-search-surface>.pudge-search-input")
    assert (search["padding"], search["font-size"]) == ("15px 17px", "17px")


def test_reader_blur_setting_and_click_to_reveal_then_zoom_contract(tmp_path: Path, page) -> None:
    service = LightNovelService(_cfg(tmp_path))
    assert service.settings_payload()["blur_images"] is False
    assert service.save_settings({"blur_images": True})["blur_images"] is True
    assert LightNovelService(service.config).settings_payload()["blur_images"] is True

    result = page.run(
        r"""
        $('lnReaderShell').classList.add('open');
        populateLnReaderAppearance({blur_images: false}); applyLnReaderSettings({blur_images: false});
        $('lnReader').innerHTML = '<figure class="ln-inline-image"><img src="covers/illustration.jpg" alt=""></figure>';
        const press = async () => {
          for (const type of ['pointerdown', 'pointerup']) await __fire('#lnReader .ln-inline-image img', type, {button: 0, pointerId: 3, pointerType: 'mouse', clientX: 50, clientY: 50}, 'PointerEvent');
          await __fire('#lnReader .ln-inline-image img', 'click', {button: 0, clientX: 50, clientY: 50}, 'MouseEvent');
          await __tick(50);
        };
        const state = () => ({blurred: $('lnReader').classList.contains('blur-images'), revealed: document.querySelector('#lnReader .ln-inline-image').classList.contains('revealed'),
                              preview: PudgeCoverPreview.isOpen()});
        await __input('#lnrBlurImages', true); await __tick(300);
        const on = state();
        await press();
        const first = state();
        await press();
        const second = state();
        PudgeCoverPreview.close();
        // A click without pointer events (keyboard, accessibility) reveals first too.
        $('lnReader').innerHTML = '<figure class="ln-inline-image"><img src="covers/illustration.jpg" alt=""></figure>';
        await __click('#lnReader .ln-inline-image img');
        const keyboard = state();
        await __click('#lnReader .ln-inline-image img');
        const keyboardAgain = state();
        PudgeCoverPreview.close();
        await __input('#lnrBlurImages', false); await __tick(300);
        $('lnReader').innerHTML = '<figure class="ln-inline-image"><img src="covers/illustration.jpg" alt=""></figure>';
        await press();
        return {on, first, second, keyboard, keyboardAgain, plain: state()};
        """,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": []}},
    )
    out = result.value
    assert out["on"] == {"blurred": True, "revealed": False, "preview": False}
    # The first click only reveals the blurred picture; the next one zooms it.
    assert out["first"] == {"blurred": True, "revealed": True, "preview": False}
    assert out["second"]["preview"] is True
    assert out["keyboard"] == out["first"] and out["keyboardAgain"]["preview"] is True
    # Without blurring the first click zooms.
    assert out["plain"] == {"blurred": False, "revealed": False, "preview": True}
    assert [call[0]["blur_images"] for call in result.calls_of("light_novel_save_settings")] == [True, False]
    rules = css_rules("index.html")
    assert rules.get(".ln-reader.blur-images .ln-inline-image:not(.revealed) img", "filter") == "blur(44px)"


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


def test_image_only_spine_item_stays_inside_logical_chapter(tmp_path: Path) -> None:
    epub = tmp_path / "illustrated.epub"
    container = """<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
    opf = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>狼と香辛料</dc:title></metadata><manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="ill" href="illustration.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/><item id="pic" href="images/pic.jpg" media-type="image/jpeg"/></manifest><spine><itemref idref="c1"/><itemref idref="ill"/><itemref idref="c2"/></spine></package>"""
    c1 = """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>第一幕</h1><p>第一幕の本文です。商人と賢狼は旅を続けます。</p></body></html>"""
    ill = """<html xmlns="http://www.w3.org/1999/xhtml"><body><figure><img src="images/pic.jpg" alt=""/></figure></body></html>"""
    c2 = """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>第二幕</h1><p>第二幕の本文です。新しい町へ向かいます。</p></body></html>"""
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


def test_cmd_a_does_not_select_manga_volumes_inside_reader(page) -> None:
    result = page.run(
        r"""
        setPage('manga'); await __tick(50);
        const selected = () => PudgeMangaReaderV2.selectedBookIds().map(Number).sort();
        await __key('a', {metaKey: true, code: 'KeyA'}, 'body');
        const library = selected();
        PudgeMangaReaderV2.clearSelection();
        await __settle(PudgeMangaReaderV2.openBook(3));
        const opened = $('mangaReaderV2').classList.contains('open');
        await __key('a', {metaKey: true, code: 'KeyA'}, 'body');
        return {library, opened, reader: selected()};
        """,
        before=(
            "__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 40, position: 0},"
            " {id: 4, title: 'Vol 2', series_title: 'Manga', page_count: 40, position: 0}], ocr_available: false});"
            "__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 40, name: `p${index}.png`,"
            " spread: false, data_uri: 'data:image/png;base64,AAAA'});"
        ),
    )
    assert result.value == {"library": [3, 4], "opened": True, "reader": []}
