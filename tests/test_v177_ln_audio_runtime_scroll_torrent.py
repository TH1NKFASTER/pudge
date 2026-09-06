from __future__ import annotations

from pathlib import Path

import pytest

import pudge.reading_audio_alignment as alignment_mod
from pudge.reading_audio_alignment import (
    _raw_anchors_with_leading_hold,
    light_novel_position_for_audio,
)

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
AUDIOBOOKS = ROOT / "pudge" / "audiobooks.py"
WEB_APP = ROOT / "pudge" / "web_app.py"
MANAGER = ROOT / "pudge" / "manager.py"


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


def test_scroll_keeps_images_and_never_forces_jiten_dom_swap_mid_scroll() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert ".ln-reader.blur-images .ln-inline-image:not(.revealed) img{filter:blur(44px);cursor:pointer}" in html
    assert "ln-reader-scrolling .ln-reader.blur-images" not in html
    fn = html.split("function applyParsedLnChapterWhenIdle", 1)[1].split("function pollLnParse", 1)[0]
    assert "sinceScroll<180" in fn
    assert "queuedAt" not in fn
    assert "<1800" not in fn


def test_manual_scroll_clears_only_cached_furigana_preview_and_handoff_lingers_100ms() -> None:
    html = INDEX.read_text(encoding="utf-8")
    helper = html.split("function clearLnPairedFuriganaPreviewState", 1)[1].split("function lnPairedFuriganaPaintWidth", 1)[0]
    assert "querySelectorAll" not in helper
    assert "ui.lnPairedPreviewWord" in helper
    handler = html.split("$('lnReaderScroll').addEventListener('scroll',()=>{", 1)[1].split("},{passive:true});", 1)[0]
    assert "if(!wasScrolling)clearLnPairedFuriganaPreviewState()" in handler
    render = html.split("function renderLnPairedPosition", 1)[1].split("function startLnPairedInterpolation", 1)[0]
    assert "lingerLnPairedFurigana(old,false)" in render
    assert "lnPairedFuriganaPaintOverlaps(old,word)" not in render
    linger = html.split("function lingerLnPairedFurigana", 1)[1].split("function stopLnPairedPoll", 1)[0]
    assert "},100)" in linger


def test_torrent_state_is_red_immediately_and_backend_invalidates_cached_off_snapshot() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="torrentToggleButton" class="torrent-off"' in html
    backend = WEB_APP.read_text(encoding="utf-8")
    fn = backend.split("    def set_torrents_enabled", 1)[1].split("\n    def ", 1)[0]
    assert "ui_state_cache.invalidate()" in fn
    manager = MANAGER.read_text(encoding="utf-8")
    assert '"complete", "completed"' in manager
    assert "GIDs" in manager or "expired GIDs" in manager


def test_v177_runtime_diagnostics_and_report_cache_contract() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert "perf_revision:'ln-paired-v178'" in html
    assert "backend_lookup_ms:Number(state.position_lookup_ms||0)" in html
    audio = AUDIOBOOKS.read_text(encoding="utf-8")
    assert 'reading-audio-v3-leading-prefix-v18' in audio
    assert "self._alignment_report_cache" in audio
    assert '"position_lookup_ms": position_lookup_ms' in audio
