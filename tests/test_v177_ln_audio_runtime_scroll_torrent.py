"""v1.77: a spoken chapter title holds the text clock at zero, the runtime position lookup,
scrolling while listening, the torrent switch's state, paired-poll diagnostics (behaviour;
audit 71Б rewrite).

Covered elsewhere: Jiten's parse is swapped in only after scrolling stops and keeps loaded
pictures, blurred pictures stay visible while scrolling,
test_v141_ln_blur_scroll_open_alignment_perf.py; the previous word's reading lingers 100 ms
even where it overlaps the next, test_v131_furigana_search_images_stt.py::
test_previous_reading_lingers_briefly_even_where_it_overlaps_the_next and
test_v0749_ln_listen_resume_and_furigana_timing.py; the paired state's position lookup time,
test_v163_ln_audio_startup_alignment.py, and its place in the exported sync trace,
test_v180_ln_furigana_clock_scroll.py::test_sync_trace_export_carries_clock_and_render_diagnostics.
Dropped: the alignment cache revision string (no behaviour of its own)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pudge.reading_audio_alignment as alignment_mod
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime
from pudge.reading_audio_alignment import (
    _raw_anchors_with_leading_hold,
    light_novel_position_for_audio,
)
from t71g19_support import LN_PAIRED, OPEN
from t71g22_support import USER_SCROLL, ln_audio_pair
from test_web_app import make_api


def test_second_chapter_marker_holds_zero_until_verified_prose() -> None:
    chapter = {
        "chapter_index": 1,
        "start": 6852.68,
        "end": 7000.0,
        "normalized_length": 1000,
        "leading_prefix_start": 6856.134,
        "leading_prefix_debug": {"recovered": True},
        "anchors": [
            {"offset": 0, "time": 6852.68},
            {"offset": 10.999, "time": 6853.62},
            {"offset": 10.999, "time": 6856.10},
            {"offset": 23, "time": 6861.60},
            {"offset": 35, "time": 6865.06},
        ],
    }
    rows = _raw_anchors_with_leading_hold(chapter)
    assert rows[:2] == [
        {"offset": 0, "time": 6852.68},
        {"offset": 0, "time": 6856.134},
    ]
    assert not any(float(row["offset"]) > 0 and float(row["time"]) < 6856.133 for row in rows)
    assert any(float(row["offset"]) == 23 and float(row["time"]) == pytest.approx(6861.60) for row in rows)


def test_live_position_prefers_later_chapter_in_boundary_overlap_and_indexes_once(monkeypatch) -> None:
    calls = 0
    original = alignment_mod._prune_unreachable_leading_anchors

    def counted(rows, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(rows, *args, **kwargs)

    monkeypatch.setattr(alignment_mod, "_prune_unreachable_leading_anchors", counted)
    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "start": 0.0,
                "end": 10.5,
                "normalized_length": 100,
                "anchors": [{"offset": 0, "time": 0.0}, {"offset": 100, "time": 10.5}],
            },
            {
                "chapter_index": 1,
                "start": 10.0,
                "end": 20.0,
                "normalized_length": 100,
                "anchors": [{"offset": 0, "time": 10.0}, {"offset": 100, "time": 20.0}],
            },
        ]
    }
    first = light_novel_position_for_audio(alignment, 10.2)
    assert first is not None and first["chapter_index"] == 1
    first_call_count = calls
    assert first_call_count == 2
    again = light_novel_position_for_audio(alignment, 10.3)
    assert again is not None and again["chapter_index"] == 1
    assert calls == first_call_count
    assert "_runtime_audio_position_index" in alignment


def test_scrolling_hides_the_reading_shown_early_for_the_next_word(page) -> None:
    # The next word's furigana shows 0.1 s before it is spoken; when the user starts
    # scrolling that early reading goes away at once (it stayed on a word scrolled away).
    result = page.run(
        OPEN
        + USER_SCROLL
        + r"""
        for (let k = 0; k < 300 && !words()[1].preview; k++) await __tick(10);
        const before = {spoken: current(), early: words().map(w => w.preview)};
        wheel(); scroller.dispatchEvent(new Event('scroll'));
        return {before, scrolling: {spoken: current(), early: words().map(w => w.preview)}};
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert out["before"] == {"spoken": 0, "early": [False, True] + [False] * 5}
    assert out["scrolling"] == {"spoken": 0, "early": [False] * 7}


def test_torrent_switch_shows_off_before_the_first_state_arrives(page) -> None:
    # Until the backend has answered, the switch is the red "off" one, never a neutral grey.
    result = page.run(
        "const b = $('torrentToggleButton'); return {classes: b.className, text: b.innerText, disabled: b.disabled};",
        ready=False,
    )
    assert result.value["classes"].split() == ["torrent-off"] and result.value["disabled"] is False


def test_switching_torrents_on_shows_in_the_next_state_at_once(tmp_path: Path) -> None:
    # The window's state is cached between changes. Switching torrents on drops that
    # cache: an unfinished download of a home card no longer shows as paused.
    api = make_api(tmp_path)
    api.config.aria2.enabled = True
    api.manager.config.aria2.enabled = True
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=5,
            title="Show",
            status="CURRENT",
            episodes=12,
            media_status="FINISHED",
            progress=0,
            format="TV",
        )
    )
    api.manager.db.upsert_download(
        DownloadItem(
            torrent_hash="abc",
            name="Show 01",
            state="downloading",
            progress=0.3,
            save_path=str(tmp_path),
            content_path=str(tmp_path / "Show 01.mkv"),
            media_id=5,
            episode=1,
            raw={"backend": "aria2"},
        )
    )

    def card_download(state: dict) -> dict:
        [card] = state["home"]["download_available"]
        return {
            "enabled": state["settings"]["torrents_enabled"],
            "shown": card["download"]["effective_state"],
        }

    assert card_download(api.get_state_fast()) == {"enabled": False, "shown": "paused"}
    api.set_torrents_enabled(True)
    assert card_download(api.get_state_fast()) == {"enabled": True, "shown": "downloading"}


def test_torrents_off_pauses_running_tasks_but_not_finished_aria2_ones(tmp_path: Path) -> None:
    # aria2 keeps the metadata of finished / failed tasks whose GIDs are gone: pausing them
    # failed with HTTP 400 on every check. qBittorrent's finished torrents still seed, so
    # Torrents off pauses them.
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.aria2.enabled = cfg.qbittorrent.enabled = True
    cfg.nyaa.torrents_enabled = False
    manager = AnimeManager(cfg, log=lambda _message: None)

    def item(name: str, state: str, **raw: object) -> DownloadItem:
        return DownloadItem(
            torrent_hash=name,
            name=name,
            state=state,
            progress=1.0 if state.startswith("complet") else 0.5,
            save_path=str(tmp_path),
            content_path=str(tmp_path / name),
            raw=dict(raw),
        )

    class Client:
        def __init__(self, items: list[DownloadItem]) -> None:
            self.items, self.paused = items, []

        def torrents(self, category: str = "") -> list[DownloadItem]:
            return list(self.items)

        def pause(self, torrent_hash: str) -> None:
            self.paused.append(torrent_hash)

        def close(self) -> None:
            pass

    aria2 = Client(
        [
            item("running", "downloading"),
            item("complete", "complete"),
            item("completed", "completed"),
            item("failed", "error"),
            item("orphan", "waiting", orphaned_metadata=True),
            item("no-control", "active", recoverable_missing_control=True),
        ]
    )
    category = cfg.qbittorrent.category
    qbittorrent = Client(
        [
            item("q-finished", "completed", category=category),
            item("q-seeding", "uploading", category=category),
        ]
    )
    manager.torrent_clients = lambda: [("aria2", aria2), ("qbittorrent", qbittorrent)]  # type: ignore[method-assign]
    manager.sync_downloads()
    assert aria2.paused == ["running"]
    assert qbittorrent.paused == ["q-finished", "q-seeding"]


def test_paired_polls_read_the_alignment_report_once(tmp_path: Path, monkeypatch) -> None:
    # The reader polls four times a second; the alignment report (its quality grade) is
    # read from disk once, not on every poll.
    _ln, audio, ln_id, audio_id = ln_audio_pair(tmp_path, ["狼と香辛料。" * 20])
    alignment = {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 0,
                "start": 0.0,
                "end": 100.0,
                "normalized_length": 120,
                "anchors": [{"offset": 0, "time": 0.0}, {"offset": 120, "time": 100.0}],
            }
        ],
    }
    monkeypatch.setattr(audio, "_load_alignment", lambda *_args: alignment)
    report = audio._alignment_report_path(ln_id, audio_id)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"schema": "pudge-alignment-report-v1", "grade": "good"}), encoding="utf-8")
    reads: list[Path] = []
    read_text = Path.read_text

    def counting(path: Path, *args, **kwargs):
        if path == report:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    grades = [audio.paired_state(ln_id)["alignment"]["quality_grade"] for _ in range(4)]
    assert grades == ["good"] * 4
    assert len(reads) == 1
