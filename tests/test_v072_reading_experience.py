from __future__ import annotations

import json
import sys
import types
from pathlib import Path


from pudge.audiobooks import AudiobookService
from pudge.database import Database, LATEST_SCHEMA_VERSION
from pudge.light_novels import LightNovelService, _is_technical_epub_section
from pudge.manga import MangaService
from pudge.metadata_cache import MetadataCache
from pudge.reading_audio_alignment import (
    align_light_novel_to_transcript,
    audio_position_for_light_novel,
    light_novel_position_for_audio,
    normalize_reading_text,
)
from pudge.subtitles import stt_worker
from t71g20_support import LN_PAIRED
from test_web_app import make_api
from tests.i18n_support import en, ru


def test_stt_alignment_maps_real_ln_chapters_and_word_clock() -> None:
    first = "吾輩は猫である。名前はまだない。どこで生まれたか見当がつかぬ。"
    second = "何でも薄暗いじめじめした所で泣いていたことだけは記憶している。"
    spoken = normalize_reading_text(first + second)
    alignment = align_light_novel_to_transcript(
        [
            {"chapter_index": 0, "title": "第一章", "text": first},
            {"chapter_index": 3, "title": "第二章", "text": second},
        ],
        [{"start": 10.0, "end": 50.0, "text": spoken}],
        duration=60.0,
        model="test-model",
    )
    assert [row["chapter_index"] for row in alignment["chapters"]] == [0, 3]
    start = audio_position_for_light_novel(alignment, 3, 0.5)
    assert start is not None and 10 < start < 60
    mapped = light_novel_position_for_audio(alignment, start)
    assert mapped is not None
    assert mapped["chapter_index"] == 3
    assert 0.40 <= mapped["chapter_progress"] <= 0.60


def test_epub_technical_sections_are_removed_but_afterword_is_kept() -> None:
    rights = (
        "本書の著作権およびその他の権利は正当な権利を有する第三者に帰属します。"
        "無断転載および複製・転載を禁じます。"
    )
    assert _is_technical_epub_section("奥付", "発行 2026年") is True
    assert _is_technical_epub_section("Chapter 1", rights) is True
    assert _is_technical_epub_section("あとがき", "読者のみなさん、ありがとうございました。") is False


def test_audiobook_bookmarks_sleep_and_light_novel_link(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    source = tmp_path / "book.mp3"
    source.write_bytes(b"audio")
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "_probe", lambda _path: (600.0, []))
    book = service.import_file(source)
    service.set_position(book["id"], 120)

    bookmark = service.add_bookmark(book["id"], "Good scene")
    assert bookmark["book"]["bookmarks"][0]["title"] == "Good scene"
    assert bookmark["book"]["bookmarks"][0]["position"] == 120
    timer = service.set_sleep_timer(book["id"], seconds=900)
    assert 895 <= timer["book"]["sleep_timer_seconds"] <= 900

    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE ln_chapters ("
            "id INTEGER PRIMARY KEY,book_id INTEGER,chapter_index INTEGER,"
            "title TEXT,text TEXT,text_hash TEXT)"
        )
        novel_id = 7
        for index in range(3):
            conn.execute(
                "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
                (novel_id, index, f"Chapter {index + 1}", "text", f"hash-{index}"),
            )
    link = service.link_light_novel(novel_id, book["id"])
    assert link["link"]["book"]["id"] == book["id"]
    audio_id, position = service._paired_audio_position(novel_id, 1, 0.5)
    assert audio_id == book["id"]
    assert position == 300


def test_audiobook_service_prefers_cached_stt_alignment_over_chapter_counts(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "db.sqlite3")
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
        stt_model="test-model",
    )
    monkeypatch.setattr(service, "_probe", lambda _path: (120.0, []))
    audiobook = service.import_file(source)
    first = "吾輩は猫である。名前はまだない。どこで生まれたか見当がつかぬ。"
    second = "何でも薄暗いじめじめした所で泣いていたことだけは記憶している。"
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE ln_chapters ("
            "id INTEGER PRIMARY KEY,book_id INTEGER,chapter_index INTEGER,"
            "title TEXT,text TEXT,text_hash TEXT)"
        )
        for index, title, text in ((0, "第一章", first), (4, "第二章", second)):
            conn.execute(
                "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
                (9, index, title, text, f"hash-{index}"),
            )
    service.link_light_novel(9, audiobook["id"])
    alignment = align_light_novel_to_transcript(
        [
            {"chapter_index": 0, "title": "第一章", "text": first},
            {"chapter_index": 4, "title": "第二章", "text": second},
        ],
        [{"start": 10.0, "end": 110.0, "text": first + second}],
        duration=120.0,
        model="test-model",
    )
    path = service._alignment_path(9, audiobook["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(alignment, ensure_ascii=False), encoding="utf-8")

    audio_id, exact = service._paired_audio_position(9, 4, 0.5)
    assert audio_id == audiobook["id"]
    service.set_position(audio_id, exact)
    state = service.paired_state(9)
    assert state["alignment_mode"] == "stt"
    assert state["ln_chapter_index"] == 4
    assert 0.40 <= state["chapter_progress"] <= 0.60


def test_character_placeholders_survive_spacing_and_cache_is_replaceable(tmp_path: Path) -> None:
    glossary = [{"source": "千早", "preferred": "Chihaya"}]
    protected, replacements = LightNovelService._protect_character_names("千早は走った。", glossary)
    assert "千早" not in protected
    restored = LightNovelService._restore_character_names(
        protected.replace("PUDGEZXQ", "pudge z x q "), replacements
    )
    assert restored == "Chihayaは走った。"

    cache = MetadataCache(tmp_path, "test", schema="v2")
    cache.put({"path": "replaceable"}, {"value": 1})
    assert cache.get({"path": "replaceable"}, ttl_seconds=60) == {"value": 1}
    path = next((tmp_path / "metadata" / "test").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "v2"


def test_manga_series_repairs_legacy_link_and_can_unlink_all_volumes(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    now = 123.0
    with db.connect() as conn:
        for title in ("One Piece Vol. 1", "Vol2 One Piece"):
            conn.execute(
                "INSERT INTO manga_books(path,title,page_count,position,reading_direction,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (str(tmp_path / f"{title}.cbz"), title, 10, 0, "rtl", now, now),
            )
        first_id, second_id = [
            int(row[0]) for row in conn.execute("SELECT id FROM manga_books ORDER BY id").fetchall()
        ]
        conn.execute(
            "UPDATE manga_books SET anilist_id=30013,cover_url='cover',site_url='site' WHERE id=?",
            (first_id,),
        )

    assert LATEST_SCHEMA_VERSION >= 7
    service = MangaService(db, cache_dir=tmp_path / "cache")
    books = service.state()["books"]
    assert {book["anilist_id"] for book in books} == {30013}

    service.unbind_anilist(second_id)
    books = service.state()["books"]
    assert {book["anilist_id"] for book in books} == {None}
    assert all(not book["site_url"] for book in books)


# ---- the window side of v0.7.2 (behaviour; audit 71Б rewrite of the source-text contracts)

# Manga series "Manga" (AniList 30013) with two volumes.
MANGA_SERIES = r"""
__api.manga_state = async () => ({books: [
  {id: 3, title: 'Vol 1', series_title: 'Manga', volume: 1, page_count: 20, position: 0, anilist_id: 30013, site_url: 'https://anilist.co/manga/30013'},
  {id: 4, title: 'Vol 2', series_title: 'Manga', volume: 2, page_count: 20, position: 0, anilist_id: 30013, site_url: 'https://anilist.co/manga/30013'}],
  ocr_available: true});
__api.set_literature_score = async () => ({ok: true});
__api.media_identity_current = async (kind, id) => ({anilist_id: 30013});
__api.media_identity_search = async () => [];
__api.media_identity_unbind = async () => ({ok: true});
"""
MENU = r"""
const menu = () => [...document.querySelectorAll('#contextMenu.open button')].map(b => b.dataset.mangaContextAction);
const rightClick = async selector => { await __fire(selector, 'contextmenu', {button: 2, clientX: 50, clientY: 50}, 'MouseEvent'); await __tick(10); return menu(); };
"""


def test_manga_menus_score_the_series_and_run_ocr_per_volume(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            MENU
            + r"""
            setPage('manga'); await __tick(300);
            const out = {volume: await rightClick('[data-manga-book="3"]')};
            out.series = await rightClick('[data-manga-series-key]');
            out.scoreLabel = __text('[data-manga-context-action="score-series"]');
            await __click('[data-manga-context-action="score-series"]'); await __tick(1500);
            out.modal = {open: __visible('#modalBackdrop'), title: __text('#modalTitle')};
            await __click('#modalBackdrop [data-action="score-anime"]'); await __tick(50);
            out.saved = {open: __visible('#modalBackdrop'), toasts: __toasts()};
            // "Change AniList" on a linked volume offers to unlink it.
            await rightClick('[data-manga-book="3"]');
            await __click('[data-manga-context-action="anilist-search"]'); await __tick(50);
            out.unlinkLabel = __text('#mediaIdentityUnlink');
            const fetched = __apiCalls.filter(c => c.name === 'manga_state').length;
            await __click('#mediaIdentityUnlink'); await __tick(50);
            out.unlinked = {open: __visible('#modalBackdrop'), refreshed: __apiCalls.filter(c => c.name === 'manga_state').length > fetched};
            out.texts = {score: t('action.setScore'), title: t('modal.setScore'), saved: t('toast.scoreSaved'), unlink: PudgeI18n.t('ui.unlink')};
            return out;
            """,
            lang=lang,
            before=MANGA_SERIES,
        )
        out, texts = result.value, result.value["texts"]
        # A volume has its own OCR; the AniList score belongs to the series.
        assert "ocr-book" in out["volume"] and not {"score", "score-series"} & set(out["volume"])
        assert "score-series" in out["series"] and "ocr-series" in out["series"]
        assert out["scoreLabel"] == texts["score"]
        assert out["modal"] == {"open": True, "title": texts["title"]}
        [[kind, book_id, media_id, score]] = result.calls_of("set_literature_score")
        assert (kind, book_id, media_id) == ("manga", 3, 30013) and 1 <= score <= 10
        assert out["saved"]["open"] is False and texts["saved"] in out["saved"]["toasts"]
        assert out["unlinkLabel"] == texts["unlink"]
        assert result.calls_of("media_identity_unbind") == [["manga", 3]]
        assert out["unlinked"] == {"open": False, "refreshed": True}


# Volume 3 (10 pages) whose background preparation is still parsing Jiten words; page 1 has
# three vertical balloons that the OCR lists left, right, middle. Parsing a balloon takes 100 ms.
MANGA_PREPARING = r"""
window.OCR = {running: true, state: 'parsing', parsed_regions: 2, total_regions: 5, total_pages: 10, completed: 3, cached_pages: 3};
__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 10, position: 0, ocr_complete: false}], ocr_available: true});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 10, name: `p${index}.png`, spread: false, data_uri: 'data:image/png;base64,AAAA'});
__api.manga_ocr_book_status = async () => ({...OCR});
window.REGIONS = [
  {text: '左', x: .1, y: .5, width: .05, height: .3, orientation: 'vertical'},
  {text: '右', x: .8, y: .5, width: .05, height: .3, orientation: 'vertical'},
  {text: '中', x: .45, y: .5, width: .05, height: .3, orientation: 'vertical'},
];
__api.manga_text_regions = async (id, index) => ({book_id: id, page_index: index, available: true, cached: true, regions: index === 0 ? REGIONS : []});
window.PARSING = 0; window.MAX_PARSING = 0;
__api.study_parse_text = async text => {
  PARSING += 1; MAX_PARSING = Math.max(MAX_PARSING, PARSING);
  await new Promise(resolve => setTimeout(resolve, 100));
  PARSING -= 1;
  return {paragraphs: [text], tokens: [[{wordId: 1, readingIndex: 0, start: 0, end: 1}]], vocabulary: [{wordId: 1, readingIndex: 0, spelling: text, states: ['new']}]};
};
"""


def test_manga_reader_shows_preparation_progress_then_reads_balloons_in_order(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(100);
            const balloons = () => [...document.querySelectorAll('.manga-v2-text-region')].map(r => r.querySelector('.manga-v2-region-content')?.innerText);
            const out = {preparing: __text('#mangaV2OcrProgress'), early: balloons()};
            OCR.parsed_regions = 4; await __tick(800);
            out.later = __text('#mangaV2OcrProgress');
            Object.assign(OCR, {running: false, state: 'ready', complete: true, completed: 10, cached_pages: 10});
            await __tick(2000);
            out.ready = __text('#mangaV2OcrProgress');
            out.balloons = balloons();
            out.words = document.querySelectorAll('.manga-v2-text-region .pudge-study-word').length;
            out.parallel = MAX_PARSING;
            out.texts = [2, 4].map(done => PudgeI18n.t('manga.preparingJitenProgress', {done, total: 5}) + ' · OCR 3/10').concat([PudgeI18n.t('manga.ocrReady') + ' · 10/10']);
            return out;
            """,
            lang=lang,
            before=MANGA_PREPARING,
            state={"settings": {"review_gate_manga_enabled": False}},
        )
        out = result.value
        assert [out["preparing"], out["later"], out["ready"]] == out["texts"]
        # A page Mokuro has read shows its text while the rest of the volume is still read.
        assert out["early"] == ["右", "中", "左"]
        # Only the cached OCR is read; the balloons are parsed one at a time in reading order
        # (right to left) and each shows its words.
        assert result.calls_of("manga_text_regions") == [[3, 0, False, True]]
        assert [args[0] for args in result.calls_of("study_parse_text")] == ["右", "中", "左"]
        assert out["parallel"] == 1
        assert out["balloons"] == ["右", "中", "左"] and out["words"] == 3
    assert en("manga.preparingJitenProgress", done=2, total=5) == "Preparing Jiten 2/5"
    assert ru("manga.preparingJitenProgress", done=2, total=5) == "Готовлю Jiten 2/5"


def test_manga_reader_keeps_only_the_eight_latest_pages(page) -> None:
    result = page.run(
        r"""
        await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(300);
        const fetched = () => __apiCalls.filter(c => c.name === 'manga_page').map(c => c.args[1]);
        for (let i = 0; i < 11; i++) { await __click('[data-manga-v2-action="next"]'); await __tick(300); }
        const forward = fetched();
        for (let i = 0; i < 11; i++) { await __click('[data-manga-v2-action="previous"]'); await __tick(300); }
        return {forward, back: fetched().slice(forward.length), label: __text('#mangaV2PageLabel')};
        """,
        before=r"""
        __api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 20, position: 0, ocr_complete: true}], ocr_available: true});
        __api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 20, name: `p${index}.png`, spread: false, data_uri: 'data:image/png;base64,AAAA'});
        __api.manga_text_regions = async (id, index) => ({book_id: id, page_index: index, available: true, cached: true, regions: []});
        """,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    # Reading to page 12 (13 preloaded): pages 6-13 are still in memory, 1-5 are fetched again.
    assert result.value["forward"] == list(range(13))
    assert result.value["back"] == [4, 3, 2, 1, 0]
    assert result.value["label"] == "1 / 20"


def test_audiobook_card_has_a_scrubber_sleep_timer_and_collapsible_chapters(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('audiobooks'); await __tick(300);
            const card = '[data-audiobook-id="5"]';
            const chapters = () => ({show: __visible(`${card} .audiobook-chapters-open`), hide: __visible(`${card} .audiobook-chapters-close`),
              list: [...document.querySelectorAll(`${card} .chapter-list button`)].filter(b => b.checkVisibility()).map(b => b.textContent)});
            const out = {closed: chapters(), labels: [PudgeI18n.t('media.show'), PudgeI18n.t('media.hide')]};
            await __click(`${card} .audiobook-chapters summary`); await __tick(50);
            out.opened = chapters();
            out.openText = __text(`${card} .audiobook-chapters summary`);
            await __input(`${card} .audiobook-scrubber`, '450'); await __tick(50);
            out.sleep = [...document.querySelectorAll(`${card} [data-audio-sleep] option`)].map(o => o.value);
            await __input(`${card} [data-audio-sleep]`, '30'); await __tick(50);
            return out;
            """,
            lang=lang,
            before=r"""
            const BOOK = {id: 5, title: 'Audio', duration: 600, position: 120, speed: 1, bookmarks: [],
                          chapters: [{title: 'One', start: 0}, {title: 'Two', start: 300}]};
            __api.audiobook_state = async () => ({books: [BOOK]});
            __api.audiobook_seek_to = async (id, position) => ({ok: true, book: {...BOOK, position}});
            __api.audiobook_sleep_timer = async () => ({ok: true, book: BOOK});
            """,
        )
        out = result.value
        assert out["closed"] == {"show": True, "hide": False, "list": []}
        assert out["opened"] == {"show": False, "hide": True, "list": ["One", "Two"]}
        assert out["labels"][1] in out["openText"]
        assert result.calls_of("audiobook_seek_to") == [[5, 450]]
        assert out["sleep"] == ["off", "15", "30", "45", "60", "chapter"]
        assert result.calls_of("audiobook_sleep_timer") == [[5, "30"]]


SUGGESTION = {
    "media_id": 154587,
    "media_kind": "anime",
    "format": "TV",
    "title": "Frieren",
    "native_title": "葬送のフリーレン",
    "description": "An elf mage outlives her party.\nShe sets out again.",
    "year": 2023,
    "episodes": 28,
    "genres": ["Adventure", "Drama"],
    "cover": "",
    "site_url": "https://anilist.co/anime/154587",
    "media_status": "FINISHED",
    "mean_score": 91,
}


def test_planning_search_suggests_anilist_titles_with_their_details(page) -> None:
    result = page.run(
        r"""
        setPage('planned', true); await __tick(50);
        await __input('#plannedSearch', 'fr'); await __tick(400);          // too short: no request
        await __input('#plannedSearch', 'Frieren'); await __tick(400);
        const info = document.querySelector('#plannedSuggestions .planned-suggestion-info');
        return {title: info?.querySelector('strong')?.textContent, text: info?.innerText, add: __text('[data-planning-suggestion="154587"]'),
                addLabel: t('ui.add'), description: info?.querySelector('p')?.textContent};
        """,
        api={"planning_search_anilist": [SUGGESTION]},
    )
    out = result.value
    assert result.calls_of("planning_search_anilist") == [["Frieren"]]
    assert out["title"] == "Frieren" and out["add"] == out["addLabel"]
    assert out["description"] == SUGGESTION["description"]
    assert "Adventure · Drama" in out["text"]


def test_planning_search_asks_anilist_for_plain_text_descriptions(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    api.config.anilist.enabled = True
    api.config.anilist.access_token = "token"
    queries: list[str] = []

    def post(query: str, variables: dict) -> dict:
        queries.append(query)
        return {
            "anime": {
                "media": [
                    {
                        "id": 154587,
                        "format": "TV",
                        "status": "FINISHED",
                        "meanScore": 91,
                        "seasonYear": 2023,
                        "episodes": 28,
                        "genres": ["Adventure"],
                        "title": {"userPreferred": "Frieren"},
                        "description": "An elf mage<br>outlives &amp; <i>remembers</i>.",
                        "siteUrl": "https://anilist.co/anime/154587",
                    }
                ]
            },
            "literature": {"media": []},
        }

    monkeypatch.setattr(api.light_novels, "_anilist_post", post)
    assert api.planning_search_anilist("fr") == []  # too short to search
    [row] = api.planning_search_anilist("Frieren")
    assert row["title"] == "Frieren" and row["media_kind"] == "anime"
    assert row["description"] == "An elf mage\noutlives & remembers."
    # Both the anime and the manga/novel search ask for plain text.
    assert len(queries) == 1 and " ".join(queries[0].split()).count("description(asHtml:false)") == 2
    # The same search again comes from the cache.
    assert (
        api.planning_search_anilist("frieren")[0]["description"] == row["description"] and len(queries) == 1
    )


def test_listen_together_tray_controls_and_the_highlighted_passage(page) -> None:
    result = page.run(
        r"""
        await __settle(openLightNovel(6)); await __tick(400);
        const tray = [...$('lnPairedTray').querySelectorAll('button,input,select')]
          .filter(n => n.checkVisibility() && !n.matches('.pudge-select-button')).map(n => n.id || n.dataset.lnAudioSeek);
        await __input('#lnPairedSpeed', '1.5'); await __tick(20);
        await __input('#lnPairedScrubber', '23.5'); await __tick(20);
        return {tray, align: $('lnPairedAlign'), passage: [...document.querySelectorAll('#lnReader .ln-paired-current')].map(p => p.dataset.lnParagraph),
                word: document.querySelector('#lnReader .ln-paired-word-current')?.textContent};
        """,
        # The backend gives only the character offset of the position (no anchor path).
        before=LN_PAIRED
        + r"""
        const withAnchors = pairedState;
        window.pairedState = () => { const state = withAnchors(); delete state.anchor_window; delete state.chapter_char_offset_exact;
                                     return {...state, chapter_char_offset: Math.floor(state.position)}; };
        """,
    )
    out = result.value
    assert out["tray"] == [
        "lnPairedAudio",
        "-15",
        "15",
        "lnPairedScrubber",
        "lnPairedSpeed",
        "lnPairedAutoScroll",
    ]
    assert out["align"] is None  # the manual "Align" button is gone
    assert result.calls_of("audiobook_set_speed") == [[9, 1.5]]
    assert result.calls_of("audiobook_seek_to") == [[9, 23.5]]
    assert out["passage"] == ["2"] and out["word"] == "いいい"


def test_listen_prepares_the_alignment_first_when_there_is_none(page) -> None:
    result = page.run(
        r"""
        await __settle(openLightNovel(6)); await __tick(400);
        await __click('#lnPairedAudio'); await __tick(50);
        return null;
        """,
        before=LN_PAIRED
        + r"""
        const prepared = pairedState;
        window.pairedState = () => ({...prepared(), alignment: {ready: false, status: 'not_prepared'}, player_running: false, playing: false});
        __api.light_novel_prepare_audio_alignment = async () => ({status: 'queued'});
        """,
    )
    assert result.calls_of("light_novel_prepare_audio_alignment") == [[6, False]]
    assert not result.called("light_novel_play_paired")


def test_prepare_alignment_reaches_the_audiobook_service(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(
        api.audiobooks,
        "prepare_alignment",
        lambda book_id, force=False: calls.append((book_id, force)) or {"status": "queued"},
    )
    assert api.light_novel_prepare_audio_alignment("6", 1) == {"status": "queued"}
    assert calls == [(6, True)]


def test_stt_worker_returns_word_timestamps_when_asked(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = lambda audio, **options: (
        calls.append(options) or {"text": "ねこ", "segments": [{"words": [{"word": "ねこ"}]}]}
    )
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"audio")
    assert stt_worker.main(["--words", str(audio), str(tmp_path / "words.json"), "model"]) == 0
    assert stt_worker.main([str(audio), str(tmp_path / "plain.json"), "model"]) == 0
    assert [call["word_timestamps"] for call in calls] == [True, False]
    assert calls[0]["language"] == "ja" and calls[0]["path_or_hf_repo"] == "model"
    assert json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))["segments"][0]["words"] == [
        {"word": "ねこ"}
    ]


def test_continue_watching_card_menu_and_play_do_not_block_each_other(page) -> None:
    # The old 700 ms "ignore clicks after a right-click" lock swallowed real clicks; the
    # context menu also has to open while playback is starting.
    result = page.run(
        r"""
        setPage('current', true); renderCurrent(); await __tick(50);
        const card = '[data-continue-card="1"]';
        const menuOpen = () => __visible('#contextMenu');
        await __fire(card, 'contextmenu', {button: 2, clientX: 10, clientY: 10}, 'MouseEvent'); await __tick(20);
        const out = {menu: menuOpen(), playsAfterMenu: __apiCalls.filter(c => c.name === 'play').length};
        await __fire(card, 'pointerup', {button: 0, ctrlKey: true}, 'PointerEvent'); await __tick(20);   // Ctrl+click is a secondary click
        out.playsAfterCtrl = __apiCalls.filter(c => c.name === 'play').length;
        await __fire('#scroll', 'pointerdown', {button: 0}, 'PointerEvent');
        await __fire(card, 'pointerup', {button: 0}, 'PointerEvent'); await __tick(20);    // right after the menu
        out.plays = __apiCalls.filter(c => c.name === 'play').map(c => c.args[0]);
        await __click('#scroll'); await __tick(20);
        out.closed = !menuOpen();
        await __fire(card, 'contextmenu', {button: 2, clientX: 10, clientY: 10}, 'MouseEvent'); await __tick(20);
        out.menuWhileStarting = {open: menuOpen(), starting: document.querySelector(card).classList.contains('play-starting')};
        return out;
        """,
        state={
            "home": {
                "continue_watching": [
                    {
                        "media_id": 7,
                        "title": "Show",
                        "episode": 3,
                        "video_path": "/lib/Show - 03.mkv",
                        "progress_percent": 40,
                        "resume_start": 600,
                        "site_url": "https://anilist.co/anime/7",
                    }
                ]
            }
        },
        before="__api.play = async () => ({ok: true, status: 'starting'});",
    )
    out = result.value
    assert out["menu"] is True and out["playsAfterMenu"] == 0
    assert out["plays"] == ["/lib/Show - 03.mkv"]
    assert out["playsAfterCtrl"] == 0
    assert out["closed"] is True and out["menuWhileStarting"] == {"open": True, "starting": True}


def test_hovering_a_study_word_opens_its_card_once(page) -> None:
    # Sidebar live text and the VN reader open the word card on hover (one listener, one request).
    result = page.run(
        r"""
        const box = document.createElement('div');
        box.setAttribute('data-pudge-study-hover', '');
        box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: ['猫'], tokens: [[{wordId: 1, readingIndex: 0, start: 0, end: 1}]],
          vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫', reading: 'ねこ', states: ['new']}]}, {backend: 'jiten'});
        document.body.appendChild(box);
        await __fire('[data-pudge-study-token]', 'pointerover', {}, 'PointerEvent'); await __tick(100);
        const early = PudgeReadingTools.study.isOpen();
        await __tick(200);
        return {early, open: PudgeReadingTools.study.isOpen()};
        """
    )
    assert result.value == {"early": False, "open": True}
    assert len(result.calls_of("study_word")) == 1


def test_media_tabs_only_reload_after_actual_navigation(page) -> None:
    result = page.run(
        r"""
        const count = name => __apiCalls.filter(c => c.name === name).length;
        const nav = async name => { await __click(`.nav button[data-page="${name}"]`); await __tick(300); };
        const out = {};
        await nav('manga');
        const library = $('mangaContent').firstElementChild;
        out.manga = count('manga_state');
        await nav('manga');                                     // the tab that is already open
        out.again = {manga: count('manga_state'), same: $('mangaContent').firstElementChild === library};
        const lnBefore = count('light_novel_state');
        await nav('lightnovels');
        const shelf = $('lightNovelsContent').firstElementChild;
        await nav('lightnovels');
        out.ln = {same: $('lightNovelsContent').firstElementChild === shelf, fetched: count('light_novel_state') - lnBefore};
        // Leaving a tab before it started loading: it never loads.
        await __click('.nav button[data-page="audiobooks"]');
        await __click('.nav button[data-page="settings"]'); await __tick(300);
        out.audio = count('audiobook_state');
        return out;
        """
    )
    out = result.value
    assert out["manga"] == 1
    assert out["again"] == {"manga": 1, "same": True}
    assert out["ln"] == {"same": True, "fetched": 0}
    assert out["audio"] == 0
