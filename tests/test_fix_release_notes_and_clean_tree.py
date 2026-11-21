"""Release hygiene: the current version's changelog section is what the GitHub release shows, and
Xcode's own workspace file never makes `make release` refuse a tree as unclean."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from source_policy import source
from t71g01_support import load_workflow, run_step, workflow_step

import pudge

ROOT = Path(__file__).resolve().parents[1]


def test_the_release_workflow_finds_titled_notes_for_this_version(tmp_path: Path) -> None:
    assert (
        subprocess.run([sys.executable, ".github/release/check_release_metadata.py"], cwd=ROOT).returncode
        == 0
    )
    # policy: the workflow file is configuration for GitHub; its step is run here.
    step = workflow_step(
        load_workflow(source(".github/workflows/release.yml")),
        "build-release",
        "Extract release notes from changelog",
    )
    os.symlink(ROOT / "CHANGELOG.md", tmp_path / "CHANGELOG.md")
    (tmp_path / "dist").mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    os.symlink(sys.executable, tools / "python")
    done = run_step(
        step,
        tmp_path,
        tools=tools,
        expressions={
            "github.event_name == 'workflow_dispatch' && inputs.tag || github.ref_name": f"v{pudge.__version__}"
        },
    )
    assert done.returncode == 0, done.stdout + done.stderr
    notes = (tmp_path / "dist" / "release-notes.md").read_text(encoding="utf-8")
    assert notes.startswith("Range: [") and "/compare/" in notes.splitlines()[0]
    assert notes.count("\n- ") >= 20 and "TODO" not in notes


def test_xcodes_workspace_file_is_ignored() -> None:
    path = "ios/Pudge.xcodeproj/project.xcworkspace/contents.xcworkspacedata"
    assert subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT).returncode == 0
