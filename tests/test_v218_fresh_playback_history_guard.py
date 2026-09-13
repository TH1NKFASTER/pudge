from __future__ import annotations

import hashlib
from pathlib import Path

import pudge.subtitle_runtime as runtime


class _Db:
    def __init__(self, marker_prefix: str | None = None) -> None:
        self.marker_prefix = marker_prefix
        self.history_reads = 0

    def get_state(self, key: str, default: str = "") -> str:
        if self.marker_prefix and key.startswith(self.marker_prefix):
            return "1"
        return default

    def latest_selected_subtitle(self, _video: Path):
        self.history_reads += 1
        return {"source": "jimaku", "candidate_path": "/definitely/stale.srt"}

    def latest_selected_subtitle_for_media_or_filename(self, **_kwargs):
        self.history_reads += 1
        return None


def _disable_embedded(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_probe_embedded", lambda *_a, **_k: (None, None, ""))


def test_force_rebuild_blocks_stored_path_and_history(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    old = tmp_path / "old.srt"
    old.write_text("old", encoding="utf-8")
    _disable_embedded(monkeypatch)
    db = _Db("subtitle_force_rebuild:")

    selected = runtime.resolve_episode_subtitle(
        db,
        video_path=video,
        media_id=1,
        episode=1,
        stored_path=old,
        stored_origin="jimaku",
    )
    assert selected.found is False
    assert db.history_reads == 0


def test_debug_force_blocks_stored_path_and_history(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    old = tmp_path / "old.srt"
    old.write_text("old", encoding="utf-8")
    _disable_embedded(monkeypatch)
    db = _Db("debug_force_subtitle:")

    selected = runtime.resolve_episode_subtitle(
        db,
        video_path=video,
        media_id=1,
        episode=1,
        stored_path=old,
    )
    assert selected.found is False
    assert db.history_reads == 0


def test_normal_playback_keeps_existing_selection(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    current = tmp_path / "current.srt"
    current.write_text("current", encoding="utf-8")
    _disable_embedded(monkeypatch)
    db = _Db(None)

    selected = runtime.resolve_episode_subtitle(
        db,
        video_path=video,
        media_id=1,
        episode=1,
        stored_path=current,
        stored_origin="jimaku",
    )
    assert selected.external_path == current.resolve()
    assert selected.reason == "database"


def test_marker_digest_matches_manager_database_convention(tmp_path: Path) -> None:
    video = (tmp_path / "episode.mkv").resolve()
    digest = hashlib.sha1(str(video).encode("utf-8")).hexdigest()
    db = _Db("subtitle_force_rebuild:")
    assert db.get_state("subtitle_force_rebuild:" + digest, "") == "1"
