from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.manager import AnimeManager


def test_open_book_never_pre_disables_volume_ocr_before_page_render() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / 'pudge/web/manga_reader_v2.js').read_text(encoding='utf-8')
    start = js.index('async function openBook(bookId)')
    end = js.index('function closeReader()', start)
    block = js[start:end]
    show_at = block.index('await showCurrent();')
    before_render = block[:show_at]
    assert 'ocrButton.disabled = true' not in before_render
    assert 'ocrButton.disabled = false' in before_render
    assert "ocrButton.classList.remove('busy')" in before_render
    assert 'manga_ocr_book_status(Number(bookId))' in before_render
    assert before_render.index('manga_ocr_book_status(Number(bookId))') < show_at


def test_history_guard_keeps_legacy_db_stubs_compatible() -> None:
    root = Path(__file__).resolve().parents[1]
    manager = (root / 'pudge/manager.py').read_text(encoding='utf-8')
    start = manager.index('def _recover_selected_text_subtitle_from_history')
    end = manager.index('def process_subtitle_jobs', start)
    block = manager[start:end]
    assert 'get_state = getattr(self.db, "get_state", None)' in block
    assert 'if callable(get_state):' in block


class _MarkerDb:
    def __init__(self, active_prefix: str) -> None:
        self.active_prefix = active_prefix
        self.history_reads = 0
        self.episode_reads = 0

    def get_state(self, key: str, default: str = '') -> str:
        return '1' if key.startswith(self.active_prefix) else default

    def episode_by_path(self, _video: Path):
        self.episode_reads += 1
        return SimpleNamespace(subtitle_path=None, embedded_subtitle_id=None)

    def latest_selected_subtitle(self, _video: Path):
        self.history_reads += 1
        raise AssertionError('explicit fresh/rebuild must not read old subtitle history')


def _manager_with_db(db: _MarkerDb) -> AnimeManager:
    manager = AnimeManager.__new__(AnimeManager)
    manager.db = db
    manager.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    manager.config = SimpleNamespace(
        matching=SimpleNamespace(ocr_counts_as_ready=True),
    )
    return manager


def test_force_rebuild_marker_bypasses_history_recovery_before_any_resurrection() -> None:
    db = _MarkerDb('subtitle_force_rebuild:')
    manager = _manager_with_db(db)
    result = manager._recover_selected_text_subtitle_from_history(
        Path('/tmp/pudge-v216-video.mkv'), media_id=159309, episode=10
    )
    assert result is None
    assert db.history_reads == 0
    assert db.episode_reads == 0


def test_debug_fresh_marker_also_bypasses_history_recovery() -> None:
    db = _MarkerDb('debug_force_subtitle:')
    manager = _manager_with_db(db)
    result = manager._recover_selected_text_subtitle_from_history(
        Path('/tmp/pudge-v216-video.mkv'), media_id=159309, episode=10
    )
    assert result is None
    assert db.history_reads == 0
    assert db.episode_reads == 0
