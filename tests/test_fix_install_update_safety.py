"""install.sh keeps a working Pudge when an install or update fails or is cut short, puts the
agent where launchd does not load it at login, and leaves the Dock alone (review MAC-2, MAC-4,
MAC-6, MAC-7, MAC-8). install.sh runs with stand-ins for macOS (tests/t71g01_support.py)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from t71g01_support import APPKIT, MacSim, plist


def _installed(tmp_path: Path) -> MacSim:
    sim = MacSim(tmp_path / "mac")
    first = sim.install(sim.project("pudge-9.8.6", version="9.8.6"))
    assert first.returncode == 0, first.output
    return sim


def test_an_update_stopped_while_backing_up_keeps_the_installed_package(tmp_path: Path) -> None:
    # MAC-2: ditto fails after two entries of the backup copy (a full disk). The rollback used
    # to replace the untouched package with that partial copy: Pudge no longer started.
    sim = _installed(tmp_path)
    before = sim.runtime()
    result = sim.install(
        sim.project("pudge-9.8.7", version="9.8.7"), "--update", env={"MACSIM_DITTO_FAILS_AFTER": "2"}
    )
    assert result.returncode != 0
    assert "No space left on device" in result.output
    assert "restoring the previous Pudge package" not in result.output
    assert sim.runtime() == before
    assert not (sim.data / "update-package-backup").exists()
    # The next update (disk freed) works from the intact package.
    retry = sim.install(sim.project("pudge-9.8.7b", version="9.8.7"), "--update")
    assert retry.returncode == 0, retry.output
    assert "pudge-9.8.7.dist-info/METADATA" in sim.runtime()


def test_a_finished_backup_is_still_the_rollback_source(tmp_path: Path) -> None:
    # The rollback still runs once the backup is complete (here: the launcher build fails).
    sim = _installed(tmp_path)
    before = sim.runtime()
    result = sim.install(
        sim.project("pudge-9.8.7", version="9.8.7"),
        "--update",
        env={"PUDGE_FORCE_NATIVE_REBUILD": "1", "MACSIM_FAIL_CLANG": "1"},
    )
    assert result.returncode != 0
    assert "Update failed; restoring the previous Pudge package..." in result.output
    assert sim.runtime() == before


def test_a_full_install_that_cannot_download_keeps_the_working_runtime(tmp_path: Path) -> None:
    # MAC-6: ./install.sh without --update deleted the venv first; a failed download left the
    # app and the agent with an empty one.
    sim = _installed(tmp_path)
    (sim.venv / "marker").write_text("the working venv", encoding="utf-8")
    before = sim.runtime()
    result = sim.install(
        sim.project("pudge-9.8.7", version="9.8.7"), env={"MACSIM_FAIL_PIP_REQUIREMENTS": "1"}
    )
    assert result.returncode != 0
    assert "Install failed; restoring the previous runtime environment..." in result.output
    assert (sim.venv / "marker").read_text(encoding="utf-8") == "the working venv"
    assert sim.runtime() == before
    assert os.access(sim.venv / "bin/python", os.X_OK)
    assert not (sim.data / "venv-before-install").exists()
    # A full install that succeeds replaces the environment and leaves nothing behind.
    done = sim.install(sim.project("pudge-9.8.7b", version="9.8.7"))
    assert done.returncode == 0, done.output
    assert not (sim.venv / "marker").exists()
    assert "pudge-9.8.7.dist-info/METADATA" in sim.runtime()
    assert not list(sim.data.glob("venv-before-install*"))


def test_an_install_killed_with_the_runtime_set_aside_puts_it_back_first(tmp_path: Path) -> None:
    # Power loss during a full install: the working venv is still set aside and the new one is
    # half-built (no PyObjC yet). The next run starts from the working one, so its Cmd+Q step
    # (which imports AppKit from the venv) does not fail.
    sim = _installed(tmp_path)
    (sim.venv / "marker").write_text("the working venv", encoding="utf-8")
    aside = sim.data / "venv-before-install"
    sim.venv.rename(aside)
    shutil.copytree(aside, sim.venv, symlinks=True)
    (sim.venv / "lib/site/AppKit.py").unlink()
    (sim.venv / "marker").unlink()
    result = sim.install(sim.project("pudge-9.8.7", version="9.8.7"), "--update")
    assert result.returncode == 0, result.output
    assert "Restoring the runtime environment an interrupted install set aside..." in result.output
    assert (sim.venv / "marker").read_text(encoding="utf-8") == "the working venv"
    assert (sim.venv / "lib/site/AppKit.py").read_text(encoding="utf-8") == APPKIT
    assert not aside.exists()


def test_the_package_is_renamed_into_place_never_missing_while_pip_runs(tmp_path: Path) -> None:
    # MAC-7: the old package was removed and then pip installed the new one in place; power
    # loss in between left no package. It is installed into a staging root now.
    sim = _installed(tmp_path)
    leftover = sim.data / "update-package-stage/left-by-a-killed-update"
    leftover.mkdir(parents=True)
    result = sim.install(
        sim.project("pudge-9.8.7", version="9.8.7", files={"new_module.py": "NEW = 1\n"}), "--update"
    )
    assert result.returncode == 0, result.output
    assert result.calls("pip-saw-installed-package") == [["True"]]
    [install] = [call for call in result.calls("pip") if call[-1].endswith("pudge-9.8.7-py3-none-any.whl")]
    assert install[install.index("--root") + 1] == str(sim.data / "update-package-stage")
    runtime = sim.runtime()
    assert runtime["pudge/new_module.py"] == b"NEW = 1\n"
    assert sorted(name.split("/")[0] for name in runtime if name.endswith("METADATA")) == [
        "pudge-9.8.7.dist-info"
    ]
    assert not (sim.data / "update-package-stage").exists()
    assert not (sim.data / "update-package-previous").exists()


def test_install_leaves_the_dock_alone(tmp_path: Path) -> None:
    # MAC-8: killall Dock ran on every install and update; mac_app_aliases.py restarts the
    # Dock itself when it changed a pin.
    sim = _installed(tmp_path)
    update = sim.install(sim.project("pudge-9.8.7", version="9.8.7"), "--update")
    assert update.returncode == 0, update.output
    assert update.calls("killall") == []


def test_the_agent_plist_moves_out_of_launch_agents(tmp_path: Path) -> None:
    # MAC-4: a plist in ~/Library/LaunchAgents is loaded by launchd at every login, so the agent
    # ran all day after a reboot although it works only while Pudge is open. An existing install
    # is migrated: the old plist is booted out and removed.
    sim = _installed(tmp_path)
    assert sim.agent_plist == sim.data / "launchd/com.pudge.agent.plist"
    assert sim.agent_plist.is_file() and not sim.legacy_agent_plist.exists()
    sim.legacy_agent_plist.parent.mkdir(parents=True, exist_ok=True)
    sim.legacy_agent_plist.write_bytes(sim.agent_plist.read_bytes())
    update = sim.install(sim.project("pudge-9.8.7", version="9.8.7"), "--update")
    assert update.returncode == 0, update.output
    assert not sim.legacy_agent_plist.exists()
    bootouts = [call for call in update.calls("launchctl") if call[0] == "bootout"]
    assert ["bootout", f"gui/{os.getuid()}", str(sim.legacy_agent_plist)] in bootouts
    # Nothing is bootstrapped by the installer: the window does it when it opens.
    assert not [call for call in update.calls("launchctl") if call[0] == "bootstrap"]
    agent = plist(sim.agent_plist)
    assert agent["Label"] == "com.pudge.agent"
    assert agent["ProgramArguments"][:4] == [str(sim.venv / "bin/python"), "-m", "pudge.agent", "--scheduled"]
    assert f"Agent: {sim.agent_plist}" in update.output


BROKEN_CONFIG = '[ui]\nlanguage = "ru"\n\n[library]\nroot_dir = "/Volumes/Anime"\ndatabase_path = \n'


def test_an_update_leaves_a_broken_config_as_it_is_and_says_which_line(tmp_path: Path) -> None:
    # The installer's config step (tool paths) ran write_config, which refuses to write the
    # defaults over a config.toml it cannot read: the update failed and rolled back. The
    # legacy-name rewrite before it stops an update on such a file before the runtime is
    # touched (test_fix_review_storage); here it is left out, so the config step sees it.
    from t71g00_support import run_install

    first = run_install(tmp_path)
    assert first.returncode == 0, first.output
    (first.project / "pudge/legacy_install.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    first.config.write_text(BROKEN_CONFIG, encoding="utf-8")
    for _attempt in range(2):
        update = run_install(tmp_path, update=True)
        assert update.returncode == 0, update.output
        assert f"Installer warning: {first.config} has a syntax error at line 6" in update.output
        assert "Update failed" not in update.output
    assert first.config.read_text(encoding="utf-8") == BROKEN_CONFIG
    # One copy of the broken file, however often it is read.
    [copy] = first.config.parent.glob("config.toml.bad-*")
    assert copy.read_text(encoding="utf-8") == BROKEN_CONFIG
