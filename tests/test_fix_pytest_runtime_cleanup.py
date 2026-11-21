"""The pytest runtime folder (fake HOME with test-built pudge.app bundles) must stay out of
Spotlight / Launch Services and must not outlive a killed run: leftover 9.8.x test bundles with
Pudge's bundle id outranked the real app in Spotlight."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent


def _child_env(tmp: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PUDGE_HOME", None)
    env.update(
        TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp), PYTHONPATH=os.pathsep.join([str(TESTS), str(ROOT)])
    )
    return env


def _start_child(tmp: Path) -> tuple[subprocess.Popen[str], Path]:
    code = textwrap.dedent(
        """
        import sys, time
        import conftest
        print(conftest._CREATED_RUNTIME, flush=True)
        time.sleep(60)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code], cwd=tmp, env=_child_env(tmp), stdout=subprocess.PIPE, text=True
    )
    assert child.stdout is not None
    runtime = Path(child.stdout.readline().strip())
    return child, runtime


def test_runtime_folder_is_not_indexed_and_records_its_owner(tmp_path: Path) -> None:
    child, runtime = _start_child(tmp_path)
    try:
        assert runtime.parent == tmp_path
        assert runtime.name.startswith("pudge-pytest-runtime.") and runtime.name.endswith(".noindex")
        assert (runtime / "owner.pid").read_text() == str(child.pid)
        assert (runtime / "home").is_dir()
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_removes_the_runtime_folder_and_still_terminates(tmp_path: Path) -> None:
    child, runtime = _start_child(tmp_path)
    assert runtime.is_dir()
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=20) == -signal.SIGTERM
    assert not runtime.exists()


def test_next_run_removes_only_runtimes_of_finished_runs(tmp_path: Path) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    stale = tmp_path / "pudge-pytest-runtime.dead1.noindex"
    (stale / "home" / "Applications" / "pudge.app").mkdir(parents=True)
    (stale / "owner.pid").write_text(str(dead.pid))
    alive = tmp_path / "pudge-pytest-runtime.alive.noindex"
    alive.mkdir()
    (alive / "owner.pid").write_text(str(os.getpid()))
    unknown = tmp_path / "pudge-pytest-runtime.legacy"
    unknown.mkdir()
    unrelated = tmp_path / "something-else"
    unrelated.mkdir()
    (unrelated / "owner.pid").write_text(str(dead.pid))

    child, runtime = _start_child(tmp_path)
    try:
        deadline = time.monotonic() + 5
        while stale.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not stale.exists()
        assert alive.is_dir() and unknown.is_dir() and unrelated.is_dir() and runtime.is_dir()
    finally:
        child.kill()
        child.wait()
