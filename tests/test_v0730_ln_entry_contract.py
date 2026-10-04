"""LN reader entry/Space/offset/menu/chapter contracts run against real index.html code."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = [
    "nested_playing", "paused_player", "linked_idle", "unlinked", "explicit_read_together_idle",
    "sidebar_handoff_validation", "open_race", "close_during_entry", "space_keydown",
    "offset_parity", "no_forward_clamp", "menu_predicates", "stale_chapter_error",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_ln_entry_contract(scenario: str) -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/ln_entry_contract.cjs"), str(ROOT / "pudge/web"), scenario],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{scenario}: PASS" in result.stdout
