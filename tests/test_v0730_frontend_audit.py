from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["paged", "progress", "backend", "vertical", "part_grant", "gate_disabled", "ln_generation", "sidebar_keys", "sidebar_handoff", "sidebar_seek", "hydration", "reader_inputs", "grammar_selector", "assistant_ruby"])
def test_frontend_audit_behavior(scenario: str) -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/frontend_audit.cjs"), str(ROOT / "pudge/web"), scenario],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
