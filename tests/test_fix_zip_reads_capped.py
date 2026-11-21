"""Every ZIP member Pudge reads is capped like EPUB/CBZ pages (behaviour).

A tiny zip can unpack to gigabytes. Three readers skipped ``archive_limits``: a
``subs.zip`` found next to a video (copied whole into the cache), a speaker
annotations zip (read whole into memory) and the Mokuro OCR worker's page read (a
process that already holds ~2 GB of models).
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

import pudge.local_search as local_search
import pudge.manga as manga_module
import pudge.manga_mokuro_worker as mokuro_worker
import pudge.web_app as web_app_module
from pudge.database import Database
from pudge.light_novels import LightNovelError
from pudge.manga import MangaOcrWorkerError, MangaService
from pudge.models import VideoIdentity
from pudge.web_app import WebAppApi

JAPANESE_SRT = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは、世界。今日はいい天気ですね。\n\n" * 20


def test_subtitle_zip_member_over_the_text_limit_is_skipped(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "Show - 01.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Show - 01.ja.srt", JAPANESE_SRT + "あ" * 50_000)
        zf.writestr("Show - 01 small.ja.srt", JAPANESE_SRT)
    monkeypatch.setattr(local_search, "TEXT_MEMBER_LIMIT", len(JAPANESE_SRT.encode()) + 10)
    found = local_search._extract_best_from_zip(
        archive, VideoIdentity(title="Show", episode=1), tmp_path / "Show - 01.mkv", tmp_path / "cache", True
    )
    assert found is not None and found.name == "Show - 01 small.ja.srt"
    copies = sorted(path.name for path in (tmp_path / "cache" / "local-archives").rglob("*.srt"))
    assert copies == ["Show - 01 small.ja.srt"]


def test_speaker_annotation_zip_is_read_with_a_cap(tmp_path: Path, monkeypatch) -> None:
    payload = {"kind": "pudge-speaker-annotations", "books": []}
    archive = tmp_path / "speakers.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pudge-speaker-annotations.json", json.dumps(payload) + " " * 10_000)
    assert WebAppApi._read_speaker_annotations_file(archive) == payload
    monkeypatch.setattr(web_app_module, "TEXT_MEMBER_LIMIT", 1000)
    with pytest.raises(LightNovelError):
        WebAppApi._read_speaker_annotations_file(archive)


def _page() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


def test_mokuro_worker_refuses_a_page_over_the_limit(tmp_path: Path, monkeypatch) -> None:
    cbz = tmp_path / "vol.cbz"
    with zipfile.ZipFile(cbz, "w") as zf:
        zf.writestr("001.jpg", _page())
    folder_page = tmp_path / "002.jpg"
    folder_page.write_bytes(_page())
    request = {"op": "ocr", "archive": str(cbz), "name": "001.jpg"}
    assert mokuro_worker._read_page(request) == _page()
    monkeypatch.setattr(mokuro_worker, "_PAGE_LIMIT", 100)
    with pytest.raises(ValueError, match="page_too_large"):
        mokuro_worker._read_page(request)
    with pytest.raises(ValueError, match="page_too_large"):
        mokuro_worker._read_page({"op": "ocr", "path": str(folder_page)})


def test_oversized_page_is_not_sent_to_mokuro(tmp_path: Path, monkeypatch) -> None:
    cbz = tmp_path / "vol.cbz"
    with zipfile.ZipFile(cbz, "w") as zf:
        zf.writestr("001.jpg", _page())
    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    sent: list[dict] = []
    monkeypatch.setattr(
        service, "_mokuro_request", lambda payload, **_kw: sent.append(payload) or {"page": {}}
    )
    assert service._mokuro_recognize(cbz, "001.jpg") == {}
    monkeypatch.setattr(manga_module, "IMAGE_MEMBER_LIMIT", 100)
    with pytest.raises(MangaOcrWorkerError, match="too_large"):
        service._mokuro_recognize(cbz, "001.jpg")
    assert len(sent) == 1
