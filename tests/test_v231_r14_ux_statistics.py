from __future__ import annotations

from pathlib import Path

import pytest

from pudge.consumption import ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


def _record_session(
    tmp_path: Path,
    *,
    kind: str,
    media_kind: str,
    method: str,
    seconds: float,
) -> tuple[ConsumptionStatistics, str]:
    db = Database(tmp_path / f"{kind}-{int(seconds)}.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(
        kind=media_kind,
        title=f"R14 {kind}",
        aliases=[("stable", f"r14:{kind}:{seconds}")],
        source_revision="r14-test",
        metadata={"page_count": 10, "duration_seconds": 1200, "character_count": 1000},
    )
    start = 1_700_000_000.0
    session = ledger.start_session(
        kind=kind,
        media_uuid=media.media_uuid,
        measurement_method=method,
        started_at_utc=start,
    )
    remaining = float(seconds)
    offset = 0.0
    while remaining > 1e-6:
        chunk = min(13.0, remaining)
        if kind == "manga":
            locator0 = locator1 = {"page_id": "p1", "page_index": 0}
            payload = {"page_count": 10}
        elif kind == "light_novel":
            locator0 = {"chapter_key": "c1", "character_offset": int(offset)}
            locator1 = {"chapter_key": "c1", "character_offset": int(offset + chunk)}
            payload = {"character_count": 1000}
        else:
            locator0 = {"position_seconds": offset}
            locator1 = {"position_seconds": offset + chunk}
            payload = {"duration_seconds": 1200, "seek_or_discontinuity": False}
        ledger.append_interval(
            session_id=session,
            kind=kind,
            interval_start_utc=start + offset,
            interval_end_utc=start + offset + chunk,
            elapsed_monotonic_ms=chunk * 1000.0,
            media=[{
                "media_uuid": media.media_uuid,
                "source_revision": "r14-test",
                "role": "primary",
                "locator_start": locator0,
                "locator_end": locator1,
            }],
            payload=payload,
        )
        offset += chunk
        remaining -= chunk
    ledger.end_session(session, ended_at_utc=start + seconds)
    return ConsumptionStatistics(db), media.media_uuid


def test_short_automatic_manga_session_is_absent_from_time_journal_and_volume(tmp_path: Path) -> None:
    stats, media_uuid = _record_session(
        tmp_path,
        kind="manga",
        media_kind="manga",
        method="reader_visible_heartbeat",
        seconds=15,
    )
    result = stats.query({"period": "all"}, now=1_700_001_000)
    assert result["summary"]["total_seconds"] == pytest.approx(0)
    assert result["summary"]["sessions"] == 0
    assert result["journal"] == []
    assert media_uuid not in result["volume"]["by_media"]


def test_one_minute_automatic_manga_session_counts_normally(tmp_path: Path) -> None:
    stats, media_uuid = _record_session(
        tmp_path,
        kind="manga",
        media_kind="manga",
        method="reader_visible_heartbeat",
        seconds=60,
    )
    result = stats.query({"period": "all"}, now=1_700_001_000)
    assert result["summary"]["total_seconds"] == pytest.approx(60)
    assert result["summary"]["sessions"] == 1
    assert len(result["journal"]) == 1
    assert media_uuid in result["volume"]["by_media"]


def test_short_anime_session_remains_counted(tmp_path: Path) -> None:
    stats, _media_uuid = _record_session(
        tmp_path,
        kind="anime",
        media_kind="anime_episode",
        method="bounded_active_chunks",
        seconds=15,
    )
    result = stats.query({"period": "all"}, now=1_700_001_000)
    assert result["summary"]["total_seconds"] == pytest.approx(15)
    assert result["summary"]["sessions"] == 1


def test_r14_short_session_policy_covers_production_non_anime_recorders() -> None:
    source = (ROOT / "pudge/consumption_statistics.py").read_text(encoding="utf-8")
    assert 'MIN_AUTOMATIC_SESSION_SECONDS = 60.0' in source
    for method in (
        "reader_visible_heartbeat",
        "audiobook_active_playback",
        "vn_capture_session",
        "companion_observation",
    ):
        assert f'"{method}"' in source
    assert 'if corrected or _normalize_kind(kind) == "anime":' in source


def test_statistics_chart_fills_available_width_for_small_day_counts() -> None:
    source = (WEB / "statistics.js").read_text(encoding="utf-8")
    assert "grid-template-columns:repeat(var(--stats-columns,1),minmax(20px,1fr))" in source
    assert 'style="--stats-columns:${Math.max(1,days.length)}"' in source
    assert ".stats-bar-wrap{min-width:20px;width:100%" in source
    assert "max-width:54px" not in source


def test_jiten_deck_choice_stays_open_and_becomes_next_card_default() -> None:
    source = (WEB / "reading_tools.js").read_text(encoding="utf-8")
    assert "const selectedDeckByBackend = new Map();" in source
    assert "localStorage.getItem(`pudge.studyDeck.${key}`)" in source
    assert "localStorage.setItem(`pudge.studyDeck.${key}`, value)" in source
    assert "select.dataset.pudgeStudyBackend = String(activeToken.backend || 'jiten');" in source
    assert "select.value = remembered;" in source
    assert "rememberStudyDeck(backend, event.target.value || '');" in source
    # PudgeSelect mounts its dropdown under body, so this guard must precede
    # the generic outside-card close path.
    menu_guard = "if (event.target.closest?.('.pudge-select-menu')) return;"
    outside_close = "if (pop?.classList.contains('open') && !event.target.closest?.('#pudgeStudyCard')) closeStudyCard();"
    assert menu_guard in source and outside_close in source
    assert source.index(menu_guard) < source.index(outside_close)


def test_double_and_triple_click_do_not_trigger_browser_word_selection() -> None:
    source = (WEB / "reading_tools.js").read_text(encoding="utf-8")
    assert "function studyTextClickTarget(target)" in source
    assert "Number(event.detail || 0) < 2" in source
    assert "document.addEventListener('dblclick', event =>" in source
    assert "selection.removeAllRanges();" in source
    # Single-click drag selection is deliberately left untouched: prevention
    # only starts on the second mousedown of a multi-click gesture.
    assert "event.button !== 0 || Number(event.detail || 0) < 2" in source


def test_mouse_drag_cover_preview_is_disabled_for_playable_anime_cards_only() -> None:
    source = (WEB / "cover_preview.js").read_text(encoding="utf-8")
    assert "const allowMouseDragPreview = image =>" in source
    assert '[data-continue-card="1"],.airing-card[data-action="play"]' in source
    assert "if(pressedCover&&allowMouseDragPreview(pressedCover))event.preventDefault();" in source
    assert "!image||!allowMouseDragPreview(image)||!imageSource(image)" in source
    assert "gesturestart" in source  # trackpad/pinch preview remains supported.


def test_escape_selection_clear_precedes_transient_surfaces_and_has_keyup_fallback() -> None:
    source = (WEB / "index.html").read_text(encoding="utf-8")
    clear = source.index("if(clearSelectionOnEscape('keydown'))return;")
    select = source.index("if(window.PudgeSelect?.closeIfOpen?.())return;")
    preview = source.index("if(window.PudgeCoverPreview?.closeIfOpen?.())return;")
    assert clear < select < preview
    assert "window.addEventListener('keyup',captureEscapeCardSelection,true);" in source
    assert "escape.selection_clear" in source
