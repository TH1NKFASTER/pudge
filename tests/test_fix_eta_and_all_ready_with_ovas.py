"""Two card labels: "ETA" that came and went, and Baccano!'s "Episodes 1-13" of 13.

User: "now and then the ETA disappears from the cards although the torrent keeps
downloading and nothing seems to stall". The ETA needed the whole traffic status to be under
15 s old; one slow poll (a busy client hands back its previous answer) made it older, and a
redraw from a snapshot with a higher percentage skipped the live row with its speed. Now the
download's own live reading (kept two minutes) also counts for a minute, and the live speed
and ETA still apply when the snapshot's percentage stays.

User: "Baccano says ready 1-13 although the anime has 13 episodes; expected All episodes
ready". The TV + Specials pack's OVA files have no episode number, and the list [1..13, None]
was compared with [1..13].
"""

from __future__ import annotations

import json
from pathlib import Path

from test_v0730_live_download_progress import DOWNLOAD, HASH, LIVE
from test_web_app import _downloaded, make_api

from pudge.manager_models import LibraryAnime, LibraryEpisode

ETA = r"""
const realNow = Date.now; let shift = 0; Date.now = () => realNow() + shift * 1000;
const label = () => [...document.querySelectorAll('[data-live-download]')].filter(n => n.dataset.liveKind === 'compact').map(n => n.textContent)[0] || '';
const push = (live, at) => PudgePush('torrents', {at, status: {ok: true, enabled: true, stale: false, transition: 'on',
  active: 1, waiting: 0, download_speed: 1000, upload_speed: 0, updated_at: at, live_downloads: {HASH: live}}});
setPage('current', true); renderCurrent(); await __tick(20);
push({progress: 0.5, downloaded_bytes: 500000, total_bytes: 1000000, download_speed: 1000}, Date.now() / 1000); await __flush();
const out = {live: label()};
// 20 s later the traffic status is "old" (a slow poll), a snapshot with a higher percentage redraws.
shift = 20;
const download = {...DOWNLOAD, progress: 0.6};
const card = {...ui.state.current[0], download};
ui.state = {...ui.state, downloads: [download], current: [card], home: {...ui.state.home, waiting: [card]}};
ui.currentRenderSignature = null; renderDataPages(true); await __flush();
out.slowPoll = label();
shift = 100; ui.currentRenderSignature = null; renderDataPages(true); await __flush();
out.minutesLater = label();
Date.now = realNow;
return out;
"""


def test_eta_stays_through_a_slow_poll_and_a_snapshot_redraw(page) -> None:
    card = {"media_id": 7, "title": "Show", "episodes": 12, "next_episode": 3, "download": DOWNLOAD}
    out = page.run(
        ETA.replace("DOWNLOAD", json.dumps(DOWNLOAD)).replace("HASH", json.dumps(HASH)),
        before=LIVE,
        state={
            "downloads": [DOWNLOAD],
            "current": [card],
            "home": {"waiting": [card]},
            "settings": {"torrents_enabled": True},
        },
    ).value
    assert out["live"].startswith("50%") and "ETA" in out["live"], out
    # The snapshot's 60 % stays; the live speed gives the ETA of what is left (400 KB at 1 KB/s).
    assert out["slowPoll"].startswith("60%") and "ETA" in out["slowPoll"], out
    assert "ETA" not in out["minutesLater"], out  # no live reading for minutes: no ETA


def test_all_episodes_ready_with_unnumbered_ova_files(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(LibraryAnime(media_id=2251, title="Baccano!", status="PLANNING", episodes=13))
    names = [(n, f"[DeeVeeDee] Baccano - {n:02d} [DVD 480p].mkv") for n in range(1, 14)]
    names += [
        (None, f"[DeeVeeDee] Baccano - S00E0{n} - Episode {13 + n} - OVA {n} [DVD 480p].mkv")
        for n in (1, 2, 3)
    ]
    for episode, name in names:
        video = tmp_path / name
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(media_id=2251, title="Baccano!", episode=episode, video_path=video, state="ready")
        )
    card = next(item for item in _downloaded(api) if item["media_id"] == 2251)
    assert card["all_episodes_ready"] is True
    assert [value for value in card["ready_episodes"] if value is not None] == list(range(1, 14))
