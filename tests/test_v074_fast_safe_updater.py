"""In-app updates keep the runtime, build the native app before replacing it, and roll back
what they changed when they fail (behaviour; audit 71Б rewrite).

install.sh and the updater's generated script run with stand-ins for the macOS tools
(tests/t71g01_support.py), on Linux and macOS."""

from __future__ import annotations

from pathlib import Path

from t71g01_support import MacSim, launch_update, release_history, tree, wheel_files

UPDATE_ENV = ("TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH", "PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE")


def test_in_app_update_keeps_the_runtime_and_restores_the_previous_package_when_it_fails() -> None:
    history = release_history()
    sim, steps = history.sim, history.steps
    update = steps["update"].result
    assert update.returncode == 0, update.output
    assert "Fast update: preserving the existing runtime environment." in update.output
    # The venv stays: not recreated, pip not upgraded; MangaOCR (Pudge's removed engine) is
    # neither looked for nor installed.
    assert not [call for call in update.calls("python3.12") if call[:2] == ["-m", "venv"]]
    assert ["install", "--upgrade", "pip"] not in update.calls("pip")
    assert "MangaOCR" not in update.output
    assert not [call for call in update.calls("pip") if any("manga-ocr" in arg for arg in call)]
    assert steps["update"].runtime_after == wheel_files(
        history.projects["9.8.7-patched"] / "pudge-9.8.7-py3-none-any.whl"
    )
    # A full install recreates the venv without MangaOCR.
    full = steps["after_swap_fails"].result
    assert ["-m", "venv", str(sim.venv)] in full.calls("python3.12")
    assert "MangaOCR" not in full.output
    assert not [call for call in full.calls("pip") if any("manga-ocr" in arg for arg in call)]
    # An update that fails after replacing the package puts the previous one back.
    for name in ("stale", "build_fails"):
        failed = steps[name]
        assert failed.result.returncode != 0
        assert "Update failed; restoring the previous Pudge package..." in failed.result.output
        assert "pudge-9.8.6.dist-info/METADATA" in failed.runtime_before
        assert failed.runtime_after == failed.runtime_before
    assert not (sim.data / "update-package-backup").exists()


def test_an_install_that_fails_after_the_app_swap_restores_the_previous_app() -> None:
    history = release_history()
    failed = history.steps["after_swap_fails"]
    assert failed.result.returncode != 0
    assert "Install failed; restoring the previous app bundle..." in failed.result.output
    assert failed.app_before and failed.app_after == failed.app_before
    assert not list((history.sim.data / "app-build").glob("*.before-swap"))


def test_native_app_runs_the_managed_pudge_without_a_frozen_copy() -> None:
    history = release_history()
    sim, install = history.sim, history.steps["install"]
    # The bundle holds a launcher, its Info.plist and the icon: no Python, no copy of Pudge.
    assert sorted(install.app_after) == [
        "Contents/Info.plist",
        "Contents/MacOS/pudge",
        "Contents/Resources/AppIcon.icns",
    ]
    assert len(install.result.calls("clang")) == 1
    # The launcher install.sh generated starts pudge.app_entry with the venv's Python in its own process.
    launcher = sim.launcher()
    assert f'const char *python = "{sim.venv}/bin/python";' in launcher
    assert 'PyConfig_SetString(&config, &config.run_module, L"pudge.app_entry")' in launcher
    assert "return Py_RunMain();" in launcher
    assert "execv" not in launcher and "PyInstaller" not in launcher


def test_app_bundle_is_built_before_the_working_bundle_is_replaced() -> None:
    history = release_history()
    sim, steps = history.sim, history.steps
    install = steps["install"].result
    # Package installed -> launcher compiled -> new bundle signed in place.
    assert (
        install.index("pip", "install", "--no-cache-dir") < install.index("clang") < install.index("codesign")
    )
    assert install.calls("codesign") == [["--force", "--deep", "--sign", "-", str(sim.app)]]
    # A running Pudge is asked to quit before anything is replaced.
    update = steps["update"].result
    assert update.index("appkit", "terminate", "com.pudge.app") < update.index("pip")
    # The launcher cannot be built: the working app is untouched.
    failed = steps["build_fails"]
    assert failed.result.calls("clang") and not failed.result.calls("codesign")
    assert failed.app_after == failed.app_before
    # The launcher clears the Tcl/Python variables a GUI parent may pass before Python starts.
    launcher = sim.launcher()
    start = launcher.index("Py_InitializeFromConfig(&config)")
    for name in UPDATE_ENV:
        assert -1 < launcher.index(f'unsetenv("{name}");') < start


# The installer of a downloaded release, run by the updater's script: records how it was
# started (zsh? arguments? which of the GUI's variables it still sees).
RECORDING_INSTALLER = (
    "line=\"installer$(printf '\\t')zsh=${ZSH_VERSION:+yes}$(printf '\\t')$*\"\n"
    f"for name in {' '.join(UPDATE_ENV)}; do line=\"$line$(printf '\\t')$name=${{(P)name-unset}}\"; done\n"
    'printf \'%s\\n\' "$line" >> "$MACSIM_LOG"\n'
)
BREAKING_INSTALLER = (
    'rm -rf "$HOME/Applications/pudge.app"\nmkdir -p "$HOME/Applications/pudge.app/Contents"\n'
    'echo half-installed > "$HOME/Applications/pudge.app/Contents/broken"\nexit 1\n'
)


def _update_script_run(tmp_path: Path, monkeypatch, installer: str):
    sim = MacSim(tmp_path / "mac")
    (sim.app / "Contents/MacOS").mkdir(parents=True)
    (sim.app / "Contents/MacOS/pudge").write_text("working app\n", encoding="utf-8")
    project = sim.base / "updates" / "v9.9.9-download" / "pudge"
    project.mkdir(parents=True)
    # An extracted release ZIP: install.sh may have lost its executable bit.
    (project / "install.sh").write_text(installer, encoding="utf-8")
    (project / "install.sh").chmod(0o644)
    command = launch_update(sim, project, monkeypatch)
    result = sim.run(command, cwd=sim.base, env={name: "/from/the/app" for name in UPDATE_ENV})
    return sim, project, result


def test_updater_runs_the_fast_installer_with_zsh_and_a_clean_environment(
    tmp_path: Path, monkeypatch
) -> None:
    sim, project, result = _update_script_run(tmp_path, monkeypatch, RECORDING_INSTALLER)
    assert result.returncode == 0, (sim.base / "update.log").read_text(encoding="utf-8")
    assert result.calls("installer") == [["zsh=yes", "--update", *(f"{name}=unset" for name in UPDATE_ENV)]]
    assert result.calls("open") == [["-n", str(sim.app)]]
    assert not project.parent.exists()  # the downloaded release is removed afterwards


def test_failed_update_restores_and_reopens_the_previous_app(tmp_path: Path, monkeypatch) -> None:
    sim, _project, result = _update_script_run(tmp_path, monkeypatch, BREAKING_INSTALLER)
    assert result.returncode == 1
    assert tree(sim.app) == {"Contents/MacOS/pudge": b"working app\n"}
    assert result.calls("open") == [["-n", str(sim.app)]]
