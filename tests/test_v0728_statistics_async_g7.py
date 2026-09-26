from __future__ import annotations

import subprocess
from pathlib import Path


def test_statistics_async_requests_rendering_and_mutations() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", str(root / "tests/js/statistics_async_g7.cjs"), str(root / "pudge/web/statistics.js")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scenarios: PASS" in result.stdout
