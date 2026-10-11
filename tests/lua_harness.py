"""mpv Lua scripts under a mock mpv (tests/lua/mpv_mock.lua), for behaviour tests.

    @needs_lua
    def test_x(tmp_path):
        out = run_mpv_scenario(tmp_path, '''
            sim.set('duration', 100); sim.fire('file-loaded'); sim.play(5)
            assert(sim.overlay():find('Skip', 1, true))
        ''', scripts=[SKIP_SEGMENTS])

The scenario runs after the scripts load and must not fail; run_mpv_scenario
returns the process output. See mpv_mock.lua for `sim` (props, events, keys,
clicks, timers, OSD/overlay, script messages, commands).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "tests" / "lua" / "mpv_mock.lua"
SCRIPTS = ROOT / "pudge" / "mpv_scripts"
SKIP_SEGMENTS = SCRIPTS / "pudge_skip_segments.lua"
ANILIST = SCRIPTS / "pudge_anilist.lua"


def _lua() -> str | None:
    configured = os.environ.get("PUDGE_TEST_LUA", "").strip()
    if configured:
        return configured
    for name in ("luajit", "lua5.1", "lua", "lua5.4", "lua5.3"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in ("/opt/homebrew/bin/luajit", "/usr/local/bin/luajit"):
        if Path(candidate).is_file():
            return candidate
    return None


LUA = _lua()
needs_lua = pytest.mark.skipif(LUA is None, reason="no lua/luajit interpreter (brew install luajit)")

PRELUDE = """
sim.set('path', '/media/Show - 05.mkv')
sim.set('working-directory', '/media')
sim.set('osd-dimensions', {w = 1920, h = 1080})
sim.set('mouse-pos', {x = 10, y = 10, hover = true})
sim.set('pause', false)
"""


def run_mpv_scenario(
    tmp_path: Path,
    scenario: str,
    *,
    scripts: list[Path],
    env: dict[str, str] | None = None,
    prelude: str = PRELUDE,
    language: str = "en",
) -> str:
    """Runs `scenario` (Lua) against `scripts` loaded into the mock mpv; fails on any error."""
    if LUA is None:
        pytest.skip("no lua/luajit interpreter")
    path = tmp_path / "scenario.lua"
    path.write_text(textwrap.dedent(prelude) + textwrap.dedent(scenario), encoding="utf-8")
    locale = ROOT / "pudge" / "web" / f"locales.{language}.js"
    full_env = {
        "PATH": os.environ.get("PATH", ""),
        "PUDGE_UI_LANGUAGE": language,
        "PUDGE_LOCALE_FILE": str(locale),
        **(env or {}),
    }
    completed = subprocess.run(
        [LUA, str(MOCK), str(path), *[str(s) for s in scripts]],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
    )
    assert completed.returncode == 0 and "SCENARIO_OK" in completed.stdout, (
        completed.stdout[-2000:],
        completed.stderr[-4000:],
    )
    return completed.stdout
