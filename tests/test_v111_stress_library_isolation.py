from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.subtitle_benchmark_cli import (
    _ensure_stress_library_ignore_marker,
    _repair_stress_library_registrations,
)


def test_stress_marker_is_detected_by_library_guard(tmp_path: Path) -> None:
    watched = tmp_path / "Downloads"
    corpus = watched / "pudge-subtitle-stress-100gb"
    video = corpus / "downloads" / "Anime - 01.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")

    marker = _ensure_stress_library_ignore_marker(corpus)
    assert marker.is_file()
    assert AnimeManager._library_scan_ignore_root_for_path(video) == corpus.resolve()
    assert AnimeManager._library_path_is_ignored(video) is True

    manager = object.__new__(AnimeManager)
    manager.config = SimpleNamespace(
        library=SimpleNamespace(root_dir=tmp_path / "Library"),
        paths=SimpleNamespace(download_dirs=[watched]),
    )
    (tmp_path / "Library").mkdir()
    assert manager._library_scan_ignore_roots() == (corpus.resolve(),)


def test_completed_download_registration_skips_marked_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "stress"
    video = corpus / "downloads" / "Anime - 01.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    _ensure_stress_library_ignore_marker(corpus)

    manager = object.__new__(AnimeManager)
    manager.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    manager._completed_download_video_files = lambda _item: [video]
    completed = []
    manager._complete_download_intent = lambda item: completed.append(item) or True
    item = SimpleNamespace(torrent_hash="abc")

    assert manager._register_completed_download(item) == 0
    assert completed == [item]


def test_repair_removes_sparse_siblings_but_keeps_authoritative_target(tmp_path: Path) -> None:
    corpus = tmp_path / "stress"
    downloads = corpus / "downloads" / "Pack"
    downloads.mkdir(parents=True)
    target = downloads / "Example Anime - 01.mkv"
    sibling = downloads / "Example Anime - 02.mkv"
    target.write_bytes(b"complete-target")
    sibling.write_bytes(b"sparse")

    event = {
        "event": "download_ok",
        "media_id": 100,
        "episode": 1,
        "title": "Example Anime",
        "video_name": target.name,
        "source_release": {
            "title": "Example Anime - 01",
            "info_hash": "deadbeef",
        },
    }
    (corpus / "stress-events.jsonl").write_text(
        json.dumps(event) + "\n", encoding="utf-8"
    )
    row = {
        "media_id": 100,
        "episode": 1,
        "title": "Example Anime",
        "titles": ["Example Anime"],
        "synonyms": [],
        "format": "TV",
        "episodes": 12,
    }

    db_path = tmp_path / "library.sqlite3"
    db = Database(db_path)
    db.upsert_episode(
        LibraryEpisode(
            media_id=100,
            title="Example Anime",
            episode=1,
            video_path=target.resolve(),
            state="waiting_subtitles",
            torrent_hash="deadbeef",
        )
    )
    db.upsert_episode(
        LibraryEpisode(
            media_id=100,
            title="Example Anime",
            episode=2,
            video_path=sibling.resolve(),
            state="waiting_subtitles",
            torrent_hash="deadbeef",
        )
    )
    db.queue_subtitle_job(target.resolve(), 100, 1)
    db.queue_subtitle_job(sibling.resolve(), 100, 2)

    result = _repair_stress_library_registrations(
        corpus,
        [row],
        SimpleNamespace(library=SimpleNamespace(database_path=db_path)),
    )

    target_row = db.episode_by_path(target.resolve())
    assert target_row is not None
    assert target_row.media_id == 100
    assert target_row.episode == 1
    assert db.episode_by_path(sibling.resolve()) is None
    jobs = {str(job["video_path"]) for job in db.subtitle_jobs()}
    assert str(target.resolve()) in jobs
    assert str(sibling.resolve()) not in jobs
    assert result["isolated_sibling_rows_removed"] == 1
    assert result["isolated_subtitle_jobs_removed"] == 1
