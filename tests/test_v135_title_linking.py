from __future__ import annotations

import time
from pathlib import Path

import pytest

from pudge.audiobooks import AudiobookService, _audiobook_path_label, _audiobook_series_title, _audiobook_title_key, _audiobook_title_match_score, _audiobook_volume
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService


def _audio(tmp_path: Path) -> AudiobookService:
    return AudiobookService(Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv", cache_dir=tmp_path / "cache")


def _ln(tmp_path: Path) -> LightNovelService:
    cfg=AppConfig(); cfg.library.database_path=tmp_path/"db.sqlite3"; cfg.library.root_dir=tmp_path/"library"; cfg.paths.cache_dir=tmp_path/"cache"
    cfg.library.root_dir.mkdir(parents=True,exist_ok=True); cfg.paths.cache_dir.mkdir(parents=True,exist_ok=True)
    return LightNovelService(cfg)


def test_title_linking_works_without_anilist_for_mata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    novels=_ln(tmp_path); now=time.time()
    conn = novels._connect()
    try:
        conn.execute("INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",("同じ夢を見ていた",str(tmp_path/"mata.epub"),"epub",1,None,now,now))
        ln_id=int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    service=_audio(tmp_path); source=tmp_path/"また、同じ夢を見ていた [audiobook.jp 237969].m4b"; source.write_bytes(b"audio")
    monkeypatch.setattr(service,"_queue_embedded_cover",lambda *_a,**_k: None)
    monkeypatch.setattr(service,"prepare_transcription",lambda *_a,**_k: {"status":"disabled"})
    monkeypatch.setattr(service,"prepare_alignment",lambda *_a,**_k: {"status":"disabled"})
    book=service._upsert(path=source,title=source.stem,duration=10.0,files=[{"index":0,"path":str(source),"title":source.stem,"duration":10.0,"start":0.0,"end":10.0}],chapters=[{"index":0,"title":source.stem,"start":0.0,"end":10.0}])
    linked=service.auto_link_audiobook(int(book["id"]))
    assert linked and linked["ln_book_id"] == ln_id, getattr(service, "_last_auto_link_reason", "missing")
    assert _audiobook_title_match_score(source.stem,"同じ夢を見ていた") >= 95
    assert _audiobook_title_key("狼と香辛料～完全版オーディオブック") == _audiobook_title_key("狼と香辛料")
    assert _audiobook_series_title("狼と香辛料～完全版オーディオブック") == "狼と香辛料"
    assert _audiobook_volume(_audiobook_path_label("/tmp/v135-build/また、同じ夢を見ていた.m4b")) is None
