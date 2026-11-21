"""JitenMPV is started again when its program crashes right after mpv opens.

User: "JitenMPV dies when mpv opens - now it fell with -1". The crash report: JitenMPV's own
.NET program aborted (an access violation in its code, SIGABRT) a second after it connected to
mpv; mpv reports that as "exit status -1" and the plugin only says "press F10 to restart it".
Pudge's mpv now presses that for the user, at most twice per mpv run.
"""

from __future__ import annotations

from pathlib import Path

from lua_harness import SCRIPTS, needs_lua, run_mpv_scenario
from test_skip_segments_playback import _launch

RESTART = SCRIPTS / "pudge_jiten_restart.lua"

# What jiten-mpv.lua does that matters here: its start binding spawns the program, and a program
# that exits with an error is reported as a warning of the "jiten_mpv" script.
FAKE_JITEN = """
local mp = require 'mp'
starts = 0
crashes_left = 0
exit_text = 'JitenMPV plugin exited (exit status -1); press F10 to restart it'
local function initialize()
    starts = starts + 1
    if crashes_left > 0 then
        crashes_left = crashes_left - 1
        sim.fire('log-message', {prefix = 'jiten_mpv', level = 'warn', text = exit_text .. '\\n'})
    end
end
mp.add_key_binding('F10', 'jiten-mpv-toggle', initialize)
mp.register_event('file-loaded', initialize)
"""


def _plugin(tmp_path: Path) -> Path:
    path = tmp_path / "jiten-mpv.lua"
    path.write_text(FAKE_JITEN, encoding="utf-8")
    return path


@needs_lua
def test_a_crash_is_followed_by_one_restart_a_second_later(tmp_path: Path) -> None:
    run_mpv_scenario(
        tmp_path,
        """
        crashes_left = 1
        sim.fire('file-loaded')
        assert(starts == 1, 'started once with the file')
        sim.play(0.5)
        assert(starts == 1, 'not before a second')
        sim.play(1.0)
        assert(starts == 2, 'started again: ' .. starts)
        local commands = sim.commands('script%-binding')
        assert(#commands == 1 and commands[1] == 'script-binding jiten_mpv/jiten-mpv-toggle', commands[1])
        local said = false
        for _, row in ipairs(sim.log) do
            if row.kind == 'osd' and row.value:find('JitenMPV упал', 1, true) then said = true end
        end
        assert(said, 'the restart is shown in the interface language')
        sim.play(10)
        assert(starts == 2, 'a plugin that runs is left alone')
        """,
        scripts=[_plugin(tmp_path), RESTART],
        language="ru",
    )


@needs_lua
def test_at_most_two_restarts_per_mpv_run(tmp_path: Path) -> None:
    run_mpv_scenario(
        tmp_path,
        """
        crashes_left = 10
        sim.fire('file-loaded')
        sim.play(10)
        assert(starts == 3, 'one start and two restarts: ' .. starts)
        """,
        scripts=[_plugin(tmp_path), RESTART],
    )


@needs_lua
def test_a_normal_exit_a_failed_start_and_a_blocked_program_are_left_alone(tmp_path: Path) -> None:
    run_mpv_scenario(
        tmp_path,
        """
        local texts = {
            'JitenMPV plugin exited; press F10 to restart it',
            'JitenMPV plugin exited (could not start: init); press F10 to restart it',
            'JitenMPV plugin exited (exit status -9). macOS may have blocked it: add mpv under '
                .. 'System Settings > Privacy & Security > Developer Tools, then restart mpv',
        }
        for _, text in ipairs(texts) do sim.fire('log-message', {prefix = 'jiten_mpv', level = 'warn', text = text}) end
        -- Another script's message is not JitenMPV's.
        sim.fire('log-message', {prefix = 'other', level = 'warn', text = 'JitenMPV plugin exited (exit status 1)'})
        sim.play(5)
        assert(#sim.commands('script%-binding') == 0)
        """,
        scripts=[_plugin(tmp_path), RESTART],
    )


def test_pudge_loads_it_only_together_with_jitenmpv(tmp_path: Path, monkeypatch) -> None:
    command, _env, _existed = _launch(tmp_path / "jiten", monkeypatch, exclusive=True, segments=False)
    assert any(arg.startswith("--script=") and arg.endswith("pudge_jiten_restart.lua") for arg in command)
    command, _env, _existed = _launch(tmp_path / "none", monkeypatch, exclusive=False, segments=False)
    assert not any(arg.endswith("pudge_jiten_restart.lua") for arg in command)
