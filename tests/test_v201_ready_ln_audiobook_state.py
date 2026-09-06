from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import (
    AudiobookService,
    _audiobook_series_title,
    _reading_hints_from_cached_parse,
)
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


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


def _api(manager: AnimeManager) -> WebAppApi:
    api = WebAppApi.__new__(WebAppApi)
    api.config = manager.config
    api.manager = manager
    api.logger = logging.getLogger("v201")
    api._play_processes = {}
    api._play_started_at = {}
    api._play_exit_codes = {}
    api._play_registry = {}
    api._play_registry_path = manager.config.paths.cache_dir / "active-playbacks.json"
    api._play_lock = threading.Lock()
    return api


def test_ready_diagnosis_requires_real_subtitle_file(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=151379, title="Akiba Meido Sensou"))
    video = (manager.config.library.root_dir / "Akiba - 01.mkv").resolve()
    video.write_bytes(b"video")
    missing = (tmp_path / "cache" / "vanished.srt").resolve()
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

    result = manager.diagnose_episode(151379, 1)
    subtitle = next(row for row in result["checks"] if row["key"] == "subtitle")
    assert subtitle["ok"] is False
    assert subtitle["detail"] == str(missing)


def test_ready_play_with_missing_cache_requeues_instead_of_launching_without_subs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "Akiba - 01.mkv").resolve()
    video.write_bytes(b"video")
    missing = (tmp_path / "cache" / "vanished.srt").resolve()
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
    monkeypatch.setattr(
        "pudge.web_app.resolve_episode_subtitle",
        lambda *_a, **_k: SimpleNamespace(found=False),
    )
    monkeypatch.setattr(
        "pudge.web_app.subprocess.Popen",
        lambda *_a, **_k: pytest.fail("mpv must not launch without a prepared subtitle"),
    )

    with pytest.raises(RuntimeError, match="queued them for repair"):
        _api(manager).play(str(video))

    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "waiting_subtitles"
    assert row.subtitle_path is None
    job = next(job for job in manager.db.subtitle_jobs() if job["video_path"] == str(video))
    assert int(job["priority"]) >= 260


def test_unchanged_candidates_keep_existing_valid_prepared_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "KimiShinu - 09.mkv").resolve()
    subtitle = (tmp_path / "cache" / "ready.srt").resolve()
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=187260,
            title="Kimi ga Shinu made Koi wo Shitai",
            episode=9,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="waiting_subtitles",
        )
    )
    manager.db.queue_subtitle_job(video, 187260, 9)
    manager.db.set_state(manager._subtitle_candidate_fingerprint_state_key(video), "same")

    class FakeProcess:
        def __init__(self, _command, **_kwargs):
            self.returncode = 4
        def poll(self):
            return self.returncode
        def communicate(self):
            return (
                "SUBTITLE_CANDIDATE_FINGERPRINT=same\n"
                "PREPARE_STATUS=waiting_unchanged_candidates\n",
                "",
            )

    monkeypatch.setattr("pudge.manager.subprocess.Popen", FakeProcess)
    assert manager.process_subtitle_jobs(limit=1) == 1
    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "ready"
    assert row.subtitle_path == subtitle
    assert manager.db.subtitle_jobs() == []


def test_reading_hints_are_not_truncated_at_64_tokens() -> None:
    paragraph = "あ" * 80
    parsed = {
        "paragraphs": [paragraph],
        "tokens": [[
            {"start": index, "end": index + 1, "reading": "あ"}
            for index in range(80)
        ]],
        "vocabulary": [],
    }
    hints = _reading_hints_from_cached_parse(parsed, source_limit=320)
    assert len(hints) == 80
    assert hints[-1]["offset_start"] == 79


def test_audiobook_series_title_strips_matching_linked_or_roman_volume() -> None:
    assert _audiobook_series_title("狼と香辛料 02", volume=2) == "狼と香辛料"
    assert _audiobook_series_title("狼と香辛料II", volume=2) == "狼と香辛料"


def test_audiobook_book_uses_linked_ln_volume_for_group_identity(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = AudiobookService(db, ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv", cache_dir=tmp_path / "cache")
    now = time.time()
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_books ("
            "id INTEGER PRIMARY KEY,title TEXT NOT NULL,volume INTEGER,anilist_id INTEGER,cover_url TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO ln_books(id,title,volume,anilist_id,cover_url) VALUES(1,'狼と香辛料 02',2,NULL,'')"
        )
    source = tmp_path / "狼と香辛料II.m4b"
    source.write_bytes(b"audio")
    book = service._upsert(
        path=source,
        title="狼と香辛料II",
        duration=10.0,
        files=[{"index": 0, "path": str(source), "title": source.stem, "duration": 10.0, "start": 0.0, "end": 10.0}],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 10.0}],
    )
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at) "
            "VALUES(1,?,'chapter',?,?)",
            (int(book["id"]), now, now),
        )
    payload = service.book(int(book["id"]), include_transcription=False)
    assert payload["volume"] == 2
    assert payload["series_title"] == "狼と香辛料"
    assert payload["series_key"]
