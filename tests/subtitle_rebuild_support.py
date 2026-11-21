"""Assertions shared by the tests of the subtitle rebuild rules (pudge/subtitle_migrations.py).

A rule that prepares an episode again keeps a Ready or Watched episode's subtitles playable
while the rebuild runs (an upgrade check that takes any ready result and puts the old
subtitles back when it fails), instead of clearing them first.
"""

from __future__ import annotations

import json
from pathlib import Path


def _queued(manager) -> set[Path]:
    return {Path(str(job["video_path"])).resolve() for job in manager.db.subtitle_jobs()}


def assert_rebuilding_with_subtitles_kept(manager, video: Path, *, state: str = "ready") -> None:
    row = manager.db.episode_by_path(video)
    assert row is not None and row.state == state, row
    assert row.subtitle_path is not None and row.subtitle_path.is_file(), row
    request = json.loads(manager.db.get_state(manager._subtitle_upgrade_state_key(video), "") or "{}")
    assert request.get("fresh_rebuild") is True, request
    assert request.get("previous_subtitle_path") == str(row.subtitle_path), request
    assert manager.db.get_state(manager._subtitle_force_rebuild_state_key(video), "") == "1"
    assert Path(video).resolve() in _queued(manager)


def assert_not_prepared_again(manager, video: Path) -> None:
    row = manager.db.episode_by_path(video)
    assert row is not None and row.subtitle_path is not None, row
    assert manager.db.get_state(manager._subtitle_upgrade_state_key(video), "") == ""
    assert Path(video).resolve() not in _queued(manager)
