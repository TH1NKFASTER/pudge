from __future__ import annotations
from pathlib import Path
from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.config import AppConfig

def _cfg(tmp_path: Path) -> AppConfig:
    cfg=AppConfig();cfg.library.root_dir=tmp_path/"library";cfg.library.database_path=tmp_path/"db.sqlite3";cfg.paths.cache_dir=tmp_path/"cache";cfg.library.root_dir.mkdir(parents=True,exist_ok=True);cfg.paths.cache_dir.mkdir(parents=True,exist_ok=True);return cfg

def test_unnumbered_ln_defaults_to_one_and_groups(tmp_path: Path) -> None:
    service=LightNovelService(_cfg(tmp_path));a=tmp_path/"あそびのかんけい.txt";b=tmp_path/"あそびのかんけい２.txt";a.write_text("一",encoding="utf-8");b.write_text("二",encoding="utf-8");first=service.import_file(a);second=service.import_file(b);assert first["volume"]==1;assert second["volume"]==2;rows=service.books();assert len({row["series_key"] for row in rows})==1

def test_ln_delete_keeps_source(tmp_path: Path) -> None:
    service=LightNovelService(_cfg(tmp_path));source=tmp_path/"book.txt";source.write_text("本文",encoding="utf-8");book=service.import_file(source);managed=Path(book["file_path"]);service.delete_book(book["id"],delete_file=False);assert managed.is_file();assert not service.books()

def test_folder_audiobook_cumulative_chapters(tmp_path: Path, monkeypatch) -> None:
    db=Database(tmp_path/"db.sqlite3");folder=tmp_path/"Audio Book";folder.mkdir();(folder/"01 Intro.mp3").write_bytes(b"a");(folder/"02 Chapter.mp3").write_bytes(b"b");service=AudiobookService(db,ffprobe="ffprobe",mpv="mpv",cache_dir=tmp_path/"cache");monkeypatch.setattr(service,"_probe",lambda p:((10.0 if p.name.startswith("01") else 20.0),[]));book=service.import_folder(folder);assert book["multi_file"] is True;assert book["file_count"]==2;assert book["duration"]==30.0;assert [c["start"] for c in book["chapters"]]==[0.0,10.0]

def test_batch02_static_contracts() -> None:
    html=Path("pudge/web/index.html").read_text(encoding="utf-8");settings=Path("pudge/web/settings.js").read_text(encoding="utf-8");debug=Path("pudge/web/debug.js").read_text(encoding="utf-8");media=Path("pudge/web/media.js").read_text(encoding="utf-8");web=Path("pudge/web_app.py").read_text(encoding="utf-8");manager=Path("pudge/manager.py").read_text(encoding="utf-8");install=Path("install.sh").read_text(encoding="utf-8")
    ready=html.index("readySections?`<div");action=html.index("section.needsAction",ready);waiting=html.index("section.waitingPreparation",action);assert ready<action<waiting
    assert "AniList #${book.anilist_id}" not in html and "book.anilist_status?`<span>" not in html and "data-ln-context-action" in html
    assert "if (has('s_agent_enabled')) return 'advanced';" in settings and "s_playback_enabled', 's_agent_enabled" not in settings
    assert "debug_reselect_subtitles" in web and "fresh-subtitles" in debug and "--force-search" in manager and "--resync" in manager
    assert "audiobookImportFolder" not in media and "stop-audio" in media and "delete-audio" in media and "audiobook_stop" in web and "audiobook_delete" in web
    assert "config.aria2.enabled = True" not in install
