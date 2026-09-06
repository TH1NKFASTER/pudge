from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from pudge import web_app
from pudge.config import AppConfig, load_config, write_config, write_torrents_enabled
from pudge.reading_audio_alignment import _raw_anchors_with_leading_hold, light_novel_position_for_audio

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"


def _second_chapter() -> dict:
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
        },
        "anchors": [
            {"offset": 0, "time": 6852.68},
            {"offset": 10.999, "time": 6853.62},
            {"offset": 10.999, "time": 6856.199},
            {"offset": 23, "time": 6861.60},
            {"offset": 35, "time": 6865.06},
        ],
    }


def _torrent_api(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = AppConfig(config_path=config_path)
    cfg.nyaa.torrents_enabled = False
    write_config(cfg, config_path)
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = cfg
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = False

    class DB:
        def __init__(self): self.values = {"ui_state_version": "1"}
        def get_state(self, key, default=""): return self.values.get(key, default)
        def set_state(self, key, value): self.values[key] = value

    api.manager = SimpleNamespace(
        config=cfg,
        db=DB(),
        download_intents=SimpleNamespace(waiting_count=lambda: 0),
        torrent_backend_name=lambda: "aria2",
    )
    api._ui_state_cache = SimpleNamespace(invalidate=lambda: None)
    api._downloads_configured = lambda: True
    api.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    return api, config_path


def test_torrent_session_state_keeps_partial_config_test_stubs_compatible() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.manager = SimpleNamespace()
    api.config = SimpleNamespace(qbittorrent=SimpleNamespace(enabled=True))

    assert api._torrent_enabled_state() is True

    api2 = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api2.manager = SimpleNamespace()
    api2.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=False),
        qbittorrent=SimpleNamespace(enabled=True),
    )
    assert api2._torrent_enabled_state() is False


def test_narrow_torrent_writer_preserves_unrelated_config_without_keychain(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[nyaa]\nbase_url = "https://nyaa.si"\ntorrents_enabled = false\n\n'
        '[qbittorrent]\npassword = "do-not-touch"\n',
        encoding="utf-8",
    )
    cfg = AppConfig(config_path=path)
    cfg.nyaa.torrents_enabled = True

    # The narrow writer must never route through the full secret persistence path.
    import pudge.config as config_module
    monkeypatch.setattr(
        type(config_module._SECRET_STORE),
        "persisted_config_value",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Keychain hot-path access")),
    )
    write_torrents_enabled(cfg, path)

    text = path.read_text(encoding="utf-8")
    assert "torrents_enabled = true" in text
    assert 'password = "do-not-touch"' in text
    assert load_config(path).nyaa.torrents_enabled is True


def test_torrent_persist_failure_restores_session_instead_of_silent_false_snapshot(monkeypatch, tmp_path: Path) -> None:
    api, _ = _torrent_api(tmp_path)
    api.task_supervisor = None
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        api.set_torrents_enabled(True)

    assert api._torrent_session_enabled is False
    assert api.config.nyaa.torrents_enabled is False
    assert api.manager.config.nyaa.torrents_enabled is False


def test_torrent_background_search_schedule_failure_cannot_undo_persisted_on(tmp_path: Path) -> None:
    api, config_path = _torrent_api(tmp_path)

    class BrokenSupervisor:
        def start(self, **kwargs):
            raise RuntimeError("supervisor closed")

    api.task_supervisor = BrokenSupervisor()
    result = api.set_torrents_enabled(True)

    assert result["enabled"] is True
    assert result["search_scheduled"] is False
    assert api._torrent_session_enabled is True
    assert load_config(config_path).nyaa.torrents_enabled is True


def test_torrent_ack_releases_frontend_pending_intent_to_backend_session_state() -> None:
    html = INDEX.read_text(encoding="utf-8")
    handler = html.split("async function torrentToggleHttpCapture(){", 1)[1].split("document.addEventListener('click',event=>{", 1)[0]
    assert "Boolean(result?.enabled)!==desired" in handler
    assert "ui.state.settings.torrents_enabled=Boolean(result.enabled)" in handler
    assert "ui.torrentToggleDesired=null" in handler
    assert "scheduleTorrentTrafficPoll(0)" in handler


def test_second_chapter_hold_anchors_are_intrinsically_monotonic() -> None:
    rows = _raw_anchors_with_leading_hold(_second_chapter())
    times = [float(row["time"]) for row in rows]
    assert times == sorted(times)
    assert rows[:2] == [
        {"offset": 0, "time": 6852.494},
        {"offset": 0, "time": 6856.134},
    ]
    assert all(not (float(row["time"]) < 6856.134 and float(row["offset"]) > 0) for row in rows)


def test_second_chapter_real_clock_grid_holds_prose_until_story_start() -> None:
    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "start": 0.0,
                "end": 6856.2,
                "normalized_length": 25380,
                "anchors": [{"offset": 0, "time": 33.646}, {"offset": 25380, "time": 6856.2}],
            },
            _second_chapter(),
        ]
    }
    # Before the marker belongs to the previous chapter. From marker_start until
    # verified prose onset, chapter 1 owns the clock but consumes zero prose.
    before = light_novel_position_for_audio(alignment, 6852.0)
    assert before is not None and before["chapter_index"] == 0
    for position in (6852.5, 6853.0, 6853.6, 6854.0, 6855.0, 6856.0, 6856.13):
        state = light_novel_position_for_audio(alignment, position)
        assert state is not None
        assert state["chapter_index"] == 1
        assert state["chapter_char_offset_exact"] == pytest.approx(0.0, abs=0.001)
    after = light_novel_position_for_audio(alignment, 6856.3)
    assert after is not None and after["chapter_index"] == 1
    assert after["chapter_char_offset_exact"] > 0
    original = light_novel_position_for_audio(alignment, 6861.6)
    assert original is not None
    assert original["chapter_char_offset_exact"] == pytest.approx(23.0, abs=0.01)


def test_reader_parse_alignment_refresh_is_scheduled_after_response_not_run_inline() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.light_novels = SimpleNamespace(
        chapter_fast=lambda book, chapter: {"parsing": False, "tokens": [[{"surface": "狼"}]]},
    )
    calls: list[tuple] = []
    api.audiobooks = SimpleNamespace(
        maybe_refresh_alignment_after_reader_parse=lambda *args: calls.append(args),
    )
    queued: list[tuple] = []

    class Supervisor:
        def start(self, name, target, *, args=(), **kwargs):
            queued.append((name, target, tuple(args)))
            return SimpleNamespace()

    api.task_supervisor = Supervisor()
    api.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)

    payload = api.light_novel_chapter(180, 1)
    assert payload["tokens"]
    assert calls == []
    assert len(queued) == 1
    queued[0][1](*queued[0][2])
    assert calls == [(180, 1)]


def test_light_novel_open_preserves_legacy_log_then_emits_timing_breakdown() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    records: list[tuple[object, ...]] = []
    api.logger = SimpleNamespace(info=lambda *args: records.append(args))
    api.light_novels = SimpleNamespace(
        open_book=lambda book_id: {
            "id": int(book_id),
            "title": "test",
            "chapters": [{"chapter_index": 0, "title": "Chapter 1"}],
        }
    )
    api.audiobooks = SimpleNamespace(
        link_for_light_novel=lambda book_id, **kwargs: {"book_id": int(book_id), "playing": False}
    )

    result = api.light_novel_open(7)

    assert result["paired_audio"]["book_id"] == 7
    assert records[0][0] == "LN open book=%s chapters=%s paired=%s elapsed=%.3fs"
    assert records[1][0].startswith("TIMING step=ln.open ")

def test_torrent_loaded_config_remains_authoritative_until_first_explicit_toggle() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.manager = SimpleNamespace()
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=False),
        qbittorrent=SimpleNamespace(enabled=True),
    )
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = False
    api._torrent_session_authoritative = False

    api.config.nyaa.torrents_enabled = True

    assert api._torrent_enabled_state() is True


def test_torrent_explicit_session_intent_ignores_later_stale_config_value() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.manager = SimpleNamespace()
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=False),
        qbittorrent=SimpleNamespace(enabled=True),
    )
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = True
    api._torrent_session_authoritative = True

    assert api._torrent_enabled_state() is True
