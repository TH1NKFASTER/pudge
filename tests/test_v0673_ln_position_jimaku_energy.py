from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.manager import _subtitle_retry_is_network_error, _subtitle_retry_is_rate_limit
from pudge.providers.jimaku import JimakuClient, JimakuError


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    return cfg


def test_v0673_ln_reader_width_state_and_position_frontend() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert 'id="lnrWidth" type="number" min="360" max="2400"' in html
    assert "calc((100vw - 900px)/2)" not in html
    assert "function lnStudyStateLabel" in html
    assert "function lnRubyReading" in html
    assert "State: ${escapeHtml(states)}" not in html
    assert "Number(book.current_offset||0)" in html
    assert "function saveLnReaderPositionNow" in html
    assert "function restoreLnReaderOffset" in html
    assert "await saveLnReaderPositionNow();stopLnParsePoll()" in html


def test_v0673_reader_width_accepts_wide_displays(tmp_path: Path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    saved = service.save_settings({"reader_width": 2200})
    assert saved["reader_width"] == 2200
    saved = service.save_settings({"reader_width": 9999})
    assert saved["reader_width"] == 2400


def test_v0673_rate_limit_helpers_recognize_real_httpx_message() -> None:
    detail = "Client error '429 Too Many Requests' for url 'https://jimaku.cc/api/entries/search'"
    assert _subtitle_retry_is_rate_limit(detail)
    assert _subtitle_retry_is_network_error(detail)
    assert _subtitle_retry_is_rate_limit("Jimaku rate limited (429); retry in 8 min")


def test_v0673_jimaku_429_creates_shared_cooldown_without_rehitting(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, request=request)

    client = JimakuClient("https://jimaku.cc", "key", cache_dir=tmp_path / "cache")
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(JimakuError) as exc:
        client.search_entries(query="Rate Limited Anime")
    message = str(exc.value)
    assert "rate limited (429)" in message
    assert "retry in" in message
    assert "https://" not in message
    assert calls == 1

    def should_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Jimaku network was hit during shared cooldown")

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(should_not_run))
    with pytest.raises(JimakuError):
        client.search_entries(query="Rate Limited Anime")
    client.close()
    assert (tmp_path / "cache" / "jimaku-api" / "rate-limit.json").is_file()


def test_v0673_jimaku_429_uses_stale_nonempty_cache(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=[{"id": 7, "name": "Cached", "anilist_id": 77}], request=request)
        return httpx.Response(429, headers={"Retry-After": "60"}, request=request)

    client = JimakuClient("https://jimaku.cc", "key", cache_dir=tmp_path / "cache", cache_ttl_seconds=0.01)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    first = client.search_entries(query="Cached")
    assert first and first[0].id == 7
    time.sleep(0.02)
    second = client.search_entries(query="Cached")
    client.close()
    assert second and second[0].id == 7
    assert calls == 2


def test_v0673_defer_does_not_increment_attempts(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Anime - 01.mkv"
    video.write_bytes(b"video")
    db.queue_subtitle_job(video, 1, 1)
    with db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET attempts=4,state='processing' WHERE video_path=?", (str(video),))
    db.defer_subtitle_job(video, "Jimaku rate limited (429); retry in 10 min", 600)
    row = next(row for row in db.subtitle_jobs() if row["video_path"] == str(video))
    assert int(row["attempts"]) == 4
    assert row["state"] == "pending"
    assert "rate limited" in row["last_error"]


def test_v0673_energy_throttle_and_handoff_are_present() -> None:
    manager = Path("pudge/manager.py").read_text(encoding="utf-8")
    assert "migration_budget = 0 if has_regular_jobs else 1" in manager
    assert "reason=energy_throttle" in manager
    assert not Path("docs/LLM_HANDOFF.md").exists()
