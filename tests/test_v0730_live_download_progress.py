from __future__ import annotations

import json
import threading
from types import SimpleNamespace


from pudge import web_app
from pudge.providers.aria2 import Aria2Client

HASH = "4ae3d4b812e7ba766d008d04b41b79344b7ad0bb"


def test_aria2_live_progress_reads_tell_active_without_starting(tmp_path, monkeypatch):
    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    monkeypatch.setattr(client, "_probe", lambda: True)
    calls = []

    def rpc(method, params=None):
        calls.append(method)
        assert method == "aria2.tellActive"
        return [
            {
                "gid": "1",
                "infoHash": HASH.upper(),
                "totalLength": "1000",
                "completedLength": "250",
                "downloadSpeed": "50",
            },
            {"gid": "2", "infoHash": "b" * 40, "totalLength": "0", "completedLength": "0"},  # metadata phase
            {
                "gid": "3",
                "infoHash": "c" * 40,
                "totalLength": "10",
                "completedLength": "1",
                "followedBy": ["4"],
            },
        ]

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    try:
        assert client.live_progress() == {
            HASH: {"progress": 0.25, "downloaded_bytes": 250, "total_bytes": 1000, "download_speed": 50},
        }
    finally:
        client.close()
    assert calls == ["aria2.tellActive"]


def _api(fake):
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=True), qbittorrent=SimpleNamespace(category="pudge")
    )
    api.manager = SimpleNamespace(
        torrent_clients=lambda: [("aria2", fake)], download_intents=SimpleNamespace(waiting_count=lambda: 0)
    )
    api.logger = SimpleNamespace(debug=lambda *args, **kwargs: None)
    api._torrent_traffic_lock = threading.Lock()
    api._last_torrent_traffic = {}
    api._downloads_configured = lambda: True
    return api


def test_traffic_status_carries_live_per_torrent_progress(monkeypatch):
    class FakeAria2:
        def traffic_stats(self):
            return {"download_speed": 50, "upload_speed": 0, "active": 1, "waiting": 0}

        def live_progress(self):
            return {HASH: {"progress": 0.5, "downloaded_bytes": 5, "total_bytes": 10, "download_speed": 50}}

        def close(self):
            pass

    monkeypatch.setattr(web_app, "Aria2Client", FakeAria2)
    result = _api(FakeAria2()).torrent_traffic_status()
    assert result["live_downloads"][HASH]["progress"] == 0.5


def test_traffic_status_skips_live_progress_when_nothing_is_active(monkeypatch):
    class FakeAria2:
        def traffic_stats(self):
            return {"download_speed": 0, "upload_speed": 0, "active": 0, "waiting": 0}

        def live_progress(self):
            raise AssertionError("no tellActive round-trip when aria2 has no active downloads")

        def close(self):
            pass

    monkeypatch.setattr(web_app, "Aria2Client", FakeAria2)
    assert _api(FakeAria2()).torrent_traffic_status()["live_downloads"] == {}


# A download shown on Home twice (a waiting card and a "download available" card that
# carries the hash in upper case) and in the download center. Python pushes the live
# per-torrent progress; a library resync later brings the old 10 % back from the database.
DOWNLOAD = {"torrent_hash": HASH, "name": "Show 03", "state": "downloading", "progress": 0.1, "media_id": 7}
LIVE = """
for (const name of ['get_state', 'get_state_fast']) {
  const original = __api[name];
  __api[name] = async (...args) => { const state = await original(...args); if (window.__v2) { state.ready_state_version = '2'; state.ui_state_version = '2'; } return state; };
}
__api.ui_state_versions = async () => window.__v2 ? {ready: '2', ui: '2'} : {ready: '1', ui: '1'};
__api.torrent_downloads = async () => ({enabled: true, backend: 'aria2', observed_at: Date.now() / 1000,
  downloads: [{hash: HASH, name: 'Show 03', state: 'downloading', progress: 0.1, media_id: 7, backend: 'aria2'}]});
""".replace("HASH", json.dumps(HASH))


def test_frontend_patches_live_progress_into_every_download_view(page) -> None:
    waiting = {"media_id": 8, "title": "Other", "episodes": 12, "next_episode": 3, "download": DOWNLOAD}
    available = {
        "media_id": 7,
        "title": "Show",
        "episodes": 12,
        "next_episode": 3,
        "download": {**DOWNLOAD, "torrent_hash": HASH.upper()},
    }
    for lang in ("en", "ru"):
        result = page.run(
            """
            const labels = () => [...document.querySelectorAll('[data-live-download]')].map(n => [n.dataset.liveKind, n.textContent]);
            const center = () => ({progress: document.querySelector('#modalBody .download-progress')?.title,
                                   card: __text('#modalBody .download-card')});
            const push = live => PudgePush('torrents', {at: Date.now() / 1000, status: {ok: true, enabled: true, stale: false, transition: 'on',
              active: 1, waiting: 0, download_speed: 29, upload_speed: 0, updated_at: Date.now() / 1000, live_downloads: {HASH: live}}});
            setPage('current', true); renderCurrent(); await __tick(20);
            await openDownloadCenter(null); await __tick(10);
            const out = {before: labels(), centerBefore: center().progress};
            push({progress: 0.42, downloaded_bytes: 42, total_bytes: 100, download_speed: 29}); await __flush();
            out.live = labels(); out.center = center();
            window.__v2 = true; PudgePush('versions', {ready: '2', ui: '2'}); await __tick(50);
            renderCurrent(); await __flush();                       // the next redraw of the cards
            out.resynced = {labels: labels(), version: ui.state.ui_state_version};
            push({progress: 0, downloaded_bytes: 0, total_bytes: 0}); await __flush();     // metadata phase: no size yet
            out.noSize = labels();
            out.eta = PudgeI18n.t('home.eta', {time: torrentEta(2)}); out.etaCell = torrentEta(2);
            out.downloading = t('label.downloading', {progress: 42});
            out.downloaded = `${torrentBytes(42)} / ${torrentBytes(100)}`;
            return out;
            """.replace("HASH", json.dumps(HASH)),
            lang=lang,
            before=LIVE,
            state={
                "downloads": [DOWNLOAD],
                "current": [waiting],
                "home": {"download_available": [available], "waiting": [waiting]},
                "settings": {"torrents_enabled": True},
            },
        )
        out = result.value
        assert out["before"] == [["compact", "10%"], ["home", "10%"]] and out["centerBefore"] == "10%"
        live = [["compact", f"42% · {out['eta']}"], ["home", out["downloading"]]]
        assert out["live"] == live  # both Home cards, with the ETA from the live speed
        assert out["center"]["progress"] == "42%"
        assert out["downloaded"] in out["center"]["card"] and out["etaCell"] in out["center"]["card"]
        assert out["resynced"] == {"labels": live, "version": "2"}  # the resync does not bring 10 % back
        assert out["noSize"] == live
