"""Release searches in the background, and Download while Torrents are off (window side).

"Searching releases…" held the window until the search answered. Now the dialog can be
closed (Close, Escape, "Search in background") and the search goes on: the sidebar under
"Open folder" (#releaseTasks, next to the Planning search line) lists each pending search,
"Releases ready: <title>" once it answered; a click shows the results, × drops it.
Download with Torrents off answers "queued" (web_app.add_release): the page says so, the
Downloads window lists the queued release with Cancel, the Home card says "Waiting for
torrents to be turned on". Pages run in the node harness (tests/page_harness.py)."""

from __future__ import annotations

import json

import pytest

ANIME = {"media_id": 77, "title": "Queued Show", "episodes": 12, "media_status": "FINISHED", "format": "TV"}
RELEASE = {
    "title": "[Group] Queued Show (01-12) [Batch]",
    "seeders": 40,
    "downloads": 100,
    "size_text": "12 GiB",
    "group": "Group",
    "info_hash": "a" * 40,
}

PENDING_SEARCHES = r"""
window.searches = [];
__api.search_releases = (mediaId, episode, batch) => new Promise((resolve, reject) => {
  window.searches.push({mediaId, episode, batch, resolve, reject});
});
"""


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_closing_the_search_keeps_it_running_and_the_sidebar_shows_the_result(page, lang) -> None:
    result = page.run(
        r"""
        const out = {};
        const opened = openRelease(77, null, true); await __flush();
        out.searching = __text('#modalBody .empty');
        out.hideButton = __text('[data-action="release-task-hide"]');
        out.chipWhileShown = __visible('#releaseTasks');
        await __click('#modalClose'); await __flush();
        out.modalOpen = document.getElementById('modalBackdrop').classList.contains('open');
        out.chipSearching = __text('#releaseTasks [data-action="release-task-open"]');
        window.searches[0].resolve([window.release]); await opened; await __flush();
        out.modalAfterAnswer = document.getElementById('modalBackdrop').classList.contains('open');
        out.chipReady = __text('#releaseTasks [data-action="release-task-open"]');
        await __click('#releaseTasks [data-action="release-task-open"]'); await __flush();
        out.rows = [...document.querySelectorAll('#modalBody .release-row strong')].map(n => n.textContent);
        out.chipAfterOpen = __visible('#releaseTasks');
        out.searchCalls = window.searches.length;
        out.expected = {
          searching: t('label.searchingReleases'), hide: t('releaseTask.hide'),
          chipSearching: t('releaseTask.searching', {title: 'Queued Show'}),
          chipReady: t('releaseTask.ready', {title: 'Queued Show'}),
        };
        return out;
        """,
        lang=lang,
        state={"current": [ANIME]},
        before=PENDING_SEARCHES + f"window.release = {json.dumps(RELEASE)};",
    )
    out = result.value
    assert out["searching"] == out["expected"]["searching"]
    assert out["hideButton"] == out["expected"]["hide"]
    assert out["chipWhileShown"] is False
    assert out["modalOpen"] is False
    assert out["chipSearching"] == out["expected"]["chipSearching"]
    assert out["modalAfterAnswer"] is False  # the answer does not pop the dialog over the page
    assert out["chipReady"] == out["expected"]["chipReady"]
    assert out["rows"] == [RELEASE["title"]]
    assert out["chipAfterOpen"] is False
    assert out["searchCalls"] == 1
    assert not result.errors


def test_several_searches_one_line_each_and_cancel_drops_one(page) -> None:
    result = page.run(
        r"""
        const out = {};
        const first = openRelease(77, null, true); await __flush();
        await __key('Escape'); await __flush();
        const second = openRelease(78, 3, false); await __flush();
        await __click('[data-action="release-task-hide"]'); await __flush();
        // The same search again: the running one, no second request.
        const again = openRelease(77, null, true); await __flush(); await __click('#modalClose'); await __flush();
        out.calls = window.searches.length;
        out.lines = [...document.querySelectorAll('#releaseTasks [data-release-task-chip]')].map(n => n.dataset.releaseTaskChip);
        await __click('#releaseTasks [data-release-task-chip="77:batch"] [data-action="release-task-cancel"]'); await __flush();
        window.searches[0].resolve([window.release]); window.searches[1].reject(new Error('Nyaa timed out'));
        await first; await second; await again; await __flush();
        out.after = [...document.querySelectorAll('#releaseTasks [data-release-task-chip]')].map(n => [n.dataset.releaseTaskChip, n.className]);
        out.failedText = __text('#releaseTasks [data-action="release-task-open"]');
        await __click('#releaseTasks [data-action="release-task-open"]'); await __flush();
        out.error = __text('#modalBody .danger');
        out.expectedFailed = t('releaseTask.failed', {title: 'Other Show'});
        return out;
        """,
        state={"current": [ANIME, {**ANIME, "media_id": 78, "title": "Other Show"}]},
        before=PENDING_SEARCHES + f"window.release = {json.dumps(RELEASE)};",
    )
    out = result.value
    assert out["calls"] == 2
    assert out["lines"] == ["77:batch", "78:3"]
    assert out["after"] == [["78:3", "release-task error"]]
    assert out["failedText"] == out["expectedFailed"]
    assert "Nyaa timed out" in out["error"]
    assert not result.errors


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_download_with_torrents_off_is_queued_and_cancellable(page, lang) -> None:
    result = page.run(
        r"""
        const out = {};
        const opened = openRelease(77, null, true); await __flush();
        window.searches[0].resolve([window.release]); await opened; await __flush();
        await __click('[data-action="download-release"]'); await __flush(); await __tick(20);
        out.toast = __toasts().at(-1);
        out.addArgs = __apiCalls.filter(c => c.name === 'add_release').map(c => c.args.slice(0, 3));
        await openDownloadCenter(77); await __flush(); await __tick(10);
        out.queuedTitle = __text('.queued-releases h3');
        out.queuedState = __text('.queued-release .download-state');
        await __click('[data-action="queued-release-cancel"]'); await __flush(); await __tick(20);
        out.cancelArgs = __apiCalls.filter(c => c.name === 'cancel_queued_release').map(c => c.args);
        out.afterCancel = document.querySelectorAll('.queued-release').length;
        out.cancelToast = __toasts().at(-1);
        out.expected = {toast: t('toast.releaseQueued'), title: t('downloads.queuedTitle'),
                        state: t('release.queuedTorrentsOff'), cancel: t('toast.queuedReleaseCancelled')};
        return out;
        """,
        lang=lang,
        state={"current": [ANIME]},
        before=PENDING_SEARCHES
        + f"window.release = {json.dumps(RELEASE)};"
        + r"""
        window.queued = [{id: '77:batch', media_id: 77, episode: null, batch: true, title: window.release.title,
                          size_text: '12 GiB', anime_title: 'Queued Show', queued_at: 1, error: '', origin: 'manual',
                          cancellable: true}];
        __api.add_release = async () => ({ok: true, added: false, queued: true, already_queued: false, already_downloading: false});
        __api.torrent_downloads = async () => ({backend: 'aria2', enabled: false, configured: true, downloads: [],
                                               queued: window.queued, storage: {}, network_guard: {}, warning: ''});
        __api.cancel_queued_release = async () => { window.queued = []; return {ok: true, removed: true, queued: []}; };
        """,
    )
    out = result.value
    assert out["toast"] == out["expected"]["toast"]
    assert out["addArgs"] == [[77, None, True]]
    assert out["queuedTitle"] == out["expected"]["title"]
    assert out["queuedState"] == out["expected"]["state"]
    assert out["cancelArgs"] == [["77:batch"]]
    assert out["afterCancel"] == 0
    assert out["cancelToast"] == out["expected"]["cancel"]
    assert not result.errors


def test_home_card_of_a_queued_release_says_waiting_for_torrents(page) -> None:
    card = {
        **ANIME,
        "site_url": "https://anilist.co/anime/77",
        "progress": 0,
        "queued_release": {"id": "77:batch", "batch": True, "title": RELEASE["title"]},
        "presentation": {"status": "queued_torrents_off", "ready": False, "action_code": ""},
    }
    result = page.run(
        r"""
        renderDataPages(true); await __tick(50);
        const node = document.querySelector('.download-available-card .queued-release-state');
        return {text: node && node.textContent, action: node && node.dataset.action,
                button: document.querySelectorAll('.download-available-card .download-card-button').length,
                expected: t('release.queuedTorrentsOff'),
                status: episodePresentationStatus({presentation: {status: 'queued_torrents_off'}}, 3)};
        """,
        state={"home": {"download_available": [card]}, "current": [ANIME]},
    )
    out = result.value
    assert out["text"] == out["expected"] and out["action"] == "open-downloads"
    assert out["button"] == 0
    assert out["status"] == out["expected"]
    assert not result.errors


def test_asking_again_for_a_finished_search_searches_again(page) -> None:
    result = page.run(
        r"""
        const first = openRelease(77, null, true); await __flush();
        await __click('#modalClose'); await __flush();
        window.searches[0].resolve([window.release]); await first; await __flush();
        const again = openRelease(77, null, true); await __flush();
        const out = {calls: window.searches.length, body: __text('#modalBody .empty'), expected: t('label.searchingReleases')};
        window.searches[1].resolve([]); await again; await __flush();
        out.noReleases = __text('#modalBody .empty') === t('label.noReleases');
        return out;
        """,
        state={"current": [ANIME]},
        before=PENDING_SEARCHES + f"window.release = {json.dumps(RELEASE)};",
    )
    out = result.value
    assert out["calls"] == 2 and out["body"] == out["expected"] and out["noReleases"] is True
    assert not result.errors


def test_another_dialog_over_a_running_search_brings_its_line_back(page) -> None:
    result = page.run(
        r"""
        const opened = openRelease(77, null, true); await __flush();
        const before = __visible('#releaseTasks');
        await openDownloadCenter(null); await __flush(); await __tick(10);
        const out = {before, after: __visible('#releaseTasks'),
                     text: __text('#releaseTasks [data-action="release-task-open"]'),
                     expected: t('releaseTask.searching', {title: 'Queued Show'})};
        window.searches[0].resolve([window.release]); await opened; await __flush();
        out.ready = __text('#releaseTasks [data-action="release-task-open"]') === t('releaseTask.ready', {title: 'Queued Show'});
        out.downloadsStillShown = !!document.querySelector('#modalBody .download-center');
        return out;
        """,
        state={"current": [ANIME]},
        before=PENDING_SEARCHES
        + f"window.release = {json.dumps(RELEASE)};"
        + r"""
        __api.torrent_downloads = async () => ({backend: 'aria2', enabled: false, configured: true, downloads: [],
                                               queued: [], storage: {}, network_guard: {}, warning: ''});
        """,
    )
    out = result.value
    assert out["before"] is False and out["after"] is True and out["text"] == out["expected"]
    assert out["ready"] is True and out["downloadsStillShown"] is True
    assert not result.errors


def test_exhausted_queued_release_says_it_could_not_start(page) -> None:
    result = page.run(
        r"""
        await openDownloadCenter(null); await __flush(); await __tick(10);
        return {state: __text('.queued-release .download-state'),
                expected: t('release.queuedFailed', {error: 'aria2 is not answering'}),
                extra: document.querySelectorAll('.queued-release .danger').length};
        """,
        state={"current": [ANIME], "settings": {"torrents_enabled": True}},
        before=r"""
        __api.torrent_downloads = async () => ({backend: 'aria2', enabled: true, configured: true, downloads: [],
          queued: [{id: '77:episode:3', media_id: 77, episode: 3, batch: false, title: 'Show - 03', size_text: '',
                    anime_title: 'Queued Show', queued_at: 1, error: 'aria2 is not answering', attempts: 5,
                    exhausted: true, origin: 'manual', cancellable: true}],
          storage: {}, network_guard: {}, warning: ''});
        """,
    )
    out = result.value
    assert out["state"] == out["expected"] and out["extra"] == 0
    assert not result.errors
