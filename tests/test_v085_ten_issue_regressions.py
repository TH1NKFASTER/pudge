from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import agent
from desktop_isolation import patch_webapp_popen

from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_later_ready_episodes_do_not_make_missing_nearest_episode_ready(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=79001,
            title="Sayonara Lara",
            status="PLANNING",
            media_status="RELEASING",
            progress=0,
            episodes=12,
        )
    )
    for episode in range(2, 7):
        video = tmp_path / f"Lara - {episode:02d}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=79001,
                title="Sayonara Lara",
                episode=episode,
                video_path=video,
                state="ready",
            )
        )

    home = api.get_state()["home"]

    assert not any(item.get("media_id") == 79001 for item in home["new_ready"])
    assert any(item.get("media_id") == 79001 for item in home["waiting"])


def test_airing_search_skips_unwatched_local_episode_and_reaches_new_release(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.qbittorrent.enabled = True
    cfg.nyaa.max_auto_download_per_anime = 2
    manager = AnimeManager(cfg)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=79002,
            title="Airing",
            status="CURRENT",
            media_status="RELEASING",
            progress=5,
            episodes=12,
            next_airing_episode=8,
            next_airing_at=int(time.time()) + 3600,
        )
    )
    episode6 = tmp_path / "Airing - 06.mkv"
    episode6.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=79002,
            title="Airing",
            episode=6,
            video_path=episode6,
            state="ready",
        )
    )
    release = NyaaRelease(
        title="[Group] Airing - 07 [1080p]",
        link="",
        torrent_url="https://example.invalid/7.torrent",
        info_hash="7" * 40,
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=100,
        leechers=0,
        downloads=100,
        trusted=True,
        remake=False,
        score=100.0,
        group="Group",
    )
    searched: list[int] = []
    monkeypatch.setattr(
        manager,
        "search_releases",
        lambda _media_id, *, episode, batch, automatic=False: searched.append(episode) or [release],
    )
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: True)
    monkeypatch.setattr(manager, "add_release", lambda *args, **kwargs: release)

    assert manager.auto_search_current() == 1
    assert searched == [7]


def test_scheduled_agent_runs_due_subtitle_job_without_general_poll(monkeypatch, tmp_path: Path) -> None:
    now = time.time()
    calls: list[tuple[str, int | None]] = []

    class FakeDb:
        def get_state(self, key, default=""):
            assert key == "agent_last_run"
            return str(now)

        def subtitle_jobs(self):
            return [{"state": "pending", "next_check": now - 1}]

    class FakeManager:
        def __init__(self, _config):
            self.db = FakeDb()

        def anilist_refresh_due(self, *, now):
            return False

        def process_subtitle_jobs(self, *, limit):
            calls.append(("subs", limit))
            return 1

        def run_once(self):
            raise AssertionError("general maintenance must not be needed")

    config = SimpleNamespace(
        agent=SimpleNamespace(enabled=True, poll_minutes=60),
    )
    monkeypatch.setattr(agent, "load_config", lambda _path: config)
    monkeypatch.setattr(agent, "app_session_active", lambda: True)
    monkeypatch.setattr(agent, "app_session_window_active", lambda: False)
    monkeypatch.setattr(agent, "AnimeManager", FakeManager)
    monkeypatch.setattr(agent.time, "time", lambda: now)

    assert agent.main(["--scheduled", "--config", str(tmp_path / "config.toml")]) == 0
    assert calls == [("subs", 8)]


def test_couldnt_sync_raw_play_bypasses_resolver_and_uses_original_file(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = (tmp_path / "episode.mkv").resolve()
    raw = (tmp_path / "raw-jimaku.srt").resolve()
    video.write_bytes(b"video")
    raw.write_text("1\n00:00:10,000 --> 00:00:11,000\n字幕\n", encoding="utf-8")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=79003,
            title="Unsynced",
            episode=1,
            video_path=video,
            subtitle_path=raw,
            state="waiting_subtitles",
        )
    )
    api.manager.db.set_couldnt_sync_subtitle(video, raw, origin="jimaku")

    class FakeProcess:
        pid = 79003

        def poll(self):
            return None

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "pudge.web_app.resolve_episode_subtitle",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("raw watch must bypass resolver")),
    )
    patch_webapp_popen(
        monkeypatch,
        lambda command, **kwargs: calls.append(command) or FakeProcess(),
    )

    result = api.play(str(video), allow_unsynced_subtitles=True)

    assert result["duplicate"] is False
    command = calls[0]
    assert command[command.index("--sub") + 1] == str(raw)
    assert "--no-sync" in command


@pytest.mark.parametrize("diagnostics_running", [False, True])
def test_resume_within_two_minutes_has_no_rewind(tmp_path: Path, monkeypatch, diagnostics_running) -> None:
    api = make_api(tmp_path)
    video = (tmp_path / "resume.mkv").resolve()
    subtitle = (tmp_path / "resume.srt").resolve()
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=79004,
            title="Resume",
            episode=1,
            video_path=video,
            subtitle_path=subtitle,
            state="ready",
        )
    )
    api.manager.db.record_playback(video, 321.5, 1400.0)

    class FakeProcess:
        pid = 79004

        def poll(self):
            return None

    calls: list[list[str]] = []
    real_popen = subprocess.Popen
    diagnostic_commands = []

    def diagnostic_popen(command, *args, **kwargs):
        diagnostic_commands.append(command)
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", diagnostic_popen)
    patch_webapp_popen(
        monkeypatch,
        lambda command, **kwargs: calls.append(command) or FakeProcess(),
    )

    if diagnostics_running:
        from pudge.energy_diagnostics import EnergyDiagnosticsMonitor

        # Force a diagnostics spawn while the playback mock is installed. With
        # the old global patch this ps call deterministically became calls[0].
        probe = threading.Thread(target=EnergyDiagnosticsMonitor._process_rows)
        probe.start()
        probe.join(timeout=10)
        assert not probe.is_alive()
        assert calls == []
        assert diagnostic_commands[0][:2] == ["ps", "axo"]

    api.play(str(video), resume=True)

    assert len(calls) == 1
    command = calls[0]
    assert command[command.index("--start-at") + 1] == "321.500"


def test_active_mpv_registry_is_restored_after_webapp_restart(tmp_path: Path, monkeypatch, page) -> None:
    api = make_api(tmp_path)
    video = (tmp_path / "open.mkv").resolve()
    video.write_bytes(b"video")

    class FakeProcess:
        pid = 79123

        def poll(self):
            return None

    # Patch only WebApp's Popen namespace; startup and diagnostics subprocess.run
    # calls must still retain the stdlib Popen protocol.
    with monkeypatch.context() as mpv_patch:
        patch_webapp_popen(mpv_patch, lambda *args, **kwargs: FakeProcess())
        api.play(str(video))

    monkeypatch.setattr("pudge.web_app.os.kill", lambda pid, signal: None)
    reopened = WebAppApi(api.config.config_path)
    state = json.loads(json.dumps(reopened.get_state(), default=str))
    active = state["active_playbacks"]

    assert any(item["video_path"] == str(video) and item["pid"] == 79123 for item in active)

    # The reopened window keeps watching that mpv: it polls its status, refuses a second
    # player for the same file, and allows playing again once mpv has closed.
    result = page.run(
        r"""
        window.__mpv = {status: 'running'};
        __api.play_status = async () => window.__mpv;
        await __tick(2500);
        const polled = __apiCalls.filter(call => call.name === 'play_status').map(call => call.args[0]);
        await startPlay(VIDEO);
        const second = {toasts: __toasts(), open: t('toast.videoOpen')};
        window.__mpv = {status: 'exited', exit_code: 0};
        await __tick(2500);
        await startPlay(VIDEO);
        return {polled, second};
        """.replace("VIDEO", json.dumps(str(video))),
        state=state,
        api={"play": {"status": "starting"}},
    )
    out = result.value
    assert out["polled"] and set(out["polled"]) == {str(video)}
    assert out["second"]["open"] in out["second"]["toasts"]
    assert [call[0] for call in result.calls_of("play")] == [str(video)]  # only after mpv closed


# Cmd+Shift+L in mpv: the episode's debug snapshot is exported and shown in Finder.
EXPORT_KEY = """
sim.press('Meta+Shift+l')
for _, row in ipairs(sim.log) do
    if row.kind == 'osd' then print('OSD:' .. row.value) end
    if row.kind == 'subprocess' and row.value:find('pudge.cli', 1, true) then print('RUN:' .. row.value) end
end
"""


@needs_lua
def test_mpv_cmd_shift_l_exports_the_episode_debug_snapshot(tmp_path: Path) -> None:
    for language, text in (("en", en), ("ru", ru)):
        out = run_mpv_scenario(
            tmp_path,
            EXPORT_KEY,
            scripts=[ANILIST],
            language=language,
            env={
                "PUDGE_PYTHON": "python3",
                "PUDGE_CONFIG": "/cfg/config.toml",
                "PUDGE_PLAYBACK_VIDEO": "/media/Show - 05.mkv",
            },
        )
        runs = [line[4:] for line in out.splitlines() if line.startswith("RUN:")]
        assert runs == [
            "python3 -m pudge.cli --export-episode-debug --playback-video /media/Show - 05.mkv --config /cfg/config.toml"
        ]
        osd = [line[4:] for line in out.splitlines() if line.startswith("OSD:")]
        assert osd == [text("osd.exportingEpisodeLogs"), text("osd.episodeLogsExported")]
    # A file Pudge did not start has no library episode to export.
    out = run_mpv_scenario(tmp_path, EXPORT_KEY, scripts=[ANILIST], env={"PUDGE_PYTHON": "python3"})
    assert "RUN:" not in out
    assert f"OSD:{en('osd.debugExportUnavailable')}" in out


def test_episode_debug_export_writes_snapshot_to_debug_logs_and_reveals_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from pudge import cli

    api = make_api(tmp_path)
    api.manager.db.upsert_anime(LibraryAnime(media_id=79001, title="Sayonara Lara", episodes=12))
    video = (tmp_path / "Lara - 03.mkv").resolve()
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=79001, title="Sayonara Lara", episode=3, video_path=video, state="ready")
    )
    logs = tmp_path / "debug-logs"
    logs.mkdir()
    revealed: list[list[str]] = []
    monkeypatch.setattr(cli, "debug_log_dir", lambda: logs)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    # Only the CLI's own subprocess calls are captured (other threads keep the real ones).
    monkeypatch.setattr(
        cli,
        "subprocess",
        SimpleNamespace(**{**vars(subprocess), "run": lambda command, **_kwargs: revealed.append(command)}),
    )

    assert (
        cli.main(
            [
                "--export-episode-debug",
                "--playback-video",
                str(video),
                "--config",
                str(api.config.config_path),
            ]
        )
        == 0
    )

    [snapshot] = list(logs.glob("pudge-debug-79001-ep3-*.json"))
    assert json.loads(snapshot.read_text(encoding="utf-8"))
    assert revealed == [["open", "-R", str(snapshot)]]
    assert f"PUDGE_EPISODE_DEBUG={snapshot}" in capsys.readouterr().out
