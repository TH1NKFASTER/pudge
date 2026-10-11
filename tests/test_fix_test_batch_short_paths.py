"""The release test batches give every test file a short, neutral temporary folder (behaviour).

`make release` runs the suite through .github/release/run_test_batch.py. Its per-file folder was
named after the test file, which on a Mac made Unix socket paths longer than macOS's 104 bytes
(AF_UNIX path too long) and put names such as "_v0725_" into the ancestors of tmp_path, which
the manga import read as a volume folder."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

RUNNER = Path(__file__).parents[1] / ".github" / "release" / "run_test_batch.py"

PROBE = """import os
import tempfile
from pathlib import Path


def test_paths_are_short_and_neutral(tmp_path):
    isolated = Path(os.environ["PUDGE_HOME"]).parent  # the runner's folder for this test file
    socket_path = Path(tempfile.mkdtemp(prefix="pudge-ipc-")) / "mpv.sock"
    # On a Mac the runner's base is /private/tmp, so this part decides the 104-byte limit.
    assert len(str(socket_path.relative_to(isolated.parent))) <= 60, socket_path
    assert "v0725" not in isolated.name and len(isolated.name) <= 20, isolated
    assert tmp_path.is_relative_to(isolated)
"""


def test_batch_runner_temp_folders_fit_unix_sockets_and_carry_no_test_names(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_v0725_selection_drop_reconcile_with_a_long_descriptive_name.py").write_text(
        PROBE, encoding="utf-8"
    )
    results = tmp_path / "results"
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--batch", "0", "--batches", "1", "--results-dir", str(results)]
        + ["--timeout", "30"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    summary = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    [row] = summary["files"]
    log = Path(row["stdout"]).read_text(encoding="utf-8")
    assert completed.returncode == 0 and row["returncode"] == 0, log
