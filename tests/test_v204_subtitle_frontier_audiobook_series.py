from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.qbittorrent.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def test_invalidated_missing_ready_forces_full_rebuild_even_with_same_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "Akiba - 01.mkv").resolve()
    video.write_bytes(b"video")
    missing = (manager.config.paths.cache_dir / "playback-srt" / "v12-gone.srt").resolve()
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=151379,
            title="Akiba Meido Sensou",
            episode=1,
            video_path=video,
            subtitle_path=missing,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.set_state(manager._subtitle_candidate_fingerprint_state_key(video), "same")
    manager.db.invalidate_subtitle(video, 151379, 1, "Prepared subtitle cache file is missing")
    manager.db.queue_subtitle_job(video, 151379, 1, priority=260)

    force_key = manager._subtitle_force_rebuild_state_key(video)
    assert manager.db.get_state(force_key, "") == "1"
    assert manager.db.get_state(manager._subtitle_candidate_fingerprint_state_key(video), "") == ""

    prepared = (manager.config.paths.cache_dir / "playback-srt" / "v15-new.srt").resolve()
    prepared.parent.mkdir(parents=True, exist_ok=True)
    prepared.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    commands: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            commands.append(list(command))
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return (
                f"PREPARED_SUBTITLE={prepared}\n"
                "SUBTITLE_CANDIDATE_FINGERPRINT=same\n"
                "PREPARE_STATUS=ready\n"
                'PREPARED_SUBTITLE_META={"source":"jimaku","name":"same.srt"}\n',
                "",
            )

    # Keep the test focused on subtitle preparation.  On macOS the work
    # scheduler probes battery/thermal state via subprocess.run(["pmset", ...]),
    # which internally uses the same subprocess.Popen object.  If we replace
    # Popen globally before bypassing that resource probe, the first captured
    # command is pmset rather than the Pudge prepare command.
    monkeypatch.setattr(manager.work_scheduler, "background_allowed", lambda **_kwargs: True)
    monkeypatch.setattr("pudge.manager.subprocess.Popen", FakeProcess)
    assert manager.process_subtitle_jobs(limit=1) == 1
    assert commands
    command = commands[0]
    assert "--force-search" in command
    assert "--resync" in command
    assert "--previous-candidate-fingerprint" not in command

    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "ready"
    assert row.subtitle_path is not None and row.subtitle_path.is_file()
    assert row.subtitle_path.parent == manager.config.library.database_path.parent / "prepared-subtitles"
    assert manager.db.get_state(force_key, "") == ""
    assert manager.db.subtitle_jobs() == []


def test_subtitle_queue_prioritizes_each_titles_watchable_frontier_before_middle(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=1, title="Series A", progress=0, episodes=12))
    db.upsert_anime(LibraryAnime(media_id=2, title="Series B", progress=4, episodes=12))
    db.upsert_anime(LibraryAnime(media_id=3, title="Series C", progress=0, episodes=12))

    # Deliberately give middle episodes much larger static priorities.  Viewer
    # availability still wins: A1/B5 are the exact next unwatched episodes, and
    # C7 is the first broken episode of its local unresolved range.
    rows = [
        (1, 1, 0), (1, 2, 900), (1, 3, 1000),
        (2, 5, 0), (2, 6, 950),
        (3, 7, 0), (3, 8, 999),
    ]
    for media_id, episode, priority in rows:
        video = tmp_path / f"m{media_id}-e{episode}.mkv"
        video.write_bytes(b"video")
        db.queue_subtitle_job(video, media_id, episode, priority=priority)

    due = [(int(row["media_id"]), int(row["episode"])) for row in db.due_subtitle_jobs(limit=20)]
    assert set(due[:2]) == {(1, 1), (2, 5)}
    assert due.index((3, 7)) < due.index((3, 8))
    assert due.index((1, 1)) < due.index((1, 2))
    assert due.index((2, 5)) < due.index((2, 6))

    claimed = [(int(row["media_id"]), int(row["episode"])) for row in db.claim_due_subtitle_jobs(limit=2)]
    assert set(claimed) == {(1, 1), (2, 5)}


def test_audiobook_series_ui_is_one_large_scrollable_card_with_volume_labels() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge" / "web" / "media.js").read_text(encoding="utf-8")
    css = (root / "pudge" / "web" / "media.css").read_text(encoding="utf-8")

    assert 'class="audiobook-series-card ${selectionClass}"' in js
    assert 'class="audiobook-series-scroll"' in js
    assert "data-first-unfinished-id" in js
    assert "seriesScrollPositions" in js
    assert "displayTitle=grouped&&volume>0" in js
    assert "`Volume ${volume}`" in js
    assert "audiobook-series-header" not in js
    assert ".audiobook-series-card{" in css
    assert ".audiobook-series-scroll{" in css
    assert "max-height:430px" in css
    assert "overflow-y:auto" in css
