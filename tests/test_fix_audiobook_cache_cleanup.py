"""Audiobook caches shrink: no chunk checkpoints after a transcript, nothing left of a deleted book (behaviour).

* The ``.<fingerprint>-chunks/`` folder (one JSON per 5-minute chunk plus the
  worker's log) stayed next to every finished transcript, duplicating it.
* Deleting an audiobook left its alignments, reports and chapter-start caches in
  Caches forever (they are excluded from eviction). Now they go with the book, unless
  another book has the same audio. Its transcript and speech map stay (adding the same
  files again reuses them) until no book used them for half a year
  (test_fix_audiobook_transcripts_kept_and_aged.py).
* Regenerating a generated audiobook deletes the old one's folder only when it still
  is a generated folder.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.web_app as web_app_module
from pudge.audiobooks import AudiobookService
from pudge.database import Database
from test_v093_audiobook_generation_lifecycle import _api, _Client
from test_v224_alignment_vn_generation import _paired_audio


def _add_book(service: AudiobookService, path: Path, content: bytes) -> int:
    path.write_bytes(content)
    book = service._upsert(
        path=path,
        title=path.stem,
        duration=120.0,
        files=[
            {"index": 0, "path": str(path), "title": path.stem, "duration": 120.0, "start": 0.0, "end": 120.0}
        ],
        chapters=[{"index": 0, "title": "1", "start": 0.0, "end": 120.0}],
    )
    return int(book["id"])


def _write_caches(service: AudiobookService, audiobook_id: int, ln_book_id: int | None = None) -> list[Path]:
    transcript = service._transcript_path(audiobook_id)
    checkpoints = service._transcription_checkpoint_dir(transcript)
    activity = service._activity_path(audiobook_id)
    files = [transcript, checkpoints / "chunk-00001.json", activity]
    if ln_book_id is not None:
        alignment = service._alignment_path(ln_book_id, audiobook_id)
        files += [
            alignment,
            service._alignment_report_path(ln_book_id, audiobook_id),
            service._chapter_start_precision_cache_path(alignment, 3),
        ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    return files


def test_deleting_an_audiobook_removes_its_alignment_caches(tmp_path: Path) -> None:
    audio, ln_id, audio_id = _paired_audio(tmp_path)
    other = _add_book(audio, tmp_path / "other.m4b", b"other audio")
    written = _write_caches(audio, audio_id, ln_id)
    speech, alignment = written[:3], written[3:]
    kept = _write_caches(audio, other)
    unrelated = audio.cache_dir / "audiobook-transcripts" / "someone-else.json"
    unrelated.write_text("{}", encoding="utf-8")

    assert audio.delete(audio_id)["ok"]
    assert [path for path in alignment if path.exists()] == []
    # Speech-to-text and the speech map are named by the audio: kept for a re-added copy.
    assert all(path.exists() for path in speech)
    assert all(path.exists() for path in kept) and unrelated.exists()
    # The audio files themselves are kept (delete_files=False).
    assert (tmp_path / "book.m4b").is_file()


def test_alignment_caches_shared_with_another_copy_of_the_same_audio_stay(tmp_path: Path) -> None:
    audio, ln_id, first = _paired_audio(tmp_path)
    second = _add_book(audio, tmp_path / "second copy.m4b", (tmp_path / "book.m4b").read_bytes())
    alignment = audio._alignment_path(ln_id, first)
    shared = _write_caches(audio, first, ln_id)[3:]
    audio.unlink_light_novel(ln_id)
    audio.link_light_novel(ln_id, second, prepare_alignment=False)
    assert audio._alignment_path(ln_id, second) == alignment

    audio.delete(first)
    assert all(path.exists() for path in shared)
    audio.delete(second)
    assert not any(path.exists() for path in shared)


def test_regenerate_never_deletes_a_folder_that_is_no_generated_audiobook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
    book_id, old_id = 8, 88
    staging = api._audiobook_generation_root() / "Book [Regenerating 8-test]"
    monkeypatch.setattr(
        api,
        "_audiobook_generation_runtime",
        lambda *_a, **_k: {
            "provider": "external",
            "base_url": "https://tts.invalid/v1",
            "api_key": "",
            "model": "tts",
            "voice": "voice",
            "caption": "",
            "speed": 1.0,
        },
    )
    monkeypatch.setattr(
        api.light_novels,
        "tts_source",
        lambda _id: {"title": "Book", "chapters": [{"title": "One", "text": "本文です。"}]},
    )
    monkeypatch.setattr(web_app_module.httpx, "Client", _Client)
    monkeypatch.setattr(api, "_merge_audiobook_parts", lambda _parts, output: output.write_bytes(b"M" * 4096))
    monkeypatch.setattr(api.audiobooks, "import_folder", lambda *_a, **_k: {"id": 99})
    monkeypatch.setattr(api.audiobooks, "link_light_novel", lambda *_a, **_k: {"ok": True})
    deleted: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        api.audiobooks,
        "delete",
        lambda audiobook_id, delete_files=False: deleted.append((int(audiobook_id), bool(delete_files))),
    )
    # The id from the manifest now names the user's own audiobook folder.
    users_folder = tmp_path / "My Audiobooks" / "Book"
    users_folder.mkdir(parents=True)
    (users_folder / "01.mp3").write_bytes(b"audio")
    monkeypatch.setattr(
        api.audiobooks, "book", lambda audiobook_id, **_k: {"id": audiobook_id, "path": str(users_folder)}
    )

    job_id = api.job_center.start("audiobook_generation", "regenerate", total=5)
    with api._irodori_tts_lock:
        api._irodori_tts_job_ids[book_id] = job_id
        api._audiobook_generation_start_params[book_id] = {
            "mode": "regenerate",
            "output_dir": str(staging),
            "old_audiobook_id": old_id,
        }
    api._irodori_tts_worker(book_id, job_id)

    assert api.job_center.get(job_id)["state"] == "succeeded"
    assert deleted == []
    assert (users_folder / "01.mp3").is_file()


def test_generated_folder_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    root = api._audiobook_generation_root()
    generated = root / "Book [Generated 4]"
    generated.mkdir(parents=True)
    (generated / ".pudge-audiobook-profile.json").write_text("{}", encoding="utf-8")
    plain = root / "Copied by hand"
    plain.mkdir()
    (root / ".pudge-audiobook-profile.json").write_text("{}", encoding="utf-8")
    assert api._generated_audiobook_folder(generated)
    assert not api._generated_audiobook_folder(plain)
    assert not api._generated_audiobook_folder(root)
    assert not api._generated_audiobook_folder(tmp_path / "elsewhere")


def test_transcript_success_removes_its_chunk_checkpoints(tmp_path: Path) -> None:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    source = tmp_path / "long.m4b"
    source.write_bytes(b"long")
    book_id = int(
        service._upsert(
            path=source,
            title="long",
            duration=650.0,
            files=[
                {"index": 0, "path": str(source), "title": "l", "duration": 650.0, "start": 0.0, "end": 650.0}
            ],
            chapters=[{"index": 0, "title": "l", "start": 0.0, "end": 650.0}],
        )["id"]
    )
    service._extract_stt_chunk = lambda _s, destination, **_k: destination.write_bytes(b"chunk")  # type: ignore[method-assign]
    service._resolved_ffmpeg = lambda: "/usr/bin/false"  # type: ignore[method-assign]

    class Worker:
        process = SimpleNamespace(poll=lambda: None)

        def __init__(self) -> None:
            self.answers: list = []

        def submit(self, request):
            Path(request["output"]).write_text(json.dumps({"segments": []}), encoding="utf-8")
            self.answers.append({"id": request["id"], "ok": True})
            return True

        def answer(self, _timeout):
            return self.answers.pop(0) if self.answers else False

        def stderr_tail(self):
            return ""

        def stop(self, timeout=15.0):
            pass

        def kill(self):
            pass

    service._start_stt_worker = lambda _env, _path: Worker()  # type: ignore[method-assign]
    service._transcription_cancel_events[book_id] = threading.Event()
    transcript = service._transcript_path(book_id)
    service._transcribe_worker(book_id, transcript, threading.Event())
    assert transcript.is_file()
    assert not service._transcription_checkpoint_dir(transcript).exists()
