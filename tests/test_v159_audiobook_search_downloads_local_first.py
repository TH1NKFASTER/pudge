"""Audiobook search on Nyaa shows the local cache first; downloads start in the background;
the Downloads window shows local rows before the backend answers (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from tests.i18n_support import en


# The LN 狼と香辛料II (volume 2, id 180) and what the backend knows about its audiobook on Nyaa.
# The fresh search waits for window.releaseSearch(); the cache answers at once.
COLLECTION = {
    "title": "TMW Japanese Audiobooks Collection",
    "collection": True,
    "target_volume": 2,
    "volume_title": "狼と香辛料II",
    "selected_track_count": 12,
    "selected_size_bytes": 512 * 1024 * 1024,
    "size": "40 GiB",
    "seeders": 3,
    "selected_files": ["[A]/狼と香辛料/[02]/01 chapter.mp3", "[A]/狼と香辛料/[02]/02 chapter.mp3"],
}
SINGLE = {
    "title": "狼と香辛料II 単品",
    "collection": False,
    "target_volume": 2,
    "selected_track_count": None,
    "size": "300 MiB",
    "seeders": 1,
}
NYAA_BOOKS = [
    {"id": 180, "title": "狼と香辛料II", "volume": 2},
    {
        "id": 181,
        "title": "狼と香辛料III",
        "volume": 3,
        "paired_audio": {"audiobook_id": 9, "book": {"id": 9}},
    },
]
NYAA = """
const cached = {cached: true, target_volume: 2, inspected_releases: 3, matches: [COLLECTION]};
const fresh = {target_volume: 2, inspected_releases: 5, matches: [COLLECTION, SINGLE]};
let releaseSearch; const searched = new Promise(resolve => { releaseSearch = resolve; });
window.releaseSearch = releaseSearch;
__api.light_novel_cached_audiobook_nyaa = async () => cached;
__api.light_novel_search_audiobook_nyaa = async () => { await searched; return fresh; };
__api.start_light_novel_download_audiobook_nyaa = async () => ({started: true, running: true});
""".replace("COLLECTION", json.dumps(COLLECTION, ensure_ascii=False)).replace(
    "SINGLE", json.dumps(SINGLE, ensure_ascii=False)
)
NYAA_OPTIONS = {
    "before": NYAA,
    "api": {"light_novel_state": {"version": 1, "settings": {}, "books": NYAA_BOOKS}},
}
# Test-side: open the LN menu of a book and search its audiobook; read the result rows.
NYAA_PAGE = r"""
const menu = async id => {
  await __fire(`[data-ln-book="${id}"]`, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
  return [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].filter(b => b.checkVisibility()).map(b => [b.dataset.lnContextAction, b.innerText]);
};
const rows = () => [...document.querySelectorAll('#modalBody .ln-nyaa-row')].map(row => ({
  text: row.querySelector('.title').innerText, size: row.children[1].innerText, sizeTitle: row.children[1].title}));
const header = () => document.querySelector('#modalBody > .subtle')?.innerText ?? '';
const spinnerTitle = () => document.querySelector('#modalBody .subtle [title] .spinner')?.parentElement.title ?? null;
setPage('lightnovels', true); await __tick(200);
"""


def test_audiobook_search_shows_local_cache_first_then_fresh_results(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            NYAA_PAGE
            + r"""
            await menu(180);
            await __click('[data-ln-context-action="find-audiobook-nyaa"]'); await __tick(50);
            const out = {cachedRows: rows(), cachedHeader: header(), refreshing: spinnerTitle()};
            window.releaseSearch(); await __tick(50);
            out.freshRows = rows(); out.freshHeader = header(); out.refreshingAfter = spinnerTitle();
            out.texts = {cache: t('ui.showingLocalCache'), refreshing: t('ui.refreshingResults'), inspecting: t('uiB.ln.inspectingReleases'),
                         size: torrentBytes(512 * 1024 * 1024), sizeTitle: t('ui.selectedVolumeSize')};
            return out;
            """,
            lang=lang,
            **NYAA_OPTIONS,
        )
        out = result.value
        texts = out["texts"]
        # The cached collection is shown at once, marked as cache, while the search refreshes.
        assert len(out["cachedRows"]) == 1
        assert texts["cache"] in out["cachedHeader"] and texts["refreshing"].strip() in out["cachedHeader"]
        assert out["refreshing"] == texts["inspecting"]
        assert texts["cache"] not in out["freshHeader"] and out["refreshingAfter"] is None
        assert len(out["freshRows"]) == 2
        # The size is the selected volume's, not the whole collection's.
        collection = out["freshRows"][0]
        assert (collection["size"], collection["sizeTitle"]) == (texts["size"], texts["sizeTitle"])
        assert out["freshRows"][1]["size"] == "300 MiB" and out["freshRows"][1]["sizeTitle"] == ""
        assert result.calls_of("light_novel_cached_audiobook_nyaa") == [[180]]
        assert result.calls_of("light_novel_search_audiobook_nyaa") == [[180]]
    assert en("ui.showingLocalCache") == "showing local cache"
    assert en("ui.refreshingResults").strip() == "Refreshing results…"


def test_volume_download_starts_in_the_background_and_says_how_many_files(page) -> None:
    result = page.run(
        NYAA_PAGE
        + r"""
        const download = async index => {
          await menu(180); await __click('[data-ln-context-action="find-audiobook-nyaa"]'); await __tick(50);
          window.releaseSearch(); await __tick(50);
          await __click(`[data-ln-audiobook-release="${index}"]`); await __tick(50);
          return {open: __visible('#modalBackdrop'), toasts: __toasts()};
        };
        const out = {collection: await download(0), single: await download(1)};
        out.texts = {files: tn('uiC.ln.volumeDownloadStartedFiles', 12), plain: t('uiC.ln.volumeDownloadStarted')};
        return out;
        """,
        **NYAA_OPTIONS,
    )
    out = result.value
    assert out["collection"]["open"] is False and out["texts"]["files"] in out["collection"]["toasts"]
    assert out["single"]["open"] is False and out["texts"]["plain"] in out["single"]["toasts"]
    started = result.calls_of("start_light_novel_download_audiobook_nyaa")
    assert [book_id for book_id, _row in started] == [180, 180]
    assert (
        started[0][1]["selected_files"] == COLLECTION["selected_files"]
    )  # exactly the files the search chose
    assert not result.called(
        "light_novel_download_audiobook_nyaa"
    )  # the slow work never runs in the window's call
    assert en("uiC.ln.volumeDownloadStarted") == "Volume download started in background"
    assert (
        en("uiC.ln.volumeDownloadStartedFiles", count=2) == "Volume download started in background · 2 files"
    )


# torrent_downloads(media, refresh): the local snapshot answers at once, a backend refresh waits.
DOWNLOADS = """
const row = {name: 'Show - 01.mkv', progress: 0.5, state: 'downloading', size: 1000};
let releaseBackend; window.backend = new Promise(resolve => { releaseBackend = resolve; });
window.releaseBackend = releaseBackend;
__api.torrent_downloads = async (mediaId, refresh) => {
  if (refresh) await window.backend;
  return {downloads: [row], backend: refresh ? 'qbittorrent' : '', observed_at: Date.now() / 1000};
};
"""


def test_download_center_renders_local_rows_before_backend_refresh(page) -> None:
    result = page.run(
        r"""
        await __settle(openDownloadCenter(null));
        const local = __text('#modalBody');
        window.releaseBackend(); await __tick(50);
        for (let i = 0; i < 6; i++) await __tick(2000);
        return {local, open: __visible('#modalBackdrop')};
        """,
        before=DOWNLOADS,
    )
    out = result.value
    assert "Show - 01.mkv" in out["local"]
    refresh_flags = [refresh for _media, refresh in result.calls_of("torrent_downloads")]
    # Local snapshot first, one backend refresh right after, then cheap polls with a backend refresh every 5th.
    assert refresh_flags[:2] == [False, True]
    assert refresh_flags[2:7] == [False, False, False, True, False]


def test_cached_audiobook_search_is_network_free() -> None:
    import pudge.web_app as web_app

    class FakeCache:
        def get(self, key, *, ttl_seconds):
            assert key["series"] == "狼と香辛料"
            assert key["volume"] == 2
            assert ttl_seconds > 0
            return {
                "target_volume": 2,
                "matches": [{"title": "cached"}],
                "cached_at": time.time() - 5,
            }

    api = object.__new__(web_app.WebAppApi)
    api.light_novels = SimpleNamespace(
        book=lambda book_id: {
            "id": book_id,
            "title": "狼と香辛料II",
            "series_title": "狼と香辛料",
            "volume": 2,
        }
    )
    api._ln_audiobook_search_cache = FakeCache()
    result = api.light_novel_cached_audiobook_nyaa(180)
    assert result["cached"] is True
    assert result["matches"] == [{"title": "cached"}]
    assert result["cache_age_seconds"] >= 4


def test_torrent_downloads_refresh_false_does_not_touch_aria2() -> None:
    import pudge.web_app as web_app

    class FakeDB:
        def anime_list(self):
            return []

        def downloads(self):
            return []

    class FakeManager:
        db = FakeDB()

        def torrent_clients(self):
            raise AssertionError("local downloads snapshot must not touch torrent backends")

    api = object.__new__(web_app.WebAppApi)
    api.manager = FakeManager()
    api.config = SimpleNamespace(aria2=SimpleNamespace(enabled=True))
    api._last_network_guard = {"bound": True, "protected": True, "interface": "utun9"}
    api._downloads_configured = lambda: False
    api._downloads_enabled = lambda: False
    api._storage_payload = lambda refresh=False: {"used_gb": 1.0, "limit_gb": 10.0}

    result = api.torrent_downloads(refresh=False)
    assert result["downloads"] == []
    assert result["network_guard"]["interface"] == "utun9"


def test_audiobook_download_start_is_background(monkeypatch) -> None:
    import pudge.web_app as web_app

    started_threads: list[str] = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            started_threads.append(self.name)

    api = object.__new__(web_app.WebAppApi)
    api._ln_audiobook_download_lock = threading.Lock()
    api._ln_audiobook_download_jobs = {}
    api.logger = SimpleNamespace(exception=lambda *a, **k: None)
    api.light_novel_download_audiobook_nyaa = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("slow metadata/download work must not execute inline")
    )
    monkeypatch.setattr(web_app.threading, "Thread", FakeThread)

    result = api.start_light_novel_download_audiobook_nyaa(180, {"title": "book"})
    assert result["started"] is True
    assert result["running"] is True
    assert started_threads == ["ln-audiobook-download-180"]
