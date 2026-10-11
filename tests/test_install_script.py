"""install.sh: what it does on a Mac (behaviour; audit 71Б rewrite).

The installer runs end to end under zsh with stand-ins for macOS (tests/t71g00_support.py):
Homebrew, the managed venv and pip, AppKit's running apps, clang, launchctl, codesign ... are
recorded, while the installer's own Python steps (session guard, brand migration, config,
launcher source, install records) run for real against a HOME in tmp_path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from desktop_isolation import _REAL_POPEN
from t71g00_support import (
    FAKE_REVISION,
    TOOLS,
    build_launcher,
    build_wheel,
    clone_install,
    make_project,
    run_install,
    set_version,
)


@pytest.mark.parametrize("shell", ["bash", "sh", "zsh"])
def test_installer_selects_zsh_before_shell_specific_syntax(tmp_path, shell) -> None:
    if shutil.which(shell) is None or not Path("/bin/zsh").is_file():
        pytest.skip("zsh installer entrypoint requires zsh")
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project with spaces"
    (project / "pudge").mkdir(parents=True)
    shutil.copy2(root / "install.sh", project / "install.sh")
    shutil.copy2(root / "pudge/brand.env", project / "pudge/brand.env")
    result = subprocess.run(
        [shell, str(project / "install.sh"), "--update", "--invalid"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2
    assert "Unknown installer argument: --invalid" in result.stderr
    assert "unbound variable" not in result.stderr


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    """A first install of release 9.8.7 on a Mac that still has the old "Anime MPV" brand."""
    work = tmp_path_factory.mktemp("fresh")
    home = work / "home"
    for folder in (
        "Applications/Anime MPV.app/Contents",
        "Applications/Anime MPV Settings.app",
        "Movies/Anime MPV",
        ".config/anime-mpv",
        "Library/LaunchAgents",
    ):
        (home / folder).mkdir(parents=True)
    (home / "Movies/Anime MPV/episode.mkv").write_bytes(b"video")
    (home / ".config/anime-mpv/config.toml").write_text(
        f'[library]\nroot_dir = "{home / "Movies/Anime MPV"}"\n[qbittorrent]\ncategory = "anime-mpv"\n',
        encoding="utf-8",
    )
    (home / "Library/LaunchAgents/com.anime-mpv.agent.plist").write_text("<plist/>", encoding="utf-8")
    result = run_install(work)
    assert result.returncode == 0, result.output
    return result


def _existing(fresh, tmp_path: Path, *, version: str = "9.8.8") -> Path:
    """A copy of the fresh installation, with release `version` unpacked next to install.sh."""
    work = clone_install(fresh.home.parent, tmp_path / "install")
    set_version(work / "project", version)
    build_wheel(work / "project", version)
    return work


@pytest.fixture(scope="module")
def updated(fresh, tmp_path_factory):
    """`install.sh --update` to 9.8.8 while Pudge (and an old-brand copy) run, aria2 is switched
    off, the old package has a file the new one does not, and the session marker is left over
    from an earlier Pudge whose PID now belongs to another process."""
    work = _existing(fresh, tmp_path_factory.mktemp("updated"))
    site = next((work / "home/.local/share/pudge/venv/lib").glob("python*/site-packages"))
    (site / "pudge" / "stale_module.py").write_text("OLD = True\n", encoding="utf-8")
    config = work / "home/.config/pudge/config.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(re.sub(r"(\[aria2\]\nenabled = )true", r"\1false", text), encoding="utf-8")
    marker = work / "home/.local/share/pudge/app-session.json"
    marker.write_text(json.dumps({"pid": os.getpid(), "started_at": 1.0}), encoding="utf-8")
    app_info = work / "home/Applications/pudge.app/Contents/Info.plist"
    before = {
        "marker": marker.read_text(encoding="utf-8"),
        "info": app_info.read_bytes(),
        "info_inode": app_info.stat().st_ino,
    }
    result = run_install(
        work, update=True, running_apps=[{"bundle_id": "com.pudge.app"}, {"bundle_id": "com.anime-mpv.app"}]
    )
    assert result.returncode == 0, result.output
    return result, before


def test_install_builds_a_native_shell_around_the_managed_venv(fresh) -> None:
    # The app is a small native launcher: no frozen copy of Python or Pudge inside (the old
    # PyInstaller bundle), so an update of the venv is what the app runs next time.
    app_files = sorted(
        path.relative_to(fresh.app).as_posix() for path in fresh.app.rglob("*") if path.is_file()
    )
    assert app_files == ["Contents/Info.plist", "Contents/MacOS/pudge", "Contents/Resources/AppIcon.icns"]
    info = fresh.plist(fresh.app / "Contents/Info.plist")
    assert (info["CFBundleExecutable"], info["CFBundleIdentifier"], info["CFBundleShortVersionString"]) == (
        "pudge",
        "com.pudge.app",
        "9.8.7",
    )
    [clang] = fresh.calls_of("clang")
    assert str(fresh.launcher_source) in clang
    assert clang[clang.index("-o") + 1].endswith("/pudge.app/Contents/MacOS/pudge")
    assert {clang[index + 1] for index, arg in enumerate(clang) if arg == "-framework"} == {
        "AppKit",
        "Foundation",
        "UserNotifications",
    }
    # Python is embedded with the flags of the venv's own python-config.
    assert fresh.calls_of("python-config") == [["--embed", "--cflags"], ["--embed", "--ldflags"]]
    assert "-I/fake/python/include" in clang and "-lpython-embed" in clang
    assert ["--force", "--deep", "--sign", "-", str(fresh.app)] in fresh.calls_of("codesign")
    assert (fresh.data_dir / "installed-version.txt").read_text(encoding="utf-8") == "9.8.7\n"


def test_agent_gets_the_tool_paths_and_the_apps_notification_helper(fresh) -> None:
    agent = fresh.plist(fresh.agent_plist)
    assert agent["Label"] == "com.pudge.agent"
    assert agent["ProgramArguments"] == [
        str(fresh.venv / "bin/python"),
        "-m",
        "pudge.agent",
        "--scheduled",
        "--config",
        str(fresh.config),
    ]
    env = agent["EnvironmentVariables"]
    # Finder-started processes have no Homebrew PATH: 7-Zip and aria2 come as absolute paths.
    assert env["PUDGE_7ZIP"] == fresh.prefix("sevenzip/bin/7zz")
    assert env["PUDGE_ARIA2C"] == fresh.prefix("aria2/bin/aria2c")
    assert env["PUDGE_NOTIFICATION_HELPER"] == str(fresh.app / "Contents/MacOS/pudge")
    assert ["bootout", "gui/" + str(os.getuid()), str(fresh.agent_plist)] in fresh.calls_of("launchctl")


LAUNCHER_ENTRY = r"""
import json, os, sys
with open(os.environ["FAKE_TRACE"], "a", encoding="utf-8") as trace:
    trace.write("python " + json.dumps({
        "exe": os.readlink("/proc/self/exe"), "argv": sys.argv[1:], "prefix": sys.prefix,
        "env": {key: value for key, value in os.environ.items() if key.startswith(("PUDGE_", "PYTHONPATH", "PYTHONHOME", "TCL", "TK_"))},
    }) + "\n")
"""


def _launch(launcher: Path, trace: Path, *args: str, **env: str) -> tuple[int, list[str]]:
    trace.write_text("", encoding="utf-8")
    environment = {"PATH": "/usr/bin:/bin", "FAKE_TRACE": str(trace), **env}
    # desktop_isolation turns notification launches into no-ops; this is the stand-in launcher.
    process = _REAL_POPEN(
        [str(launcher), *args], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return process.wait(timeout=60), trace.read_text(encoding="utf-8").splitlines()


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/clang").exists(), reason="the macOS SDK is only on a Mac"
)
def test_launcher_builds_with_the_macos_sdk(fresh, tmp_path: Path) -> None:
    """On a Mac the installer's own clang command builds the launcher against the real SDK
    (the launcher runs with stand-in frameworks only on Linux, in the tests above and below)."""
    import sysconfig

    config = (
        Path(str(sysconfig.get_config_var("BINDIR"))) / f"python{sysconfig.get_config_var('VERSION')}-config"
    )
    if not config.is_file():
        pytest.skip(f"no {config.name} for this Python")
    [clang] = fresh.calls_of("clang")
    out = clang.index("-o")
    # The stand-in python-config's flags are replaced by this Python's real ones.
    args = [arg for arg in clang[:out] + clang[out + 2 :] if "/fake/" not in arg and arg != "-lpython-embed"]
    real = []
    for kind in ("--cflags", "--ldflags"):
        flags = _REAL_POPEN([str(config), "--embed", kind], stdout=subprocess.PIPE, text=True)
        real += (flags.communicate(timeout=30)[0] or "").split()
    built = _REAL_POPEN(
        ["/usr/bin/clang", *args, *real, "-o", str(tmp_path / "pudge")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = built.communicate(timeout=180)[0]
    assert built.returncode == 0, output
    assert (tmp_path / "pudge").stat().st_size > 0


def test_launcher_runs_pudge_in_its_own_process_after_its_startup_pool(fresh, tmp_path: Path) -> None:
    launcher = build_launcher(fresh.launcher_source, tmp_path)
    (fresh.site_packages() / "pudge" / "app_entry.py").write_text(LAUNCHER_ENTRY, encoding="utf-8")
    status, trace = _launch(
        launcher,
        tmp_path / "trace",
        "--open",
        "episode.mkv",
        PYTHONPATH=str(tmp_path),
        PYTHONHOME="/nowhere",
        TCL_LIBRARY="/old/tcl",
        FAKE_BUNDLE_RESOURCES=str(fresh.app / "Contents/Resources"),
    )
    assert status == 0
    # The pool for the icon lookup is drained before Python starts; nothing is drained after
    # Py_RunMain finalized Python (that crashed in PyObjC callbacks on window close).
    assert trace[:2] == ["pool-open python=0", "pool-drain python=0"] and len(trace) == 3
    report = json.loads(trace[2].removeprefix("python "))
    # pudge.app_entry runs inside the launcher itself, not an exec'ed Python (Force Quit showed
    # Python's rocket icon), from the managed venv, with the tool paths the installer found.
    assert report["exe"] == str(launcher.resolve())
    assert report["argv"] == ["--open", "episode.mkv"]
    assert Path(report["prefix"]) == fresh.venv
    assert report["env"] == {
        "PUDGE_PYTHON": str(fresh.venv / "bin/python"),
        "PUDGE_CONFIG": str(fresh.config),
        "PUDGE_7ZIP": fresh.prefix("sevenzip/bin/7zz"),
        "PUDGE_ARIA2C": fresh.prefix("aria2/bin/aria2c"),
        "PUDGE_APP_ICON": str(fresh.app / "Contents/Resources/AppIcon.icns"),
    }


def test_launcher_sends_notifications_under_pudges_identity(fresh, tmp_path: Path) -> None:
    launcher = build_launcher(fresh.launcher_source, tmp_path)
    trace = tmp_path / "trace"
    # The agent posts through the app's own executable, so macOS shows Pudge's name and icon.
    status, lines = _launch(launcher, trace, "--pudge-native-notification", "Episode ready", "Frieren 12")
    assert status == 0
    assert "activation-policy 2" in lines  # no Dock icon for the helper
    assert "notification title=pudge subtitle=Episode ready body=Frieren 12 sound=1 delegate=1" in lines
    assert not [line for line in lines if line.startswith("python ")]
    status, lines = _launch(
        launcher,
        trace,
        "--pudge-native-notification",
        "Episode ready",
        "Frieren 12",
        FAKE_NOTIFICATION_STATUS="1",
    )
    assert status == 1 and not [line for line in lines if line.startswith("notification")]
    assert _launch(launcher, trace, "--pudge-native-notification", "only a title")[0] == 2


def test_update_quits_running_pudge_like_cmd_q_and_keeps_its_session_marker(updated) -> None:
    result, before = updated
    names = result.names()
    # AppKit's terminate (the Cmd+Q path, so Pudge saves and cleans up), for the old brand too,
    # before the runtime is touched; never a kill.
    assert result.calls_of("terminate") == [["com.pudge.app"], ["com.anime-mpv.app"]]
    first_pip = next(
        index for index, call in enumerate(result.calls) if call[:3] == ["venv-python", "-m", "pip"]
    )
    assert names.index("terminate") < first_pip
    assert result.calls_of("pkill") == [] and result.calls_of("killall") == [["Dock"]]
    # The marker of a PID that now belongs to a younger process is not Pudge's: the update goes
    # on, and the marker is left for Pudge's crash detection.
    assert (result.data_dir / "app-session.json").read_text(encoding="utf-8") == before["marker"]


def test_update_stops_without_changes_when_pudge_does_not_quit(fresh, tmp_path: Path) -> None:
    work = _existing(fresh, tmp_path)
    info = (work / "home/Applications/pudge.app/Contents/Info.plist").read_bytes()
    result = run_install(work, update=True, running_apps=[{"bundle_id": "com.pudge.app", "quits": False}])
    assert result.returncode == 1
    assert (
        "Pudge is still shutting down. Quit it with Cmd+Q and rerun the installer." in result.completed.stderr
    )
    assert result.calls[-1] == ["terminate", "com.pudge.app"]  # nothing runs after the quit request
    assert (
        (result.site_packages() / "pudge/__init__.py")
        .read_text(encoding="utf-8")
        .startswith("__version__ = '9.8.7'")
    )
    assert (result.app / "Contents/Info.plist").read_bytes() == info


def test_open_pudge_window_blocks_the_update_and_keeps_its_marker(fresh, tmp_path: Path) -> None:
    # A window started from source has no bundle id to quit; its live session marker stops the update.
    work = _existing(fresh, tmp_path)
    marker = work / "home/.local/share/pudge/app-session.json"
    marker.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    result = run_install(work, update=True)
    assert result.returncode == 1
    assert (
        "A Pudge session is still active. Quit its window with Cmd+Q before installing."
        in result.completed.stderr
    )
    assert marker.read_text(encoding="utf-8") == json.dumps({"pid": os.getpid()})
    # The guard runs before the old package is backed up or anything is installed.
    assert result.calls[-1] == ["python3.12", "-", str(marker)]
    assert not (result.data_dir / "update-package-backup").exists()
    assert (result.data_dir / "installed-version.txt").read_text(encoding="utf-8") == "9.8.7\n"


def test_failed_preflight_stops_before_migration_quit_or_runtime_changes(fresh, tmp_path: Path) -> None:
    # Update: nothing after the check runs (no brand migration, no quitting the running app).
    result = run_install(
        _existing(fresh, tmp_path / "update"),
        update=True,
        checks_status=1,
        running_apps=[{"bundle_id": "com.pudge.app"}],
    )
    assert result.returncode == 1
    assert result.calls[-1][0] == "python3.12" and result.calls[-1][1].endswith("/pudge/install_checks.py")
    # First install: no folders are created and the old brand's config is not moved.
    work = tmp_path / "first"
    (work / "home/.config/anime-mpv").mkdir(parents=True)
    (work / "home/.config/anime-mpv/config.toml").write_text("", encoding="utf-8")
    result = run_install(work, checks_status=1)
    assert result.returncode == 1
    assert result.calls[-1][1].endswith("/pudge/install_checks.py")
    assert not result.data_dir.exists() and (work / "home/.config/anime-mpv/config.toml").exists()


def test_update_replaces_the_installed_package_with_the_bundled_wheel(updated) -> None:
    result, _before = updated
    site = result.site_packages()
    wheel = str(result.project / "pudge-9.8.8-py3-none-any.whl")
    assert ["venv-python", "-m", "pip", "install", "--no-cache-dir", "--no-deps", wheel] in result.calls
    # The old package folder is removed first: no file of 9.8.7 survives a same-name install.
    assert not (site / "pudge/stale_module.py").exists()
    assert (site / "pudge/__init__.py").read_text(encoding="utf-8").startswith("__version__ = '9.8.8'")
    assert sorted(path.name for path in site.glob("pudge-*.dist-info")) == ["pudge-9.8.8.dist-info"]
    assert (result.data_dir / "installed-version.txt").read_text(encoding="utf-8") == "9.8.8\n"
    assert not (result.data_dir / "update-package-backup").exists()
    assert "Fast update: preserving the existing runtime environment." in result.completed.stdout


def test_fast_update_keeps_the_native_app_and_its_folder_grants(updated) -> None:
    # macOS ties folder permissions to the app's code identity: an ordinary update (even to a
    # new version) leaves the signed launcher alone.
    result, before = updated
    assert result.calls_of("clang") == [] and result.calls_of("codesign") == []
    assert (
        "Fast update: preserving native app identity and existing macOS folder grants."
        in result.completed.stdout
    )
    info = result.app / "Contents/Info.plist"
    assert info.read_bytes() == before["info"] and info.stat().st_ino == before["info_inode"]
    assert (result.data_dir / "native-shell-rev").read_text(encoding="utf-8") == "2\n"


@pytest.mark.parametrize("change", ["forced", "older shell"])
def test_native_app_is_rebuilt_when_forced_or_its_shell_changed(fresh, tmp_path: Path, change: str) -> None:
    work = _existing(fresh, tmp_path)
    env = {}
    if change == "forced":
        env["PUDGE_FORCE_NATIVE_REBUILD"] = "1"
    else:
        (work / "home/.local/share/pudge/native-shell-rev").write_text("1\n", encoding="utf-8")
    result = run_install(work, update=True, env=env)
    assert result.returncode == 0, result.output
    assert len(result.calls_of("clang")) == 1
    assert result.plist(result.app / "Contents/Info.plist")["CFBundleShortVersionString"] == "9.8.8"
    assert (result.data_dir / "native-shell-rev").read_text(encoding="utf-8") == "2\n"


def test_update_keeps_the_torrent_backend_choice_and_refreshes_tool_paths(updated) -> None:
    result, _before = updated
    config = tomllib.loads(result.config.read_text(encoding="utf-8"))
    assert config["aria2"]["enabled"] is False
    assert config["aria2"]["binary"] == result.prefix("aria2/bin/aria2c")
    assert {key: config["tools"][key] for key in ("mpv", "ffmpeg", "ffprobe", "alass")} == {
        "mpv": result.prefix("mpv/bin/mpv"),
        "ffmpeg": result.prefix("ffmpeg/bin/ffmpeg"),
        "ffprobe": result.prefix("ffmpeg/bin/ffprobe"),
        "alass": result.prefix("alass/bin/alass-cli"),
    }


def test_development_checkout_installs_a_wheel_of_the_current_tree(tmp_path: Path) -> None:
    project = make_project(tmp_path, version="9.9.0", development=True)
    # A wheel left from an older release and the project's own build/ folder.
    build_wheel(project, "0.0.1")
    (project / "build/lib").mkdir(parents=True)
    (project / "build/lib/marker.py").write_bytes(b"old build output\n")
    result = run_install(tmp_path, project=project)
    assert result.returncode == 0, result.output
    [wheel_call] = [call for call in result.calls if call[:4] == ["venv-python", "-m", "pip", "wheel"]]
    assert wheel_call[4:8] == [str(project), "--no-deps", "--no-build-isolation", "-w"]
    site = result.site_packages()
    assert sorted(path.name for path in site.glob("pudge-*.dist-info")) == ["pudge-9.9.0.dist-info"]
    # The build is fingerprinted as this checkout, and stamped when installed.
    build_info = json.loads((site / "pudge/build-info.json").read_text(encoding="utf-8"))
    assert build_info["id"] == f"9.9.0-{FAKE_REVISION[:12]}" and build_info["installed_at"]
    # build/ is put back as it was, and the temporary wheel folder is gone.
    assert (project / "build/lib/marker.py").read_bytes() == b"old build output\n"
    assert not Path(wheel_call[8]).exists()
    assert json.loads((result.data_dir / "install-source.json").read_text(encoding="utf-8")) == {
        "channel": "development",
        "source_path": str(project),
        "source_revision": FAKE_REVISION,
    }


def test_release_folder_installs_its_wheel_and_records_the_release_channel(fresh) -> None:
    assert json.loads((fresh.data_dir / "install-source.json").read_text(encoding="utf-8")) == {
        "channel": "release",
        "source_path": "",
        "source_revision": "",
    }
    assert [
        "venv-python",
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--no-deps",
        str(fresh.project / "pudge-9.8.7-py3-none-any.whl"),
    ] in fresh.calls


def test_missing_7zip_stops_the_install_with_a_clear_error(tmp_path: Path) -> None:
    result = run_install(tmp_path, tools=tuple(tool for tool in TOOLS if not tool.startswith("sevenzip/")))
    assert result.returncode == 1
    assert "Installer error: sevenzip was installed but 7zz/7z was not found." in result.completed.stderr
    assert not result.data_dir.exists()


def test_brand_env_names_everything_the_installer_creates(tmp_path: Path) -> None:
    project = make_project(
        tmp_path,
        brand={
            "APP_NAME": "Kappa",
            "APP_SLUG": "kappa",
            "APP_BUNDLE_ID": "com.kappa.app",
            "APP_CLI": "kappa",
            "APP_AGENT_CLI": "kappa-agent",
        },
    )
    result = run_install(tmp_path, project=project)
    assert result.returncode == 0, result.output
    home = result.home
    info = result.plist(home / "Applications/Kappa.app/Contents/Info.plist")
    assert (info["CFBundleName"], info["CFBundleExecutable"], info["CFBundleIdentifier"]) == (
        "Kappa",
        "Kappa",
        "com.kappa.app",
    )
    assert (home / "Applications/Kappa.app/Contents/MacOS/Kappa").is_file()
    assert not (home / "Applications/pudge.app").exists()
    agent = result.plist(home / "Library/LaunchAgents/com.kappa.agent.plist")
    assert agent["Label"] == "com.kappa.agent"
    assert agent["ProgramArguments"][0] == str(home / ".local/share/kappa/venv/bin/python")
    assert os.readlink(home / ".local/bin/kappa") == str(home / ".local/share/kappa/venv/bin/pudge")
    assert os.readlink(home / ".local/bin/kappa-agent") == str(
        home / ".local/share/kappa/venv/bin/pudge-agent"
    )
    assert (home / ".local/share/kappa/installed-version.txt").is_file()


def test_install_migrates_and_retires_the_old_brand(fresh) -> None:
    home = fresh.home
    # The old app is removed and unregistered, not replaced by a compatibility link.
    for old in ("Anime MPV.app", "Anime MPV Settings.app"):
        assert not (home / "Applications" / old).exists() and not (home / "Applications" / old).is_symlink()
    assert ["-u", str(home / "Applications/Anime MPV.app")] in fresh.calls_of("lsregister")
    old_agent = home / "Library/LaunchAgents/com.anime-mpv.agent.plist"
    assert ["bootout", f"gui/{os.getuid()}", str(old_agent)] in fresh.calls_of(
        "launchctl"
    ) and not old_agent.exists()
    # Default folders move to the new brand before the config is read; the config follows them.
    assert (home / "Movies/pudge/episode.mkv").is_file() and not (home / "Movies/Anime MPV").exists()
    config = tomllib.loads(fresh.config.read_text(encoding="utf-8"))
    assert config["library"]["root_dir"] == str(home / "Movies/pudge")
    assert config["qbittorrent"]["category"] == "pudge"
    # Old command names keep working; Dock pins move to the new app, then the Dock restarts.
    assert os.readlink(home / ".local/bin/anime-mpv") == str(fresh.venv / "bin/pudge")
    assert os.readlink(home / ".local/bin/anime-mpv-agent") == str(fresh.venv / "bin/pudge-agent")
    aliases = [
        index
        for index, call in enumerate(fresh.calls)
        if call[:2] == ["venv-python", str(fresh.project / "pudge/mac_app_aliases.py")]
    ]
    assert len(aliases) == 1 and fresh.calls[aliases[0]][2:] == [str(fresh.app), "com.pudge.app", "Anime MPV"]
    assert fresh.calls.index(["killall", "Dock"]) > aliases[0]
