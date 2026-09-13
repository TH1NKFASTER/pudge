from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PIL import Image

from pudge.manga_ocr_artifact import (
    MANGA_OCR_ARTIFACT_SCHEMA,
    artifact_page,
    build_artifact,
    normalize_page,
    read_artifact,
    write_artifact,
)
from pudge.visual_novels import VisualNovelService


def test_manga_artifact_roundtrip_keeps_segment_geometry_and_recognizer(tmp_path: Path) -> None:
    page = normalize_page(
        0,
        [
            {
                "text": "日本語です",
                "raw_text": "日本語です",
                "orientation": "vertical",
                "confidence": 0.9,
                "detector": "vision-rectangles",
                "recognizer": "manga-ocr",
                "x": 0.6,
                "y": 0.2,
                "width": 0.2,
                "height": 0.5,
                "segments": [
                    {
                        "text": "日本語",
                        "orientation": "vertical",
                        "x": 0.7,
                        "y": 0.25,
                        "width": 0.04,
                        "height": 0.2,
                    },
                    {
                        "text": "です",
                        "orientation": "vertical",
                        "x": 0.64,
                        "y": 0.25,
                        "width": 0.04,
                        "height": 0.12,
                    },
                ],
            }
        ],
        name="001.png",
        width=1200,
        height=1800,
    )
    artifact = build_artifact(
        source_fingerprint="fixture",
        title="fixture",
        page_count=1,
        pages=[page],
        detector="apple-vision-multipass",
        recognizer="manga-ocr",
        created_at=1.0,
    )
    target = tmp_path / "ocr.json"
    write_artifact(target, artifact)
    loaded = read_artifact(target)
    assert loaded is not None
    assert loaded["schema"] == MANGA_OCR_ARTIFACT_SCHEMA
    region = artifact_page(loaded, 0)["regions"][0]
    assert region["recognizer"] == "manga-ocr"
    assert region["word_geometry"] == "mapped_segments"
    assert [segment["text"] for segment in region["segments"]] == ["日本語", "です"]
    assert region["segments"][0]["x"] == 0.7


def test_manga_artifact_v1_is_read_without_destroying_legacy_data(tmp_path: Path) -> None:
    target = tmp_path / "legacy.json"
    target.write_text(
        json.dumps(
            {
                "schema": "pudge-manga-ocr-v1",
                "source": {"fingerprint": "old", "title": "old", "page_count": 1},
                "engine": {"detector": "vision", "recognizer": "manga-ocr"},
                "created_at": 1.0,
                "summary": {"processed_pages": 1, "region_count": 1, "fallback_pages": 0, "complete": True},
                "pages": [
                    {
                        "page_index": 0,
                        "name": "001.png",
                        "width": 100,
                        "height": 100,
                        "regions": [
                            {"text": "猫", "x": 0.1, "y": 0.2, "width": 0.2, "height": 0.3}
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = read_artifact(target)
    assert loaded is not None
    assert loaded["schema"] == MANGA_OCR_ARTIFACT_SCHEMA
    assert loaded["migrated_from_schema"] == "pudge-manga-ocr-v1"
    region = artifact_page(loaded, 0)["regions"][0]
    assert region["text"] == "猫"
    assert region["word_geometry"] == "unavailable"


def test_vn_static_frame_commits_without_second_ocr(monkeypatch, tmp_path: Path) -> None:
    import pudge.visual_novels as module

    calls = 0

    def recognize(_image) -> str:
        nonlocal calls
        calls += 1
        return "日本語です"

    monkeypatch.setattr(module, "_vision_recognize", recognize)
    service = VisualNovelService()
    service._generation = 1
    service._state = {
        "running": True,
        "status": "starting",
        "detail": "",
        "current_text": "",
        "current_text_id": 0,
        "generation": 1,
    }
    stop_event = threading.Event()
    service._stop_event = stop_event

    monkeypatch.setattr(service, "_capture_target", lambda _window_id, _stop_event: (object(), object()))

    def capture(
        path: Path,
        _content_filter: object,
        _configuration: object,
        _stop_event: threading.Event,
    ) -> None:
        Image.new("RGB", (32, 32), "white").save(path)

    monkeypatch.setattr(service, "_capture_window", capture)
    thread = threading.Thread(target=service._capture_loop, args=(stop_event, 1, 123), daemon=True)
    service._thread = thread
    thread.start()
    deadline = time.monotonic() + 2.5
    state = service.state()
    while time.monotonic() < deadline and not state["transcript"]:
        time.sleep(0.05)
        state = service.state()
    stop_event.set()
    thread.join(timeout=2)

    assert calls == 1
    assert state["current_text"] == "日本語です"
    assert [row["text"] for row in state["transcript"]] == ["日本語です"]


def test_manga_legacy_empty_cache_is_retryable_until_verified() -> None:
    from pudge.manga import MangaService

    class StateDb:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def get_state(self, key: str, default: str = "") -> str:
            return self.values.get(key, default)

        def set_state(self, key: str, value: str) -> None:
            self.values[key] = value

    service = object.__new__(MangaService)
    service.db = StateDb()
    # This test intentionally exercises legacy empty-cache semantics with a
    # state-only fake DB. Source-generation ownership is covered separately.
    service._ocr_context = lambda _book_id: ("", 0, 0)

    assert service._cached_ocr_payload(1, 2, []) is None
    cached_only = service._cached_ocr_payload(1, 2, [], cached_only=True)
    assert cached_only is not None
    assert cached_only["status"] == "unknown_cached_empty"
    assert cached_only["retryable"] is True

    service._set_ocr_page_status(
        1,
        2,
        status="empty_verified",
        reason="successful_ocr_no_text",
        retryable=False,
    )
    verified = service._cached_ocr_payload(1, 2, [])
    assert verified is not None
    assert verified["status"] == "empty_verified"
    assert verified["retryable"] is False


def test_embedded_probe_cache_uses_file_identity_and_never_caches_failure(
    monkeypatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    import pudge.manager as manager_module
    from pudge.manager import AnimeManager
    from pudge.media import MediaProbeError

    class StateDb:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def get_state(self, key: str, default: str = "") -> str:
            return self.values.get(key, default)

        def set_state(self, key: str, value: str) -> None:
            self.values[key] = value

    manager = object.__new__(AnimeManager)
    manager.db = StateDb()
    manager.config = SimpleNamespace(tools=SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg"))
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"v1")
    calls = 0

    def probe(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["strict_probe_errors"] is True
        assert kwargs["ignore_sidecars"] is True
        return "embedded", None, 4

    monkeypatch.setattr(manager_module, "japanese_subtitle_details", probe)
    assert manager._cached_embedded_subtitle_details(video) == ("embedded", None, 4)
    assert manager._cached_embedded_subtitle_details(video) == ("embedded", None, 4)
    assert calls == 1

    video.write_bytes(b"different-size")
    assert manager._cached_embedded_subtitle_details(video) == ("embedded", None, 4)
    assert calls == 2

    manager.db.values.clear()

    def failing_probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise MediaProbeError("probe failed")

    monkeypatch.setattr(manager_module, "japanese_subtitle_details", failing_probe)
    try:
        manager._cached_embedded_subtitle_details(video)
    except MediaProbeError:
        pass
    else:
        raise AssertionError("probe failure must remain visible")
    assert manager.db.values == {}


def test_probe_media_has_timeout_and_wraps_timeout(monkeypatch, tmp_path: Path) -> None:
    import subprocess

    import pytest

    import pudge.media as media

    def timeout_run(command, **kwargs):
        assert kwargs["timeout"] == 20
        raise subprocess.TimeoutExpired(command, timeout=20)

    monkeypatch.setattr(media.subprocess, "run", timeout_run)
    with pytest.raises(media.MediaProbeError):
        media.probe_media(tmp_path / "video.mkv", "ffprobe")
