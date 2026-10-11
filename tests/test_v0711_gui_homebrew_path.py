"""install.sh finds Homebrew when Pudge starts it from the GUI, where the login-shell PATH is
missing (behaviour; audit 71Б rewrite).

`run_installer` runs the installer's first part (Homebrew, formulas, tool paths) under zsh
with the portable runner of t71g00_support (Linux and macOS): `brew` records each call with
the brew that ran and the PATH it saw, `uname` says Darwin, `python3.12` only records, and
`mkdir` ends the run, recording the tool paths found so far (the next step creates the install
folders). Homebrew's standard locations are looked up under the test's fake root, so a real
Homebrew is never found or run; paths under that root are recorded as they are on a Mac.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from t71g00_support import HOMEBREW, SYSTEM_PATH, copy_script, mac_env

ROOT = Path(__file__).resolve().parents[1]
LOCATIONS = HOMEBREW
GUI_PATH = SYSTEM_PATH
FORMULAS = ("mpv", "ffmpeg", "alass", "sevenzip", "aria2", "python@3.12", "python-tk@3.12")
ZSH = shutil.which("zsh")

# brew: `list --versions F` succeeds for installed formulas; `install` and `--prefix` work.
BREW = r"""#!/usr/bin/env zsh
print -r -- "brew $*|$0|$PATH" >> "$INSTALLER_LOG"
case "$1" in
  list) [[ " $INSTALLED_FORMULAS " == *" $3 "* ]] ;;
  install) true ;;
  --prefix) print -r -- "$FAKE_PREFIX/$2" ;;
esac
"""
ZSHENV = r"""
uname() { print Darwin; }
python3.12() { print -r -- "python3.12 $*|$PATH" >> "$INSTALLER_LOG"; }
mkdir() { print -r -- "stop|$BREW_BIN|$ARIA2_BIN" >> "$INSTALLER_LOG"; exit 0; }
"""


def _executable(path: Path, text: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def run_installer(
    tmp_path: Path,
    *,
    homebrew: str | None = "/opt/homebrew/bin/brew",
    path_brew: bool = False,
    installed: tuple[str, ...] = FORMULAS,
    aria2c: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Runs install.sh up to the creation of its folders with Homebrew at `homebrew` (None: at
    no standard location) and, with `path_brew`, a brew on the user's PATH. Returns the process
    and the recorded lines."""
    if ZSH is None:
        pytest.skip("zsh is not installed")
    work = tmp_path / "installer"
    prefix = work / "prefix"
    tools = ["mpv/bin/mpv", "ffmpeg/bin/ffmpeg", "alass/bin/alass-cli", "sevenzip/bin/7zz"] + (
        ["aria2/bin/aria2c"] if aria2c else []
    )
    for tool in tools:
        _executable(prefix / tool)
    project, root = work / "project", work / "root"
    copy_script(ROOT / "install.sh", project / "install.sh")
    (project / "pudge").mkdir()
    shutil.copy2(ROOT / "pudge" / "brand.env", project / "pudge" / "brand.env")
    if homebrew is not None:
        _executable(root / homebrew.lstrip("/"), BREW)
    path = GUI_PATH
    if path_brew:
        path = f"{_executable(work / 'custom' / 'bin' / 'brew', BREW).parent}:{GUI_PATH}"
    log_path = work / "log"
    log_path.touch()
    env = mac_env(
        work / "zdot",
        ZSHENV,
        root=root,
        home=work / "home",
        path=path,
        INSTALLER_LOG=str(log_path),
        FAKE_PREFIX=str(prefix),
        INSTALLED_FORMULAS=" ".join(installed),
    )
    completed = subprocess.run(
        [ZSH, str(project / "install.sh")], capture_output=True, text=True, env=env, timeout=60, cwd=work
    )
    return completed, [
        line.replace(str(root), "") for line in log_path.read_text(encoding="utf-8").splitlines()
    ]


def brew_calls(lines: list[str]) -> list[tuple[str, ...]]:
    """(call, brew that ran, PATH) for every brew call."""
    return [tuple(line.split("|", 2)) for line in lines if line.startswith("brew ")]


@pytest.mark.parametrize("homebrew", LOCATIONS)
def test_installer_finds_homebrew_without_login_shell_path(tmp_path: Path, homebrew: str) -> None:
    # Started from Pudge (a GUI app) the PATH has no Homebrew; Apple silicon and Intel Macs
    # keep it in different places.
    completed, lines = run_installer(tmp_path, homebrew=homebrew)
    assert completed.returncode == 0, completed.stderr
    calls = brew_calls(lines)
    assert calls and {ran for _call, ran, _path in calls} == {homebrew}
    assert lines[-1].split("|")[:2] == ["stop", homebrew]


def test_homebrew_path_is_fixed_before_formula_checks(tmp_path: Path) -> None:
    # Homebrew's folder is first in PATH from the first formula check on, so brew's
    # python3.12 and tools resolve as they do from Terminal.
    completed, lines = run_installer(tmp_path, installed=())
    assert completed.returncode == 0, completed.stderr
    fixed = f"/opt/homebrew/bin:{GUI_PATH}"
    calls = brew_calls(lines)
    assert calls[:2] == [
        ("brew list --versions mpv", "/opt/homebrew/bin/brew", fixed),
        ("brew install mpv", "/opt/homebrew/bin/brew", fixed),
    ]
    assert [call for call, _ran, _path in calls if call.startswith("brew install")] == [
        f"brew install {name}" for name in FORMULAS
    ]
    assert all(path == fixed for _call, _ran, path in calls)
    python = [line for line in lines if line.startswith("python3.12 ")]
    assert python and all(line.endswith(f"|{fixed}") for line in python)


def test_installer_uses_brew_from_path_or_explains_that_homebrew_is_needed(tmp_path: Path) -> None:
    # A Homebrew elsewhere is still found through the PATH of a Terminal launch.
    completed, lines = run_installer(tmp_path / "custom", homebrew=None, path_brew=True)
    assert completed.returncode == 0, completed.stderr
    custom = str(tmp_path / "custom" / "installer" / "custom" / "bin" / "brew")
    assert {ran for _call, ran, _path in brew_calls(lines)} == {custom}
    # No Homebrew at all: a clear message before anything runs.
    completed, lines = run_installer(tmp_path / "none", homebrew=None)
    assert completed.returncode == 1
    assert completed.stderr.strip() == "Homebrew is required: https://brew.sh"
    assert lines == []
