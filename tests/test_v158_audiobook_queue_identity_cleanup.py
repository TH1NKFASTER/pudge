from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import (
    AudiobookService,
    audiobook_series_path_matches,
    managed_audiobook_series_conflict,
)
from pudge.database import Database
from pudge.work_scheduler import WorkPriority


def _service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "library.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
        cover_cache_dir=tmp_path / "covers",
    )


def _put_audio(service: AudiobookService, path: Path, title: str, duration: float = 60.0) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    book = service._upsert(
        path=path,
        title=title,
        duration=duration,
        files=[{
            "index": 0,
            "path": str(path),
            "title": path.stem,
            "duration": duration,
            "start": 0.0,
            "end": duration,
        }],
        chapters=[{
            "index": 0,
            "title": path.stem,
            "start": 0.0,
            "end": duration,
        }],
    )
    return int(book["id"])


def test_strict_audiobook_series_identity_rejects_wolf_and_parchment() -> None:
    assert audiobook_series_path_matches("狼と香辛料II～完全版オーディオブック", "狼と香辛料")
    assert audiobook_series_path_matches("[支倉 凍砂]/狼と香辛料/[02] 狼と香辛料II [ASIN].m4b", "狼と香辛料")
    assert not audiobook_series_path_matches("[支倉 凍砂] 新説 狼と香辛料/[02] 新説 狼と香辛料 狼と羊皮紙II.m4b", "狼と香辛料")
    assert not audiobook_series_path_matches("狼と香辛料 狼と羊皮紙II.m4b", "狼と香辛料")


def test_managed_download_detects_conflicting_spinoff_but_not_generated_prefix() -> None:
    wrong = "/tmp/Pudge Audiobooks/狼と香辛料/Volume 02/Audiobook Collection/[支倉 凍砂] 新説 狼と香辛料/[02] 新説 狼と香辛料 狼と羊皮紙II.m4b"
    conflict = managed_audiobook_series_conflict([wrong])
    assert conflict is not None
    assert conflict["expected_series"] == "狼と香辛料"
    assert "新説 狼と香辛料" in conflict["source"]

    correct = "/tmp/Pudge Audiobooks/狼と香辛料/Volume 02/狼と香辛料II～完全版オーディオブック.m4b"
    assert managed_audiobook_series_conflict([correct]) is None


def test_resume_only_queues_linked_audiobooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    linked = _put_audio(service, tmp_path / "linked.m4b", "Linked")
    unlinked = _put_audio(service, tmp_path / "unlinked.m4b", "Unlinked")
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            (9001, linked, "chapter", 1.0, 1.0),
        )
    queued: list[tuple[int, int]] = []
    aligned: list[tuple[int, int]] = []
    monkeypatch.setattr(service, "prepare_transcription", lambda aid, **kw: queued.append((int(aid), int(kw.get("priority", 999)))) or {"status": "queued"})
    monkeypatch.setattr(service, "prepare_alignment", lambda lid, **kw: aligned.append((int(lid), int(kw.get("priority", 999)))) or {"status": "queued"})

    assert service.resume_pending_transcriptions() == 1
    assert queued == [(linked, int(WorkPriority.BACKGROUND))]
    assert aligned == [(9001, int(WorkPriority.BACKGROUND))]
    assert unlinked not in [row[0] for row in queued]


def test_managed_wrong_link_is_unlinked_without_deleting_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    source = tmp_path / "Pudge Audiobooks" / "狼と香辛料" / "Volume 02" / "Audiobook Collection" / "[支倉 凍砂] 新説 狼と香辛料" / "[02] 新説 狼と香辛料 狼と羊皮紙II.m4b"
    audiobook_id = _put_audio(service, source, "狼と香辛料II")
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            (180, audiobook_id, "chapter", 1.0, 1.0),
        )
    cancelled: list[int] = []
    monkeypatch.setattr(service, "cancel_transcription", lambda aid: cancelled.append(int(aid)) or {"status": "cancelled"})

    removed = service._drop_invalid_managed_links()
    assert removed and removed[0]["ln_book_id"] == 180
    with service.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM reading_audio_links WHERE ln_book_id=180").fetchone() is None
        assert conn.execute("SELECT 1 FROM audiobooks WHERE id=?", (audiobook_id,)).fetchone() is not None
    assert source.exists()
    assert cancelled == [audiobook_id]


def test_user_transcription_priority_moves_ahead_of_background_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_ensure_transcription_dispatcher", lambda: None)
    first = _put_audio(service, tmp_path / "first.m4b", "First")
    second = _put_audio(service, tmp_path / "second.m4b", "Second")
    service.prepare_transcription(first, priority=WorkPriority.BACKGROUND)
    service.prepare_transcription(second, priority=WorkPriority.BACKGROUND)
    assert service._transcription_queue == [first, second]

    service.prepare_transcription(second, priority=WorkPriority.USER)
    assert service._transcription_queue == [second, first]
    assert service.transcription_status(second)["queue_position"] == 1
    assert service.transcription_status(second)["priority"] == int(WorkPriority.USER)


def test_foreground_marker_drops_reused_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pudge.foreground as foreground

    marker = tmp_path / "foreground-work.json"
    marker.write_text(json.dumps({
        "pid": 123,
        "created_at": __import__("time").time(),
        "command": "python -m pudge old-video.mkv",
        "video": "old-video.mkv",
    }))
    monkeypatch.setattr(foreground, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(foreground, "_pid_command", lambda _pid: "unrelated reused process")
    assert foreground.foreground_active(tmp_path) is False
    assert not marker.exists()


def test_orphan_player_cleanup_targets_only_dead_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pudge.audiobooks as audiobooks

    ipc_root = tmp_path / "audiobook-ipc"
    ipc_root.mkdir()
    dead_sock = ipc_root / "book-75-75524.sock"
    live_sock = ipc_root / "book-76-12345.sock"
    dead_sock.touch(); live_sock.touch()
    stdout = "\n".join([
        f"501 76606 1 /opt/homebrew/bin/mpv --no-video --input-ipc-server={dead_sock} book.m4b",
        f"501 76607 1 /opt/homebrew/bin/mpv --no-video --input-ipc-server={live_sock} book.m4b",
    ])
    monkeypatch.setattr(audiobooks.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout))
    monkeypatch.setattr(audiobooks.os, "getuid", lambda: 501)
    monkeypatch.setattr(audiobooks.os, "getpid", lambda: 99999)
    signals: list[tuple[int, int]] = []
    checks = {75524: False, 12345: True, 76606: True, 76607: True}

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid == 75524:
                raise ProcessLookupError
            if pid == 76606 and any(item[0] == 76606 for item in signals):
                raise ProcessLookupError
            return
        signals.append((pid, sig))

    monkeypatch.setattr(audiobooks.os, "kill", fake_kill)
    killed = audiobooks.terminate_orphaned_audiobook_players(tmp_path, grace_seconds=0.01)
    assert killed == [76606]
    assert any(pid == 76606 for pid, _sig in signals)
    assert all(pid != 76607 for pid, _sig in signals)
    assert not dead_sock.exists()
    assert live_sock.exists()


def test_queue_wait_reason_is_visible_in_both_audio_surfaces() -> None:
    root = Path(__file__).resolve().parents[1]
    media = (root / "pudge/web/media.js").read_text(encoding="utf-8")
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "waiting for playback to finish" in media
    assert "ждёт окончания воспроизведения" in media
    assert "Audio analysis · waiting for playback" in html
