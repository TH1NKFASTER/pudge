"""A release puts one commit on GitHub, not the local history (behaviour).

The v0.8.0 release pushed all 69 local patch commits to GitHub. `make release` now turns every
local commit since origin/main, with the version bump, into the single `Pudge vX.Y.Z` commit
on top of origin/main, tags that commit and pushes it. release.py runs in a throwaway checkout
(tests/test_v0109_release_command.py)."""

from __future__ import annotations

from pathlib import Path

from test_fix_release_v0729_structure import _after_0730
from test_v0109_release_command import _git, _release


def test_local_patch_commits_reach_github_as_one_release_commit(tmp_path: Path, monkeypatch) -> None:
    repo, origin, module = _after_0730(tmp_path)
    published = _git(origin, "rev-parse", "main")
    for index in range(3):  # three local patches, as the update scripts commit them
        (repo / f"patch_{index}.txt").write_text(f"patch {index}\n")
        _git(repo, "add", f"patch_{index}.txt")
        _git(repo, "commit", "-qm", f"Local patch {index}")

    _release(monkeypatch, module, repo, "0.8.0")

    assert _git(origin, "log", "--format=%s", f"{published}..main").splitlines() == ["Pudge v0.8.0"]
    head = _git(origin, "rev-parse", "main")
    assert _git(origin, "rev-parse", f"{head}^") == published
    assert _git(origin, "rev-parse", "v0.8.0") == head == _git(repo, "rev-parse", "HEAD")
    files = _git(origin, "ls-tree", "--name-only", head).splitlines()
    assert {"patch_0.txt", "patch_1.txt", "patch_2.txt"} <= set(files)
    assert '"0.8.0"' in _git(origin, "show", f"{head}:pudge/__init__.py")
