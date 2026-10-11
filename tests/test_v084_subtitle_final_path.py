"""The prepare worker's metadata names the final subtitle file; that path wins over the
PREPARED_SUBTITLE line (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "data" / "library.sqlite3"
    cfg.library.database_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.qbittorrent.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def _worker_output(monkeypatch, stdout: str) -> None:
    class FakeProcess:
        def __init__(self, _command, **_kwargs):
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return stdout, ""

    # Only the manager's subprocess module (other threads keep the real Popen).
    monkeypatch.setattr(
        "pudge.manager.subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": FakeProcess})
    )


def test_prepare_result_meta_final_path_is_authoritative(tmp_path: Path, monkeypatch, caplog) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "Show - 01.mkv").resolve()
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=7, title="Show", episode=1, video_path=video, state="waiting_subtitles")
    )
    manager.db.queue_subtitle_job(video, 7, 1)
    final = manager.config.paths.cache_dir / "playback-srt" / "v15-final.srt"
    final.parent.mkdir(parents=True)
    final.write_text("1\n00:00:01,000 --> 00:00:02,000\n最終\n", encoding="utf-8")
    # The worker's PREPARED_SUBTITLE line names an intermediate file that no longer exists.
    stale = manager.config.paths.cache_dir / "playback-srt" / "v15-intermediate.srt"
    meta = json.dumps({"source": "jimaku", "name": "candidate.srt", "final_path": str(final)})
    _worker_output(
        monkeypatch, f"PREPARED_SUBTITLE={stale}\nPREPARE_STATUS=ready\nPREPARED_SUBTITLE_META={meta}\n"
    )
    manager.logger = logging.getLogger("test.v084")  # the app logger does not propagate to caplog

    with caplog.at_level(logging.INFO, logger="test.v084"):
        assert manager.process_subtitle_jobs(limit=1) == 1

    row = manager.db.episode_by_path(video)
    assert row is not None and row.state == "ready"
    assert row.subtitle_path is not None and row.subtitle_path.read_text(encoding="utf-8").endswith("最終\n")
    history = manager.db.latest_selected_subtitle(video)
    assert history is not None and history["details"]["cache_final_path"] == str(final)
    assert any(
        "REPAIR step=subtitle.prepare_result_path" in line and "source=meta_final_path" in line
        for line in caplog.messages
    )


def test_missing_meta_final_path_keeps_the_prepared_subtitle_line(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "Show - 02.mkv").resolve()
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=7, title="Show", episode=2, video_path=video, state="waiting_subtitles")
    )
    manager.db.queue_subtitle_job(video, 7, 2)
    prepared = manager.config.paths.cache_dir / "playback-srt" / "v15-prepared.srt"
    prepared.parent.mkdir(parents=True)
    prepared.write_text("1\n00:00:01,000 --> 00:00:02,000\n二話\n", encoding="utf-8")
    meta = json.dumps({"source": "jimaku", "name": "candidate.srt", "final_path": str(tmp_path / "gone.srt")})
    _worker_output(
        monkeypatch, f"PREPARED_SUBTITLE={prepared}\nPREPARE_STATUS=ready\nPREPARED_SUBTITLE_META={meta}\n"
    )

    assert manager.process_subtitle_jobs(limit=1) == 1

    row = manager.db.episode_by_path(video)
    assert row is not None and row.state == "ready"
    assert row.subtitle_path is not None and row.subtitle_path.read_text(encoding="utf-8").endswith("二話\n")
