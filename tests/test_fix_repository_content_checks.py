"""Tests about what the repository holds ask git, not the working folder (review ARCH-5).

A developer's folder keeps leftovers git does not track (``rename_brand.py``, ``docs/``
from older versions): they must not make "this file is gone" or the code-base policy
tests fail.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import source_policy
from repository_files import in_repository, tracked_files


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "pudge").mkdir(parents=True)
    (root / "pudge" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Pudge\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    # Leftovers of an older version, never part of the repository.
    (root / "rename_brand.py").write_text("", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "LLM_HANDOFF.md").write_text("notes\n", encoding="utf-8")
    (root / "pudge" / "old_module.py").write_text("y = 2\n", encoding="utf-8")
    return root


def test_untracked_leftovers_are_not_repository_content(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    assert in_repository("pudge/app.py", root) and in_repository("pudge", root)
    assert in_repository("README.md", root)
    assert not in_repository("rename_brand.py", root)
    assert not in_repository("docs/LLM_HANDOFF.md", root) and not in_repository("docs", root)
    assert not in_repository("pudge/old_module.py", root)


def test_a_tracked_file_deleted_from_the_folder_is_gone(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    (root / "README.md").unlink()
    assert not in_repository("README.md", root)


def test_outside_a_git_checkout_the_folder_is_the_release(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    (root / "README.md").write_text("# Pudge\n", encoding="utf-8")
    assert tracked_files(root) is None
    assert in_repository("README.md", root)
    assert not in_repository("rename_brand.py", root)


def test_policy_tests_read_only_files_the_repository_holds(tmp_path: Path, monkeypatch) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setattr(source_policy, "ROOT", root)
    assert source_policy.sources("pudge/**/*.py") == {"pudge/app.py": "x = 1\n"}
