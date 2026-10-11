"""Home cards show a compact download status (percent and ETA only); the download center keeps
the detailed rates (behaviour, page harness; audit 71Б rewrite)."""

from __future__ import annotations

import json

from tests.i18n_support import en

DOWNLOAD = {
    "hash": "aaa",
    "name": "[Sub] Show - 03.mkv",
    "anime_title": "Show",
    "episode": 3,
    "media_id": 7,
    "state": "downloading",
    "progress": 0.42,
    "eta_seconds": 600,
    "download_speed": 2097152,
    "upload_speed": 1024,
    "total_bytes": 1000,
    "downloaded_bytes": 420,
}
ANIME = {
    "media_id": 7,
    "title": "Show",
    "episodes": 12,
    "next_episode": 3,
    "download": DOWNLOAD,
    "presentation": {"status": "downloading", "episode": 3},
}
# Torrents are on and the backend reports fresh traffic and download rows.
LIVE = """
window.fetch = async () => new Response(JSON.stringify({ok: true, enabled: true, desired_enabled: true, stale: false, transition: 'on',
  updated_at: Date.now() / 1000, download_speed: 2097152, upload_speed: 1024}), {status: 200});
__api.torrent_downloads = async () => ({downloads: [DOWNLOAD], enabled: true, observed_at: Date.now() / 1000, backend: 'aria2'});
""".replace("DOWNLOAD", json.dumps(DOWNLOAD))
STATE = {
    "current": [ANIME],
    "home": {"waiting": [ANIME]},
    "downloads": [DOWNLOAD],
    "settings": {"torrents_enabled": True},
}


def test_compact_status_and_download_center_rates(page) -> None:
    assert en("home.eta", time="5m") == "ETA 5m"
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            await __tick(1000);                                   // torrent traffic polled
            setPage('current', true); renderCurrent();
            const card = __text('#currentContent [data-media-id="7"] [data-live-download="aaa"]');
            await __tick(5000);                                   // live label refreshes keep it compact
            const later = __text('#currentContent [data-media-id="7"] [data-live-download="aaa"]');
            void openDownloadCenter(null); await __tick(100);
            const noEta = compactDownloadStatus({...DOWNLOAD, eta_seconds: 0});
            return {card, later, noEta, row: __text('#modalBody [data-download-hash="aaa"]'),
                    eta: PudgeI18n.t('home.eta', {time: torrentEta(600)}), down: torrentRate(2097152), up: torrentRate(1024),
                    downloading: torrentUiLabel('downloading', ui.lang)};
            """.replace("DOWNLOAD", json.dumps(DOWNLOAD)),
            lang=lang,
            before=LIVE,
            state=STATE,
        )
        out = result.value
        # No "Downloading 42%" prefix and no rates on the card.
        assert out["card"] == out["later"] == f"42% · {out['eta']}"
        assert out["down"] not in out["card"] and out["downloading"] not in out["card"]
        assert out["noEta"] == "42%"  # no ETA until the backend knows one
        # The download center row has the rates in both directions.
        assert f"↓\n{out['down']}\n↑\n{out['up']}" in out["row"]
