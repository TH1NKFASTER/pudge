from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from pudge.web_app import _MacWindowLifecycle


class _Window:
    def __init__(self) -> None:
        self.hide_calls = 0

    def hide(self) -> None:
        self.hide_calls += 1

    def show(self) -> None:
        pass


class _Logger:
    def info(self, *_args: object, **_kwargs: object) -> None:
        pass

    def warning(self, *_args: object, **_kwargs: object) -> None:
        pass


def test_red_close_hides_gui_while_detached_playback_can_continue() -> None:
    events: list[str] = []
    window = _Window()
    lifecycle = _MacWindowLifecycle(window, _Logger(), events.append)

    assert lifecycle.handle_closing() is False
    assert lifecycle.quit_requested is False
    assert window.hide_calls == 1
    assert events == []


def test_web_playback_cli_is_detached_from_gui_process_session(tmp_path: Path, monkeypatch) -> None:
    # The episode plays in its own session, so closing (or quitting) the window does not end it.
    from desktop_isolation import patch_webapp_popen
    from test_web_app import make_api

    api = make_api(tmp_path)
    video = (tmp_path / "Show - 01.mkv").resolve()
    video.write_bytes(b"video")
    launches: list[tuple[list[str], dict]] = []

    class Launcher:
        pid = 4321

        def poll(self):
            return None

    patch_webapp_popen(
        monkeypatch, lambda command, **kwargs: launches.append((list(command), kwargs)) or Launcher()
    )

    api.play(str(video))

    [(command, options)] = launches
    assert str(video) in command
    assert options.get("start_new_session") is True


def test_detached_cli_owns_foreground_marker_for_full_playback_lifetime(tmp_path: Path, monkeypatch) -> None:
    # While the CLI plays an episode, background work sees a foreground playback; the marker
    # goes away when playback ends, also when it fails.
    from pudge import cli
    from pudge.config import AppConfig, write_config
    from pudge.foreground import foreground_active

    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.llm.enabled = False
    config_path = tmp_path / "config.toml"
    write_config(config, config_path)
    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")
    monkeypatch.delenv("PUDGE_BACKGROUND_PREPARE", raising=False)
    seen: list[bool] = []

    def playing(_video, _args, _config, _llm) -> int:
        seen.append(foreground_active(config.paths.cache_dir))
        return 0

    monkeypatch.setattr(cli, "process_video", playing)
    assert cli.main([str(video), "--config", str(config_path)]) == 0
    assert seen == [True]
    assert foreground_active(config.paths.cache_dir) is False

    def crashing(_video, _args, _config, _llm) -> int:
        seen.append(foreground_active(config.paths.cache_dir))
        raise RuntimeError("mpv failed")

    monkeypatch.setattr(cli, "process_video", crashing)
    assert cli.main([str(video), "--config", str(config_path)]) == 1
    assert seen == [True, True]
    assert foreground_active(config.paths.cache_dir) is False


def test_mpv_run_lasts_until_the_player_exits_and_stops_its_helpers(monkeypatch) -> None:
    from pudge import playback_process, player

    events: list[str] = []

    class Mpv:
        pid = 777
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            events.append("wait")
            self.returncode = 3
            return 3

    class Tree:
        def __init__(self, pid):
            events.append(f"tree:{pid}")

        def capture(self):
            pass

        def stop(self, grace=0.3):
            events.append("stop-helpers")
            return []

    monkeypatch.setattr(
        player,
        "subprocess",
        SimpleNamespace(
            **{**vars(subprocess), "Popen": lambda command, env=None: events.append("spawn") or Mpv()}
        ),
    )
    monkeypatch.setattr(playback_process, "OwnedProcessTree", Tree)
    monkeypatch.delenv("PUDGE_MPV_START_ACK", raising=False)

    assert player.run_mpv(["mpv", "/media/Show - 01.mkv"]) == 3
    assert events == ["spawn", "tree:777", "wait", "stop-helpers"]
