from pathlib import Path

import pytest

from pudge.player import build_mpv_command, run_mpv


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_build_mpv_command_adds_ipc_socket_before_video_separator(tmp_path: Path, monkeypatch, platform):
    monkeypatch.setattr("pudge.player.sys.platform", platform)
    monkeypatch.setattr("pudge.player.mpv_supports_option", lambda *_args: True)
    socket_path = tmp_path / "mpv.sock"
    command = build_mpv_command(
        "mpv",
        Path("episode.mkv"),
        Path("episode.srt"),
        None,
        ["--pause"],
        ipc_socket=socket_path,
    )

    assert command == [
        "mpv",
        "--pause",
        *(["--macos-app-activation-policy=accessory"] if platform == "darwin" else []),
        "--idle=no",
        "--keep-open=no",
        "--sub-fix-timing=no",
        "--secondary-sid=no",
        "--secondary-sub-visibility=no",
        f"--input-ipc-server={socket_path}",
        "--sub-file=episode.srt",
        "--",
        "episode.mkv",
    ]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_build_mpv_command_adds_native_anilist_script(tmp_path: Path, monkeypatch, platform):
    monkeypatch.setattr("pudge.player.sys.platform", platform)
    monkeypatch.setattr("pudge.player.mpv_supports_option", lambda *_args: True)
    script = tmp_path / "pudge_anilist.lua"
    command = build_mpv_command(
        "mpv",
        Path("episode.mkv"),
        None,
        3,
        [],
        script=script,
    )

    assert command == [
        "mpv",
        *(["--macos-app-activation-policy=accessory"] if platform == "darwin" else []),
        "--idle=no",
        "--keep-open=no",
        "--sub-fix-timing=no",
        "--secondary-sid=no",
        "--secondary-sub-visibility=no",
        f"--script={script}",
        "--sid=3",
        "--",
        "episode.mkv",
    ]


def test_build_mpv_command_keeps_tracker_with_an_explicit_plugin(tmp_path: Path):
    tracker = tmp_path / "pudge_anilist.lua"
    plugin = tmp_path / "jpdb" / "main.lua"
    command = build_mpv_command(
        "mpv",
        Path("episode.mkv"),
        None,
        None,
        ["--load-scripts=no", f"--script={plugin}"],
        script=tracker,
    )

    assert f"--script={plugin}" in command
    assert f"--script={tracker}" in command


def test_build_mpv_command_respects_explicit_sub_fix_timing():
    command = build_mpv_command(
        "mpv",
        Path("episode.mkv"),
        Path("episode.srt"),
        None,
        ["--sub-fix-timing=yes"],
    )

    assert command.count("--sub-fix-timing=yes") == 1
    assert "--sub-fix-timing=no" not in command



def test_build_mpv_command_respects_explicit_secondary_subtitle_options():
    command = build_mpv_command(
        "mpv",
        Path("episode.mkv"),
        Path("episode.srt"),
        None,
        ["--secondary-sid=2", "--secondary-sub-visibility=yes"],
    )

    assert command.count("--secondary-sid=2") == 1
    assert "--secondary-sid=no" not in command
    assert command.count("--secondary-sub-visibility=yes") == 1
    assert "--secondary-sub-visibility=no" not in command

def test_run_mpv_focus_uses_direct_process_and_waits(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4321

        def poll(self):
            return 0

        def wait(self, timeout=None):
            calls.append(["wait", str(self.pid)])
            return 0

    # The PID is invented: never inspect or signal the host's process tree.
    monkeypatch.setattr("pudge.process_cleanup.process_snapshot", lambda: {})
    monkeypatch.setattr("pudge.player.subprocess.Popen", lambda command, env=None: calls.append(command) or FakeProcess())
    monkeypatch.setattr("pudge.player._focus_mpv_process", lambda pid: calls.append(["focus", str(pid)]))

    result = run_mpv(["mpv", "--fs", "--", "episode.mkv"], focus=True)

    assert result == 0
    assert calls == [["mpv", "--fs", "--", "episode.mkv"], ["focus", "4321"], ["wait", "4321"]]
