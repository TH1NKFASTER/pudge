"""A live download percentage does not step back for a moment on a redraw.

User: "Rakugo's status changes strangely: 85 % ETA x turns into 84 % for a split second and
goes on, now it is 85 again." The live per-torrent progress patched the cards, but many
places replace ``ui.state`` with a fresh snapshot whose database progress lags the live one,
and the live reading was only re-applied through one path and only while it was under 10 s
old. Each such redraw showed the older percentage until the next live tick.
"""

from __future__ import annotations

import json

from tests.test_v0730_live_download_progress import DOWNLOAD, HASH, LIVE

SCRIPT = r"""
const realNow = Date.now; let shift = 0; Date.now = () => realNow() + shift * 1000;
const labels = () => [...document.querySelectorAll('[data-live-download]')].map(n => [n.dataset.liveKind, n.textContent]);
const push = live => PudgePush('torrents', {at: Date.now() / 1000, status: {ok: true, enabled: true, stale: false, transition: 'on',
  active: 1, waiting: 0, download_speed: 29, upload_speed: 0, updated_at: Date.now() / 1000, live_downloads: {HASH: live}}});
const snapshot = progress => {
  const download = {...DOWNLOAD, progress};
  const card = {media_id: 7, title: 'Show', episodes: 12, next_episode: 3, download};
  return {...ui.state, downloads: [download], current: [card], home: {...ui.state.home, waiting: [card], download_available: []}};
};
const redraw = async progress => { ui.state = snapshot(progress); ui.currentRenderSignature = null; renderDataPages(true); await __flush(); return labels(); };
setPage('current', true); renderCurrent(); await __tick(20);
push({progress: 0.85, downloaded_bytes: 85, total_bytes: 100, download_speed: 29}); await __flush();
const out = {live: labels()};
out.snapshot = await redraw(0.84);           // a snapshot set straight into ui.state, older than the live reading
out.direct = (ui.state = snapshot(0.84), renderCurrent(), await __flush(), labels());
await openDownloadCenter(null); await __tick(10);  // its list comes from the database: 10 %
out.center = document.querySelector('#modalBody .download-progress')?.title;
shift = 30; out.after30s = await redraw(0.84);  // no live tick for a while: still the newest known value
out.newerSnapshot = await redraw(0.9);          // a snapshot newer than the last live reading wins
shift = 200; out.forgotten = await redraw(0.84); // a reading minutes old is dropped
Date.now = realNow;
return out;
"""


def test_a_redraw_from_an_older_snapshot_keeps_the_live_percentage(page) -> None:
    result = page.run(
        SCRIPT.replace("DOWNLOAD", json.dumps(DOWNLOAD)).replace("HASH", json.dumps(HASH)),
        before=LIVE,
        state={
            "downloads": [DOWNLOAD],
            "current": [
                {"media_id": 7, "title": "Show", "episodes": 12, "next_episode": 3, "download": DOWNLOAD}
            ],
            "home": {
                "waiting": [
                    {"media_id": 7, "title": "Show", "episodes": 12, "next_episode": 3, "download": DOWNLOAD}
                ]
            },
            "settings": {"torrents_enabled": True},
        },
    )
    out = result.value
    first = lambda rows: sorted({text.split(" ")[0] for kind, text in rows})  # noqa: E731 - the percentages shown
    assert out["live"] and first(out["live"]) == ["85%"], out
    for name in ("snapshot", "direct", "after30s"):
        assert first(out[name]) == ["85%"], (name, out)
    assert out["center"] == "85%", out
    assert first(out["newerSnapshot"]) == ["90%"], out
    assert first(out["forgotten"]) == ["84%"], out
