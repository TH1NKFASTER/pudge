"""Shared window set-ups for the manga reader / volume OCR tests of audit 71Б (workstream g08).
This module has no tests of its own.

Built on tests/t71g03_support.py (MANGA: a two-volume library, READER: open(id), pages(), callsOf()).

OCR_STATUS (for `before`, after MANGA): the backend's volume OCR status is window.STATUS
(tests assign a new status between ticks). window.MANGA.hold.status = true makes status
answers wait for window.release('status').

OCR_UI (run body, after READER):
  ocrUi()   what the reader's toolbar shows about volume OCR:
            {progress: text or null when hidden, button: {text, disabled, busy}}
  statusCalls()  number of manga_ocr_book_status calls so far
"""

from __future__ import annotations

OCR_STATUS = r"""
window.waiting.status = [];
window.STATUS = {book_id: 3, running: false, state: 'ready', complete: true, total_pages: 6, completed: 6, cached_pages: 6};
window.MANGA.status = () => ({...window.STATUS});
__api.manga_ocr_book_status = async id => {
  if (window.MANGA.hold.status) await window.held('status');
  return window.MANGA.status(id);
};
"""

OCR_UI = r"""
const ocrUi = () => {
  const button = document.querySelector('#mangaReaderV2 [data-manga-v2-action="ocr-book"]');
  return {
    progress: (__visible('#mangaV2OcrProgress') && __text('#mangaV2OcrProgress')) || null,
    button: {text: button.textContent.trim(), disabled: button.disabled, busy: button.classList.contains('busy')},
  };
};
const statusCalls = () => callsOf('manga_ocr_book_status').length;
"""


# ---------------------------------------------------------------- Python side


def subtitle_library(tmp_path, monkeypatch):
    """An AnimeManager with one Ready episode whose subtitle (old.srt, outside the cache)
    was selected earlier and recorded in the subtitle history, and a fake preparation
    process that reports new.srt. Returns (manager, video, commands): every preparation
    command line the manager started."""
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.qbittorrent.enabled = False
    manager = AnimeManager(cfg, log=lambda _message: None)
    video = (cfg.library.root_dir / "Show - 01.mkv").resolve()
    video.write_bytes(b"video")
    old = (tmp_path / "old.srt").resolve()
    old.write_text("1\n00:00:01,000 --> 00:00:02,000\n古い\n", encoding="utf-8")
    new = (tmp_path / "new.srt").resolve()
    new.write_text("1\n00:00:01,000 --> 00:00:02,000\n新しい\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=5,
            title="Show",
            episode=1,
            video_path=video,
            subtitle_path=old,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=5,
        episode=1,
        source="jimaku",
        candidate_name="old.srt",
        candidate_path=old,
        status="selected",
        details={"final_path": str(old)},
    )
    manager.db.set_state(manager._subtitle_candidate_fingerprint_state_key(video), "same-candidates")
    commands: list[list[str]] = []

    class Preparation:
        def __init__(self, command, **_kwargs):
            commands.append([str(part) for part in command])
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, *_args, **_kwargs):
            return (
                f"PREPARED_SUBTITLE={new}\nSUBTITLE_CANDIDATE_FINGERPRINT=same-candidates\n"
                'PREPARE_STATUS=ready\nPREPARED_SUBTITLE_META={"source":"jimaku","name":"new.srt"}\n',
                "",
            )

    # Only the manager's own process starts are replaced (ps, ffprobe ... stay real).
    import subprocess
    from types import SimpleNamespace

    import pudge.manager as manager_module

    monkeypatch.setattr(
        manager_module, "subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": Preparation})
    )
    return manager, video, commands
