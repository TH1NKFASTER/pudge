from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace


def test_audiobook_search_ui_reuses_cache_and_shows_selected_volume_size() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "light_novel_cached_audiobook_nyaa" in html
    assert "showing local cache" in html
    assert "Refreshing results" in html
    assert "selected_size_bytes||0" in html
    assert "torrentBytes(selectedBytes)" in html
    assert "start_light_novel_download_audiobook_nyaa" in html
    assert "Volume download started in background" in html


def test_download_center_renders_local_rows_before_backend_refresh() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "torrent_downloads(ui.downloadCenter.mediaId??null,false)" in html
    assert "void refreshDownloadCenter(true,true)" in html
    assert "ui.downloadCenterPollCount%5===0" in html


def test_cached_audiobook_search_is_network_free() -> None:
    import pudge.web_app as web_app

    class FakeCache:
        def get(self, key, *, ttl_seconds):
            assert key["series"] == "狼と香辛料"
            assert key["volume"] == 2
            assert ttl_seconds > 0
            return {
                "target_volume": 2,
                "matches": [{"title": "cached"}],
                "cached_at": time.time() - 5,
            }

    api = object.__new__(web_app.WebAppApi)
    api.light_novels = SimpleNamespace(
        book=lambda book_id: {
            "id": book_id,
            "title": "狼と香辛料II",
            "series_title": "狼と香辛料",
            "volume": 2,
        }
    )
    api._ln_audiobook_search_cache = FakeCache()
    result = api.light_novel_cached_audiobook_nyaa(180)
    assert result["cached"] is True
    assert result["matches"] == [{"title": "cached"}]
    assert result["cache_age_seconds"] >= 4


def test_torrent_downloads_refresh_false_does_not_touch_aria2() -> None:
    import pudge.web_app as web_app

    class FakeDB:
        def anime_list(self):
            return []

        def downloads(self):
            return []

    class FakeManager:
        db = FakeDB()

        def torrent_clients(self):
            raise AssertionError("local downloads snapshot must not touch torrent backends")

    api = object.__new__(web_app.WebAppApi)
    api.manager = FakeManager()
    api.config = SimpleNamespace(aria2=SimpleNamespace(enabled=True))
    api._last_network_guard = {"bound": True, "protected": True, "interface": "utun9"}
    api._downloads_configured = lambda: False
    api._downloads_enabled = lambda: False
    api._storage_payload = lambda refresh=False: {"used_gb": 1.0, "limit_gb": 10.0}

    result = api.torrent_downloads(refresh=False)
    assert result["downloads"] == []
    assert result["network_guard"]["interface"] == "utun9"


def test_audiobook_download_start_is_background(monkeypatch) -> None:
    import pudge.web_app as web_app

    started_threads: list[str] = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            started_threads.append(self.name)

    api = object.__new__(web_app.WebAppApi)
    api._ln_audiobook_download_lock = threading.Lock()
    api._ln_audiobook_download_jobs = {}
    api.logger = SimpleNamespace(exception=lambda *a, **k: None)
    api.light_novel_download_audiobook_nyaa = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("slow metadata/download work must not execute inline")
    )
    monkeypatch.setattr(web_app.threading, "Thread", FakeThread)

    result = api.start_light_novel_download_audiobook_nyaa(180, {"title": "book"})
    assert result["started"] is True
    assert result["running"] is True
    assert started_threads == ["ln-audiobook-download-180"]
