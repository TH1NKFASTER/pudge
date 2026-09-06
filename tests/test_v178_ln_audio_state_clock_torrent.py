from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from pudge import web_app
from pudge.config import AppConfig
from pudge.reading_audio_alignment import _raw_anchors_with_leading_hold, light_novel_position_for_audio

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
WEB_APP = ROOT / "pudge" / "web_app.py"


def _real_second_chapter_shape() -> dict:
    # Fresh v177 diagnostics: final chapter.start had already been rebased to
    # story_start=6856.134 while old marker-time prose anchors still survived.
    return {
        "chapter_index": 1,
        "start": 6856.134,
        "end": 7000.0,
        "normalized_length": 1000,
        "leading_prefix_start": 6856.134,
        "leading_prefix_debug": {
            "recovered": True,
            "chapter_marker": True,
            "marker_start": 6852.494,
            "marker_end": 6853.594,
            "story_start": 6856.134,
            "original_first_offset": 23,
            "original_first_time": 6861.6,
            "reconstructed_prose_prefix": False,
        },
        "anchors": [
            {"offset": 0, "time": 6852.68},
            {"offset": 10.999, "time": 6853.62},
            {"offset": 10.999, "time": 6856.199},
            {"offset": 23, "time": 6861.60},
            {"offset": 35, "time": 6865.06},
        ],
    }


def test_real_second_chapter_shape_holds_zero_even_when_declared_start_is_already_prose_start() -> None:
    rows = _raw_anchors_with_leading_hold(_real_second_chapter_shape())
    assert rows[0]["offset"] == 0
    assert float(rows[0]["time"]) == pytest.approx(6852.494, abs=0.001)
    assert any(
        float(row["offset"]) == 0 and float(row["time"]) == pytest.approx(6856.134, abs=0.001)
        for row in rows
    )
    assert not any(
        float(row["offset"]) > 0 and float(row["time"]) < 6856.133
        for row in rows
    )
    assert not any(float(row["offset"]) == pytest.approx(10.999) for row in rows)
    assert any(float(row["offset"]) == pytest.approx(23.0) for row in rows)


def test_runtime_lookup_stays_in_second_chapter_during_spoken_marker() -> None:
    chapter1 = _real_second_chapter_shape()
    chapter1["anchors"] = _raw_anchors_with_leading_hold(chapter1)
    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "start": 0.0,
                "end": 6856.2,
                "normalized_length": 25380,
                "anchors": [
                    {"offset": 0, "time": 33.646},
                    {"offset": 25380, "time": 6856.2},
                ],
            },
            chapter1,
        ]
    }
    for position in (6852.68, 6852.887, 6853.178, 6853.62, 6856.10):
        state = light_novel_position_for_audio(alignment, position)
        assert state is not None
        assert state["chapter_index"] == 1
        assert state["chapter_char_offset_exact"] == pytest.approx(0.0, abs=0.001)


def test_transport_reconcile_does_not_pull_playing_clock_backward_on_stale_poll() -> None:
    html = INDEX.read_text(encoding="utf-8")
    fn = html.split("function lnPairedTransportClockReconcile", 1)[1].split(
        "function lnPairedTransportClockReset", 1
    )[0]
    assert "drift<0&&Math.abs(drift)<=1.5){position=local;staleBackward=true;}" in fn
    assert "drift>=0&&drift<=.45)position=local+drift*.35" in fn
    assert "drift>.45&&drift<=.9)position=local+drift*.2" in fn


def test_furigana_expires_100ms_after_word_reaches_100_percent_even_on_plateau() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert ".ln-paired-furigana-expired" in html
    expiry = html.split("function scheduleLnPairedFuriganaExpiry", 1)[1].split(
        "function stopLnPairedPoll", 1
    )[0]
    assert "Number(progress)<99.9" in expiry
    assert "ln-paired-furigana-expired" in expiry
    assert "},100)" in expiry
    paint = html.split("const paintWordProgress=()=>", 1)[1].split(";};", 1)[0]
    assert "scheduleLnPairedFuriganaExpiry(active,progress)" in paint


def test_torrent_toggle_uses_authoritative_state_instead_of_masking_stale_backend() -> None:
    html = INDEX.read_text(encoding="utf-8")
    helper = html.split("function torrentToggleUiEnabled", 1)[1].split(
        "async function pollForegroundWork", 1
    )[0]
    assert "return Boolean(ui.state?.settings?.torrents_enabled)" in helper
    assert "torrents_enabled:desired" not in helper
    poll = html.split("async function pollTorrentTraffic()", 1)[1].split("function renderSafely", 1)[0]
    assert "ui.torrentTraffic?.enabled" in poll
    assert "ui.state.settings.torrents_enabled=Boolean(ui.torrentTraffic.enabled)" in poll
    handler = html.split("async function torrentToggleHttpCapture(){", 1)[1].split(
        "document.addEventListener('click',event=>{", 1
    )[0]
    assert "ui.torrentToggleDesired=desired" in handler
    assert "const previous=torrentToggleUiEnabled()" in handler


def test_backend_torrent_enable_ack_does_not_wait_for_auto_search(monkeypatch, tmp_path: Path) -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.nyaa.torrents_enabled = False
    calls: list[str] = []
    api.manager = SimpleNamespace(
        auto_search_current=lambda: calls.append("search") or 3,
        download_intents=SimpleNamespace(waiting_count=lambda: 0),
        torrent_backend_name=lambda: "aria2",
    )
    api._downloads_configured = lambda: True
    api._ui_state_cache = SimpleNamespace(invalidate=lambda: calls.append("invalidate"))
    api.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)

    queued: list[object] = []

    class Supervisor:
        def start(self, *, name, target, replace=False, **kwargs):
            calls.append(f"queued:{name}")
            queued.append(target)
            return SimpleNamespace()

    api.task_supervisor = Supervisor()
    monkeypatch.setattr(web_app, "write_config", lambda *a, **k: calls.append("persist"))

    result = api.set_torrents_enabled(True)

    assert result["enabled"] is True
    assert result["search_scheduled"] is True
    assert "search" not in calls
    assert queued
    queued[0]()
    assert "search" in calls


def test_runtime_defensively_applies_debug_story_start_without_serialized_prefix_start() -> None:
    chapter = _real_second_chapter_shape()
    chapter.pop("leading_prefix_start", None)
    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "start": 0.0,
                "end": 6856.2,
                "normalized_length": 25380,
                "anchors": [{"offset": 0, "time": 33.646}, {"offset": 25380, "time": 6856.2}],
            },
            chapter,
        ]
    }
    for position in (6852.68, 6852.887, 6853.62, 6856.10):
        state = light_novel_position_for_audio(alignment, position)
        assert state is not None
        assert state["chapter_index"] == 1
        assert state["chapter_char_offset_exact"] == pytest.approx(0.0, abs=0.001)


def test_backend_torrent_session_state_is_single_owner_and_stale_snapshot_cannot_restore_off(tmp_path: Path, monkeypatch) -> None:
    class DB:
        def __init__(self) -> None:
            self.values = {"ui_state_version": "7"}
        def get_state(self, key: str, default: str = "") -> str:
            return self.values.get(key, default)
        def set_state(self, key: str, value: str) -> None:
            self.values[key] = value

    class Cache:
        def __init__(self) -> None:
            self.payload = None
        def store(self, version, payload):
            self.payload = (str(version), payload)
            return payload
        def invalidate(self):
            self.payload = None

    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.nyaa.torrents_enabled = False
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = False
    db = DB()
    api.manager = SimpleNamespace(
        db=db,
        config=api.config,
        auto_search_current=lambda: 0,
        download_intents=SimpleNamespace(waiting_count=lambda: 0),
        torrent_backend_name=lambda: "aria2",
    )
    api._ui_state_cache = Cache()
    api._downloads_configured = lambda: False
    api.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    monkeypatch.setattr(web_app, "write_config", lambda *a, **k: None)

    stale = {"ui_state_version": "7", "settings": {"torrents_enabled": False}}
    result = api.set_torrents_enabled(True)
    assert result["enabled"] is True
    stored = api._store_ui_state_snapshot(stale)
    assert stored["settings"]["torrents_enabled"] is True
    assert stored["ui_state_version"] == "8"


def test_generic_settings_no_longer_owns_torrent_toggle_and_scroll_freezes_dom() -> None:
    backend = WEB_APP.read_text(encoding="utf-8")
    save = backend.split("    def save_settings", 1)[1].split("    def complete_onboarding", 1)[0]
    assert 'values.get("torrents_enabled"' not in save
    assert "cfg.nyaa.torrents_enabled = self._torrent_enabled_state()" in save

    html = INDEX.read_text(encoding="utf-8")
    collect = html.split("function collectSettings()", 1)[1].split("function renderSettings", 1)[0]
    assert "torrents_enabled:" not in collect
    render = html.split("function renderLnPairedPosition", 1)[1].split("function startLnPairedInterpolation", 1)[0]
    defer_at = render.index("if(manualScrolling&&options.scrollCatchup!==true)")
    change_at = render.index("if(changed&&!blocked)")
    assert defer_at < change_at
    assert "lnPairedFuriganaPaintOverlaps(old,word)" not in render
    handler = html.split("$('lnReaderScroll').addEventListener('scroll',()=>{", 1)[1].split("},{passive:true});", 1)[0]
    assert "lnPairedDeferredRender" in handler
    assert "scrollCatchup:true" in handler
