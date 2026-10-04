from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager_models import LibraryEpisode


# --- X04: consistent mpv global position snapshot ---------------------------


class _TransitionPlayer:
    """mpv IPC stub that switches files right after the first time-pos read."""

    def __init__(self) -> None:
        self.index = 0
        self.time_reads = 0

    def files(self, _book_id):
        return [
            {"file_index": 0, "start": 0.0, "duration": 100.0},
            {"file_index": 1, "start": 100.0, "duration": 100.0},
        ]

    def get(self, _path, name):
        if name == "playlist-pos":
            return self.index
        if name == "time-pos":
            self.time_reads += 1
            if self.time_reads == 1:
                self.index = 1  # transition happens after this read
                return 99.0
            return 0.25
        return None


def test_global_position_does_not_mix_old_local_time_with_new_file_start() -> None:
    from pudge.audiobooks import AudiobookService

    stub = _TransitionPlayer()
    service = AudiobookService.__new__(AudiobookService)
    service._file_rows = stub.files
    service._ipc_get = stub.get

    position = AudiobookService._global_position(service, 1, Path("/tmp/x.sock"))

    assert position == pytest.approx(100.25)


def test_global_position_stable_snapshot_unchanged() -> None:
    from pudge.audiobooks import AudiobookService

    service = AudiobookService.__new__(AudiobookService)
    service._file_rows = _TransitionPlayer().files
    live = {"playlist-pos": 1, "time-pos": 12.5}
    service._ipc_get = lambda _path, name: live.get(name)

    assert AudiobookService._global_position(service, 1, Path("/tmp/x.sock")) == pytest.approx(112.5)


# --- X05: ffmpeg stderr drained concurrently, overall timeout ----------------


def _fake_ffmpeg(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def test_analyze_audio_activity_survives_large_stderr_before_stdout(tmp_path: Path) -> None:
    from pudge.audio_activity import analyze_audio_activity

    ffmpeg = _fake_ffmpeg(
        tmp_path,
        "sys.stderr.write('w' * (512 * 1024)); sys.stderr.flush()\n"
        "import struct, math\n"
        "data = b''.join(struct.pack('<h', int(8000*math.sin(i/5.0)) if (i//4000)%2 else 0) for i in range(32000))\n"
        "sys.stdout.buffer.write(data); sys.stdout.flush()",
    )
    result: dict = {}

    def run() -> None:
        try:
            result["value"] = analyze_audio_activity(tmp_path / "a.mp3", ffmpeg=ffmpeg, timeout=30)
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(20)
    assert not thread.is_alive(), "analyze_audio_activity deadlocked on a full stderr pipe"
    assert "error" not in result, result.get("error")
    assert result["value"]["schema"] == "audio-activity-v1"


def test_analyze_audio_activity_kills_hung_decoder_after_timeout(tmp_path: Path) -> None:
    from pudge.audio_activity import analyze_audio_activity

    ffmpeg = _fake_ffmpeg(tmp_path, "time.sleep(60)")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        analyze_audio_activity(tmp_path / "a.mp3", ffmpeg=ffmpeg, timeout=1.0)
    assert time.monotonic() - started < 15


# --- X06: atomic SRT writes and validated caches -----------------------------


def test_write_srt_is_atomic_when_replace_fails(tmp_path: Path, monkeypatch) -> None:
    from pudge import subtitle_formats

    target = tmp_path / "out.srt"
    target.write_text("1\n00:00:01,000 --> 00:00:02,000\nold\n", encoding="utf-8")

    def boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(subtitle_formats.os, "replace", boom)
    with pytest.raises(OSError):
        subtitle_formats.write_srt([(1.0, 2.0, "new")], target)
    assert "old" in target.read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.srt"]


def test_clean_srt_regenerates_truncated_cache(tmp_path: Path) -> None:
    from pudge.subtitle_formats import clean_srt_for_playback, parse_srt

    source = tmp_path / "ep.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n2\n00:00:03,000 --> 00:00:04,000\nさようなら\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    output, info = clean_srt_for_playback(source, cache)
    assert info["cleaned"] and parse_srt(output)
    output.write_text("1\n00:00:0", encoding="utf-8")  # interrupted write

    again, info = clean_srt_for_playback(source, cache)
    assert again == output
    assert info["reason"] != "cached"
    assert len(parse_srt(again)) == 2


def test_convert_to_plain_srt_regenerates_unparseable_cache(tmp_path: Path) -> None:
    from pudge.subtitle_formats import convert_to_plain_srt, parse_srt

    source = tmp_path / "ep.ass"
    source.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize\nStyle: Default,Arial,20\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,こんにちは\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    output, info = convert_to_plain_srt(source, cache, ffmpeg_path="/nonexistent/ffmpeg")
    assert info["converted"] and parse_srt(output)
    output.write_text("garbage", encoding="utf-8")

    again, info = convert_to_plain_srt(source, cache, ffmpeg_path="/nonexistent/ffmpeg")
    assert info["reason"] != "cached"
    assert parse_srt(again)


# --- X08 / X09: work scheduler lease bookkeeping -----------------------------


def test_old_release_does_not_reset_priority_of_new_user_lease(tmp_path: Path) -> None:
    from pudge.work_scheduler import WorkPriority, WorkScheduler

    scheduler = WorkScheduler(tmp_path)
    first = scheduler.acquire_heavy("bg", foreground_sensitive=False, priority=WorkPriority.BACKGROUND)
    assert first is not None
    real_lock = scheduler._local_lock
    acquired: list = []

    class RacingLock:
        def acquire(self, *args, **kwargs):
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            real_lock.release()
            if not acquired:
                acquired.append(
                    scheduler.acquire_heavy("user", foreground_sensitive=False, priority=WorkPriority.USER)
                )

    scheduler._local_lock = RacingLock()
    first.release()

    assert acquired and acquired[0] is not None
    assert scheduler._active_priority == WorkPriority.USER
    assert scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND)
    acquired[0].release()
    assert scheduler._active_priority is None


def test_acquire_heavy_releases_locks_when_second_admission_check_raises(tmp_path: Path, monkeypatch) -> None:
    from pudge.work_scheduler import WorkScheduler

    scheduler = WorkScheduler(tmp_path)
    calls = {"n": 0}

    def flaky(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("probe failed")
        return None

    monkeypatch.setattr(scheduler, "background_wait_reason", flaky)
    with pytest.raises(RuntimeError):
        scheduler.acquire_heavy("job", blocking=True)

    assert scheduler._waiters == []
    assert scheduler._local_lock.acquire(blocking=False)
    scheduler._local_lock.release()
    lease = scheduler.acquire_heavy("job2")
    assert lease is not None
    lease.release()


# --- DISK: enforce_disk_limit honours identity-repair protection -------------


def _manager(tmp_path: Path):
    from pudge.manager import AnimeManager

    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir.mkdir(parents=True)
    return AnimeManager(cfg)


class _FakeClient:
    def __init__(self, deleted: list[str]) -> None:
        self.deleted = deleted

    def delete(self, torrent_hash, delete_files=False):
        self.deleted.append(torrent_hash)

    def close(self):
        pass


@pytest.mark.parametrize("protected", [True, False])
def test_enforce_disk_limit_skips_identity_repair_protected_file(tmp_path: Path, monkeypatch, protected: bool) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "Show" / "ep1.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x" * 4096)
    torrent_hash = "a" * 40
    manager.db.upsert_episode(
        LibraryEpisode(media_id=1, title="Show", episode=1, video_path=video, torrent_hash=torrent_hash)
    )
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET state='watched', watched_at=? WHERE video_path=?",
            (time.time() - 86400, str(video)),
        )
    until = time.time() + (3600 if protected else -3600)
    manager.db.set_state(
        "release_identity:" + torrent_hash,
        json.dumps({"video_path": str(video), "cleanup_protected_until": until}),
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        manager, "storage_status",
        lambda: {"over_limit": True, "used_bytes": 4096, "limit_bytes": 1},
    )
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)
    monkeypatch.setattr(manager, "qbt_client", lambda: _FakeClient(deleted))
    monkeypatch.setattr(manager.db, "episode_count_for_torrent", lambda _h: 1)

    count = manager.enforce_disk_limit()

    if protected:
        assert count == 0 and deleted == []
        assert video.exists()
        assert any(item.video_path == video for item in manager.db.episodes())
    else:
        assert count == 1 and deleted == [torrent_hash]


def test_cleanup_and_disk_limit_share_protection_predicate(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = tmp_path / "v.mkv"
    manager.db.set_state(
        "release_identity:abc",
        json.dumps({"video_path": str(video), "cleanup_protected_until": time.time() + 60}),
    )
    assert manager._identity_repair_protected("ABC", video)
    assert not manager._identity_repair_protected("abc", tmp_path / "other.mkv")
    assert not manager._identity_repair_protected("abc", video, now=time.time() + 120)
