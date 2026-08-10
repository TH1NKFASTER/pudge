from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.ocr import _VisionTextRow, _filter_probable_furigana_rows


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.download_dirs = []
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.nyaa.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    return cfg


def test_spatial_ocr_filter_removes_small_kana_furigana_above_base_text() -> None:
    rows = [
        _VisionTextRow(y=0.56, x=0.33, width=0.14, height=0.025, text="まほう"),
        _VisionTextRow(y=0.50, x=0.30, width=0.30, height=0.050, text="魔法使いの夜"),
        _VisionTextRow(y=0.42, x=0.28, width=0.34, height=0.052, text="始まるよ"),
    ]

    filtered = _filter_probable_furigana_rows(rows)

    assert [row.text for row in filtered] == ["魔法使いの夜", "始まるよ"]


def test_spatial_ocr_filter_preserves_normal_same_size_multiline_dialogue() -> None:
    rows = [
        _VisionTextRow(y=0.58, x=0.20, width=0.45, height=0.052, text="どうしてなの？"),
        _VisionTextRow(y=0.49, x=0.22, width=0.42, height=0.050, text="わからないよ"),
    ]

    assert _filter_probable_furigana_rows(rows) == rows


def test_legacy_v10_cleaned_ocr_is_recognized(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.matching.ocr_image_subtitles = False
    manager = AnimeManager(cfg)
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"video")
    ocr_srt = cfg.paths.cache_dir / "ocr" / "legacy.srt"
    ocr_srt.parent.mkdir(parents=True, exist_ok=True)
    ocr_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nJP\n", encoding="utf-8")
    stat = ocr_srt.stat()
    digest = hashlib.sha1(
        f"{ocr_srt.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:playback-srt-v10".encode()
    ).hexdigest()[:20]
    cleaned = cfg.paths.cache_dir / "playback-srt" / f"v10-{digest}.srt"
    cleaned.parent.mkdir(parents=True, exist_ok=True)
    cleaned.write_text(ocr_srt.read_text(encoding="utf-8"), encoding="utf-8")
    manager.db.upsert_episode(LibraryEpisode(None, "Movie", 1, video, subtitle_path=cleaned, state="ready"))

    row = manager.db.episode_by_path(video)
    assert row is not None
    assert manager._is_legacy_ocr_prepared_subtitle(row) is True


def test_old_final_pipeline_manifest_can_prove_ocr_origin_without_raw_ocr_cache(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.matching.ocr_image_subtitles = False
    manager = AnimeManager(cfg)
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"video")
    cleaned = cfg.paths.cache_dir / "playback-srt" / "v10-deadbeef.srt"
    cleaned.parent.mkdir(parents=True, exist_ok=True)
    cleaned.write_text("1\n00:00:01,000 --> 00:00:02,000\nJP\n", encoding="utf-8")
    manifest = cfg.paths.cache_dir / "final-pipeline" / "old.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"schema": "final-pipeline-v2", "source": "ocr", "subtitle": str(cleaned)}), encoding="utf-8")
    manager.db.upsert_episode(LibraryEpisode(None, "Movie", 1, video, subtitle_path=cleaned, state="ready"))

    row = manager.db.episode_by_path(video)
    assert row is not None
    assert manager._is_legacy_ocr_prepared_subtitle(row) is True
