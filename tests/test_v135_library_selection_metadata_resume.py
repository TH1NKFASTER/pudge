from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from pudge.audiobooks import AudiobookService, _audiobook_title_match_score, _audiobook_title_key, _audiobook_series_title
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService

ROOT = Path(__file__).resolve().parents[1]


def _audio(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def _ln(tmp_path: Path) -> LightNovelService:
    cfg=AppConfig(); cfg.library.database_path=tmp_path/"db.sqlite3"; cfg.library.root_dir=tmp_path/"library"; cfg.paths.cache_dir=tmp_path/"cache"
    cfg.library.root_dir.mkdir(parents=True,exist_ok=True); cfg.paths.cache_dir.mkdir(parents=True,exist_ok=True)
    return LightNovelService(cfg)


def _put_audio(service: AudiobookService, source: Path, title: str, duration: float=10.0) -> dict:
    source.write_bytes(b"audio")
    return service._upsert(
        path=source,title=title,duration=duration,
        files=[{"index":0,"path":str(source),"title":title,"duration":duration,"start":0.0,"end":duration}],
        chapters=[{"index":0,"title":title,"start":0.0,"end":duration}],
    )


def test_icloud_probe_timeout_imports_placeholder_and_defers_stt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service=_audio(tmp_path); source=tmp_path/"また、同じ夢を見ていた.m4b"; source.write_bytes(b"audio")
    calls=[]
    def timeout_probe(path: Path, *, timeout: float=30.0):
        calls.append(timeout)
        raise subprocess.TimeoutExpired(["ffprobe",str(path)],timeout)
    queued=[]
    monkeypatch.setattr(service,"_probe",timeout_probe)
    monkeypatch.setattr(service,"_queue_metadata_refresh",lambda book_id,path: queued.append((book_id,path)))
    monkeypatch.setattr(service,"prepare_transcription",lambda *_a,**_k: pytest.fail("STT must wait for metadata"))
    book=service.import_file(source)
    assert calls == [3.0]
    assert book["duration"] == 0.0
    assert queued and queued[0][1] == source.resolve()


def test_zero_duration_state_retries_metadata_instead_of_stt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service=_audio(tmp_path); source=tmp_path/"cloud.m4b"; book=_put_audio(service,source,"Cloud",0.0)
    queued=[]
    monkeypatch.setattr(service,"_queue_metadata_refresh",lambda book_id,path: queued.append((book_id,path)))
    monkeypatch.setattr(service,"prepare_transcription",lambda *_a,**_k: pytest.fail("must not queue STT before metadata"))
    state=service.state(queue_missing=True)
    assert state["books"][0]["metadata_pending"] is True
    assert queued == [(int(book["id"]),source.resolve())]


def test_v2_transcript_cache_is_migrated_without_retranscription(tmp_path: Path) -> None:
    service=_audio(tmp_path); source=tmp_path/"wolf.m4b"; book=_put_audio(service,source,"Wolf",60.0); book_id=int(book["id"])
    legacy=service._legacy_transcript_path_v2(book_id); legacy.parent.mkdir(parents=True,exist_ok=True)
    legacy.write_text(json.dumps({"schema":"audiobook-stt-v2","model":service.stt_model,"segments":[{"start":1.0,"end":2.0,"text":"狼"}]}),encoding="utf-8")
    current=service._transcript_path(book_id); assert not current.exists()
    status=service.prepare_transcription(book_id)
    assert status["ready"] is True and status["status"] == "ready"
    payload=json.loads(current.read_text(encoding="utf-8"))
    assert payload["schema"] == "audiobook-stt-v3"
    assert payload["migrated_from"] == "audiobook-stt-v2"
    assert not service._transcription_queue


def test_bulk_delete_avoids_expensive_cancel_and_stop_for_inactive_books(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service=_audio(tmp_path); ids=[]
    for i in range(3): ids.append(int(_put_audio(service,tmp_path/f"b{i}.mp3",f"B{i}")["id"]))
    monkeypatch.setattr(service,"cancel_transcription",lambda *_a,**_k: pytest.fail("inactive STT cancel must not run"))
    monkeypatch.setattr(service,"stop",lambda *_a,**_k: pytest.fail("inactive player stop must not run"))
    result=service.delete_many(ids)
    assert result == {"ok":True,"removed":ids,"errors":[]}
    assert service.state(queue_missing=False)["books"] == []


def test_v135_library_selection_and_furigana_contracts() -> None:
    html=(ROOT/"pudge/web/index.html").read_text(encoding="utf-8")
    media=(ROOT/"pudge/web/media.js").read_text(encoding="utf-8")
    css=(ROOT/"pudge/web/media.css").read_text(encoding="utf-8")
    manga=(ROOT/"pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    study=(ROOT/"pudge/web/reading_tools.css").read_text(encoding="utf-8")

    assert "translateX(calc(-50% - 1em))" in html
    assert "#lnReader:not(.vertical){line-height:calc(var(--ln-line-height,1.9) + .5)}" in html
    assert "Words: " in html and "Jiten · words:" not in html
    assert "Слов: " in html and "Jiten · слов:" not in html
    assert "ui.lnPairedStarted=!!book.paired_audio?.playing" in html
    assert "const startFromReader=desired&&ui.lnPairedStarted!==true;" in html
    assert "if(!forceReaderStart&&state.alignment?.ready&&state.player_running&&state.audiobook_id){" in html

    assert "data-audio-selection-mode" not in media
    assert "data-audio-select-all" not in media
    assert "audiobook-select-toggle" not in media
    assert "details,summary" in media
    assert "window.PudgeAudiobookSelection" in media
    assert "selectAll" in media and "deleteSelected" in media
    assert "pudge-audiobook-selection-changed" in html
    assert "PudgeAudiobookSelection?.clearSelection" in html
    assert "PudgeAudiobookSelection?.selectAll" in html
    assert "PudgeMangaReaderV2?.selectAll" in html and "selectAll:" in manga
    assert ".audiobook-card:hover" in css
    assert "box-shadow:inset" in css
    assert ".pudge-study-head{display:flex;align-items:flex-end" in study
    assert ".pudge-study-head-actions{display:flex;align-items:flex-end" in study


def test_sorted_audiobook_cover_actions_bind_by_card_id_not_render_index() -> None:
    media=(ROOT/"pudge/web/media.js").read_text(encoding="utf-8")
    assert "audiobookById(Number(card?.dataset.audiobookId))" in media
    assert "const book=books[index]" not in media


def test_import_file_keeps_legacy_one_argument_probe_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _audio(tmp_path)
    source = tmp_path / "legacy-probe.mp3"
    source.write_bytes(b"audio")
    calls: list[Path] = []

    def old_probe(path: Path):
        calls.append(path)
        return 42.0, []

    monkeypatch.setattr(service, "_probe", old_probe)
    monkeypatch.setattr(service, "prepare_transcription", lambda *_a, **_k: {"status": "disabled"})
    book = service.import_file(source)
    assert calls == [source.resolve()]
    assert book["duration"] == 42.0
