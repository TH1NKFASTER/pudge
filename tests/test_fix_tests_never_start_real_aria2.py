"""Tests never start a real aria2c (a test's copy outlived the run on the user's Mac).

Pudge starts aria2c in its own session. A test that switched torrents on started the
Homebrew aria2c, which kept running after the release test batches and held port 6801 of
the user's own Pudge: Torrents Off then stayed "Shutdown not confirmed".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pudge.providers.aria2 import Aria2Client, Aria2Error


def _program_named_aria2c(tmp_path: Path) -> Path:
    # A real program (not a script) under the name aria2c, as Homebrew's is.
    real = shutil.which("true")
    assert real
    link = tmp_path / "bin" / "aria2c"
    link.parent.mkdir()
    link.symlink_to(real)
    return link


def test_a_real_aria2c_is_refused_as_not_installed(tmp_path: Path) -> None:
    program = _program_named_aria2c(tmp_path)
    with pytest.raises(FileNotFoundError):
        subprocess.Popen([str(program), "--enable-rpc=true"])
    with pytest.raises(FileNotFoundError):
        subprocess.run(f"{program} --version", shell=True, check=False)


def test_switching_aria2_on_in_a_test_starts_no_process(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_ARIA2C", str(_program_named_aria2c(tmp_path)))
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    try:
        with pytest.raises(Aria2Error):
            client._start()
        assert getattr(client, "_managed_process", None) is None
    finally:
        client.close()


def test_a_stand_in_script_named_aria2c_still_runs(tmp_path: Path) -> None:
    script = tmp_path / "aria2c"
    script.write_text("#!/bin/sh\necho stand-in\n", encoding="utf-8")
    script.chmod(0o755)
    completed = subprocess.run([str(script)], capture_output=True, text=True, check=False)
    assert (completed.returncode, completed.stdout) == (0, "stand-in\n")
