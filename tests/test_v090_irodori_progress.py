from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.web_app as web_app_module
from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi


def _api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebAppApi:
    monkeypatch.setattr(web_app_module, "DATA_DIR", tmp_path / "data")
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    write_config(config, config.config_path)
    return WebAppApi(config.config_path)


def _book(api: WebAppApi, tmp_path: Path) -> dict:
    novel = tmp_path / "Novel.txt"
    novel.write_text("第一章\n猫が好きです。\n\n第二章\n犬も好きです。", encoding="utf-8")
    book = api.light_novels.import_file(novel)
    api.light_novels.save_settings({"irodori_tts_enabled": True})
    return book


def test_duplicate_irodori_click_returns_existing_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    book = _book(api, tmp_path)
    release = threading.Event()
    entered = threading.Event()

    def worker(book_id: int, job_id: str) -> None:
        entered.set()
        release.wait(timeout=5)
        api.job_center.finish(job_id)
        with api._irodori_tts_lock:
            api._irodori_tts_threads.pop(book_id, None)
            api._irodori_tts_job_ids.pop(book_id, None)

    monkeypatch.setattr(api, "_irodori_tts_worker", worker)
    first = api.generate_light_novel_tts(int(book["id"]))
    assert entered.wait(timeout=2)
    second = api.generate_light_novel_tts(int(book["id"]))
    assert first["started"] is True
    assert second["started"] is False
    assert second["running"] is True
    assert second["job_id"] == first["job_id"]
    assert second["job"]["active"] is True
    release.set()


def test_light_novel_state_exposes_irodori_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    book = _book(api, tmp_path)
    job_id = api.job_center.start("irodori_tts", "test", payload={"book_id": int(book["id"])}, total=4)
    api.job_center.update(job_id, state="running", current=2, total=4, message="Generating chapter 3/4")

    class Alive:
        def is_alive(self) -> bool:
            return True

    with api._irodori_tts_lock:
        api._irodori_tts_job_ids[int(book["id"])] = job_id
        api._irodori_tts_threads[int(book["id"])] = Alive()  # type: ignore[assignment]

    state = api.light_novel_state()
    row = next(item for item in state["books"] if int(item["id"]) == int(book["id"]))
    assert state["irodori_tts_active"] is True
    assert row["irodori_tts"]["active"] is True
    assert row["irodori_tts"]["current"] == pytest.approx(2)
    assert row["irodori_tts"]["total"] == pytest.approx(4)
    assert row["irodori_tts"]["progress"] == pytest.approx(0.5)
    assert row["irodori_tts"]["message"] == "Generating chapter 3/4"


def test_irodori_server_exit_surfaces_server_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    log = api._irodori_server_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("boot\nModuleNotFoundError: bad dependency\n", encoding="utf-8")
    api._irodori_server_process = SimpleNamespace(poll=lambda: 7)  # type: ignore[assignment]
    detail = api._irodori_server_failure_detail()
    assert "exited with code 7" in detail
    assert "ModuleNotFoundError: bad dependency" in detail


def test_v90_ui_disables_repeat_irodori_and_renders_progress() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "lnIrodoriStarting:new Set()" in html
    assert 'class="ln-irodori-progress"' in html
    assert "generationBusy" in html
    assert "patchLnAudiobookProgress" in html
    assert "irodori_tts_active" in html
    assert "scheduleLnStatePoll" in html
