"""Wave-4 follow-up to WEBFLOW-8: a completion recorded while the poll's reconcile runs
is filed by the next poll, not up to 5 minutes later (reviewer repro rev7)."""

from __future__ import annotations

from pathlib import Path

from pudge.manager import AnimeManager
from tests.test_fix_poll_reconcile_cost import _download, _manager


def test_completion_landing_during_poll_reconcile_is_not_skipped(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    _download(manager, 1)
    real = AnimeManager.reconcile_completed_download_rows
    landed: dict[str, Path] = {}

    def racing(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        if not landed:
            # sync_downloads in another thread / the agent records a completion while
            # the poll's reconcile is running (after it read the downloads)
            landed["video"] = _download(manager, 2, with_row=False)
        return result

    monkeypatch.setattr(AnimeManager, "reconcile_completed_download_rows", racing)
    manager.reconcile_completed_download_rows_from_poll()
    monkeypatch.setattr(AnimeManager, "reconcile_completed_download_rows", real)
    for _ in range(3):
        manager.reconcile_completed_download_rows_from_poll()
    row = manager.db.episode_by_path(landed["video"].resolve())
    assert row is not None, "completed download not filed by later polls (skipped up to 5 min)"


def test_a_quiet_library_still_settles_after_one_extra_run(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    for number in range(1, 4):
        _download(manager, number)
    runs: list[int] = []
    real = AnimeManager.reconcile_completed_download_rows

    def counting(self, *args, **kwargs):
        runs.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(AnimeManager, "reconcile_completed_download_rows", counting)
    for _ in range(6):
        manager.reconcile_completed_download_rows_from_poll()
    assert len(runs) <= 2
