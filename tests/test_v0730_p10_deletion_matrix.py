"""P10: deleting library content while it is being consumed (synthetic data only)."""
from __future__ import annotations

import io
import json
import logging
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.consumption import ConsumptionLedger
from pudge.content_review import ContentReviewService
from pudge.database import Database
from pudge.job_center import JobCenter
from pudge.light_novels import LightNovelService
from pudge.manga import MangaService
from pudge.review_gate import ContentReviewIdentity
from pudge.web_app import WebAppApi

ROOT = Path(__file__).resolve().parents[1]


def _cbz(path: Path) -> Path:
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(data, format="PNG")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", data.getvalue())
    return path


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _manga_api(tmp_path: Path) -> tuple[WebAppApi, Database]:
    cfg = _cfg(tmp_path)
    db = Database(cfg.library.database_path)
    api = object.__new__(WebAppApi)
    api.config = cfg
    api.manager = SimpleNamespace(db=db)
    api.manga = MangaService(db, cache_dir=cfg.paths.cache_dir)
    api.consumption = ConsumptionLedger(db)
    api.job_center = JobCenter(db)
    api.logger = logging.getLogger("p10-test")
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_ocr_run_lock = threading.Lock()
    api._manga_book_ocr_threads = {}
    api._manga_book_ocr_state = {}
    api._manga_ocr_cancel_events = {}
    api._manga_ocr_job_ids = {}
    return api, db


class _Player:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.terminated = True


def _paired(tmp_path: Path) -> tuple[AudiobookService, LightNovelService, Database, int, int]:
    cfg = _cfg(tmp_path)
    novels = LightNovelService(cfg)
    source = tmp_path / "book.txt"
    source.write_text("第一章\n架空の物語です。", encoding="utf-8")
    ln_id = int(novels.import_file(source)["id"])
    db = Database(cfg.library.database_path)
    audio = AudiobookService(db, ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv", cache_dir=cfg.paths.cache_dir)
    audio_id = _put_audio(audio, tmp_path / "book.m4b", "Book")
    audio.link_light_novel(ln_id, audio_id, prepare_alignment=False)
    return audio, novels, db, ln_id, audio_id


def _put_audio(audio: AudiobookService, path: Path, title: str) -> int:
    path.write_bytes(f"audio-{title}".encode())
    return int(audio._upsert(
        path=path, title=title, duration=120.0,
        files=[{"index": 0, "path": str(path), "title": title, "duration": 120.0, "start": 0.0, "end": 120.0}],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 120.0}],
    )["id"])


def _ln_api(audio: AudiobookService, novels: LightNovelService, db: Database) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.audiobooks = audio
    api.light_novels = novels
    api.manager = SimpleNamespace(db=db)
    api.consumption = ConsumptionLedger(db)
    api._irodori_tts_lock = threading.Lock()
    api._audiobook_generation_controls = {}
    api.logger = logging.getLogger("p10-test")
    return api


# Row 1: manga deleted during OCR / content review -------------------------------------------

def test_manga_delete_cancels_running_ocr_and_restart_does_not_resume_it(tmp_path, monkeypatch):
    api, db = _manga_api(tmp_path)
    book_id = int(api.manga.import_file(_cbz(tmp_path / "Vol 1.cbz"))["id"])
    monkeypatch.setattr(api, "_manga_ocr_backend_available", lambda **_k: True)
    entered, release, seen_cancel = threading.Event(), threading.Event(), []

    def late_ocr(_book_id, *, progress=None, cancelled=None):
        entered.set()
        release.wait(3)
        seen_cancel.append(bool(cancelled and cancelled()))
        # A late worker ignores cancellation and reports a complete result.
        return {"complete": True, "cached_pages": 1, "total_pages": 1, "errors": []}

    monkeypatch.setattr(api.manga, "ocr_book", late_ocr)
    api.start_manga_ocr_book(book_id)
    job_id = api._manga_ocr_job_ids[book_id]
    assert entered.wait(2)

    assert api.manga_remove_books([book_id]) == {"removed": 1}
    release.set()
    api._manga_book_ocr_threads[book_id].join(3)

    assert seen_cancel == [True]
    assert book_id not in api._manga_book_ocr_state
    assert book_id not in api._manga_ocr_job_ids
    assert api.job_center.get(job_id)["state"] == "cancelled"
    # Row 5: restart never offers a dead OCR job for the deleted volume.
    assert JobCenter(db).resume_candidates(kind="ocr") == []
    assert JobCenter(db).get(job_id)["state"] == "cancelled"


def test_late_ocr_publish_cannot_recreate_deleted_or_reimported_manga(tmp_path):
    api, db = _manga_api(tmp_path)
    source = _cbz(tmp_path / "Vol 1.cbz")
    book_id = int(api.manga.import_file(source)["id"])
    fingerprint, generation, _revision = api.manga._ocr_context(book_id)
    api.manga_remove_books([book_id])
    update = [(0, [{"text": "架空", "box": [0, 0, 1, 1]}], "ready", "", False)]
    with pytest.raises(KeyError):
        api.manga._commit_ocr_page_updates(book_id, source_fingerprint=fingerprint, generation=generation, updates=update)
    # Re-import the same file; if SQLite reuses the id, the stale generation
    # captured by the old worker must still be rejected.
    again = int(api.manga.import_file(source)["id"])
    try:
        published = api.manga._commit_ocr_page_updates(
            book_id, source_fingerprint=fingerprint, generation=generation, updates=update)
    except KeyError:
        published = False
    assert published is False
    assert again == book_id or published is False
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM manga_ocr_cache").fetchone()[0] == 0


def test_manga_delete_clears_content_review_sessions_and_unknown_words(tmp_path, monkeypatch):
    api, db = _manga_api(tmp_path)
    book_id = int(api.manga.import_file(_cbz(tmp_path / "Vol 1.cbz"))["id"])
    keep_id = int(api.manga.import_file(_cbz(tmp_path / "Vol 2.cbz"))["id"])
    api.config.ui.review_gate_manga_enabled = True
    api.config.ui.review_gate_manga_all_due = True
    api.light_novels = SimpleNamespace(
        settings=lambda: SimpleNamespace(study_backend="jiten", jiten_api_key="synthetic-key"))
    gate = ContentReviewService(api)
    api._content_review_service = gate
    gate.source = lambda kind, bid, part, *, prepare: (
        ContentReviewIdentity(kind, f"content-{bid}", "rev", part), "架空")
    monkeypatch.setattr("pudge.content_review.build_text_due_review_cards", lambda *a: {
        "cards": [{"wordId": 3, "readingIndex": 0}], "unknown_words": [{"text": "架空"}]})
    doomed = gate.begin("manga", book_id, 0)
    kept = gate.begin("manga", keep_id, 0)
    doomed_keys = json.loads(db.get_state(f"content_review_book_keys:v1:manga:{book_id}", "[]"))
    assert any(key.startswith("manga_unknown_words:") for key in doomed_keys)

    api.manga_remove_books([book_id])

    assert doomed["token"] not in gate.sessions and kept["token"] in gate.sessions
    assert all(db.get_state(key, "") == "" for key in doomed_keys)
    assert db.get_state(f"content_review_book_keys:v1:manga:{book_id}", "") == ""
    assert db.get_state(f"content_review_book_keys:v1:manga:{keep_id}", "") != ""
    with pytest.raises(ValueError, match="expired"):
        gate.review(doomed["token"], 3, 0, "good", "late")
    # A scan that finishes after deletion must not open a new session.
    with pytest.raises(ValueError, match="removed"):
        gate.begin("manga", book_id, 0)
    assert db.get_state(f"content_review_book_keys:v1:manga:{book_id}", "") == ""


def test_manga_delete_detaches_history_and_without_review_service_cleans_persisted_keys(tmp_path):
    api, db = _manga_api(tmp_path)
    book_id = int(api.manga.import_file(_cbz(tmp_path / "Vol 1.cbz"))["id"])
    media = api.consumption.manga_media(book_id)
    db.set_state("manga_unknown_words:synthetic", "[]")
    db.set_state(f"content_review_book_keys:v1:manga:{book_id}", json.dumps(["manga_unknown_words:synthetic"]))
    api.manga_remove_books([book_id])
    assert db.get_state("manga_unknown_words:synthetic", "") == ""
    with db.connect() as conn:
        row = conn.execute("SELECT current_library_id,deleted_at FROM consumption_media WHERE media_uuid=?",
                           (media.media_uuid,)).fetchone()
    assert row["current_library_id"] == "" and row["deleted_at"] is not None


# Row 2: paused audiobook deleted ------------------------------------------------------------

def test_paused_audiobook_delete_stops_player_saves_once_and_leaves_no_sidebar_block(tmp_path, monkeypatch):
    audio, _novels, db, ln_id, audio_id = _paired(tmp_path)
    player = _Player()
    audio._players[audio_id] = player
    audio._last_positions[audio_id] = 42.0
    audio._review_contexts[audio_id] = {"origin": "ln", "consumer": "sidebar", "ln_book_id": ln_id,
                                        "chapter": 0, "pending_chapter": 1, "ranges": []}
    saved = []
    original = audio.set_position
    monkeypatch.setattr(audio, "set_position", lambda bid, pos: (saved.append((bid, pos)), original(bid, pos)))
    assert audio.sidebar_state()["books"][0]["player_running"] is True

    result = audio.delete_many([audio_id])

    assert result == {"ok": True, "removed": [audio_id], "errors": []}
    assert player.terminated and saved == [(audio_id, 42.0)]
    assert audio.sidebar_state() == {"books": []}
    assert audio_id not in audio._review_contexts and audio_id not in audio._players
    assert audio.state(queue_missing=False)["books"] == []


def test_sidebar_state_skips_player_whose_book_row_is_gone(tmp_path):
    audio, _novels, db, _ln_id, audio_id = _paired(tmp_path)
    audio._players[audio_id] = _Player()
    with db.connect() as conn:
        conn.execute("DELETE FROM audiobooks WHERE id=?", (audio_id,))
    assert audio.sidebar_state() == {"books": []}


# Row 3: linked audiobook deleted while the LN is open --------------------------------------

def test_deleting_linked_audiobook_clears_ln_paired_panel_and_keeps_ln(tmp_path):
    audio, novels, db, ln_id, audio_id = _paired(tmp_path)
    api = _ln_api(audio, novels, db)
    assert audio.paired_state(ln_id)["linked"] is True
    api.audiobook_delete_many([audio_id])
    assert api.light_novel_paired_state(ln_id) == {"linked": False, "playing": False}
    assert novels.book(ln_id)["id"] == ln_id


# Row 4: LN deleted while its audiobook plays ------------------------------------------------

def test_deleting_ln_disconnects_reader_review_but_keeps_audio(tmp_path):
    audio, novels, db, ln_id, audio_id = _paired(tmp_path)
    api = _ln_api(audio, novels, db)
    player = _Player()
    audio._players[audio_id] = player
    audio._review_contexts[audio_id] = {"origin": "ln", "consumer": "ln", "ln_book_id": ln_id, "chapter": 0,
                                        "pending_chapter": 2,
                                        "ranges": [{"chapter_index": 3, "start": 10.0}]}
    paused = []
    audio.set_paused = lambda *a: paused.append(a)
    audio.seek_to = lambda *a: None

    assert api.light_novel_delete(ln_id)["ok"] is True

    assert not player.terminated and audio.is_playing(audio_id)
    assert audio_id not in audio._review_contexts
    assert api.content_review_boundary_done(audio_id, 2) == {"ok": False}
    audio._check_reader_review_boundary(audio_id, 50.0)
    assert paused == []
    assert audio.book(audio_id)["linked_light_novel"] is None
    assert audio.sidebar_state()["books"][0]["id"] == audio_id


# Row 5: restart after audiobook delete ------------------------------------------------------

def test_restart_after_audiobook_delete_restores_no_job_for_deleted_book(tmp_path, monkeypatch):
    audio, _novels, db, _ln_id, audio_id = _paired(tmp_path)
    audio.delete_many([audio_id])
    restarted = AudiobookService(Database(db.path), ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv",
                                 cache_dir=tmp_path / "cache")
    monkeypatch.setattr(restarted, "prepare_transcription", lambda *a, **k: pytest.fail("dead STT job resumed"))
    assert restarted.resume_pending_transcriptions() == 0
    assert restarted.sidebar_state() == {"books": []}


# Row 6: mass delete with several active owners ---------------------------------------------

def test_mass_manga_delete_cancels_every_active_ocr_owner(tmp_path):
    api, _db = _manga_api(tmp_path)
    ids = [int(api.manga.import_file(_cbz(tmp_path / f"Vol {i}.cbz"))["id"]) for i in range(3)]
    events = {}
    for book_id in ids[:2]:
        events[book_id] = threading.Event()
        api._manga_ocr_cancel_events[book_id] = events[book_id]
        api._manga_ocr_job_ids[book_id] = api.job_center.start("ocr", f"OCR {book_id}", resumable=True)
        api._manga_book_ocr_state[book_id] = {"state": "running", "running": True}
    jobs = dict(api._manga_ocr_job_ids)
    assert api.manga_remove_books(ids) == {"removed": 3}
    assert all(event.is_set() for event in events.values())
    assert api._manga_book_ocr_state == {} and api._manga_ocr_job_ids == {}
    assert {api.job_center.get(job)["state"] for job in jobs.values()} == {"cancelled"}


def test_mass_audiobook_delete_one_failure_keeps_others_cleaned_and_history_linked(tmp_path, monkeypatch):
    audio, novels, db, _ln_id, first = _paired(tmp_path)
    second = _put_audio(audio, tmp_path / "b2.m4b", "B2")
    third = _put_audio(audio, tmp_path / "b3.m4b", "B3")
    api = _ln_api(audio, novels, db)
    players = {book_id: _Player() for book_id in (first, second, third)}
    audio._players.update(players)
    for book_id in players:
        audio._review_contexts[book_id] = {"origin": "audio", "consumer": "sidebar"}
    medias = {book_id: api.consumption.audiobook_media(book_id) for book_id in players}
    original_stop = audio.stop

    def stop(book_id):
        if book_id == second:
            raise RuntimeError("synthetic stop failure")
        return original_stop(book_id)

    monkeypatch.setattr(audio, "stop", stop)
    result = api.audiobook_delete_many([first, second, third])

    assert result["removed"] == [first, third]
    assert [row["book_id"] for row in result["errors"]] == [second]
    assert players[first].terminated and players[third].terminated
    assert first not in audio._review_contexts and third not in audio._review_contexts
    with db.connect() as conn:
        current = {
            book_id: conn.execute("SELECT current_library_id FROM consumption_media WHERE media_uuid=?",
                                  (media.media_uuid,)).fetchone()[0]
            for book_id, media in medias.items()
        }
    assert current == {first: "", second: str(second), third: ""}


# Row 7: repeat delete is a no-op ------------------------------------------------------------

def test_repeat_delete_is_idempotent_for_every_library(tmp_path):
    api, _db = _manga_api(tmp_path / "manga")
    book_id = int(api.manga.import_file(_cbz((tmp_path / "manga").joinpath("Vol.cbz")))["id"])
    assert api.manga_remove_books([book_id]) == {"removed": 1}
    assert api.manga_remove_books([book_id]) == {"removed": 0}

    audio, novels, db, ln_id, audio_id = _paired(tmp_path / "reading")
    ln_api = _ln_api(audio, novels, db)
    assert ln_api.audiobook_delete_many([audio_id])["removed"] == [audio_id]
    assert ln_api.audiobook_delete_many([audio_id]) == {"ok": True, "removed": [], "errors": []}
    assert ln_api.light_novel_delete(ln_id)["ok"] is True
    again = ln_api.light_novel_delete(ln_id)
    assert again["ok"] is True and again.get("missing") is True


# Frontend: the sidebar/float drop a book that is absent from the next state ---------------

def test_sidebar_companion_hides_activity_when_book_disappears() -> None:
    js = (ROOT / "pudge/web/sidebar_companion.js").read_text(encoding="utf-8")
    assert "return books.find(book => book.playing)\n      || books.find(book => book.player_running)\n      || null;" in js
    assert "if (!book) {\n      host.hidden = true;" in js
