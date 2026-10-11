"""The installed runtime is exactly the wheel being installed, even for the same version
number, and a development build never picks up a stale build/ folder (behaviour; audit 71Б
rewrite: install.sh runs with stand-ins, tests/t71g01_support.py)."""

from __future__ import annotations

from t71g01_support import release_history, source_history, tree, wheel_files


def test_same_version_update_replaces_the_whole_package() -> None:
    # pip treats a wheel of the installed version as already installed; the installer removes
    # the old package first. A module dropped from the new build must not stay importable.
    history = release_history()
    sim, update = history.sim, history.steps["update"]
    assert (
        "pudge/old_module.py" in update.runtime_before
        and "pudge-9.8.7.dist-info/METADATA" in update.runtime_before
    )
    assert update.runtime_after == wheel_files(
        history.projects["9.8.7-patched"] / "pudge-9.8.7-py3-none-any.whl"
    )
    # The scheduled agent is stopped before the package disappears for a moment.
    result = update.result
    assert result.index("launchctl", "bootout") < result.index("pip", "install", "--no-cache-dir")
    bootout = result.calls("launchctl")[0]
    assert (bootout[0], bootout[2]) == ("bootout", str(sim.agent_plist))


def test_installer_compares_the_installed_files_with_the_wheel() -> None:
    stale = release_history().steps["stale"]
    assert stale.result.returncode != 0
    assert "Installer error: stale runtime file after wheel install: " in stale.result.output
    assert (
        stale.result.output.split("stale runtime file after wheel install: ", 1)[1]
        .split()[0]
        .endswith("/pudge/web/app.js")
    )
    # Nothing past the check ran: the app and the records are as before.
    assert not stale.result.calls("clang") and stale.app_after == stale.app_before
    assert stale.records["installed-version.txt"] == "9.8.6\n"


def test_current_tree_wheel_does_not_use_a_stale_build_folder() -> None:
    history = source_history()
    sim, checkout, steps = history.sim, history.projects["checkout"], history.steps
    install = steps["install"]
    assert install.result.returncode == 0, install.result.output
    assert install.runtime_after["pudge/web/app.js"] == b"// Pudge 9.8.7 work in progress\n"
    # The developer's build/ folder is back as it was after both installs (the second one's
    # wheel build failed), and no temporary wheel folder is left.
    assert steps["wheel_fails"].result.returncode != 0
    assert tree(checkout / "build") == {"lib/pudge/web/app.js": b"// stale build output\n"}
    assert list(sim.tmp.iterdir()) == []
