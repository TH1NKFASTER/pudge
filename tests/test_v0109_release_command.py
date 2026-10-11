"""`make release VERSION=X.Y.Z` and .github/release/release.py (behaviour; audit 71Б rewrite).

The release helper runs in a throwaway checkout with a bare `origin`: git and the version bump
run for real; `uv lock` and the `make quality` / `make test-batches` gates are recorded
stand-ins (no network, no nested test run).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
MAKE = shutil.which("make")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _checkout(tmp_path: Path):
    """main at 0.7.29 (tagged v0.7.29 and pushed), with the release helpers; returns (repo, origin, module)."""
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    (repo / "pudge").mkdir(parents=True)
    (repo / ".github/release").mkdir(parents=True)
    for name in ("release.py", "bump_version.py"):
        shutil.copy2(ROOT / ".github/release" / name, repo / ".github/release" / name)
    (repo / "pudge/__init__.py").write_text('__version__ = "0.7.29"\n')
    (repo / "pyproject.toml").write_text('[project]\nname = "pudge"\nversion = "0.7.29"\n')
    (repo / "README.md").write_text("Current version: **0.7.29**.\n\nunzip 0.7.29.zip\n")
    (repo / "CHANGELOG.md").write_text(
        "# Changes\n\n## Unreleased\n\n- New things\n\n## v0.7.29\n\n- Old things\n"
    )
    (repo / "uv.lock").write_text("version = 1\n")
    (repo / ".gitignore").write_text(
        "__pycache__/\n"
    )  # as in the real repository: importing the helper writes bytecode
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        _git(repo, "config", key, value)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Pudge v0.7.29")
    _git(repo, "tag", "v0.7.29")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main", "--tags")
    spec = importlib.util.spec_from_file_location("release_under_test", repo / ".github/release/release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return repo, origin, module


def _release(
    monkeypatch, module, repo: Path, *argv: str, calls: list | None = None, gates=None
) -> list[tuple[str, ...]]:
    """Runs release.py's main; records every command it runs, in order, into `calls`."""
    calls = [] if calls is None else calls
    real_run = module.run

    def run(*args: str, capture: bool = False, check: bool = True):
        calls.append(args)
        if args[0] in {"uv", "make"}:
            if args == ("uv", "lock"):
                (repo / "uv.lock").write_text((repo / "uv.lock").read_text() + "# relocked\n")
                return subprocess.CompletedProcess(args, 0, "", "")
            status = (gates or {}).get(args[1], lambda: 0)()
            if check and status:
                raise module.ReleaseError(f"command failed ({status}): {' '.join(args)}")
            return subprocess.CompletedProcess(args, status, "", "")
        return real_run(*args, capture=capture, check=check)

    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(sys, "argv", ["release.py", *argv])
    module.main()
    return calls


def _steps(calls) -> list[tuple[str, ...]]:
    """The non-git steps (bump, lock, quality gates)."""
    return [call for call in calls if call[0] != "git"]


def test_release_bumps_validates_commits_only_release_files_and_publishes(
    tmp_path: Path, monkeypatch
) -> None:
    repo, origin, module = _checkout(tmp_path)
    calls = _release(monkeypatch, module, repo, "0.7.30")
    python = sys.executable
    quality, batches = ("make", "quality", f"PYTHON={python}"), ("make", "test-batches", f"PYTHON={python}")
    assert _steps(calls) == [
        (python, ".github/release/bump_version.py", "0.7.30"),
        ("uv", "lock"),
        quality,
        batches,
    ]
    # Fresh tags are fetched and a stale checkout is ruled out before anything changes.
    assert calls.index(("git", "fetch", "origin", "main", "--tags")) < calls.index(_steps(calls)[0])
    assert ("git", "merge-base", "--is-ancestor", "origin/main", "HEAD") in calls
    # Whitespace errors are checked before the cheap gate, after the long one and once staged.
    diff_checks = [index for index, call in enumerate(calls) if call == ("git", "diff", "--check")]
    assert diff_checks[0] < calls.index(quality) < calls.index(batches) < diff_checks[1]
    assert ("git", "diff", "--cached", "--check") in calls
    assert ("git", "--no-pager", "diff", "--cached", "--stat") in calls
    head = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "log", "-1", "--pretty=%s") == "Pudge v0.7.30"
    assert sorted(_git(repo, "show", "--pretty=", "--name-only", "HEAD").splitlines()) == [
        "CHANGELOG.md",
        "README.md",
        "pudge/__init__.py",
        "pyproject.toml",
        "uv.lock",
    ]
    assert "## v0.7.30\n\n- New things" in (repo / "CHANGELOG.md").read_text()
    assert "unzip pudge-macos-v0.7.30.zip\n" in (repo / "README.md").read_text()
    # main and the vX.Y.Z tag (lightweight, as v0.7.29) reach origin, both at the release commit.
    assert _git(origin, "rev-parse", "main") == head
    assert _git(origin, "rev-parse", "v0.7.30") == head
    assert _git(origin, "tag", "--list").split() == ["v0.7.29", "v0.7.30"]


def test_release_never_stages_unrelated_work(tmp_path: Path, monkeypatch) -> None:
    repo, origin, module = _checkout(tmp_path)
    (repo / "pudge/feature.py").write_text("WIP = True\n")
    calls: list = []
    with pytest.raises(
        module.ReleaseError,
        match="working tree must be clean before a version bump; unrelated changes: pudge/feature.py",
    ):
        _release(monkeypatch, module, repo, "0.7.30", calls=calls)
    assert _steps(calls) == [] and (repo / "pyproject.toml").read_text().endswith('version = "0.7.29"\n')
    # Files that appear during validation (test results ...) abort the release instead of
    # being swept into the release commit.
    (repo / "pudge/feature.py").unlink()
    base = _git(repo, "rev-parse", "HEAD")

    def leave_results():
        (repo / "test-results").mkdir()
        (repo / "test-results/summary.json").write_text("{}")
        return 0

    with pytest.raises(module.ReleaseError, match="files outside the explicitly staged release metadata"):
        _release(monkeypatch, module, repo, "0.7.30", gates={"test-batches": leave_results})
    assert _git(repo, "rev-parse", "HEAD") == base and _git(origin, "rev-parse", "main") == base
    assert _git(origin, "tag", "--list").split() == ["v0.7.29"]


def test_release_refuses_a_stale_checkout_and_failed_gates(tmp_path: Path, monkeypatch) -> None:
    repo, origin, module = _checkout(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    # A failing quality gate: nothing is committed, pushed or tagged.
    with pytest.raises(module.ReleaseError, match="make quality"):
        _release(monkeypatch, module, repo, "0.7.30", gates={"quality": lambda: 2})
    assert _git(repo, "rev-parse", "HEAD") == base and _git(origin, "rev-parse", "main") == base
    _git(repo, "checkout", "-q", "--", ".")
    # origin/main moved on (someone else pushed): the release stops before the bump.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    for key, value in (
        ("user.name", "Other"),
        ("user.email", "other@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        _git(other, "config", key, value)
    (other / "NOTES.md").write_text("elsewhere\n")
    _git(other, "add", "NOTES.md")
    _git(other, "commit", "-q", "-m", "Elsewhere")
    _git(other, "push", "-q", "origin", "main")
    calls: list = []
    with pytest.raises(module.ReleaseError, match="origin/main is not an ancestor of HEAD"):
        _release(monkeypatch, module, repo, "0.7.30", calls=calls)
    assert _steps(calls) == []


def _make(folder: Path, *args: str) -> subprocess.CompletedProcess[str]:
    if MAKE is None:
        pytest.skip("make is not installed")
    shutil.copy2(ROOT / "Makefile", folder / "Makefile")
    return subprocess.run(
        [MAKE, "-s", *args],
        cwd=folder,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": f"{folder / 'bin'}:/usr/bin:/bin"},
    )


def _recorder(path: Path, log: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{path.name} $*" >> "{log}"\n')
    path.chmod(0o755)


def test_make_release_requires_a_version_and_runs_the_release_helper(tmp_path: Path) -> None:
    log = tmp_path / "calls"
    usage = _make(tmp_path, "release")
    assert usage.returncode == 2 and "Usage: make release VERSION=0.7.23" in usage.stdout
    # The project's test venv is the default interpreter; python3 otherwise.
    _recorder(tmp_path / "bin" / "python3", log)
    assert _make(tmp_path, "release", "VERSION=0.7.30").returncode == 0
    _recorder(tmp_path / ".venv-test" / "bin" / "python", log)
    assert _make(tmp_path, "release", "VERSION=0.7.31").returncode == 0
    _recorder(tmp_path / "build_release.sh", log)
    assert _make(tmp_path, "build-release").returncode == 0
    assert log.read_text().splitlines() == [
        "python3 .github/release/release.py 0.7.30 --python python3",
        "python .github/release/release.py 0.7.31 --python .venv-test/bin/python",
        "build_release.sh ",
    ]
