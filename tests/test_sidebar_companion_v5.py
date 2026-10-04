from pathlib import Path
import json
import os
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_reader_close_keeps_playback_and_hands_off_before_response() -> None:
    result = subprocess.run(["node", str(ROOT / "tests/js/sidebar_v5.cjs"), str(ROOT / "pudge/web")], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
