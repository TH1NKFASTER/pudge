"""v1.49: reading progress by characters, find in a light-novel chapter, global search without
waiting for AniList, the foil shine while the anime list scrolls (behaviour; audit 71Б rewrite).

Window checks run index.html in node (tests/page_harness.py). That the library shows the new
progress once the reader is closed is covered by
test_patch_e_ui.py::test_ln_shelf_is_not_rebuilt_while_reading."""

from pathlib import Path
import time
from types import SimpleNamespace

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.manager_models import LibraryAnime
from pudge.web_app import WebAppApi
from t71g21_support import computed_style
from test_web_app import make_api


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    return LightNovelService(cfg)


def _two_chapter_book(service: LightNovelService, tmp_path: Path) -> int:
    # Chapter 1: 100 characters, chapter 2: 900; the bookmark is in chapter 2 at 90 %.
    now = time.time()
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO ln_books(
                title,file_path,file_type,current_chapter,current_offset,
                finished,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("Book", str(tmp_path / "book.txt"), "txt", 1, 0.9, 0, now, now),
        )
        book_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 0, "One", "a" * 100, "one"),
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 1, "Two", "b" * 900, "two"),
        )
    return book_id


def test_bookmark_progress_summary_moves_back_immediately_by_characters(tmp_path: Path) -> None:
    service = _service(tmp_path)
    book_id = _two_chapter_book(service, tmp_path)

    service.save_bookmark(book_id, 0, 0.1, source="manual")
    summary = service.progress_summary(book_id)
    assert summary["current_chapter"] == 0
    assert summary["current_offset"] == 0.1
    assert summary["character_count"] == 1000
    assert summary["read_character_count"] == 10.0
    assert summary["reading_progress_percent"] == 1.0


def test_bookmark_answer_carries_the_new_progress_for_the_library(tmp_path: Path) -> None:
    # The window repaints the shelf from this answer without asking for the whole state.
    api = make_api(tmp_path)
    book_id = _two_chapter_book(api.light_novels, tmp_path)
    result = api.light_novel_bookmark(book_id, 0, 0.1, "manual")
    assert result["ok"] is True and result["bookmark_source"] == "manual"
    assert (result["current_chapter"], result["current_offset"]) == (0, 0.1)
    assert (
        result["read_character_count"],
        result["character_count"],
        result["reading_progress_percent"],
    ) == (10.0, 1000, 1.0)


def test_read_up_to_here_saves_the_character_position_of_the_word(page) -> None:
    # "Read up to here" on 猫 in the second paragraph: 8 of the chapter's 13 characters come
    # before it (猫が好き。 + line break + 犬も). It used to save the scroll position instead.
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            await __settle(openLightNovel(6)); await __tick(300);
            await __click('#lnReader [data-ln-token="1:1"]'); await __tick(20);
            const label = __text('[data-pudge-study-extra-action="bookmark-here"]');
            await __click('[data-pudge-study-extra-action="bookmark-here"]'); await __tick(20);
            return {label, toast: __toasts().at(-1), expected: [t('ui.readUpToHere'), t('ui.bookmarkAndProgressUpdated')]};
            """,
            lang=lang,
            before=r"""
            __api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
            __api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, text: '猫が好き。\n犬も猫も好き。',
              paragraphs: ['猫が好き。', '犬も猫も好き。'],
              tokens: [[{start: 0, end: 1, wordId: 1, readingIndex: 0}], [{start: 0, end: 1, wordId: 2, readingIndex: 0}, {start: 2, end: 3, wordId: 1, readingIndex: 0}]],
              vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫', reading: 'ねこ', cardState: ['new']},
                           {wordId: 2, readingIndex: 0, spelling: '犬', reading: 'いぬ', cardState: ['new']}]});
            __api.light_novel_bookmark = async (id, chapter, offset) => ({current_chapter: chapter, current_offset: offset});
            """,
            state={"settings": {"review_gate_ln_enabled": False}},
        )
        assert [result.value["label"], result.value["toast"]] == result.value["expected"]
        [[book, chapter, offset, source]] = result.calls_of("light_novel_bookmark")
        assert (book, chapter, source) == (6, 0, "manual")
        assert abs(offset - 8 / 13) < 1e-9


SHOW = {
    "media_id": 7,
    "title": "Show",
    "episode": 3,
    "video_path": "/library/Show - 03.mkv",
    "cover": "https://img.example/7.jpg",
    "resume_start": 300,
    "progress_percent": 20,
}


def test_foil_covers_keep_their_shine_while_the_anime_list_scrolls(page) -> None:
    # Scrolling Watching used to switch the foil (polychrome) effect off and ignore hovers.
    result = page.run(
        computed_style("filter", "box-shadow", "animation")
        + r"""
        await __tick(3000);                                           // the start-up shine is over
        const cover = document.querySelector('#currentContent .cover-shell.polychrome'), img = cover.querySelector('img');
        const look = () => ({filter: __css(img, 'filter'), shadow: __css(cover, 'box-shadow'), sweep: __css(cover, 'animation', 'before')});
        const out = {idle: look()};
        await __fire('#scroll', 'scroll');
        out.scrolling = document.documentElement.classList.contains('ui-anime-scrolling');
        out.whileScrolling = look();
        await __fire('#currentContent .cover-shell.polychrome img', 'pointerover', {}, 'PointerEvent');
        out.hovered = look();
        return out;
        """,
        state={"home": {"continue_watching": [SHOW]}},
    )
    out = result.value
    assert out["scrolling"] is True
    assert out["idle"]["filter"] == "contrast(1.1) saturate(1.28) brightness(1.05)" and out["idle"][
        "shadow"
    ].startswith("0 0 0 2px")
    assert out["idle"]["sweep"] == "none"
    assert out["whileScrolling"] == out["idle"]
    assert out["hovered"] == {**out["idle"], "sweep": "polychrome-flow 1.875s ease-out 1 both"}


def test_global_search_uses_cached_aliases_and_never_waits_for_anilist(tmp_path: Path, monkeypatch) -> None:
    # The local search answers at once: AniList aliases already fetched are used, missing
    # ones are not looked up on the way (that waited for the network on every keystroke).
    monkeypatch.setattr(
        "pudge.web_app._global_search_score",
        lambda query, names: next(
            ((100.0, n) for n in names if query.casefold() in str(n).casefold()), (0.0, "")
        ),
    )
    api = WebAppApi.__new__(WebAppApi)
    anime = [
        LibraryAnime(media_id=5, title="Ookami to Koushinryou", status="CURRENT"),
        LibraryAnime(media_id=6, title="Other Show", status="CURRENT"),
    ]
    api.manager = SimpleNamespace(
        db=SimpleNamespace(
            anime_list=lambda *a, **k: list(anime), episodes=lambda *a, **k: [], downloads=lambda: []
        ),
        incomplete_download_paths=lambda: set(),
        _path_within=lambda *_: False,
    )

    asked: list[object] = []
    api.light_novels = SimpleNamespace(
        state=lambda: {"books": []}, _anilist_post=lambda *args, **kwargs: asked.append(args) or {}
    )
    api.manga = SimpleNamespace(state=lambda: {"books": []})
    api.audiobooks = SimpleNamespace(search_catalog=lambda: [])
    api.config = SimpleNamespace(anilist=SimpleNamespace(enabled=True, access_token="token"))
    api.logger = SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None
    )
    api._cover_uri = lambda item: ""
    api._global_search_anilist_alias_cache = {5: ["Spice and Wolf"]}
    rows = api.global_media_search("spice and wolf", 40)
    assert [row["media_id"] for row in rows if row["kind"] == "anime"] == [5]
    assert asked == []


FIND_BOOK = r"""
window.PARAS = Array.from({length: 30}, (_, i) => '猫が猫を見た。' + (i % 3 === 0 ? '犬もいた。' : ''));
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: PARAS,
  tokens: PARAS.map(p => [{start: 0, end: 1, wordId: 1, readingIndex: 0}, {start: 2, end: 3, wordId: 1, readingIndex: 0}, {start: 4, end: 6, wordId: 3, readingIndex: 0}]
    .concat(p.length > 7 ? [{start: 7, end: 8, wordId: 2, readingIndex: 0}] : [])),
  vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫', reading: 'ねこ', cardState: ['new']}, {wordId: 2, readingIndex: 0, spelling: '犬', reading: 'いぬ', cardState: ['new']},
               {wordId: 3, readingIndex: 0, spelling: '見る', reading: 'みる', cardState: ['known']}]});
"""


def test_find_in_chapter_marks_every_hit_with_one_lookup_per_paragraph(page) -> None:
    # 30 paragraphs, 猫 twice in each, 犬 in every third. ⌘F in the reader finds words by
    # their text or their reading; the search looks each paragraph up once (it used to
    # query the page again for every word of every hit) and logs a slow search.
    result = page.run(
        r"""
        await __settle(openLightNovel(6)); await __tick(100);
        await __key('f', {code: 'KeyF', metaKey: true}, 'body'); await __tick(50);
        const reader = $('lnReader'), clock = performance.now;
        let lookups = 0, lookupCost = 0, spent = 0;
        performance.now = () => clock() + spent;
        for (const name of ['querySelector', 'querySelectorAll']) { const own = reader[name]; reader[name] = function (...args) { lookups++; spent += lookupCost; return own.apply(this, args); }; }
        const search = async query => {
          lookups = 0; await __input('#lnFindInput', query); await __tick(60);
          return {hits: document.querySelectorAll('#lnReader .ln-find-hit').length, active: document.querySelectorAll('#lnReader .ln-find-active').length,
                  count: __text('#lnFindCount'), lookups};
        };
        const out = {bar: __visible('#lnFindBar'), dog: await search('犬'), cat: await search('猫'), reading: await search('ねこ')};
        // A slow machine (5 ms per lookup): the search is logged. The harness reports only
        // warnings and errors of the console, so the log is read here.
        const logged = []; const info = console.info; console.info = (...args) => { logged.push(args.join(' ')); info(...args); };
        lookupCost = 5; await search('猫が');
        out.logged = logged;
        return out;
        """,
        before=FIND_BOOK,
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    out = result.value
    assert out["bar"] is True
    assert {key: out["dog"][key] for key in ("hits", "active", "count")} == {
        "hits": 10,
        "active": 1,
        "count": "1/10",
    }
    assert {key: out["cat"][key] for key in ("hits", "count")} == {"hits": 60, "count": "1/60"}
    assert {key: out["reading"][key] for key in ("hits", "count")} == {"hits": 60, "count": "1/60"}
    assert all(out[name]["lookups"] <= 30 + 3 for name in ("dog", "cat", "reading"))
    assert [line.split(" tokens=")[0] for line in out["logged"]] == [
        "[LN perf] find query_len=2 paragraphs=30"
    ]
