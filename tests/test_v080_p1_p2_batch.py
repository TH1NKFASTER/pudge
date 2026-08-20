from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import AudiobookService
from pudge.database import LATEST_SCHEMA_VERSION, Database
from pudge.light_novels import LightNovelService
from pudge.reading_audio_alignment import (
    align_light_novel_to_transcript,
    audio_position_for_light_novel_offset,
)
from pudge.visual_novels import VisualNovelService


def test_latest_schema_has_shared_identity_and_name_overrides(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    Database(path)
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert LATEST_SCHEMA_VERSION == version == 7
    assert {"character_name_overrides", "media_identities"} <= tables


def test_user_character_name_overrides_win_without_anilist(tmp_path: Path) -> None:
    config = SimpleNamespace(
        library=SimpleNamespace(
            database_path=tmp_path / "library.sqlite3",
            root_dir=tmp_path,
            cover_cache_dir=tmp_path / "covers",
        ),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        anilist=SimpleNamespace(enabled=False, access_token=""),
        ui=SimpleNamespace(language="en"),
    )
    Database(config.library.database_path)
    service = LightNovelService(config)
    rows = service.save_character_glossary_override(123, "雪乃", "Yukino")
    assert rows == [{"source": "雪乃", "preferred": "Yukino", "user_override": True}]
    assert service.delete_character_glossary_override(123, "雪乃") == []


def test_dense_alignment_supports_word_click_seek() -> None:
    text = "".join(f"第{index}節では主人公が静かに歩きました。" for index in range(80))
    normalized = "".join(f"第{index}節では主人公が静かに歩きました" for index in range(80))
    words = [
        {"word": character, "start": index * 0.08, "end": (index + 1) * 0.08}
        for index, character in enumerate(normalized)
    ]
    alignment = align_light_novel_to_transcript(
        [{"chapter_index": 0, "title": "One", "text": text}],
        [{"start": 0, "end": len(words) * 0.08, "words": words}],
        duration=len(words) * 0.08,
        model="test",
    )
    anchors = alignment["chapters"][0]["anchors"]
    assert len(anchors) > 50
    position = audio_position_for_light_novel_offset(alignment, 0, 120)
    assert position is not None and position > 0


def test_visual_novel_reader_is_idle_until_explicit_start() -> None:
    service = VisualNovelService()
    state = service.state()
    assert state["running"] is False
    assert state["status"] == "idle"
    assert state["transcript"] == []


def test_audiobook_import_queues_transcription_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "library.sqlite3")
    service = AudiobookService(database, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    source = tmp_path / "book.mp3"
    source.write_bytes(b"audio")
    monkeypatch.setattr(service, "_probe", lambda _path: (120.0, []))
    queued: list[int] = []
    monkeypatch.setattr(service, "prepare_transcription", lambda book_id, **_kwargs: queued.append(int(book_id)) or {"status": "queued"})
    book = service.import_file(source)
    assert queued == [int(book["id"])]


def test_frontend_contracts_for_requested_batch() -> None:
    root = Path(__file__).parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    manga = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    reading = (root / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    vn = (root / "pudge/web/visual_novels.js").read_text(encoding="utf-8")
    assert "lnPairedAlign" not in html
    assert "light_novel_play_paired_at_offset" in html
    assert "await loadLightNovels(true);return;}\n    if(target.id==='lnPairedAudio'" not in html
    assert "anchorRect" in reading
    assert "activateTextRegion" not in manga
    assert "deactivateTextRegion" not in manga
    assert "mangaRegionReadingOrder" in manga
    assert 'data-page="visualnovels"' in html
    assert "visual_novel_start" in vn
    assert "media_identity_search" in html
    assert "save_character_glossary_override" in html
