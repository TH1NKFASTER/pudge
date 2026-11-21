"""An emptied "Delete hours after watching" field keeps the saved delay (never 0 = delete now)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api


def _api_with_pending_cleanup(tmp_path: Path):
    api = make_api(tmp_path)
    api.manager.config.anilist.enabled = False
    db = api.manager.db
    db.upsert_anime(LibraryAnime(media_id=11, title="Kept", status="CURRENT", episodes=12, progress=1))
    video = api.manager.config.library.root_dir / "Kept" / "Kept - 01.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x" * 100)
    db.upsert_episode(
        LibraryEpisode(media_id=11, title="Kept", episode=1, video_path=video, state="ready"),
        downloaded_at=time.time() - 7200,
    )
    Database(api.manager.config.library.database_path).schedule_cleanup(video, 24.0, media_id=11, episode=1)
    return api, video


@pytest.mark.parametrize("value", [None, "", "   ", "abc", float("nan"), float("inf")])
def test_empty_or_invalid_delete_hours_keeps_the_saved_value(tmp_path: Path, value) -> None:
    api, video = _api_with_pending_cleanup(tmp_path)
    try:
        assert api.config.agent.delete_after_watched_hours == 24.0
        assert api.manager.db.due_cleanup() == []
        api.save_settings({"delete_hours": value})
        assert api.config.agent.delete_after_watched_hours == 24.0
        assert api.manager.db.due_cleanup() == []
        api.manager.config.anilist.enabled = False
        api.manager.cleanup()
        assert video.exists()
    finally:
        api.close()


def test_typed_delete_hours_are_still_saved(tmp_path: Path) -> None:
    api, _video = _api_with_pending_cleanup(tmp_path)
    try:
        api.save_settings({"delete_hours": "48"})
        assert api.config.agent.delete_after_watched_hours == 48.0
        api.save_settings({"delete_hours": -5})
        assert api.config.agent.delete_after_watched_hours == 0.0
    finally:
        api.close()


def test_collect_settings_sends_null_for_an_emptied_delete_hours_field(page) -> None:
    result = page.run(
        r"""
        let field = document.getElementById('s_delete_hours');
        if (!field) {
          document.body.insertAdjacentHTML('beforeend', '<input id="s_delete_hours" type="number">');
          field = document.getElementById('s_delete_hours');
        }
        const values = [];
        for (const raw of ['', '  ', 'abc', '12', '0', '1.5']) {
          field.value = raw;
          values.push(collectSettings().delete_hours);
        }
        return values;
        """
    )
    assert result.value == [None, None, None, 12, 0, 1.5]
