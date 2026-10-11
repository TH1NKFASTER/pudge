"""Compact download status on anime cards (behaviour, page harness; audit 71Б rewrite)."""

from __future__ import annotations

# A download in progress and fresh torrent traffic (updated now, by the page's clock).
SETUP = """
const download = {state: 'downloading', progress: 0.42, eta_seconds: 600};
const fresh = enabled => ({enabled, stale: false, updated_at: Date.now() / 1000, transition: enabled ? 'on' : 'off_confirmed'});
const status = () => compactDownloadStatus(download);
"""


def test_status_is_the_percentage_with_eta_only_while_torrents_run(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            SETUP
            + """
            const out = {};
            ui.state.settings.torrents_enabled = true; ui.torrentTraffic = fresh(true);
            out.running = status();
            out.eta = PudgeI18n.t('home.eta', {time: torrentEta(600)});
            ui.torrentToggleDesired = false;                       // switching off: no ETA at once
            out.switchingOff = status();
            ui.torrentToggleDesired = null; ui.state.settings.torrents_enabled = false; ui.torrentTraffic = fresh(false);
            out.off = status();
            out.paused = t('ui.paused');
            return out;
            """,
            lang=lang,
        )
        out = result.value
        assert out["running"] == f"42% · {out['eta']}"  # no "Downloading 42%" prefix
        assert out["eta"] not in out["switchingOff"]
        assert out["off"] == f"{out['paused']} · 42%"


def test_switching_torrents_off_redraws_cards_before_the_answer(page) -> None:
    # The ETA disappears as soon as the switch is clicked, not when the backend answers;
    # after the answer the cards are drawn again and nothing fails.
    anime = {
        "media_id": 7,
        "title": "Show",
        "episodes": 12,
        "next_episode": 3,
        "download": {"state": "downloading", "progress": 0.42, "eta_seconds": 600},
        "presentation": {"status": "downloading", "episode": 3},
    }
    result = page.run(
        """
        ui.torrentTraffic = {enabled: true, stale: false, updated_at: Date.now() / 1000, transition: 'on'};
        setPage('current', true); renderCurrent();
        const eta = PudgeI18n.t('home.eta', {time: torrentEta(600)});
        const before = __text('#current');
        let answer;
        window.fetch = (url, init) => {
          __fetches.push({url: String(url), method: init?.method || 'GET'});
          if (!String(url).endsWith('/api/torrents/enabled')) return Promise.resolve(new Response('{"ok":true,"enabled":false}'));
          return new Promise(resolve => { answer = resolve; });
        };
        await __click('#torrentToggleButton');
        const pending = __text('#current');
        answer(new Response(JSON.stringify({ok: true, enabled: false, transition: 'off_requested'})));
        await __tick(10);
        return {eta, before, pending, after: __text('#current'), enabled: ui.state.settings.torrents_enabled};
        """,
        state={"current": [anime], "home": {"waiting": [anime]}, "settings": {"torrents_enabled": True}},
    )
    out = result.value
    assert out["eta"] in out["before"]
    assert out["eta"] not in out["pending"]
    assert out["eta"] not in out["after"] and out["enabled"] is False
