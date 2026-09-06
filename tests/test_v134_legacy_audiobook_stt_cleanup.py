from __future__ import annotations

import signal
from types import SimpleNamespace

import pudge.audiobooks as audiobooks


def test_legacy_whole_book_stt_workers_are_terminated_but_chunk_workers_survive(monkeypatch):
    own_uid = 501
    ps_text = "\n".join(
        [
            f"{own_uid} 72995 1 /opt/homebrew/bin/python -m pudge.subtitles.stt_worker --words /books/old.m4b /Users/me/Library/Caches/pudge/audiobook-transcripts/.abc-work/file-0001.json mlx-community/whisper-tiny /tmp/file-0001.progress.json",
            f"{own_uid} 74760 123 /opt/homebrew/bin/python -m pudge.subtitles.stt_worker --words /tmp/chunk-00001.flac /Users/me/Library/Caches/pudge/audiobook-transcripts/abc/chunk-00001.json mlx-community/whisper-tiny /tmp/chunk-00001.progress.json",
            f"{own_uid + 1} 80000 1 /opt/homebrew/bin/python -m pudge.subtitles.stt_worker --words /books/other.m4b /tmp/file-0002.json model /tmp/p.json",
            f"{own_uid} 90000 1 /opt/homebrew/bin/python -m something.else --words /books/x.m4b /tmp/file-0003.json model /tmp/p.json",
        ]
    )
    monkeypatch.setattr(audiobooks.os, "name", "posix")
    monkeypatch.setattr(audiobooks.os, "getuid", lambda: own_uid)
    monkeypatch.setattr(audiobooks.os, "getpid", lambda: 12345)
    monkeypatch.setattr(
        audiobooks.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=ps_text),
    )
    calls: list[tuple[int, int]] = []
    alive = {72995}

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if sig == signal.SIGTERM:
            return
        if sig == 0:
            if pid in alive:
                return
            raise ProcessLookupError
        if sig == signal.SIGKILL:
            alive.discard(pid)

    monkeypatch.setattr(audiobooks.os, "kill", fake_kill)
    monkeypatch.setattr(audiobooks.time, "sleep", lambda _: None)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(audiobooks.time, "monotonic", lambda: next(ticks, 1.0))

    terminated = audiobooks.terminate_legacy_audiobook_stt_workers(grace_seconds=0.1)

    assert terminated == [72995]
    assert (72995, signal.SIGTERM) in calls
    assert (72995, signal.SIGKILL) in calls
    assert all(pid != 74760 for pid, _ in calls)
    assert all(pid != 80000 for pid, _ in calls)


def test_cleanup_is_noop_when_ps_fails(monkeypatch):
    monkeypatch.setattr(audiobooks.os, "name", "posix")
    monkeypatch.setattr(
        audiobooks.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert audiobooks.terminate_legacy_audiobook_stt_workers() == []
