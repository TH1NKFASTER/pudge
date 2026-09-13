from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "run_test_batch.py"


def test_isolated_runner_recurses_continues_and_times_out(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    (tests / "nested").mkdir(parents=True)
    (tests / "test_a_writer.py").write_text(
        """from pathlib import Path\nimport os\n\ndef test_writer():\n    home = Path(os.environ['PUDGE_HOME'])\n    home.mkdir(parents=True, exist_ok=True)\n    (home / 'marker').write_text('x')\n""",
        encoding="utf-8",
    )
    (tests / "test_b_reader.py").write_text(
        """from pathlib import Path\nimport os\n\ndef test_reader_is_isolated():\n    assert not (Path(os.environ['PUDGE_HOME']) / 'marker').exists()\n""",
        encoding="utf-8",
    )
    (tests / "nested" / "test_c_nested.py").write_text(
        "def test_nested_is_collected():\n    assert True\n",
        encoding="utf-8",
    )
    (tests / "test_d_failure.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )
    (tests / "test_e_timeout.py").write_text(
        "import time\n\ndef test_timeout():\n    time.sleep(5)\n",
        encoding="utf-8",
    )

    results = tmp_path / "results"
    shared_log = tmp_path / "must-not-be-used.log"
    env = dict(os.environ)
    env["PUDGE_RUNTIME_LOG_PATH"] = str(shared_log)
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--batch",
            "0",
            "--batches",
            "1",
            "--results-dir",
            str(results),
            "--timeout",
            "1",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 1
    summary = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    rows = {row["path"]: row for row in summary["files"]}
    assert set(rows) == {
        "tests/test_a_writer.py",
        "tests/test_b_reader.py",
        "tests/nested/test_c_nested.py",
        "tests/test_d_failure.py",
        "tests/test_e_timeout.py",
    }
    assert rows["tests/test_a_writer.py"]["returncode"] == 0
    assert rows["tests/test_b_reader.py"]["returncode"] == 0
    assert rows["tests/nested/test_c_nested.py"]["returncode"] == 0
    assert rows["tests/test_d_failure.py"]["returncode"] != 0
    assert rows["tests/test_e_timeout.py"]["timeout"] is True
    assert summary["totals"]["timeouts"] == 1
    assert summary["totals"]["failed_files"] == 2
    assert not shared_log.exists()
