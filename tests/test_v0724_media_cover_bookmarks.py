"""Light novel audiobook badge, manga covers, removed mpv shortcuts, audiobook bookmarks,
cover preview gestures and Cmd+F in the readers (behaviour; audit 71Б rewrite).

Window checks run the real index.html in node (tests/page_harness.py, fixture `page`)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import load_config, write_config
from pudge.database import Database
from pudge.manga import MangaService
from test_skip_segments_playback import _launch
from test_web_app import make_api
from tests.css_support import css_rules
from tests.i18n_support import en, has_text
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario

OLD_AUDIOBOOK_BADGES = ("Has local audiobook", "Has TTS-generated audiobook")


def test_ln_cards_show_an_audiobook_badge_with_a_tooltip(page) -> None:
    assert en("ui.hasAudiobook") == "Has audiobook"
    assert not any(has_text(text) for text in OLD_AUDIOBOOK_BADGES)
    books = [
        {"id": 4, "title": "Paired", "volume": 1, "paired_audio": {"audiobook_id": 9}},
        {"id": 5, "title": "Plain", "volume": 1, "paired_audio": None},
    ]
    for lang in ("en", "ru"):
        result = page.run(
            """
            const badge = id => document.querySelector(`#lightNovelsContent [data-ln-book="${id}"] .ln-paired-audio-badge`);
            const out = {paired: badge(4) && {text: badge(4).textContent, tooltip: badge(4).dataset.tooltip, label: badge(4).getAttribute('aria-label')},
                         plain: Boolean(badge(5)), expected: PudgeI18n.t('ui.hasAudiobook'), page: __text('#lightNovelsContent')};
            await __fire('#lightNovelsContent [data-ln-book="4"] .ln-paired-audio-badge', 'mouseover', {}, 'MouseEvent');
            const tip = document.getElementById('titleTooltip');
            out.hover = tip.classList.contains('show') ? tip.innerText : null;
            return out;
            """,
            lang=lang,
            api={"light_novel_state": {"version": 1, "settings": {}, "books": books}},
        )
        out = result.value
        assert out["paired"] == {"text": "🎧", "tooltip": out["expected"], "label": out["expected"]}
        assert out["hover"] == out["expected"]
        assert out["plain"] is False
        assert not any(text in out["page"] for text in OLD_AUDIOBOOK_BADGES)
    # The headphones are white on the dark card (not the emoji's default colours).
    assert css_rules("index.html").get(".ln-paired-audio-badge", "color") == "#fff"


def test_manga_uses_anilist_cover_only_for_first_volume(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "_cached_remote_cover_data_uri", lambda _url: "remote-cover")
    monkeypatch.setattr(service, "_local_cover_data_uri", lambda row: f"local-{row['id']}")
    now = time.time()
    with db.connect() as conn:
        first = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,cover_url,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) RETURNING id",
            (
                str(tmp_path / "Series Vol. 1.cbz"),
                "Series Vol. 1",
                20,
                0,
                "rtl",
                "https://example/series.jpg",
                now,
                now,
            ),
        ).fetchone()[0]
        second = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,cover_url,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) RETURNING id",
            (
                str(tmp_path / "Series Vol. 2.cbz"),
                "Series Vol. 2",
                20,
                0,
                "rtl",
                "https://example/series.jpg",
                now,
                now,
            ),
        ).fetchone()[0]
    first_payload = service._payload(service._book(int(first)))
    second_payload = service._payload(service._book(int(second)))
    assert first_payload["volume"] == 1
    assert first_payload["cover_url"] == "remote-cover"
    assert first_payload["cover_source"] == "anilist_cache"
    assert second_payload["volume"] == 2
    assert second_payload["cover_url"] == f"local-{int(second)}"
    assert second_payload["cover_source"] == "first_page"


# The mpv shortcuts "open on AniList" and "correct the match" were removed.
OLD_CONFIG = """
[shortcuts]
mpv_mark_watched = "Ctrl+a"
mpv_open_anilist = "Ctrl+o"
mpv_correct_match = "Ctrl+m"
"""


def test_removed_mpv_anilist_open_and_correct_shortcuts_are_gone(tmp_path: Path, page, monkeypatch) -> None:
    # Settings show only the four mpv shortcuts (and the system-wide Screen text one) and save only them.
    result = page.run(
        """
        const ids = [...document.querySelectorAll('#settingsContent [id^="s_shortcut_"]')].map(el => el.id);
        await __click('#saveSettings'); await __tick(10);
        return ids;
        """,
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    assert sorted(result.value) == [
        "s_shortcut_mpv_explain",
        "s_shortcut_mpv_skip",
        "s_shortcut_mpv_translate",
        "s_shortcut_mpv_watched",
        "s_shortcut_screen_text",
    ]
    [[saved]] = result.calls_of("save_settings")
    assert sorted(key for key in saved if key.startswith("shortcut_")) == [
        "shortcut_mpv_explain_subtitle",
        "shortcut_mpv_mark_watched",
        "shortcut_mpv_skip_segment",
        "shortcut_mpv_translate_subtitle",
        "shortcut_screen_text",
    ]

    # An old config still loads; the removed keys are neither offered nor written back.
    path = tmp_path / "old" / "config.toml"
    path.parent.mkdir()
    path.write_text(OLD_CONFIG, encoding="utf-8")
    cfg = load_config(path, resolve_secrets=False)
    assert cfg.shortcuts.mpv_mark_watched == "Ctrl+a"
    write_config(cfg, path)
    rewritten = path.read_text(encoding="utf-8")
    assert "Ctrl+o" not in rewritten and "Ctrl+m" not in rewritten
    settings = make_api(tmp_path / "api").get_state()["settings"]
    assert not [key for key in settings if "open_anilist" in key or "correct_match" in key]

    # Playback passes mpv only the remaining shortcuts.
    _command, env, _ = _launch(tmp_path / "play", monkeypatch, exclusive=False, segments=False)
    assert sorted(key for key in env if key.startswith("PUDGE_SHORTCUT_")) == [
        "PUDGE_SHORTCUT_EXPLAIN_SUBTITLE",
        "PUDGE_SHORTCUT_MARK_WATCHED",
        "PUDGE_SHORTCUT_SKIP_SEGMENT",
        "PUDGE_SHORTCUT_TRANSLATE_SUBTITLE",
    ]


@needs_lua
def test_mpv_script_binds_no_anilist_open_or_correct_keys(tmp_path: Path) -> None:
    # Even an environment from an old launcher does not bring the keys back.
    out = run_mpv_scenario(
        tmp_path,
        """
        local names = {}
        for name, b in pairs(sim.bindings) do table.insert(names, name .. '=' .. tostring(b.key)) end
        table.sort(names)
        print('BINDINGS=' .. table.concat(names, ','))
        assert(not sim.press('Ctrl+o') and not sim.press('Ctrl+m'))
        """,
        scripts=[ANILIST],
        env={"PUDGE_SHORTCUT_OPEN_ANILIST": "Ctrl+o", "PUDGE_SHORTCUT_CORRECT_MATCH": "Ctrl+m"},
    )
    line = next(row for row in out.splitlines() if row.startswith("BINDINGS="))
    assert line.split("=", 1)[1].split(",") == [
        "pudge-close-window-shortcut=Meta+w",
        "pudge-close-window=CLOSE_WIN",
        "pudge-explain-subtitle=Ctrl+Shift+t",
        "pudge_anilist_update=Ctrl+a",
        "pudge_episode_debug_export=Meta+Shift+l",
        "pudge_subtitle_translate=Ctrl+t",
    ]


def test_audiobook_bookmarks_can_be_renamed_and_reordered(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    now = time.time()
    with db.connect() as conn:
        book_id = int(
            conn.execute(
                "INSERT INTO audiobooks(path,title,duration,position,finished,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) RETURNING id",
                (str(tmp_path / "book.m4b"), "Book", 600.0, 0.0, 0, now, now),
            ).fetchone()[0]
        )
        first = int(
            conn.execute(
                "INSERT INTO audiobook_bookmarks(book_id,position,title,sort_order,created_at) VALUES(?,?,?,?,?) RETURNING id",
                (book_id, 10.0, "A", 0, now),
            ).fetchone()[0]
        )
        second = int(
            conn.execute(
                "INSERT INTO audiobook_bookmarks(book_id,position,title,sort_order,created_at) VALUES(?,?,?,?,?) RETURNING id",
                (book_id, 20.0, "B", 1, now + 1),
            ).fetchone()[0]
        )
    assert [row["id"] for row in service._bookmarks(book_id)] == [first, second]
    service.rename_bookmark(first, "Opening")
    assert service._bookmarks(book_id)[0]["title"] == "Opening"
    service.reorder_bookmarks(book_id, [second, first])
    assert [row["id"] for row in service._bookmarks(book_id)] == [second, first]
    deleted = service.delete_bookmark(second)
    assert deleted["deleted"]["id"] == second
    assert [row["sort_order"] for row in service._bookmarks(book_id)] == [0]
    restored = service.restore_bookmark(
        book_id,
        deleted["deleted"]["position"],
        deleted["deleted"]["title"],
        deleted["deleted"]["sort_order"],
        deleted["deleted"]["created_at"],
        deleted["deleted"]["id"],
    )
    rows = service._bookmarks(book_id)
    assert restored["bookmark_id"] == second
    assert [row["id"] for row in rows] == [second, first]
    assert [row["sort_order"] for row in rows] == [0, 1]
    assert rows[0]["created_at"] == deleted["deleted"]["created_at"]


def test_window_api_reveals_the_audiobook_and_restores_a_deleted_bookmark(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    book = api.audiobooks._upsert(
        path=source,
        title="Book",
        duration=600.0,
        files=[
            {"index": 0, "path": str(source), "title": "Book", "duration": 600.0, "start": 0.0, "end": 600.0}
        ],
        chapters=[],
    )
    book_id = int(book["id"])
    opened: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("pudge.web_app.subprocess.Popen", lambda command, *a, **k: opened.append(command))
    assert api.audiobook_reveal_source(book_id) == {"ok": True, "path": str(source.resolve())}
    assert opened == [["open", "-R", str(source.resolve())]]  # selected in Finder

    api.audiobook_add_bookmark(book_id, "")
    marks = [dict(row) for row in api.audiobooks._bookmarks(book_id)]
    deleted = api.audiobook_delete_bookmark(int(marks[0]["id"]))["deleted"]
    assert api.audiobooks._bookmarks(book_id) == []
    api.audiobook_restore_bookmark(
        book_id,
        deleted["position"],
        deleted["title"],
        deleted["sort_order"],
        deleted["created_at"],
        deleted["id"],
    )
    assert [dict(row) for row in api.audiobooks._bookmarks(book_id)] == marks

    source.unlink()
    assert api.audiobook_reveal_source(book_id)["ok"] is False and len(opened) == 1


# Audiobook 5 with two bookmarks; the fake backend keeps them like the real one.
AUDIOBOOK = """
const marks = [{id: 1, position: 10, title: 'A', sort_order: 0, created_at: 1790000000},
               {id: 2, position: 20, title: 'B', sort_order: 1, created_at: 1790000100}];
const book = () => ({id: 5, title: 'Book', duration: 600, position: 30, anilist_id: 77, anilist_site_url: 'https://anilist.co/manga/77',
                     cover_url: 'cover.jpg', bookmarks: marks.map(mark => ({...mark}))});
const state = () => ({state: {books: [book()]}});
__api.audiobook_state = async () => ({books: [book()]});
__api.audiobook_delete_bookmark = async id => { const [gone] = marks.splice(marks.findIndex(m => m.id === id), 1); return {...state(), deleted: gone}; };
__api.audiobook_restore_bookmark = async (_book, position, title, sortOrder, createdAt, id) => {
  marks.splice(sortOrder, 0, {id, position, title, sort_order: sortOrder, created_at: createdAt}); return state(); };
__api.audiobook_rename_bookmark = async (id, title) => { marks.find(m => m.id === id).title = title; return state(); };
__api.audiobook_reorder_bookmarks = async (_book, ids) => { marks.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id)); return state(); };
__api.audiobook_seek_to = async () => state();
__api.audiobook_reveal_source = async () => ({ok: true});
"""


def test_audiobook_cover_and_bookmark_menus_rename_delete_undo_and_drag(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('audiobooks', true); await __tick(50);
            const labels = () => [...document.querySelectorAll('#audiobooksContent .audiobook-bookmark-label')].map(n => n.textContent);
            const menu = () => [...document.querySelectorAll('#contextMenu [data-audio-context-action]')].map(b => b.dataset.audioContextAction);
            const mark = id => document.querySelector(`.audiobook-bookmark[data-bookmark-id="${id}"]`);
            const rightClick = sel => __fire(sel, 'contextmenu', {clientX: 40, clientY: 50}, 'MouseEvent');
            const created = new Date(1790000000 * 1000).toLocaleString(PudgeI18n.lang(), {year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
            const out = {labels: labels(), tooltip: mark(1).dataset.tooltip, created: PudgeI18n.t('media.created', {created})};

            await rightClick('.audiobook-cover[data-audio-cover-id="5"]');
            out.coverMenu = menu();
            await __click('#contextMenu [data-audio-context-action="reveal"]');
            await rightClick('.audiobook-cover[data-audio-cover-id="5"]');
            await __click('#contextMenu [data-audio-context-action="open-anilist"]');

            await rightClick('.audiobook-bookmark[data-bookmark-id="1"]');
            out.markMenu = menu();
            await __click('#contextMenu [data-audio-context-action="rename-bookmark"]'); await __tick(20);
            out.rename = {value: $('audioBookmarkRenameInput').value, focused: document.activeElement?.id};
            await __input('#audioBookmarkRenameInput', 'Opening');
            await __click('[data-media-action="save-audio-bookmark-name"]'); await __tick(10);
            out.renamed = labels();

            await __click('.audiobook-bookmark[data-bookmark-id="2"] .audiobook-bookmark-delete'); await __tick(10);
            out.deleted = {labels: labels(), toasts: __toasts()};
            await __key('z', {metaKey: true}, 'body'); await __tick(10);
            out.restored = {labels: labels(), toasts: __toasts()};

            // Drag "Opening" past the middle of "B" with the mouse.
            mark(2).__rect = {x: 200, y: 10, width: 80, height: 24};
            window.__elementFromPoint = x => (x >= 200 ? mark(2) : mark(1));
            const pointer = (type, x) => __fire('.audiobook-bookmark[data-bookmark-id="1"] .audiobook-bookmark-label', type,
                                                {clientX: x, clientY: 20, button: 0, pointerId: 7}, 'PointerEvent');
            await pointer('pointerdown', 120);
            out.dragging = mark(1).classList.contains('dragging');
            await pointer('pointermove', 270); await __tick(20);
            await pointer('pointerup', 270);
            await __click('.audiobook-bookmark[data-bookmark-id="1"] .audiobook-bookmark-label');   // the drop is not a click
            out.dropped = {labels: labels(), dragging: mark(1).classList.contains('dragging'),
                           seeks: __apiCalls.filter(c => c.name === 'audiobook_seek_to').length};
            await __tick(400);
            await __click('.audiobook-bookmark[data-bookmark-id="1"] .audiobook-bookmark-label');   // a later click seeks
            out.nativeDrag = document.querySelectorAll('#audiobooksContent [draggable="true"]').length;
            out.texts = {deleted: PudgeI18n.t('media.bookmarkDeletedZToUndo'), restored: PudgeI18n.t('media.bookmarkRestored')};
            return out;
            """,
            lang=lang,
            before=AUDIOBOOK,
        )
        out = result.value
        assert out["labels"] == ["A", "B"]
        assert out["tooltip"] == out["created"]  # creation date on hover
        assert out["coverMenu"] == ["play", "open-anilist", "identify", "edit-metadata", "reveal", "delete"]
        assert result.calls_of("audiobook_reveal_source") == [[5]]
        assert result.calls_of("open_url") == [["https://anilist.co/manga/77"]]
        assert out["markMenu"] == ["rename-bookmark", "delete-bookmark"]
        assert out["rename"] == {"value": "A", "focused": "audioBookmarkRenameInput"}
        assert result.calls_of("audiobook_rename_bookmark") == [[1, "Opening"]] and out["renamed"] == [
            "Opening",
            "B",
        ]
        assert result.calls_of("audiobook_delete_bookmark") == [[2]]
        assert out["deleted"] == {"labels": ["Opening"], "toasts": [out["texts"]["deleted"]]}
        assert result.calls_of("audiobook_restore_bookmark") == [[5, 20, "B", 1, 1790000100, 2]]
        assert (
            out["restored"]["labels"] == ["Opening", "B"]
            and out["restored"]["toasts"][-1] == out["texts"]["restored"]
        )
        assert out["dragging"] is True
        assert result.calls_of("audiobook_reorder_bookmarks") == [[5, [2, 1]]]
        assert out["dropped"] == {"labels": ["B", "Opening"], "dragging": False, "seeks": 0}
        assert result.calls_of("audiobook_seek_to") == [[5, 10]]
        assert out["nativeDrag"] == 0


# Pointer and trackpad gestures on a cover image (the window has no layout: covers get a box).
GESTURES = r"""
const preview = () => document.querySelector('.pudge-cover-preview');
const peek = () => document.querySelector('.pudge-cover-peek');
const pointer = (img, type, dx) => {
  const event = new PointerEvent(type, {bubbles: true, cancelable: true, clientX: 20 + dx, clientY: 20, button: 0, pointerId: 3, pointerType: 'mouse'});
  img.dispatchEvent(event);
  return event.defaultPrevented;
};
const gesture = (img, type, scale) => { const event = new Event(type, {bubbles: true, cancelable: true}); event.scale = scale; img.dispatchEvent(event); };
const close = async () => { if (preview()) await __click('.pudge-cover-preview-close'); await __tick(300); };
// Drags a cover `distance` px with the mouse; what was on screen before letting go.
const drag = async (img, distance, click = false) => {
  img.__rect = {x: 10, y: 10, width: 76, height: 108};
  const prevented = pointer(img, 'pointerdown', 0);
  pointer(img, 'pointermove', distance / 2); pointer(img, 'pointermove', distance); await __flush();
  const seen = {prevented, peek: peek()?.style.transform ?? null, preview: preview()?.querySelector('img')?.getAttribute('src') ?? null};
  pointer(img, 'pointerup', distance);
  if (click) img.click();                           // the click the browser sends after the button is released
  await __tick(50);
  seen.snapBack = Boolean(peek()?.classList.contains('snap-back'));
  seen.open = Boolean(preview()?.classList.contains('open'));
  seen.focused = document.activeElement?.className ?? '';
  await close();
  seen.left = Boolean(peek() || preview());
  return seen;
};
const pinch = async (img, scale) => {
  img.__rect = {x: 10, y: 10, width: 76, height: 108};
  gesture(img, 'gesturestart', 1); gesture(img, 'gesturechange', scale); await __flush();
  const seen = {peek: peek()?.style.transform ?? null, preview: preview()?.querySelector('img')?.getAttribute('src') ?? null};
  gesture(img, 'gestureend', scale); await __flush();
  seen.snapBack = Boolean(peek()?.classList.contains('snap-back'));
  await close();
  return seen;
};
"""
MEDIUM = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/medium/a{}.jpg"
LARGE = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/a{}.jpg"


def test_dragging_or_pinching_a_cover_opens_its_full_size_preview(page) -> None:
    waiting = {
        "media_id": 7,
        "title": "Waiting",
        "episodes": 12,
        "next_episode": 3,
        "cover": MEDIUM.format(7),
        "presentation": {"status": "waiting", "episode": 3},
    }
    ready = {
        "media_id": 8,
        "title": "Ready",
        "episodes": 12,
        "next_episode": 4,
        "cover": MEDIUM.format(8),
        "local": {"video_path": "/v/8.mkv", "episode": 4},
        "episode": 4,
    }
    novel = {"id": 4, "title": "Novel", "volume": 1, "cover_url": "/covers/n4.jpg.list.jpg"}
    result = page.run(
        GESTURES
        + """
        const out = {stylesheet: Boolean(document.querySelector('link[rel="stylesheet"][href="cover_preview.css"]'))};
        setPage('lightnovels', true); await __tick(20);
        const ln = document.querySelector('#lightNovelsContent img.ln-card-cover');
        out.short = await drag(ln, 60, true);            // grows under the pointer, snaps back
        out.lnOpened = __apiCalls.some(call => call.name === 'light_novel_open');
        out.long = await drag(ln, 150);                  // far enough: the preview opens
        out.smallPinch = await pinch(ln, 1.2);
        out.pinch = await pinch(ln, 1.4);

        setPage('current', true); renderCurrent(); await __tick(20);
        out.waiting = await drag(document.querySelector('.waiting-card .cover-shell img'), 150);
        const play = document.querySelector('.airing-card[data-action="play"] .cover-shell img');
        out.playCard = await drag(play, 150);
        out.playCardPinch = await pinch(play, 1.4);

        setPage('audiobooks', true); await __tick(20);
        out.audiobook = await drag(document.querySelector('.audiobook-cover img'), 150);

        setPage('planned', true); await __tick(20);
        renderPlannedSuggestions([{media_id: 9, media_kind: 'anime', title: 'Suggested', cover: 'MEDIUM9', site_url: 'https://anilist.co/anime/9'}], 'sug');
        out.suggestion = await drag(document.querySelector('.planned-suggestion-grid img'), 150);
        return out;
        """.replace("MEDIUM9", MEDIUM.format(9)),
        state={"current": [waiting, ready], "home": {"waiting": [waiting], "new_ready": [ready]}},
        api={
            "light_novel_state": {"version": 1, "settings": {}, "books": [novel]},
            "audiobook_state": {
                "books": [
                    {"id": 5, "title": "Book", "duration": 600, "position": 0, "cover_url": "/covers/b5.jpg"}
                ]
            },
        },
    )
    out = result.value
    assert out["stylesheet"] is True
    # Pressing a cover keeps the browser from dragging the image or selecting text.
    assert out["short"] == {
        "prevented": True,
        "peek": f"scale({1 + 60 / 360})",
        "preview": None,
        "snapBack": True,
        "open": False,
        "focused": "",
        "left": False,
    }
    assert out["lnOpened"] is False  # the drag was not a click on the card
    long = out["long"]
    assert long["preview"] == "/covers/n4.jpg" and long["open"] is True  # the full cover, not the list copy
    assert long["focused"] == "pudge-cover-preview-close" and long["left"] is False
    assert out["smallPinch"] == {"peek": "scale(1.2)", "preview": None, "snapBack": True}
    assert out["pinch"]["preview"] == "/covers/n4.jpg"
    assert out["waiting"]["preview"] == LARGE.format(7)
    # Primary click on a playable card plays the episode: moving the mouse while
    # clicking never turns into a preview, but a trackpad pinch still does.
    assert (
        out["playCard"]["prevented"] is False
        and out["playCard"]["preview"] is None
        and out["playCard"]["peek"] is None
    )
    assert out["playCardPinch"]["preview"] == LARGE.format(8)
    assert out["audiobook"]["preview"] == "/covers/b5.jpg"
    assert out["suggestion"]["preview"] == LARGE.format(9)
    assert result.calls_of("cover_preview_resolve")[0][:2] == ["light_novel", 4]
    # The overlay fades in once open; a released peek fades out while shrinking back.
    rules = css_rules("cover_preview.css")
    assert (
        rules.get(".pudge-cover-preview", "opacity") == "0"
        and rules.get(".pudge-cover-preview.open", "opacity") == "1"
    )
    assert rules.get(".pudge-cover-peek.snap-back", "opacity") == "0"


# A light novel with a katakana word, a kanji word with its reading and full-width letters.
LN_READER = """
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index,
  paragraphs: ['カタカナの猫です。', 'ＡＢＣの本。'],
  tokens: [[{start: 5, end: 6, surface: '猫', reading: 'ねこ', wordId: 1, readingIndex: 0}], []]});
"""
FIND = """
const find = async query => {
  await __input('#lnFindInput', query); await __tick(100);
  return {count: __text('#lnFindCount'),
          hits: [...document.querySelectorAll('#lnReader .ln-find-hit, #lnReader .ln-find-paragraph-hit')].map(node => node.textContent)};
};
"""


def test_cmd_f_in_the_ln_reader_finds_text_with_japanese_variants(page) -> None:
    result = page.run(
        FIND
        + """
        await __settle(openLightNovel(6));
        const out = {before: __visible('#lnFindBar')};
        await __key('f', {metaKey: true, code: 'KeyF'}); await __tick(50);
        out.bar = {visible: __visible('#lnFindBar'), focused: document.activeElement?.id,
                   placeholder: $('lnFindInput').placeholder, expected: PudgeI18n.t('ui.findInText')};
        out.katakana = await find('かたかな');           // hiragana finds katakana
        out.reading = await find('ねこ');                // a word by its reading
        out.readingKatakana = await find('ネコ');        // ... typed in katakana
        out.wide = await find('abc');                   // full-width letters, any case
        out.none = await find('いぬ');
        out.global = __visible('#globalSearchOverlay');
        await __click('#lnFindClose');
        out.closed = {visible: __visible('#lnFindBar'), hits: document.querySelectorAll('#lnReader .ln-find-hit').length};
        return out;
        """,
        before=LN_READER,
    )
    out = result.value
    assert out["before"] is False
    assert out["bar"]["visible"] is True and out["bar"]["focused"] == "lnFindInput"
    assert out["bar"]["placeholder"] == out["bar"]["expected"]
    assert out["katakana"] == {"count": "1/1", "hits": ["カタカナの"]}
    assert out["reading"] == out["readingKatakana"] == {"count": "1/1", "hits": ["猫"]}
    assert out["wide"] == {"count": "1/1", "hits": ["ＡＢＣの本"]}
    assert out["none"] == {"count": "0/0", "hits": []}
    assert out["global"] is False
    assert out["closed"] == {"visible": False, "hits": 0}


MANGA = """
__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 20, position: 0}], ocr_available: false});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 20, name: `p${index}.png`, spread: false,
                                         data_uri: 'data:image/png;base64,AAAA'});
__api.toggle_fullscreen = async () => ({ok: true});
"""


def test_cmd_f_in_the_manga_reader_does_nothing_and_f_toggles_fullscreen(page) -> None:
    result = page.run(
        """
        await __settle(PudgeMangaReaderV2.openBook(3));
        const fullscreen = () => __apiCalls.filter(call => call.name === 'toggle_fullscreen').length;
        await __key('f', {metaKey: true, code: 'KeyF'}); await __tick(50);
        const out = {global: __visible('#globalSearchOverlay'), find: __visible('#lnFindBar'), fullscreen: fullscreen()};
        await __key('f', {code: 'KeyF'}); await __tick(50);
        out.plainF = fullscreen();
        return out;
        """,
        before=MANGA,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    assert result.value == {"global": False, "find": False, "fullscreen": 0, "plainF": 1}
