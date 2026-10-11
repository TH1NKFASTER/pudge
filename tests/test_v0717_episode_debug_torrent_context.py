from __future__ import annotations

from pathlib import Path

import httpx

from pudge.first_experience import mpv_study_status
from pudge.models import SubtitleCandidate
from pudge.providers.qbittorrent import QBittorrentClient
from pudge.syncing import _exact_jimaku_audio_clock_consensus
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario


def test_ansatsu_exact_audio_consensus_selects_netflix_srt(tmp_path: Path) -> None:
    items = []
    for name, score, offset in (
        ("FFF.ass", 93.0, -0.04),
        ("Kamigami.ass", 82.6, 0.01),
        ("Netflix.ja.srt", 81.0, -0.08),
        ("TV.srt", 83.5, -44.32),
    ):
        path = tmp_path / name
        path.write_text("subtitle", encoding="utf-8")
        candidate = SubtitleCandidate(
            path,
            "jimaku",
            score,
            name,
            details={"episode_match": "exact", "entry_anilist_match": True},
        )
        items.append(
            (
                candidate,
                path,
                {"sync_was_successful": True, "offset_seconds": offset},
            )
        )

    selected, payload = _exact_jimaku_audio_clock_consensus(items)

    assert selected is not None and selected[0].name == "Netflix.ja.srt"
    assert payload["offsets_seconds"] == [-0.08, -0.04, 0.01]


def test_jpdb_detection_accepts_versioned_macos_layout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plugin = tmp_path / "Library" / "Application Support" / "mpv" / "scripts" / "jpdb-player"
    plugin.mkdir(parents=True)
    script = plugin / "jpdb.lua"
    script.write_text("-- jpdb", encoding="utf-8")
    server = plugin / "jpdb-mpv-plugin-0.11.0"
    server.write_text("binary", encoding="utf-8")
    server.chmod(0o755)

    status = mpv_study_status(jpdb_api_token="token", selected_plugin="jpdb")

    assert status["jpdb_mpv"]["available"] is True
    assert status["jpdb_mpv"]["script_path"] == str(script)


def test_qbittorrent_stop_falls_back_to_legacy_pause() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(404 if request.url.path.endswith("/stop") else 200)

    client = QBittorrentClient("http://qbt.local", api_key="test")
    client.client.close()
    client.client = httpx.Client(base_url="http://qbt.local", transport=httpx.MockTransport(handler))
    try:
        client.pause("abc")
    finally:
        client.close()

    assert seen == ["/api/v2/torrents/stop", "/api/v2/torrents/pause"]


# Anime Debug for a multi-episode title: the backend lists the local episodes.
DEBUG_SNAPSHOT = """
__api.anime_debug_snapshot = async (mediaId, episode) => ({
  anime: {media_id: mediaId, title: 'Show', episodes: window.__episodes ?? 12, format: window.__format ?? 'TV'},
  selected_episode: episode ?? 3,
  available_episodes: [{episode: 2, state: 'watched'}, {episode: 3, state: 'ready'}],
  selected_local_episode: {state: episode === 2 ? 'watched' : 'ready', video_path: `/lib/Show - 0${episode ?? 3}.mkv`},
});
"""


def test_anime_debug_switches_between_the_local_episodes(page) -> None:
    result = page.run(
        """
        const picker = () => ({visible: __visible('#pudgeDebugEpisodeSelect'), value: document.getElementById('pudgeDebugEpisodeSelect').value,
          options: [...document.querySelectorAll('#pudgeDebugEpisodeSelect option')].map(o => o.innerText),
          video: __text('#pudgeDebugBody').includes('/lib/Show - 02.mkv') ? 2 : __text('#pudgeDebugBody').includes('/lib/Show - 03.mkv') ? 3 : null});
        void PudgeDebug.open({media_id: 7, title: 'Show'});
        await __tick(10);
        const opened = picker();
        await __input('#pudgeDebugEpisodeSelect', '2');
        await __tick(10);
        const switched = picker();
        const hiddenFor = {};
        for (const [episodes, format] of [[1, 'TV'], [1, 'MOVIE'], [12, 'MOVIE']]) {
          window.__episodes = episodes; window.__format = format;
          void PudgeDebug.open({media_id: 7, title: 'Show'}); await __tick(10);
          hiddenFor[`${episodes} ${format}`] = !__visible('#pudgeDebugEpisodeSelect');
        }
        return {opened, switched, hiddenFor,
                options: [2, 3].map((episode, k) => PudgeI18n.t('debug.episodeOption', {episode, state: ['watched', 'ready'][k]}))};
        """,
        lang="ru",
        before=DEBUG_SNAPSHOT,
        api={"startup_maintenance": {"running": False}, "ui_state_versions": {"ready": "1", "ui": "1"}},
    )
    out = result.value
    assert out["opened"] == {"visible": True, "value": "3", "options": out["options"], "video": 3}
    assert out["switched"] == {"visible": True, "value": "2", "options": out["options"], "video": 2}
    assert result.calls_of("anime_debug_snapshot")[:2] == [[7, None], [7, 2]]
    assert out["hiddenFor"] == {"1 TV": True, "1 MOVIE": True, "12 MOVIE": True}


DOWNLOADS = [
    {
        "hash": "aaa",
        "name": "[Sub] Show - 03.mkv",
        "anime_title": "Show",
        "episode": 3,
        "media_id": 7,
        "state": "downloading",
        "progress": 0.4,
        "backend": "qBittorrent",
        "backend_id": "qbittorrent",
        "total_bytes": 1000,
        "downloaded_bytes": 400,
    },
    {
        "hash": "bbb",
        "name": "[Sub] Show - 04.mkv",
        "anime_title": "Show",
        "episode": 4,
        "media_id": 7,
        "state": "downloading",
        "progress": 0.2,
        "backend": "aria2",
        "backend_id": "aria2",
        "total_bytes": 1000,
        "downloaded_bytes": 200,
    },
]


def test_download_actions_go_to_the_torrent_client_that_owns_the_download(page) -> None:
    # qBittorrent and aria2 coexist: each row's actions name its own backend. There is no
    # global "stop all" control here any more (the sidebar torrent switch does that).
    result = page.run(
        """
        void openDownloadCenter(null);
        await __tick(100);
        const actions = [...new Set([...document.querySelectorAll('#modalBody button')].map(b => b.dataset.action))];
        await __click('#modalBody [data-hash=bbb][data-action=torrent-pause]');
        await __tick(10);
        await __click('#modalBody [data-hash=aaa][data-action=torrent-remove]');
        await __tick(20);
        const question = __text('#pudgeConfirmMessage') === PudgeI18n.t('ui.removeTheTorrentFromThe');
        await __click('#pudgeConfirmBackdrop [data-pudge-confirm=ok]');
        await __tick(10);
        return {actions, question};
        """,
        state={"settings": {"torrents_enabled": True}},
        api={
            "startup_maintenance": {"running": False},
            "ui_state_versions": {"ready": "1", "ui": "1"},
            "torrent_downloads": {"downloads": DOWNLOADS, "backend": "qbittorrent"},
            "torrent_download_action": {"downloads": DOWNLOADS},
        },
    )
    assert result.calls_of("torrent_download_action") == [
        ["bbb", "pause", False, "aria2"],
        ["aaa", "remove", False, "qbittorrent"],
    ]
    assert result.value["question"] is True
    assert set(result.value["actions"]) == {
        "refresh-downloads",
        "torrent-filter",
        "torrent-pause",
        "torrent-reconnect",
        "torrent-remove",
        "torrent-remove-files",
    }


# Plays 20 subtitle lines with an English secondary track, then translates the visible line;
# prints the arguments of the helper the shortcut starts.
TRANSLATE = """
local mp = require 'mp'
local launched = {}
local real = mp.command_native_async
mp.command_native_async = function(command, callback)
    if command.name == 'subprocess' then table.insert(launched, command.args) end
    return real(command, callback)
end
sim.fire('file-loaded')
for k = 1, 20 do sim.set('sub-text', 'セリフ' .. k); sim.play(0.5) end
sim.set('secondary-sub-text', 'Line twenty')
sim.press('Ctrl+t')
print('LAUNCHED:' .. #launched)
for k, value in ipairs(launched[1] or {}) do print('ARG:' .. value:gsub('\\n', '|')) end
print('PAUSED:' .. tostring(sim.props['pause']))
"""


@needs_lua
def test_mpv_translation_uses_sixteen_previous_lines_and_the_english_track_as_context(tmp_path: Path) -> None:
    out = run_mpv_scenario(tmp_path, TRANSLATE, scripts=[ANILIST], env={"PUDGE_PYTHON": "python3"})
    args = [line[4:] for line in out.splitlines() if line.startswith("ARG:")]
    assert "LAUNCHED:1" in out and "PAUSED:true" in out  # one helper, no separate study window
    assert args[:4] == ["python3", "-m", "pudge.cli", "--subtitle-translate"]
    assert args[args.index("--subtitle-study-text") + 1] == "セリフ20"
    context = args[args.index("--subtitle-study-context") + 1].split("|")
    assert context == [
        "Previous Japanese subtitles:",
        *[f"セリフ{k}" for k in range(4, 20)],
        "Corresponding English subtitle (supporting context only):",
        "Line twenty",
    ]
