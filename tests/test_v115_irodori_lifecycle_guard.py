"""The Irodori TTS server Pudge starts lives only as long as it is needed (behaviour; audit 71Б
rewrite): a settings test stops it again, audiobook generation keeps it while generating,
it is stopped with its whole process group, and importing a book that already has an
audiobook does not start a generation."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pudge.web_app as web_app_module
from pudge.web_app import WebAppApi
from test_web_app import make_api


def _bare_api() -> WebAppApi:
    api = object.__new__(WebAppApi)
    api._irodori_server_lock = threading.Lock()
    api._irodori_server_process = None
    api.logger = SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)
    return api


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_stopping_the_managed_server_ends_its_helper_processes_too(tmp_path: Path) -> None:
    # The server runs workers of its own; stopping only the parent left them running.
    pid_file = tmp_path / "worker.pid"
    server = subprocess.Popen(
        ["/bin/sh", "-c", f"sleep 60 & echo $! > {pid_file}; wait"], start_new_session=True
    )
    try:
        deadline = time.monotonic() + 5
        while not pid_file.is_file() or not pid_file.read_text().strip():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        worker = int(pid_file.read_text())
        api = _bare_api()
        api._irodori_server_process = server

        api._stop_managed_irodori_server()

        assert server.poll() is not None
        deadline = time.monotonic() + 5
        while not _gone(worker):
            assert time.monotonic() < deadline, "the server's worker survived"
            time.sleep(0.02)
        assert api._irodori_server_process is None
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGKILL)
            server.wait()


def test_a_server_that_ignores_the_polite_stop_is_killed(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []
    waits = iter([subprocess.TimeoutExpired("irodori", 10), 0])

    def wait(timeout=None):
        result = next(waits)
        if isinstance(result, Exception):
            raise result
        return result

    server = SimpleNamespace(pid=4321, poll=lambda: None, wait=wait)
    monkeypatch.setattr(web_app_module.os, "getpgid", lambda pid: 4321)
    monkeypatch.setattr(web_app_module.os, "killpg", lambda group, sig: signals.append((group, sig)))
    api = _bare_api()
    api._irodori_server_process = server

    api._stop_managed_irodori_server()

    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


def test_pudge_stops_its_irodori_server_when_it_exits(tmp_path: Path, monkeypatch) -> None:
    registered: list = []
    monkeypatch.setattr("atexit.register", registered.append)
    api = make_api(tmp_path)
    assert api._stop_managed_irodori_server in registered


class _Health:
    def json(self):
        return {"status": "ok"}


def _probe_api(tmp_path: Path, monkeypatch, *, running: bool) -> tuple[WebAppApi, list[str]]:
    """Irodori is installed locally; it already runs (`running`) or Pudge starts it."""
    api = make_api(tmp_path)
    events: list[str] = []
    state = {"up": running}

    def health(_url, _key, timeout=5.0):
        if not state["up"]:
            raise ConnectionError("not running")
        return _Health()

    def start(url):
        events.append("start")
        state["up"] = True
        return True

    monkeypatch.setattr(api, "_irodori_health", health)
    monkeypatch.setattr(api, "_start_managed_irodori_server", start)
    monkeypatch.setattr(api, "_stop_managed_irodori_server", lambda: events.append("stop"))
    monkeypatch.setattr(web_app_module.time, "sleep", lambda _seconds: None)
    return api, events


def test_settings_test_stops_the_server_it_started_but_not_one_that_was_running(
    tmp_path: Path, monkeypatch
) -> None:
    api, events = _probe_api(tmp_path / "started", monkeypatch, running=False)
    assert api.test_irodori_tts({"url": "http://127.0.0.1:8088"})["ok"] is True
    assert events == ["start", "stop"]

    api, events = _probe_api(tmp_path / "running", monkeypatch, running=True)
    assert api.test_irodori_tts({"url": "http://127.0.0.1:8088"})["ok"] is True
    assert events == []

    # Generation asks to keep the server it started for the chapters to come.
    api, events = _probe_api(tmp_path / "kept", monkeypatch, running=False)
    assert api.test_irodori_tts({"url": "http://127.0.0.1:8088"}, keep_managed_alive=True)["ok"] is True
    assert events == ["start"]


def _generation_api(tmp_path: Path, monkeypatch) -> tuple[WebAppApi, int, list]:
    api = make_api(tmp_path)
    novel = tmp_path / "Novel.txt"
    novel.write_text("第一章\n猫が好きです。", encoding="utf-8")
    book = api.light_novels.import_file(novel)
    api.light_novels.save_settings({"irodori_tts_enabled": True, "irodori_tts_url": "http://127.0.0.1:8088"})
    events: list = []
    monkeypatch.setattr(
        api,
        "test_irodori_tts",
        lambda values=None, **kwargs: events.append(("probe", kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(api, "_stop_managed_irodori_server", lambda: events.append("stop"))
    return api, int(book["id"]), events


def test_generation_keeps_the_server_while_it_runs_and_stops_it_after_the_last_one(
    tmp_path: Path, monkeypatch
) -> None:
    api, book_id, events = _generation_api(tmp_path, monkeypatch)
    api._irodori_tts_worker(book_id, api.job_center.start("irodori_tts", "test", total=1))
    assert events[0] == ("probe", {"keep_managed_alive": True}) and events[-1] == "stop"

    # Another book is still being generated: its server stays.
    events.clear()
    other = threading.Thread(target=threading.Event().wait, args=(5,), daemon=True)
    other.start()
    api._irodori_tts_threads[999] = other
    api._irodori_tts_worker(book_id, api.job_center.start("irodori_tts", "test", total=1))
    assert events[0] == ("probe", {"keep_managed_alive": True}) and "stop" not in events


def _import_api(tmp_path: Path, monkeypatch) -> tuple[WebAppApi, list[str], list[Path]]:
    """Automatic generation is on; the audiobook search finds an audiobook for "Linked" only."""
    api = make_api(tmp_path)
    api.light_novels.save_settings({"irodori_tts_enabled": True, "irodori_tts_auto_generate": True})
    linked: set[int] = set()
    generated: list[str] = []

    def title(book_id) -> str:
        return api.light_novels.book(int(book_id))["title"]

    monkeypatch.setattr(
        api.audiobooks,
        "auto_link_light_novel",
        lambda book_id, *a, **k: linked.add(int(book_id)) if title(book_id) == "Linked" else None,
    )
    monkeypatch.setattr(
        api.audiobooks,
        "link_for_light_novel",
        lambda book_id, *a, **k: (
            {"book": {"id": 9, "path": "/Audiobooks/Linked"}} if int(book_id) in linked else None
        ),
    )
    monkeypatch.setattr(api, "generate_light_novel_tts", lambda book_id: generated.append(title(book_id)))
    monkeypatch.setattr(api.manager.work_scheduler, "background_wait_reason", lambda: None)
    files = []
    for name in ("Linked", "Alone"):
        path = tmp_path / f"{name}.txt"
        path.write_text("第一章\n本文です。", encoding="utf-8")
        files.append(path)
    return api, generated, files


def test_import_generates_audiobooks_only_for_books_without_one(tmp_path: Path, monkeypatch) -> None:
    # Choosing files in Pudge.
    api, generated, files = _import_api(tmp_path / "choose", monkeypatch)
    api.window = SimpleNamespace(create_file_dialog=lambda *a, **k: [str(path) for path in files])
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=10))
    api.choose_light_novel_file()
    assert generated == ["Alone"]

    # Retrying a failed import job.
    api, generated, files = _import_api(tmp_path / "retry", monkeypatch)
    api._retry_import_worker("job", "light_novel", [str(path) for path in files], threading.Event())
    assert generated == ["Alone"]

    # Dropping files on the window (linking and generation run a moment later).
    api, generated, files = _import_api(tmp_path / "drop", monkeypatch)

    class Now:
        def __init__(self, _delay, callback):
            self.callback, self.daemon = callback, True

        def start(self):
            self.callback()

    monkeypatch.setattr(web_app_module.threading, "Timer", Now)
    api.import_dropped_paths([str(path) for path in files])
    assert generated == ["Alone"]
