"""Updating a development install is not blocked by Xcode's changes to the iPhone project (behaviour).

Opening ios/ in Xcode rewrites project.pbxproj and Info.plist and adds a workspace folder, after
which Settings → Application updates said "The development checkout has local changes"."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pudge.updater import AppUpdater, UpdateError


def _git(source: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)


def test_xcode_changes_do_not_block_but_other_local_changes_do(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "checkout"
    (source / "ios/Pudge.xcodeproj").mkdir(parents=True)
    (source / "ios/Pudge").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Pudge Tests")
    (source / "tracked.txt").write_text("clean\n", encoding="utf-8")
    (source / "ios/Pudge.xcodeproj/project.pbxproj").write_text("project\n", encoding="utf-8")
    (source / "ios/Pudge/Info.plist").write_text("plist\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "initial")
    _git(source, "remote", "add", "origin", "https://github.com/TH1NKFASTER/pudge.git")
    real_git = AppUpdater._git

    def offline_git(path: Path, *args: str, timeout: int = 20) -> str:
        if args[:1] == ("fetch",):  # GitHub is not asked from the tests
            raise UpdateError("offline")
        return real_git(path, *args, timeout=timeout)

    monkeypatch.setattr(AppUpdater, "_git", staticmethod(offline_git))

    (source / "ios/Pudge.xcodeproj/project.pbxproj").write_text("signed by Xcode\n", encoding="utf-8")
    (source / "ios/Pudge/Info.plist").write_text("team\n", encoding="utf-8")
    (source / "ios/Pudge.xcodeproj/project.xcworkspace").mkdir()
    (source / "ios/Pudge.xcodeproj/project.xcworkspace/contents.xcworkspacedata").write_text("x\n")
    updater = AppUpdater()
    status = updater._development_status({"source_path": str(source)})
    assert "local changes" not in status["detail"], status

    (source / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    status = updater._development_status({"source_path": str(source)})
    assert status["blocked"] is True and "local changes" in status["detail"]
