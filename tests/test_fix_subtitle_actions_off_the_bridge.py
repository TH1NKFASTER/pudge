"""WEBFLOW-12: "Find subtitles for new episodes", Retry and a chosen subtitle file
queue the work on a supervised worker and return at once."""

from __future__ import annotations

import threading
from pathlib import Path
from types import ModuleType

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryEpisode
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


class SlowPreparation:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls: list[list[Path]] = []

    def __call__(self, limit: int = 4, *, preferred_paths=(), wait_for_slot: bool = False) -> int:
        self.calls.append([Path(path) for path in preferred_paths])
        self.started.set()
        assert self.release.wait(30)
        return len(self.calls[-1])


def _join(api: WebAppApi, kind: str) -> None:
    for task in list(api.task_supervisor._tasks.values()):
        if kind in task.name:
            task.thread.join(timeout=30)


def _video(tmp_path: Path, api: WebAppApi, name: str) -> Path:
    video = (tmp_path / "library" / name).resolve()
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x")
    api.manager.db.upsert_episode(LibraryEpisode(1, "Show", 1, video, state="waiting_subtitles"))
    api.manager.db.queue_subtitle_job(video, 1, 1)
    with api.manager.db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET next_check=0")
    return video


def test_find_new_subtitles_returns_before_preparation_ends(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = _video(tmp_path, api, "Show - 01.mkv")
    slow = SlowPreparation()
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", slow)
    result = api.search_new_subtitles()  # before: blocked until every job was prepared
    assert (result["eligible"], result["processed"], result["queued"]) == (1, 0, True)
    assert slow.started.wait(10)
    assert slow.calls == [[video]]
    slow.release.set()
    _join(api, "subtitle-new-only")


def test_retry_returns_at_once_and_the_worker_prepares_it(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = _video(tmp_path, api, "Show - 02.mkv")
    slow = SlowPreparation()
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", slow)
    result = api.retry_episode_preparation(str(video))
    assert result["ok"] is True and result["queued"] is True
    assert slow.started.wait(10) and slow.calls == [[video]]
    slow.release.set()
    _join(api, "manual-subtitle-retry")


def test_worker_waits_for_a_running_maintenance_pass(tmp_path: Path, monkeypatch) -> None:
    from pudge.maintenance_lock import maintenance_lock

    api = make_api(tmp_path)
    video = _video(tmp_path, api, "Show - 03.mkv")
    slow = SlowPreparation()
    slow.release.set()
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", slow)
    held = threading.Event()
    done = threading.Event()

    def agent_pass() -> None:
        with maintenance_lock(api.config.paths.cache_dir, blocking=True):
            held.set()
            done.wait(10)

    agent = threading.Thread(target=agent_pass)
    agent.start()
    assert held.wait(10)
    api.retry_episode_preparation(str(video))
    assert not slow.started.wait(0.5)  # not alongside maintenance
    done.set()
    agent.join()
    assert slow.started.wait(10)
    _join(api, "manual-subtitle-retry")


def test_chosen_subtitle_is_aligned_in_the_background(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = _video(tmp_path, api, "Show - 04.mkv")
    chosen = tmp_path / "chosen.ja.srt"
    chosen.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    slow = SlowPreparation()
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", slow)
    monkeypatch.setattr(api.manager, "set_manual_subtitle", lambda _video, selected: selected)
    webview = ModuleType("webview")
    webview.OPEN_DIALOG = 1
    monkeypatch.setitem(__import__("sys").modules, "webview", webview)
    api.window = type("Window", (), {"create_file_dialog": lambda *_a, **_k: [str(chosen)]})()
    result = api.choose_manual_subtitle(str(video))
    assert result["ok"] is True and result["queued"] is True and result["prepared"] == 0
    assert slow.started.wait(10)
    slow.release.set()
    _join(api, "manual-subtitle-choice")
