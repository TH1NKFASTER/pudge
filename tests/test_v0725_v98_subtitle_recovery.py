from __future__ import annotations

import logging
import threading
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.web_app import WebAppApi


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    return AnimeManager(cfg, log=lambda _message: None)


def test_subtitle_invalidation_clears_stale_candidate_fingerprint(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "episode.mkv").resolve()
    subtitle = (tmp_path / "prepared.srt").resolve()
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=159309,
            title="Mobseka 2",
            episode=8,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    fingerprint_key = manager._subtitle_candidate_fingerprint_state_key(video)
    manager.db.set_state(fingerprint_key, "same-candidates")

    manager.db.invalidate_subtitle(video, 159309, 8, "revalidate alignment")

    assert manager.db.get_state(fingerprint_key, "") == ""
    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "waiting_subtitles"
    assert row.subtitle_path is None


def test_play_uses_history_subtitle_on_same_recovery_launch(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "episode.mkv").resolve()
    subtitle = (tmp_path / "prepared.srt").resolve()
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=159309,
            title="Mobseka 2",
            episode=8,
            video_path=video,
            state="waiting_subtitles",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=159309,
        episode=8,
        source="jimaku",
        candidate_name="episode-08.srt",
        candidate_path=subtitle,
        score=64.71,
        status="selected",
        reason="Preparation completed",
        details={"final_path": str(subtitle)},
    )

    api = WebAppApi.__new__(WebAppApi)
    api.config = manager.config
    api.manager = manager
    api.logger = logging.getLogger("v98-play-recovery")
    api._play_processes = {}
    api._play_started_at = {}
    api._play_exit_codes = {}
    api._play_registry = {}
    api._play_registry_path = manager.config.paths.cache_dir / "active-playbacks.json"
    api._play_lock = threading.Lock()

    calls: list[list[str]] = []

    class FakeProcess:
        pid = 98008

        def poll(self):
            return None

    monkeypatch.setattr(
        "pudge.web_app.subprocess.Popen",
        lambda command, **_kwargs: calls.append(command) or FakeProcess(),
    )

    result = api.play(str(video))

    assert result["duplicate"] is False
    assert len(calls) == 1
    command = calls[0]
    assert command[command.index("--sub") + 1] == str(subtitle)
    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "ready"
    assert row.subtitle_path == subtitle
    assert row.subtitle_origin == "jimaku"
