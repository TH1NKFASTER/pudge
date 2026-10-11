"""A fast update keeps the native app as it is and still refreshes it with LaunchServices
(behaviour; audit 71Б rewrite: install.sh runs with stand-ins, tests/t71g01_support.py)."""

from __future__ import annotations

from pathlib import Path

from t71g01_support import release_history


def test_fast_update_keeps_the_native_app_and_refreshes_launchservices() -> None:
    # v0.7.4: LSREGISTER was only set while building the native app, so an update that kept
    # the app stopped at "$LSREGISTER -f" (unset variable) after everything was installed.
    history = release_history()
    sim, update = history.sim, history.steps["update"]
    assert update.result.returncode == 0, update.result.output
    assert (
        "Fast update: preserving native app identity and existing macOS folder grants."
        in update.result.output
    )
    assert not update.result.calls("clang") and not update.result.calls("codesign")
    assert update.app_after == update.app_before
    assert update.result.calls("lsregister") == [["-f", str(sim.app)]]


def test_a_new_install_registers_the_app_and_unregisters_the_old_brand() -> None:
    history = release_history()
    sim, install = history.sim, history.steps["install"]
    assert install.result.calls("lsregister") == [
        ["-u", str(sim.home / "Applications/Anime MPV.app")],
        ["-f", str(sim.app)],
    ]
    assert not (sim.home / "Applications/Anime MPV.app").exists()
    # The old brand's scheduled agent is stopped and its plist removed.
    legacy_agent = str(sim.home / "Library/LaunchAgents/com.anime-mpv.agent.plist")
    assert [(call[0], call[2]) for call in install.result.calls("launchctl")][0] == ("bootout", legacy_agent)
    assert not Path(legacy_agent).exists()
