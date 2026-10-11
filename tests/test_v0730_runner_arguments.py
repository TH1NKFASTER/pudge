from pathlib import Path
import json
import subprocess
import sys


def test_batch_runner_forwards_pytest_options_after_separator(tmp_path):
    runner = Path(__file__).resolve().parents[1] / ".github/release/run_test_batch.py"
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text(
        "def test_keep():\n    assert True\n\ndef test_drop():\n    assert False\n",
        encoding="utf-8",
    )
    results = tmp_path / "results"

    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--batch",
            "0",
            "--batches",
            "1",
            "--results-dir",
            str(results),
            "--",
            "-k",
            "test_keep",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    assert summary["totals"]["passed"] == 1
    assert summary["totals"]["failed"] == 0
    assert summary["totals"]["errors"] == 0
    assert summary["totals"]["files"] == 1
