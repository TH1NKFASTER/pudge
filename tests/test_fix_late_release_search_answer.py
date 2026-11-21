"""A slow release search answer does not land in the dialog opened after it.

Releases for Show 1 (nyaa slow), closed, then Releases for Show 2 / the Download Center
opened: Show 1's late answer replaced the current dialog's rows under Show 2's title and
put ui.releaseContext back to Show 1, so "Download" added Show 1's torrent. The same
pattern was in the light novel volume search and the AniList identity search.
"""

from __future__ import annotations

SETUP = r"""
window.__resolve = {};
__api.search_releases = (id) => new Promise(resolve => { window.__resolve['r' + id] = resolve; });
__api.light_novel_search_nyaa = (q) => new Promise(resolve => { window.__resolve['ln' + q] = resolve; });
__api.media_identity_search = (kind, q) => new Promise(resolve => { window.__resolve['id' + q] = resolve; });
__api.media_identity_current = async () => ({anilist_id: 0});
__api.torrent_downloads = async () => ({downloads: [], backend: 'aria2'});
"""

STATE = {
    "planned": [
        {"media_id": 1, "title": "Show 1", "episodes": 12, "progress": 0, "status": "PLANNING"},
        {"media_id": 2, "title": "Show 2", "episodes": 12, "progress": 0, "status": "PLANNING"},
    ],
    "settings": {"torrents_enabled": True},
}


def test_late_release_answer_is_dropped(page):
    result = page.run(
        r"""
        const body = () => document.getElementById('modalBody').innerText;
        const p1 = openRelease(1, 1, false); await __flush();
        closeModal();
        const p2 = openRelease(2, 1, false); await __flush();
        window.__resolve.r2([{title: '[Good] Show 2 - 01 (1080p)', seeders: 50, downloads: 9}]); await p2;
        const shown = body();
        window.__resolve.r1([{title: '[Other] Show 1 - 01', seeders: 1, downloads: 1}]); await p1;
        const afterLate = body(), context = ui.releaseContext && ui.releaseContext.mediaId;

        closeModal();
        const p3 = openRelease(1, 1, false); await __flush();
        closeModal();
        await openDownloadCenter(null); await __flush();
        window.__resolve.r1([{title: '[Other] Show 1 - 01', seeders: 1, downloads: 1}]); await p3;
        return {shown, afterLate, context, center: body(), centerContext: ui.releaseContext};
        """,
        state=STATE,
        before=SETUP,
        allow_errors=True,
    )
    value = result.value
    assert "[Good] Show 2 - 01" in value["shown"]
    assert "[Good] Show 2 - 01" in value["afterLate"] and "Show 1" not in value["afterLate"]
    assert value["context"] == 2
    assert "[Other] Show 1" not in value["center"]
    assert value["centerContext"] is None


def test_late_light_novel_and_identity_answers_are_dropped(page):
    result = page.run(
        r"""
        const body = () => document.getElementById('modalBody').innerText;
        const a = showLnNyaa(7, 'Book A', 2); await __flush();
        const b = showLnNyaa(8, 'Book B', 3); await __flush();
        window.__resolve['lnBook B']([{title: '[G] Book B v03', size: '1 GiB', seeders: 3}]); await b;
        window.__resolve['lnBook A']([{title: '[G] Book A v02', size: '1 GiB', seeders: 3}]); await a;
        const ln = {text: body(), rows: (ui.lnNyaaRows || []).map(row => row.title)};

        closeModal();
        const shown = showMediaIdentity('manga', 5, 'First'); await __flush(); await __flush();
        const second = renderMediaIdentitySearch('Second'); await __flush();
        window.__resolve.idSecond([{title: 'Second hit', media_id: 2, media_kind: 'manga'}]); await second;
        window.__resolve.idFirst([{title: 'First hit', media_id: 1, media_kind: 'manga'}]); await shown;
        return {ln, identity: body(), rows: ui.mediaIdentity.rows.map(row => row.title)};
        """,
        state=STATE,
        before=SETUP,
        allow_errors=True,
    )
    value = result.value
    assert "Book B v03" in value["ln"]["text"] and "Book A" not in value["ln"]["text"]
    assert value["ln"]["rows"] == ["[G] Book B v03"]
    assert "Second hit" in value["identity"] and "First hit" not in value["identity"]
    assert value["rows"] == ["Second hit"]
