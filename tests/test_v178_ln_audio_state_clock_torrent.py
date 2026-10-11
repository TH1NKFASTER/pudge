"""v1.78: the second chapter's spoken title, the torrent session state, settings that do not
own the torrent switch, a reader that keeps still while the user scrolls (behaviour; audit
71Б rewrite).

Covered elsewhere: a late (stale) poll never moves the reader's clock back and a lead of the
backend is eased in, test_v0106_ln_optimistic_transport_clock.py and
test_v0729_ln_audio_sync_responsive.py::test_reader_clock_ignores_late_poll_answers_and_follows_real_jumps;
a word's reading disappears 100 ms after the word was read even when the clock stands still,
test_v0749_ln_listen_resume_and_furigana_timing.py::test_reading_of_a_word_held_at_its_end_disappears_after_100ms;
the torrent switch shows what the backend says (also when it refuses or a status poll is
late), test_v195_torrent_authoritative_state.py::test_frontend_never_turns_green_before_backend_confirmation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from pudge import web_app
from pudge.config import AppConfig, load_config
from pudge.reading_audio_alignment import _raw_anchors_with_leading_hold, light_novel_position_for_audio
from t71g19_support import LN_PAIRED, OPEN
from t71g22_support import USER_SCROLL
from test_web_app import make_api


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
        float(row["offset"]) == 0 and float(row["time"]) == pytest.approx(6856.134, abs=0.001) for row in rows
    )
    assert not any(float(row["offset"]) > 0 and float(row["time"]) < 6856.133 for row in rows)
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


def test_backend_torrent_session_state_is_single_owner_and_stale_snapshot_cannot_restore_off(
    tmp_path: Path, monkeypatch
) -> None:
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
    # The quick acknowledgement does not access SQLite. An old response must
    # keep its old version rather than masquerading as a fresh full snapshot.
    assert stored["ui_state_version"] == "7"
    assert db.values["ui_state_version"] == "7"
    api._bump_ui_state_version()  # Background bookkeeping completes later.
    assert api._store_ui_state_snapshot(stale)["ui_state_version"] == "7"
    assert api._ui_state_cache.payload is None
    fresh = {"ui_state_version": "8", "settings": {"torrents_enabled": False}}
    assert api._store_ui_state_snapshot(fresh)["settings"]["torrents_enabled"] is True
    assert api._ui_state_cache.payload == ("8", fresh)


def test_saving_settings_never_switches_torrents(page, tmp_path: Path) -> None:
    # Torrents have their own switch; the Settings page does not send the flag, and a
    # settings save that still carries an old value does not change it.
    result = page.run(
        "setPage('settings'); await __tick(20); await __click('#saveSettings'); await __tick(10); return __toasts();",
        state={"settings": {"torrents_enabled": True}},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    [[values]] = result.calls_of("save_settings")
    assert "torrents_enabled" not in values and "language" in values

    api = make_api(tmp_path)
    api.set_torrents_enabled(True)
    assert api.save_settings({"torrents_enabled": False})["settings"]["torrents_enabled"] is True
    assert load_config(api.config.config_path, resolve_secrets=False).nyaa.torrents_enabled is True
    api.set_torrents_enabled(False)
    api.save_settings({"torrents_enabled": True})
    assert api.get_state_fast()["settings"]["torrents_enabled"] is False
    assert load_config(api.config.config_path, resolve_secrets=False).nyaa.torrents_enabled is False


def test_manual_scroll_freezes_the_highlight_and_catches_up_after(page) -> None:
    # While the user scrolls, the reader does not touch the words at all (class and style
    # changes re-layout the ruby text and made the scroll look like words vanished): the
    # highlight stays where it was, no frame is painted. ~150 ms after the scroll it jumps
    # to the word being spoken and moves on by itself, even before mpv answers again.
    result = page.run(
        OPEN
        + USER_SCROLL
        + r"""
        await __tick(600);
        const before = {time: audioAt(), spoken: current(), words: words()};
        const traced = ui.lnPairedTrace.length, polled = __apiCalls.length;
        await userScroll(1500);
        const rows = ui.lnPairedTrace.slice(traced);
        const during = {time: audioAt(), spoken: current(), words: words(), scrolling: scrolling(),
                        painted: rows.filter(r => ['frame', 'poll'].includes(r.event)).length,
                        deferred: rows.filter(r => r.event === 'scroll_defer').map(r => r.manual_scroll),
                        polls: __apiCalls.slice(polled).filter(c => c.name === 'light_novel_paired_state').length};
        AUDIO.hold = true;                         // mpv answers no more polls from here on
        await __tick(200);
        const after = {time: audioAt(), spoken: current(), progress: progressOf(current()), scrolling: scrolling(),
                       caughtUp: trace('scroll_catchup').length};
        await __tick(300);
        return {before, during, after, later: {spoken: current(), progress: progressOf(current())}};
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert out["before"]["spoken"] == 0 and out["before"]["time"] < 11
    assert (
        out["during"]["scrolling"] is True and out["during"]["time"] > 12
    )  # 言い方 (from 12 s) is being spoken
    assert out["during"]["spoken"] == 0 and out["during"]["words"] == out["before"]["words"]
    assert out["during"]["painted"] == 0 and all(out["during"]["deferred"])
    # Only each poll's answer is kept for later: the animation frames stop while scrolling.
    assert abs(len(out["during"]["deferred"]) - out["during"]["polls"]) <= 1 and out["during"]["polls"] >= 5
    assert (
        out["after"]["scrolling"] is False and out["after"]["caughtUp"] == 1 and out["after"]["spoken"] == 1
    )
    assert out["later"]["spoken"] == 1
    assert float(out["later"]["progress"].rstrip("%")) > float(out["after"]["progress"].rstrip("%")) + 5
