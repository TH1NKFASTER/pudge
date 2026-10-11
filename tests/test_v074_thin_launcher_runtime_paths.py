"""The native launcher install.sh builds: Python embedded in the Pudge process, installed tool
paths, notifications without Python, the bundle version of the installed release
(behaviour; audit 71Б rewrite: install.sh runs with stand-ins, tests/t71g01_support.py)."""

from __future__ import annotations

import re

from t71g01_support import launcher_main, plist, release_history


def test_native_launcher_embeds_the_managed_venv_python() -> None:
    history = release_history()
    sim, install = history.sim, history.steps["install"].result
    # clang links the launcher against libpython (python-config --embed), from the generated source.
    assert install.calls("python-config") == [["--embed", "--cflags"], ["--embed", "--ldflags"]]
    [clang] = install.calls("clang")
    assert clang[:2] == ["-fobjc-arc", "-fblocks"]
    assert f"-I{sim.venv}/include/python3.12" in clang and "-lpython3.12" in clang
    assert clang[clang.index("-o") + 1] == str(sim.data / "app-build/dist/pudge.app/Contents/MacOS/pudge")
    assert clang.index(str(sim.data / "app-build/pudge-launcher.m")) < clang.index("-lpython3.12")
    main = launcher_main(sim)
    assert f'const char *python = "{sim.venv}/bin/python";' in main
    assert "PyConfig_SetBytesString(&config, &config.executable, python)" in main
    assert (
        main.index('L"pudge.app_entry"')
        < main.index("Py_InitializeFromConfig(&config)")
        < main.index("return Py_RunMain();")
    )
    assert not re.search(r"\bexec[lv]", main)
    # The tools Homebrew provides and the config are passed to Pudge.
    for name, value in (
        ("PUDGE_CONFIG", sim.config),
        ("PUDGE_7ZIP", sim.prefix / "sevenzip/bin/7zz"),
        ("PUDGE_ARIA2C", sim.prefix / "aria2/bin/aria2c"),
    ):
        assert f'setenv("{name}", "{value}", 1);' in main


def test_native_launcher_sends_notifications_without_starting_python() -> None:
    history = release_history()
    sim = history.sim
    main = launcher_main(sim)
    helper = main.index('strcmp(argv[1], "--pudge-native-notification") == 0')
    first_python_call = re.search(r"\bPy[A-Z_]\w*\(", main).start()
    assert helper < main.index("return send_notification(argv[2], argv[3]);") < first_python_call
    assert "UNNotificationPresentationOptionBanner" in sim.launcher()
    # The scheduled agent sends its notifications through that app executable.
    agent = plist(sim.agent_plist)
    assert agent["EnvironmentVariables"]["PUDGE_NOTIFICATION_HELPER"] == str(sim.app / "Contents/MacOS/pudge")
    assert agent["ProgramArguments"] == [
        str(sim.venv / "bin/python"),
        "-m",
        "pudge.agent",
        "--scheduled",
        "--config",
        str(sim.config),
    ]


def test_native_bundle_version_comes_from_the_installed_release() -> None:
    history = release_history()
    info = plist(history.sim.app / "Contents/Info.plist")
    assert (info["CFBundleShortVersionString"], info["CFBundleVersion"]) == ("9.8.6", "9.8.6")
    assert (info["CFBundleExecutable"], info["CFBundleIdentifier"]) == ("pudge", "com.pudge.app")
    assert history.steps["install"].records["installed-version.txt"] == "9.8.6\n"
