"""Shared set-ups for the light-novel / audiobook alignment tests of audit 71Б (workstream
g23). This module has no tests of its own."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService


def paired_book(
    tmp_path: Path, chapters: list[tuple[str, str]], *, duration: float
) -> tuple[AudiobookService, int, int]:
    """A light novel of `chapters` ((title, text) each) linked to a one-file audiobook of
    `duration` seconds; returns (audiobook service, light novel id, audiobook id). The
    text hash of chapter i >= 1 is "chapter-<i>" (chapter 0 comes from the import)."""
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir.mkdir(parents=True)
    novels = LightNovelService(cfg)
    source = tmp_path / "book.txt"
    source.write_text(chapters[0][1], encoding="utf-8")
    ln_id = int(novels.import_file(source)["id"])
    with novels._connect() as conn:
        for index, (title, text) in enumerate(chapters[1:], start=1):
            conn.execute(
                "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
                (ln_id, index, title, text, f"chapter-{index}"),
            )
    audio = AudiobookService(
        Database(cfg.library.database_path),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=cfg.paths.cache_dir,
    )
    audio_file = tmp_path / "book.m4b"
    audio_file.write_bytes(b"audio")
    audio_id = int(
        audio._upsert(
            path=audio_file,
            title="Book",
            duration=duration,
            files=[
                {
                    "index": 0,
                    "path": str(audio_file),
                    "title": "Book",
                    "duration": duration,
                    "start": 0.0,
                    "end": duration,
                }
            ],
            chapters=[{"index": 0, "title": "Book", "start": 0.0, "end": duration}],
        )["id"]
    )
    audio.link_light_novel(ln_id, audio_id, prepare_alignment=False)
    return audio, ln_id, audio_id


def write_alignment(
    audio: AudiobookService, audio_id: int, path: Path, chapters: list[dict[str, Any]], revision: str | None
) -> None:
    """An alignment as the alignment cache stores it, made by algorithm `revision`."""
    processing: dict[str, Any] = {"transcript_fingerprint": audio._transcript_fingerprint(audio_id)}
    if revision is not None:
        processing["alignment_algorithm_revision"] = revision
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "reading-audio-v3",
                "model": audio.stt_model,
                "confidence": 0.9,
                "chapters": chapters,
                "anchor_count": sum(len(row.get("anchors") or []) for row in chapters),
                "processing": processing,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
