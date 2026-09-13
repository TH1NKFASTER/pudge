from __future__ import annotations

from pathlib import Path


def test_opening_incomplete_volume_does_not_auto_start_volume_ocr() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    start = js.index("async function ensureCurrentBookPrepared()")
    end = js.index("async function recognizeWholeBook()", start)
    ensure = js[start:end]
    assert "start_manga_ocr_book" not in ensure
    assert "manga_ocr_book_status(bookId)" in ensure
    assert "mangaOcrJobActive(status)" in ensure
    assert "pollCurrentBookPreparation(bookId)" in ensure


def test_inactive_incomplete_volume_has_enabled_button_and_no_fake_progress() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    progress = js[
        js.index("function mangaOcrProgressText(status)"):
        js.index("function mangaOcrJobActive(status)")
    ]
    helper = js[
        js.index("function mangaOcrJobActive(status)"):
        js.index("function syncMangaOcrUi(status)")
    ]
    sync = js[
        js.index("function syncMangaOcrUi(status)"):
        js.index("async function pollCurrentBookPreparation")
    ]
    assert "const active = Boolean(status?.running);" in progress
    assert "return '';" in progress
    assert "paused" not in progress
    assert "return Boolean(status?.running);" in helper
    assert "button.disabled = active" in sync


def test_debug_fresh_is_durable_force_rebuild_and_invalidates_candidate_fingerprint() -> None:
    root = Path(__file__).resolve().parents[1]
    manager = (root / "pudge/manager.py").read_text(encoding="utf-8")
    start = manager.index("def force_fresh_subtitle_selection")
    end = manager.index("\n    def ", start + 10)
    fresh = manager[start:end]
    assert 'set_state(self._subtitle_force_rebuild_state_key(video), "1")' in fresh
    assert "delete_state(self._subtitle_candidate_fingerprint_state_key(video))" in fresh
    assert 'set_state(self._debug_force_subtitle_state_key(video), "1")' in fresh
    # Marker must exist before the current selection is cleared, otherwise
    # legacy missing-selection recovery can restore the just-cleared old SRT.
    assert fresh.index("_subtitle_force_rebuild_state_key") < fresh.index("clear_subtitle_selection(video)")
    assert fresh.index("_subtitle_candidate_fingerprint_state_key") < fresh.index("clear_subtitle_selection(video)")


def test_legacy_history_repair_is_bypassed_when_force_rebuild_marker_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    manager = (root / "pudge/manager.py").read_text(encoding="utf-8")
    process_start = manager.index("def process_subtitle_jobs")
    process_end = manager.index("\n    def ", process_start + 10)
    process = manager[process_start:process_end]
    assert "force_rebuild = self.db.get_state(force_rebuild_key" in process
    assert "if not force_rebuild and self._legacy_missing_selection_needs_force_rebuild(video):" in process
    assert 'command.append("--force-search")' in process
    assert 'command.append("--resync")' in process
