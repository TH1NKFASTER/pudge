"""FE-3, FE-4, FE-5: page polls share one hidden-window policy (pollDelay/schedulePoll):
one cheap manga library OCR poller, hidden-aware reader OCR, Download Center and
audiobook polls, audiobook transcription progress patched in place.
FE-8: title tooltips are measured on hover, not on every render and resize."""

from __future__ import annotations

BOOKS = [
    {
        "id": i + 1,
        "title": f"M v{i + 1}",
        "series_title": "M",
        "volume": i + 1,
        "path": f"/m/{i}",
        "page_count": 200,
    }
    for i in range(20)
]
QUEUED = {"running": True, "state": "queued", "total_pages": 200, "completed_pages": 0}


def test_series_ocr_uses_one_cheap_poller(page) -> None:
    running = {str(book["id"]): True for book in BOOKS}
    result = page.run(
        r"""
        setPage('manga'); await __tick(1000);
        window.PudgeMangaReaderV2.selectAll();
        await window.PudgeMangaReaderV2.ocrSelected(); await __tick(1000);
        const count = name => __apiCalls.filter(call => call.name === name).length;
        const s0 = count('manga_ocr_book_status'), r0 = count('manga_ocr_running_books');
        await __tick(60000);
        const visible = {status: count('manga_ocr_book_status') - s0, running: count('manga_ocr_running_books') - r0};
        await __setHidden(true);
        const s1 = count('manga_ocr_book_status'), r1 = count('manga_ocr_running_books');
        await __tick(60000);
        const hidden = {status: count('manga_ocr_book_status') - s1, running: count('manga_ocr_running_books') - r1};
        const last = __apiCalls.filter(call => call.name === 'manga_ocr_running_books').pop();
        return {visible, hidden, ids: last ? last.args[0].length : 0, started: count('start_manga_ocr_book')};
        """,
        api={
            "manga_state": {"books": BOOKS, "ocr_available": True},
            "start_manga_ocr_book": {"running": True},
            "manga_ocr_book_status": QUEUED,
            "manga_ocr_running_books": running,
        },
    )
    value = result.value
    assert value["started"] == 20
    # Before: 20 pollers x 900 ms = ~1320 status calls a minute, hidden or not.
    assert value["visible"]["status"] == 0 and 15 <= value["visible"]["running"] <= 21
    assert value["hidden"]["status"] == 0 and value["hidden"]["running"] <= 5
    assert value["ids"] == 20


def test_a_finished_volume_gets_its_status_once(page) -> None:
    result = page.run(
        r"""
        let done = false;
        __api.manga_ocr_running_books = async ids => Object.fromEntries(ids.map(id => [String(id), !done]));
        __api.manga_ocr_book_status = async id => done ? {running: false, complete: true, total_pages: 200, completed_pages: 200} : {running: true};
        setPage('manga'); await __tick(1000);
        window.PudgeMangaReaderV2.selectAll();
        await window.PudgeMangaReaderV2.ocrSelected(); await __tick(5000);
        done = true;
        const toasts = [];
        for (let i = 0; i < 8; i++) { await __tick(500); toasts.push(...__toasts()); }
        const status = __apiCalls.filter(call => call.name === 'manga_ocr_book_status').length;
        await __tick(30000);
        return {status, after: __apiCalls.filter(call => call.name === 'manga_ocr_book_status').length, toasts, ready: PudgeI18n.t('manga.volumeOcrReady')};
        """,
        api={
            "manga_state": {"books": BOOKS[:2], "ocr_available": True},
            "start_manga_ocr_book": {"running": True},
        },
    )
    value = result.value
    assert value["status"] == 2 and value["after"] == 2  # one status per finished volume, then no poller
    assert value["ready"] in value["toasts"]


def test_download_center_and_reader_ocr_slow_down_while_hidden(page) -> None:
    result = page.run(
        r"""
        const count = name => __apiCalls.filter(call => call.name === name).length;
        await openDownloadCenter(null); await __tick(100);
        await __setHidden(true);
        const d0 = count('torrent_downloads'); await __tick(60000);
        const hidden = count('torrent_downloads') - d0;
        await __setHidden(false); await __flush();
        const woke = count('torrent_downloads') - d0 - hidden;
        const d1 = count('torrent_downloads'); await __tick(10000);
        return {hidden, woke, visible10s: count('torrent_downloads') - d1};
        """,
        state={"settings": {"torrents_enabled": True}},
        api={
            "torrent_downloads": {
                "downloads": [{"hash": "h", "name": "x", "state": "downloading", "progress": 0.2}],
                "backend": "aria2",
            }
        },
    )
    value = result.value
    assert value["hidden"] <= 5  # before: 30 calls a minute
    assert value["woke"] >= 1  # back in front: polled at once
    assert value["visible10s"] >= 4


def test_poll_delay_policy(page) -> None:
    result = page.run(
        r"""
        const visible = pollDelay(750);
        await __setHidden(true);
        const hidden = pollDelay(750), longer = pollDelay(60000, 15000);
        let ran = 0; schedulePoll(() => { ran += 1; }, 750);
        await __tick(5000); const early = ran;
        await __setHidden(false); await __flush();
        return {visible, hidden, longer, early, ran};
        """,
    )
    assert result.value == {"visible": 750, "hidden": 15000, "longer": 60000, "early": 0, "ran": 1}


AUDIO_BOOKS = [
    {
        "id": i + 1,
        "title": f"Book {i + 1}",
        "duration": 36000,
        "position": 10,
        "chapters": [{"title": f"Ch {c}", "start": c * 600} for c in range(20)],
        "transcription": {"status": "transcribing", "progress_percent": 10}
        if i == 0
        else {"status": "done", "ready": True},
    }
    for i in range(8)
]


def test_audiobook_transcription_progress_is_patched_in_place(page) -> None:
    result = page.run(
        r"""
        let percent = 10;
        __api.audiobook_state = async () => ({books: window.__books.map((book, index) => index === 0
          ? {...book, transcription: {status: 'transcribing', progress_percent: percent}} : book)});
        setPage('audiobooks'); await __tick(3000);
        const first = document.querySelector('.audiobook-card[data-audiobook-id="2"]');
        const count = () => __apiCalls.filter(call => call.name === 'audiobook_state').length;
        const n0 = count();
        for (let i = 0; i < 10; i++) { percent += 5; await __tick(1500); }
        const visible = count() - n0;
        const same = document.querySelector('.audiobook-card[data-audiobook-id="2"]') === first;
        const shown = document.querySelector('.audiobook-card[data-audiobook-id="1"] .audiobook-preparation progress')?.getAttribute('value');
        await __setHidden(true);
        const n1 = count(); await __tick(60000);
        return {visible, same, shown, hidden: count() - n1, expected: String(percent)};
        """,
        before=f"window.__books = {__import__('json').dumps(AUDIO_BOOKS)};",
        api={"sidebar_audio_state": {"books": []}},
    )
    value = result.value
    assert value["visible"] >= 8
    assert value["same"] is True  # before: every poll rebuilt every card
    assert value["shown"] == value["expected"]
    assert value["hidden"] <= 5  # before: 43 calls a minute while hidden


def test_a_new_book_still_redraws_the_page(page) -> None:
    result = page.run(
        r"""
        let books = window.__books.slice(0, 2);
        __api.audiobook_state = async () => ({books});
        setPage('audiobooks'); await __tick(3000);
        books = [...books, {...window.__books[2]}];
        await __tick(1500);
        return document.querySelectorAll('.audiobook-card').length;
        """,
        before=f"window.__books = {__import__('json').dumps(AUDIO_BOOKS)};",
        api={"sidebar_audio_state": {"books": []}},
    )
    assert result.value == 3


def test_titles_are_not_measured_on_render_or_resize(page) -> None:
    anime = [
        {
            "media_id": i,
            "title": f"Show {i} with a long long title that may clip",
            "episodes": 12,
            "progress": 1,
            "status": "CURRENT",
            "next_episode": 2,
            "site_url": f"https://anilist.co/anime/{i}",
        }
        for i in range(1, 21)
    ]
    result = page.run(
        r"""
        let measured = 0;
        const original = window.titleIsClipped;
        window.titleIsClipped = el => { measured += 1; return original(el); };
        await __tick(2000); measured = 0;
        renderDataPages(true); await __tick(1000);
        const perRender = measured; measured = 0;
        for (let i = 0; i < 10; i++) window.dispatchEvent(new Event('resize'));
        await __tick(1000);
        const titled = document.querySelectorAll('[data-full-title][title]').length;
        return {perRender, resize: measured, titles: document.querySelectorAll('[data-full-title]').length, titled};
        """,
        state={"home": {"continue_watching": anime[:10], "waiting": anime[10:]}},
    )
    value = result.value
    assert value["titles"] >= 20
    assert value["perRender"] == 0 and value["resize"] == 0  # before: 3 sweeps per render and per resize
    assert value["titled"] == 0  # the native title (a second tooltip) is still removed


def test_reader_ocr_poll_slows_down_while_hidden(page) -> None:
    result = page.run(
        r"""
        setPage('manga'); await __tick(500);
        await __settle(window.PudgeMangaReaderV2.openBook(1)); await __tick(2000);
        const count = () => __apiCalls.filter(call => call.name === 'manga_ocr_book_status').length;
        const v0 = count(); await __tick(10000); const visible = count() - v0;
        await __setHidden(true);
        const h0 = count(); await __tick(60000); const hidden = count() - h0;
        await __setHidden(false); await __flush();
        return {visible, hidden, woke: count() - h0 - hidden};
        """,
        api={
            "manga_state": {
                "books": [{"id": 1, "title": "M", "path": "/m", "page_count": 200, "position": 0}],
                "ocr_available": True,
            },
            "manga_ocr_book_status": {
                "running": True,
                "state": "running",
                "total_pages": 200,
                "completed_pages": 3,
                "cached_pages": 3,
                "complete": False,
            },
            "start_manga_ocr_book": {
                "running": True,
                "state": "running",
                "total_pages": 200,
                "completed_pages": 0,
            },
            "manga_page": {"page_index": 0, "name": "p.jpg", "url": "/p.jpg", "width": 100, "height": 150},
        },
        allow_errors=True,
    )
    value = result.value
    assert value["visible"] >= 8  # 750 ms while reading
    assert value["hidden"] <= 5  # before: 80 calls a minute while hidden
    assert value["woke"] >= 1
