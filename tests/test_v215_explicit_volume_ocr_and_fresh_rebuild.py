"""Opening an unfinished manga volume starts OCR of its missing pages only; Debug fresh
rebuilds an episode's subtitle from scratch (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

from pathlib import Path

from t71g03_support import MANGA, READER
from t71g08_support import OCR_STATUS, OCR_UI, subtitle_library

NO_GATE = {"settings": {"review_gate_manga_enabled": False}}


def test_opening_an_unfinished_volume_starts_ocr_of_its_missing_pages_only(page) -> None:
    result = page.run(
        READER
        + OCR_UI
        + r"""
        const base = {book_id: 3, total_pages: 6};
        const opened = async (status, complete) => {
          for (const book of window.MANGA.books) book.ocr_complete = complete;
          window.STATUS = {...base, ...status};
          const before = callsOf('start_manga_ocr_book').length;
          await open(3);
          const out = {start: callsOf('start_manga_ocr_book').slice(before), ui: ocrUi()};
          await __click('[data-manga-v2-action="close"]'); await __tick(50);
          return out;
        };
        // The backend skips the automatic run (on battery): the volume stays idle.
        __api.start_manga_ocr_book = async (id, refresh, attemptOf, background, auto) => ({...window.STATUS, auto_skipped: 'on_battery'});
        return {
          unfinished: await opened({running: false, state: 'idle', complete: false, completed: 2, cached_pages: 2}, false),
          complete: await opened({running: false, state: 'ready', complete: true, completed: 6, cached_pages: 6}, true),
          running: await opened({running: true, state: 'running', complete: false, completed: 3, processing: 1, current_page: 4}, false),
          labels: {volume: t('manga.ocrVolume'), running: t('manga.ocrRunning')},
        };
        """,
        before=MANGA + OCR_STATUS,
        state=NO_GATE,
    )
    out = result.value
    # Not a rebuild (refresh=false keeps finished pages); auto=true lets the backend skip it
    # (battery, only failed pages left) and stop it when the reader closes.
    assert out["unfinished"]["start"] == [[3, False, "", False, True]]
    # A skipped run shows no fake progress and leaves the button usable.
    assert out["unfinished"]["ui"] == {
        "progress": None,
        "button": {"text": out["labels"]["volume"], "disabled": False, "busy": False},
    }
    # A finished volume starts nothing; a running job is only reconnected to.
    assert out["complete"]["start"] == [] and out["running"]["start"] == []
    assert out["running"]["ui"]["button"] == {
        "text": out["labels"]["running"],
        "disabled": True,
        "busy": True,
    }


def test_debug_fresh_rebuilds_the_subtitle_from_scratch(tmp_path: Path, monkeypatch) -> None:
    manager, video, commands = subtitle_library(tmp_path, monkeypatch)
    force_key = manager._subtitle_force_rebuild_state_key(video)
    fingerprint_key = manager._subtitle_candidate_fingerprint_state_key(video)
    # What anyone reading the database sees at the moment the old selection disappears
    # (a subtitle worker running at the same time must already see the rebuild request).
    seen: list[tuple[str, str]] = []
    clear = manager.db.clear_subtitle_selection

    def watched_clear(path, **kwargs):
        seen.append((manager.db.get_state(force_key, ""), manager.db.get_state(fingerprint_key, "")))
        return clear(path, **kwargs)

    monkeypatch.setattr(manager.db, "clear_subtitle_selection", watched_clear)

    manager.force_fresh_subtitle_selection(video)

    assert seen == [("1", "")]
    episode = manager.db.episode_by_path(video)
    assert episode.subtitle_path is None and episode.state != "ready"

    assert manager.process_subtitle_jobs(limit=1) == 1
    # The old subtitle in the history is not brought back; a full search and resync runs.
    [command] = commands
    assert "--force-search" in command and "--resync" in command
    assert "--previous-candidate-fingerprint" not in command
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path is not None
    assert episode.subtitle_path.read_text(encoding="utf-8").endswith("新しい\n")
    assert manager.db.get_state(force_key, "") == ""
