from __future__ import annotations

from pathlib import Path

import pytest

from pudge import i18n
from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.providers.aria2 import Aria2Client, Aria2Error
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario


def make_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    return cfg


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = make_config(tmp_path)
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_ready_notification_skips_episode_after_nearest_unwatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=77, title="Example", status="CURRENT", progress=0, episodes=12)
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pudge.manager.send_native_notification",
        lambda title, message: calls.append((title, message)) or True,
    )

    manager._notify_ready_episode(video=tmp_path / "Example - 02.mkv", media_id=77, episode=2)
    assert calls == []
    assert manager.db.get_state("ready_notification:episode:77:2", "") == ""

    manager._notify_ready_episode(video=tmp_path / "Example - 01.mkv", media_id=77, episode=1)
    assert len(calls) == 1


def test_ready_notification_uses_media_episode_not_release_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    manager = AnimeManager(cfg, log=lambda _message: None)
    anime = LibraryAnime(
        media_id=189046,
        title="Re:Zero 4",
        status="CURRENT",
        progress=11,
        episodes=19,
    )
    manager.db.upsert_anime(anime)
    calls: list[str] = []
    monkeypatch.setattr(
        "pudge.manager.send_native_notification",
        lambda _title, message: calls.append(message) or True,
    )
    manager._notify_ready_episode(video=tmp_path / "ReZero - 78.mkv", media_id=anime.media_id, episode=12)
    assert len(calls) == 1


def test_cleanup_uses_season_local_progress_for_absolute_episode(tmp_path: Path) -> None:
    manager = AnimeManager(make_config(tmp_path), log=lambda _message: None)
    video = tmp_path / "ReZero - 78.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=189046,
            title="Re:Zero 4",
            status="CURRENT",
            progress=11,
            episodes=19,
        )
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=189046,
            title="Re:Zero 4",
            episode=12,
            media_episode=12,
            release_episode=78,
            video_path=video,
        )
    )
    manager.db.schedule_cleanup(
        video,
        24,
        list_status="CURRENT",
        media_id=189046,
        episode=None,
    )
    assert manager.db.get_anime(189046).progress == 12


def test_rating_waits_for_real_final_episode_with_absolute_numbering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_api(tmp_path)
    video = tmp_path / "ReZero - 79.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=189046,
            title="Re:Zero 4",
            status="CURRENT",
            progress=13,
            episodes=19,
        )
    )
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=189046,
            title="Re:Zero 4",
            episode=13,
            media_episode=13,
            release_episode=79,
            video_path=video,
            state="watched",
        )
    )
    assert api.play_status(str(video))["final_episode"] is False


def test_planning_download_is_visible_in_planning_and_waiting(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=123,
            title="Planning Batch",
            status="PLANNING",
            episodes=12,
            media_status="FINISHED",
        )
    )
    api.manager.db.upsert_download(
        DownloadItem(
            torrent_hash="abc",
            name="Planning Batch [Batch]",
            state="active",
            progress=0.42,
            save_path=str(tmp_path / "downloads"),
            content_path=str(tmp_path / "downloads" / "batch"),
            media_id=123,
            is_batch=True,
            added_on=100,
            raw={"backend": "aria2", "total_size": 1000, "downloaded": 420, "dlspeed": 50},
        )
    )
    state = api.get_state_fast()
    assert state["planned"][0]["download"]["progress"] == pytest.approx(0.42)
    waiting = next(row for row in state["home"]["waiting"] if row["media_id"] == 123)
    assert waiting["download"]["state"] == "active"


def test_aria2_network_and_seeding_options_are_real_launch_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Aria2Client(
        state_dir=tmp_path / "aria2",
        seed_mode="ratio_or_time",
        seed_ratio=1.5,
        seed_time_minutes=90,
        upload_limit_kib=512,
        vpn_interface="utun9",
        vpn_kill_switch=True,
    )
    monkeypatch.setattr(client, "_interface_names", lambda: {"lo0", "utun9"})
    options = client._launch_options()
    assert "--no-conf=true" in options
    assert "--seed-ratio=1.5" in options
    assert "--seed-time=90.0" in options
    assert "--max-upload-limit=512K" in options
    assert "--interface=utun9" in options
    assert client.network_guard_status()["protected"] is True
    client.close()


def test_aria2_kill_switch_refuses_missing_vpn_interface(tmp_path: Path) -> None:
    client = Aria2Client(
        state_dir=tmp_path / "aria2",
        vpn_interface="utun9",
        vpn_kill_switch=True,
        auto_start=False,
    )
    client._interface_names = lambda: {"lo0"}  # type: ignore[method-assign]
    with pytest.raises(Aria2Error, match="torrent traffic is blocked") as raised:
        client.ensure_running()
    assert "заблокирован" in i18n.message(str(raised.value), "ru")
    client.close()


def test_extended_torrent_and_subtitle_shortcut_config_round_trip(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.aria2.seed_mode = "ratio"
    cfg.aria2.seed_ratio = 2.0
    cfg.aria2.vpn_interface = "utun3"
    cfg.aria2.vpn_kill_switch = True
    cfg.shortcuts.mpv_translate_subtitle = "Ctrl+y"
    cfg.tools.mpv_study_plugin = "jpdb"
    write_config(cfg, cfg.config_path)
    loaded = load_config(cfg.config_path)
    assert loaded.aria2.seed_mode == "ratio"
    assert loaded.aria2.seed_ratio == 2.0
    assert loaded.aria2.vpn_interface == "utun3"
    assert loaded.aria2.vpn_kill_switch is True
    assert loaded.shortcuts.mpv_translate_subtitle == "Ctrl+y"
    assert loaded.tools.mpv_study_plugin == "jpdb"


# The translation runs as a helper process; its calls are recorded with their options.
TRANSLATE = r"""
local mp = require('mp')
local original = mp.command_native_async
local function show(value) return (tostring(value):gsub('\n', '\\n')) end
mp.command_native_async = function(tbl, cb)
    if tbl.name == 'subprocess' then
        print('CALL\t' .. tostring(tbl.playback_only) .. '\t' .. show(table.concat(tbl.args, '\31')))
    end
    return original(tbl, cb)
end
sim.set('sub-text', '')
sim.fire('file-loaded')
sim.play(3)                                   -- the translation cache is prewarmed after 2 s
for i = 1, 18 do sim.set('sub-text', 'セリフ' .. i) end
sim.set('secondary-sub-text', 'Line eighteen')
sim_stdout = 'progress\nPUDGE_TRANSLATION_JSON={"translation":"The eighteenth line"}\n'
print('DEFAULT\t' .. tostring(sim.binding_for_key('Ctrl+t')))
assert(sim.press('Ctrl+y'))
print('PAUSE\t' .. tostring(sim.props['pause']))
print('OVERLAY\t' .. show(sim.overlay()))
sim.set('sub-text', '')
assert(sim.press('Ctrl+y'))
for _, row in ipairs(sim.log) do if row.kind == 'osd' then print('OSD\t' .. show(row.value)) end end
"""


@needs_lua
def test_mpv_translates_the_visible_subtitle_in_the_player_with_long_context(tmp_path: Path) -> None:
    out = run_mpv_scenario(
        tmp_path,
        TRANSLATE,
        scripts=[ANILIST],
        env={
            "PUDGE_SHORTCUT_TRANSLATE_SUBTITLE": "Ctrl+y",
            "PUDGE_SUBTITLE_PATH": "/media/Show - 05.ja.srt",
            "PUDGE_ANILIST_MEDIA_ID": "77",
            "PUDGE_PYTHON": "/app/python",
        },
    )
    rows = [line.split("\t", 2) for line in out.splitlines() if "\t" in line]
    calls = [(row[1], row[2].split("\x1f")) for row in rows if row[0] == "CALL"]
    values = {row[0]: row[1] for row in rows if row[0] in ("DEFAULT", "PAUSE", "OVERLAY", "OSD")}
    prewarm, translate = calls
    # Not playback-only: the prewarm notices the next file / closed player itself
    # and logs its summary before it ends.
    assert prewarm[0] == "false" and prewarm[1][:5] == [
        "/app/python",
        "-m",
        "pudge.cli",
        "--subtitle-prewarm-file",
        "/media/Show - 05.ja.srt",
    ]
    playback_only, args = translate
    assert playback_only == "false"
    assert args[:4] == ["/app/python", "-m", "pudge.cli", "--subtitle-translate"]
    options = dict(zip(args[4::2], args[5::2]))
    assert options["--subtitle-study-text"] == "セリフ18"
    assert options["--subtitle-study-media-id"] == "77"
    # Context: the previous 16 Japanese lines and the English subtitle.
    context = options["--subtitle-study-context"].split("\\n")
    assert context == [
        "Previous Japanese subtitles:",
        *[f"セリフ{i}" for i in range(2, 18)],
        "Corresponding English subtitle (supporting context only):",
        "Line eighteen",
    ]
    # The player pauses and shows the answer over the video; no study window opens.
    assert values["PAUSE"] == "true"
    assert values["OVERLAY"].endswith("}The eighteenth line")
    assert values["DEFAULT"] == "nil"  # the configured key replaces Ctrl+t
    assert values["OSD"] == en("osd.noTextSubtitleVisible")


def test_download_center_filters_shows_ratio_vpn_state_and_reconnects(page) -> None:
    rows = [
        {
            "hash": "a1",
            "name": "Show 01",
            "anime_title": "Show",
            "state": "downloading",
            "progress": 0.4,
            "ratio": 0.25,
            "backend": "aria2",
        },
        {
            "hash": "b2",
            "name": "Show 02",
            "anime_title": "Show",
            "state": "paused",
            "progress": 0.5,
            "ratio": 0,
            "backend": "aria2",
        },
        {
            "hash": "c3",
            "name": "Show 03",
            "anime_title": "Show",
            "state": "seeding",
            "progress": 1,
            "ratio": 1.5,
            "backend": "aria2",
        },
        {
            "hash": "d4",
            "name": "Show 04",
            "anime_title": "Show",
            "state": "complete",
            "progress": 1,
            "ratio": 2,
            "backend": "aria2",
        },
        {
            "hash": "e5",
            "name": "Show 05",
            "anime_title": "Show",
            "state": "downloading",
            "progress": 0.1,
            "ratio": 0,
            "backend": "qbittorrent",
        },
    ]
    assert en("ui.ratio") == "Ratio" and ru("ui.ratio") == "Рейтинг"
    code = r"""
        await __settle(openDownloadCenter(null));
        const cards = () => [...document.querySelectorAll('#modalBody .download-card')].map(card => card.dataset.downloadHash);
        const out = {guard: __text('#modalBody .network-guard'), ratio: __text('#modalBody [data-download-hash=c3] .download-stats'),
                     label: t('ui.ratio'), reconnect: [...document.querySelectorAll('#modalBody [data-action=torrent-reconnect]')].map(b => b.dataset.hash)};
        for (const filter of ['active', 'paused', 'seeding', 'finished', 'all']) { await __click(`#modalBody [data-filter=${filter}]`); out[filter] = cards(); }
        await __click('#modalBody [data-action=torrent-reconnect][data-hash=a1]');
        return out;
    """
    for lang, guard in (
        ("en", {"bound": True, "protected": True, "interface": "utun9", "kill_switch": True}),
        ("ru", {"bound": True, "protected": False, "interface": "utun9", "kill_switch": True}),
    ):
        result = page.run(
            code,
            lang=lang,
            state={"settings": {"torrents_enabled": True}},
            api={
                "torrent_downloads": {
                    "downloads": rows,
                    "backend": "aria2",
                    "observed_at": 1790000000,
                    "network_guard": guard,
                },
                "torrent_download_action": {"downloads": rows},
            },
        )
        out = result.value
        assert out["guard"] == ("VPN utun9 ✓" if guard["protected"] else "VPN utun9 ✕")
        assert f"{out['label']}\n1.50" in out["ratio"]
        assert (out["active"], out["paused"], out["seeding"], out["finished"]) == (
            ["a1", "e5"],
            ["b2"],
            ["c3"],
            ["c3", "d4"],
        )
        assert out["all"] == ["a1", "b2", "c3", "d4", "e5"]
        # Only unfinished torrents of the built-in downloader (aria2) can reconnect.
        assert out["reconnect"] == ["a1", "b2"]
        assert result.calls_of("torrent_download_action") == [["a1", "reconnect", False, "aria2"]]


def test_vpn_kill_switch_and_mpv_study_plugin_are_saved_from_settings(page, tmp_path: Path) -> None:
    import json

    api = make_api(tmp_path)
    state = json.loads(json.dumps(api.get_state(), default=str))
    state["settings"].update(
        aria2_enabled=True,
        aria2_vpn_interface="utun3",
        mpv_study_plugins={"jiten_mpv": {"installed": True}, "jpdb_mpv": {"installed": True}},
    )
    result = page.run(
        r"""
        await __input('#s_aria2_vpn_kill_switch', true);
        await __input('#s_mpv_study_plugin', 'jpdb');
        await __click('#saveSettings'); await __tick(10);
        """,
        state=state,
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    [[values]] = result.calls_of("save_settings")
    assert (values["aria2_vpn_kill_switch"], values["mpv_study_plugin"]) == (True, "jpdb")
    api.save_settings(values)
    loaded = load_config(api.config.config_path)
    assert (loaded.aria2.vpn_kill_switch, loaded.aria2.vpn_interface, loaded.tools.mpv_study_plugin) == (
        True,
        "utun3",
        "jpdb",
    )
