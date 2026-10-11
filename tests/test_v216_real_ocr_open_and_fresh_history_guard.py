"""Opening a volume after a busy one, and the subtitle-history guard of explicit
rebuilds (behaviour; audit 71Б rewrite). The OCR button while the first page loads:
tests/test_v213_manga_ocr_atomic_volume.py."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.manager import AnimeManager
from t71g03_support import MANGA, READER
from t71g08_support import OCR_STATUS, OCR_UI


def test_next_volume_does_not_inherit_the_busy_ocr_button_of_the_previous_one(page) -> None:
    result = page.run(
        READER
        + OCR_UI
        + r"""
        for (const book of window.MANGA.books) book.ocr_complete = false;
        window.STATUS = {book_id: 3, running: true, state: 'running', complete: false, total_pages: 6, completed: 1, processing: 1, current_page: 2};
        await open(3);
        const out = {first: ocrUi().button};
        // Straight to volume 2 (its first page and OCR status are slow to come).
        window.MANGA.hold.page = 0; window.MANGA.hold.status = true;
        window.STATUS = {book_id: 4, running: false, state: 'idle', complete: false, total_pages: 6, completed: 0};
        const opening = PudgeMangaReaderV2.openBook(4);
        await __tick(50);
        out.loading = ocrUi().button;
        window.release('page'); window.MANGA.hold.status = false; window.release('status');
        await __settle(opening); await __tick(50);
        out.loaded = ocrUi().button;
        out.labels = {running: t('manga.ocrRunning'), volume: t('manga.ocrVolume')};
        return out;
        """,
        before=MANGA + OCR_STATUS,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    assert out["first"] == {"text": out["labels"]["running"], "disabled": True, "busy": True}
    assert out["loading"]["disabled"] is False and out["loading"]["busy"] is False
    assert out["loaded"] == {"text": out["labels"]["volume"], "disabled": False, "busy": False}


def test_history_recovery_works_with_a_database_without_state_keys(tmp_path: Path) -> None:
    # Older database adapters have no get_state: the explicit-rebuild check must not
    # break the normal recovery of a selected subtitle from the history.
    subtitle = tmp_path / "selected.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    ready: list[tuple] = []

    class LegacyDb:
        def episode_by_path(self, _video):
            return SimpleNamespace(subtitle_path=None, embedded_subtitle_id=None)

        def latest_selected_subtitle(self, _video):
            return {"source": "jimaku", "candidate_path": "", "details": {"final_path": str(subtitle)}}

        def set_subtitle_ready(self, video, path, sid, **kwargs):
            ready.append((video, path, sid, kwargs.get("origin")))

    manager = _manager_with_db(LegacyDb())
    video = tmp_path / "video.mkv"
    assert (
        manager._recover_selected_text_subtitle_from_history(video, media_id=1, episode=2)
        == subtitle.resolve()
    )
    assert ready == [(video, subtitle.resolve(), None, "jimaku")]


class _MarkerDb:
    def __init__(self, active_prefix: str) -> None:
        self.active_prefix = active_prefix
        self.history_reads = 0
        self.episode_reads = 0

    def get_state(self, key: str, default: str = "") -> str:
        return "1" if key.startswith(self.active_prefix) else default

    def episode_by_path(self, _video: Path):
        self.episode_reads += 1
        return SimpleNamespace(subtitle_path=None, embedded_subtitle_id=None)

    def latest_selected_subtitle(self, _video: Path):
        self.history_reads += 1
        raise AssertionError("explicit fresh/rebuild must not read old subtitle history")


def _manager_with_db(db: _MarkerDb) -> AnimeManager:
    manager = AnimeManager.__new__(AnimeManager)
    manager.db = db
    manager.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    manager.config = SimpleNamespace(
        matching=SimpleNamespace(ocr_counts_as_ready=True),
    )
    return manager


def test_force_rebuild_marker_bypasses_history_recovery_before_any_resurrection() -> None:
    db = _MarkerDb("subtitle_force_rebuild:")
    manager = _manager_with_db(db)
    result = manager._recover_selected_text_subtitle_from_history(
        Path("/tmp/pudge-v216-video.mkv"), media_id=159309, episode=10
    )
    assert result is None
    assert db.history_reads == 0
    assert db.episode_reads == 0


def test_debug_fresh_marker_also_bypasses_history_recovery() -> None:
    db = _MarkerDb("debug_force_subtitle:")
    manager = _manager_with_db(db)
    result = manager._recover_selected_text_subtitle_from_history(
        Path("/tmp/pudge-v216-video.mkv"), media_id=159309, episode=10
    )
    assert result is None
    assert db.history_reads == 0
    assert db.episode_reads == 0
