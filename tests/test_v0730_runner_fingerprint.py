import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("relative", [
    "tests/conftest.py", "tests/helpers.py", "tests/js/scenario.cjs",
    ".github/release/run_test_batch.py",
])
def test_verification_fingerprint_includes_test_support_and_runner(tmp_path, relative):
    source = Path(__file__).resolve().parents[1] / ".github/release/run_test_batch.py"
    spec = importlib.util.spec_from_file_location("runner_fingerprint_regression", source)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    scenario = tmp_path / "tests/test_scenario.py"
    scenario.parent.mkdir()
    scenario.write_text("def test_scenario(): assert True\n")
    helper = tmp_path / relative
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("first behavior\n")
    before = runner.tree_fingerprint([scenario], root=tmp_path)

    helper.write_text("changed behavior\n")

    assert runner.tree_fingerprint([scenario], root=tmp_path) != before
