from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / ".github" / "release" / "run_test_batch.py"


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
    results = tmp_path / "results"
    shared_log = tmp_path / "must-not-be-used.log"
    env = dict(os.environ)
    env["PUDGE_RUNTIME_LOG_PATH"] = str(shared_log)
    # A per-file timeout far above a pytest start on a busy Mac: these files must finish.
    completed = _run_batch(tmp_path, results, timeout=120, env=env)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    summary = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    rows = {row["path"]: row for row in summary["files"]}
    assert set(rows) == {
        "tests/test_a_writer.py",
        "tests/test_b_reader.py",
        "tests/nested/test_c_nested.py",
        "tests/test_d_failure.py",
    }
    assert rows["tests/test_a_writer.py"]["returncode"] == 0
    assert rows["tests/test_b_reader.py"]["returncode"] == 0
    assert rows["tests/nested/test_c_nested.py"]["returncode"] == 0
    assert rows["tests/test_d_failure.py"]["returncode"] != 0
    assert summary["totals"]["timeouts"] == 0
    assert summary["totals"]["failed_files"] == 1
    assert not shared_log.exists()

    # The timeout: a file that never ends is stopped at the per-file timeout (1 s, far below its
    # hour) and the next file still runs. Load can only make the first file time out sooner.
    later = tmp_path / "later"
    (later / "tests").mkdir(parents=True)
    (later / "tests" / "test_e_timeout.py").write_text(
        "import time\n\ndef test_timeout():\n    time.sleep(3600)\n",
        encoding="utf-8",
    )
    (later / "tests" / "test_f_after.py").write_text("def test_after():\n    assert True\n", encoding="utf-8")
    started = time.monotonic()
    completed = _run_batch(later, later / "results", timeout=1, env=env)
    assert time.monotonic() - started < 600
    assert completed.returncode == 1, completed.stdout + completed.stderr
    summary = json.loads((later / "results" / "summary.json").read_text(encoding="utf-8"))
    rows = {row["path"]: row for row in summary["files"]}
    assert set(rows) == {"tests/test_e_timeout.py", "tests/test_f_after.py"}
    assert rows["tests/test_e_timeout.py"]["timeout"] is True
    assert rows["tests/test_e_timeout.py"]["returncode"] == 124
    assert rows["tests/test_e_timeout.py"]["duration_seconds"] < 600
    assert summary["totals"]["timeouts"] >= 1 and summary["totals"]["failed_files"] >= 1
    assert not shared_log.exists()


def _run_batch(cwd: Path, results: Path, *, timeout: float, env: dict | None = None):
    return subprocess.run(
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
            str(timeout),
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def test_isolated_runner_neuters_macos_open_command(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_open_probe.py").write_text(
        """import os\nimport shutil\nfrom pathlib import Path\n\ndef test_open_is_isolated():\n    resolved = Path(shutil.which('open') or '')\n    isolated_root = Path(os.environ['PUDGE_HOME']).parent\n    assert resolved.is_file()\n    assert str(resolved).startswith(str(isolated_root))\n""",
        encoding="utf-8",
    )

    results = tmp_path / "results"
    completed = _run_batch(tmp_path, results, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr
