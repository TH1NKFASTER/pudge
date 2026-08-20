from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig
from pudge.episode_numbering import episode_numbering_from_graph
from pudge.episode_state import watched_by_anilist_progress
from pudge.foreground import clear_foreground, mark_foreground
from pudge.library import sidecar_subtitle
from pudge.presentation_state import derive_episode_presentation
from pudge.work_scheduler import WorkScheduler


def test_sidecar_selector_prefers_japanese_text_over_english_srt(tmp_path: Path) -> None:
    video = tmp_path / "Episode 01.mkv"
    video.write_bytes(b"x")
    (tmp_path / "Episode 01.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello there\n",
        encoding="utf-8",
    )
    japanese = tmp_path / "Episode 01.ja.ass"
    japanese.write_text(
        "[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,"
        "これは日本語の字幕です。よろしくお願いします。\n",
        encoding="utf-8",
    )

    assert sidecar_subtitle(video) == japanese


def test_sidecar_selector_does_not_mark_unknown_english_text_ready(tmp_path: Path) -> None:
    video = tmp_path / "Episode 01.mkv"
    video.write_bytes(b"x")
    (tmp_path / "Episode 01.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nEnglish only subtitle text\n",
        encoding="utf-8",
    )

    assert sidecar_subtitle(video) is None


def test_episode_numbering_crosses_side_work_without_counting_it() -> None:
    anime = SimpleNamespace(
        media_id=6,
        title="Slime S4",
        titles=["Tensei Shitara Slime Datta Ken 4th Season"],
        synonyms=[],
        episodes=24,
        format="TV",
    )
    nodes = [
        {"media_id": 1, "title": "Tensei Shitara Slime Datta Ken", "episodes": 24, "format": "TV"},
        {"media_id": 2, "title": "Tensei Shitara Slime Datta Ken Coleus", "episodes": 3, "format": "OVA"},
        {"media_id": 3, "title": "Tensei Shitara Slime Datta Ken 2nd Season", "episodes": 12, "format": "TV"},
        {"media_id": 4, "title": "Tensei Shitara Slime Datta Ken 2nd Season Part 2", "episodes": 12, "format": "TV"},
        {"media_id": 5, "title": "Tensei Shitara Slime Datta Ken 3rd Season", "episodes": 24, "format": "TV"},
        {"media_id": 6, "title": "Tensei Shitara Slime Datta Ken 4th Season", "episodes": 24, "format": "TV"},
    ]
    graph = {
        "nodes": nodes,
        "edges": [
            {"source": 1, "target": 2, "relation_type": "SEQUEL"},
            {"source": 2, "target": 3, "relation_type": "SEQUEL"},
            {"source": 3, "target": 4, "relation_type": "SEQUEL"},
            {"source": 4, "target": 5, "relation_type": "SEQUEL"},
            {"source": 5, "target": 6, "relation_type": "SEQUEL"},
        ],
    }

    result = episode_numbering_from_graph(graph, anime, 18)

    assert result is not None
    assert result.offset == 72
    assert result.release_episode == 90
    assert result.chain == (1, 2, 3, 4, 5, 6)


def test_presentation_local_ready_beats_stale_download(tmp_path: Path) -> None:
    video = tmp_path / "ready.mkv"
    video.write_bytes(b"x")
    local = {"state": "ready", "video_path": str(video)}
    stale_download = {
        "state": "waiting",
        "progress": 0.0,
        "raw": {"total_size": 1_000_000, "downloaded": 0, "download_speed": 0},
    }

    state = derive_episode_presentation(local=local, download=stale_download)

    assert state["status"] == "ready"
    assert state["ready"] is True


def test_presentation_download_has_percent_and_eta() -> None:
    download = {
        "state": "active",
        "progress": 0.09,
        "raw": {
            "total_size": 1_000_000,
            "downloaded": 90_000,
            "download_speed": 10_000,
        },
    }

    state = derive_episode_presentation(download=download)

    assert state["status"] == "downloading"
    assert state["progress_percent"] == 9
    assert state["eta_seconds"] == 91


def test_anilist_watched_presentation_does_not_require_local_watch_timestamp(
    tmp_path: Path,
) -> None:
    video = tmp_path / "episode-06.mkv"
    video.write_bytes(b"x")
    local = {"state": "ready", "video_path": str(video)}

    state = derive_episode_presentation(local=local, watched_externally=True)

    assert watched_by_anilist_progress(6, 6, total_episodes=12, media_format="TV")
    assert not watched_by_anilist_progress(7, 6, total_episodes=12, media_format="TV")
    assert state == {"status": "watched", "ready": True, "action_code": ""}


def test_work_scheduler_blocks_new_heavy_work_during_foreground(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    scheduler = WorkScheduler(cfg.paths.cache_dir)

    mark_foreground(cfg.paths.cache_dir)
    try:
        assert scheduler.background_allowed() is False
        assert scheduler.acquire_heavy("test", blocking=False) is None
    finally:
        clear_foreground(cfg.paths.cache_dir)

    lease = scheduler.acquire_heavy("test", blocking=False, foreground_sensitive=False)
    assert lease is not None
    try:
        assert scheduler.acquire_heavy("second", blocking=False, foreground_sensitive=False) is None
    finally:
        lease.release()


def test_work_scheduler_resource_probe_failure_is_nonfatal(tmp_path: Path, monkeypatch) -> None:
    scheduler = WorkScheduler(tmp_path / "cache")

    def fail_run(*_args, **_kwargs):
        raise TypeError("simulated broken process layer")

    monkeypatch.setattr("pudge.work_scheduler.subprocess.run", fail_run)
    status = scheduler.resource_status(refresh=True)

    assert status["thermal_limited"] is False
    assert status["on_battery"] is False
    assert status["battery_percent"] is None
    assert scheduler.background_allowed() is True
