"""Fixes on top of the manga extras patch: background OCR pauses, the user's volume goes
first, OCR on open is limited, cancel does not wait for the page, Jiten+ says once
(behaviour; the reader part runs in the page harness, audit 71Б rewrite)."""

from __future__ import annotations

import stat
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge import manga_mokuro_session as mokuro_sessions
from pudge import web_app as web_app_module
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.manga import MangaService
from pudge.web_app import WebAppApi
from t71g03_support import MANGA, READER

ROOT = Path(__file__).parents[1]

_SLOW_WORKER = """#!{python}
import json, sys, time, zipfile
from pathlib import Path
here = Path(__file__)
for line in sys.stdin:
    request = json.loads(line)
    with here.with_suffix(".log").open("a") as log:
        log.write(str(request.get("name")) + "\\n")
    if request.get("name") in set(here.with_suffix(".slow").read_text().split()):
        time.sleep(60)
    sys.stdout.write(json.dumps({{"id": request["id"], "page": {{"img_width": 400, "img_height": 600, "blocks": [
        {{"box": [300, 100, 340, 300], "vertical": True, "font_size": 40, "lines": ["さかなだ"],
          "lines_coords": [[[300, 100], [340, 100], [340, 300], [300, 300]]]}}]}}}}, ensure_ascii=False) + "\\n")
    sys.stdout.flush()
"""


def _worker(tmp_path: Path, slow: str = "") -> Path:
    path = tmp_path / "slow-mokuro-python"
    path.write_text(_SLOW_WORKER.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    path.with_suffix(".slow").write_text(slow, encoding="utf-8")
    return path


def _volume(tmp_path: Path, python: Path, pages: int) -> tuple[MangaService, int]:
    archive = tmp_path / "book.cbz"
    Image.new("RGB", (400, 600), "white").save(tmp_path / "page.png")
    with zipfile.ZipFile(archive, "w") as zf:
        for number in range(1, pages + 1):
            zf.write(tmp_path / "page.png", f"{number:02d}.png")
    service = MangaService(
        Database(tmp_path / "pudge.db"),
        cache_dir=tmp_path / "cache",
        mokuro_python=str(python),
    )
    return service, int(service.import_file(archive)["id"])


# Worker


def test_interrupt_ends_a_page_in_progress_at_once_and_it_is_not_marked_failed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("PUDGE_MOKURO_PYTHON", raising=False)
    python = _worker(tmp_path, slow="02.png")
    service, book_id = _volume(tmp_path, python, pages=3)
    stop = threading.Event()
    outcome: dict = {}
    run = threading.Thread(
        target=lambda: outcome.update(
            service.ocr_book_mokuro(book_id, mokuro_python=python, cancelled=stop.is_set)
        )
    )
    try:
        run.start()
        deadline = time.time() + 20
        while (
            "02.png" not in python.with_suffix(".log").read_text()
            if python.with_suffix(".log").exists()
            else True
        ):
            assert time.time() < deadline
            time.sleep(0.05)
        started = time.monotonic()
        stop.set()
        assert service.interrupt_mokuro() == 1
        run.join(10)
        assert not run.is_alive() and time.monotonic() - started < 5  # not the 60 s page
        assert outcome["cancelled"] is True
        status = service.ocr_cache_status(book_id)
        assert (
            status["completed_pages"] == 1
            and status["failed_pages"] == 0
            and status["not_started_pages"] == 2
        )
    finally:
        mokuro_sessions.close_all()


def test_idle_watchdog_follows_every_new_worker(tmp_path: Path) -> None:
    python = _worker(tmp_path)
    archive = tmp_path / "one.cbz"
    Image.new("RGB", (10, 10), "white").save(tmp_path / "p.png")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(tmp_path / "p.png", "01.png")
    session = mokuro_sessions.MokuroSession(
        python, ROOT / "pudge" / "manga_mokuro_worker.py", idle_seconds=1.0
    )
    try:
        for _round in range(2):
            assert "page" in session.request(
                {"op": "ocr", "archive": str(archive), "name": "01.png"}, timeout=20
            )
            deadline = time.time() + 10
            while (session.alive or session._watchdog is not None) and time.time() < deadline:
                time.sleep(0.1)
            assert not session.alive, "every worker gives its memory back when idle"
            assert session._watchdog is None
    finally:
        session.close()


# Background and opened volumes


def _api(tmp_path: Path, monkeypatch, pages: int = 3, books: int = 2):
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.tools.manga_background_ocr = True
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    Image.new("RGB", (40, 60), "white").save(tmp_path / "page.png")
    ids = []
    for number in range(books):
        archive = tmp_path / f"vol{number}.cbz"
        with zipfile.ZipFile(archive, "w") as zf:
            for page in range(1, pages + 1):
                zf.write(tmp_path / "page.png", f"{page:02d}.png")
        ids.append(int(api.manga.import_file(archive)["id"]))
    monkeypatch.setattr(api, "_manga_ocr_backend_available", lambda **_k: True)
    monkeypatch.setattr(api, "_mokuro_ocr_python_path", lambda: tmp_path / "python")
    interrupts: list[object] = []
    monkeypatch.setattr(
        api.manga, "interrupt_mokuro", lambda background=None: interrupts.append(background) or 0
    )
    return api, ids, interrupts


def _fake_volume_ocr(api, monkeypatch, *, page_seconds: float = 0.05):
    pages_done: dict[int, int] = {}

    def run(book_id, *, mokuro_python, progress=None, cancelled=None, background=False):
        for _page in range(200):
            if cancelled is not None and cancelled():
                return {"ok": False, "cancelled": True, "errors": []}
            time.sleep(page_seconds)
            pages_done[book_id] = pages_done.get(book_id, 0) + 1
        return {"ok": True, "complete": True, "errors": []}

    monkeypatch.setattr(api.manga, "ocr_book_mokuro", run)
    return pages_done


def _wait(predicate, timeout=10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _wait_idle(api, book_id) -> None:
    assert _wait(lambda: not api._manga_book_ocr_threads[book_id].is_alive())


def test_background_run_pauses_when_its_conditions_end(tmp_path: Path, monkeypatch) -> None:
    api, ids, interrupts = _api(tmp_path, monkeypatch)
    try:
        pages = _fake_volume_ocr(api, monkeypatch)
        blocked = {"reason": ""}
        monkeypatch.setattr(api, "_manga_background_ocr_blocked", lambda: blocked["reason"])
        api._manga_background_ocr_tried[ids[0]] = time.time()
        api.start_manga_ocr_book(ids[0], background=True)
        assert _wait(lambda: pages.get(ids[0], 0) >= 2)
        blocked["reason"] = "window_active"  # Pudge came to the front
        _wait_idle(api, ids[0])
        state = api._manga_book_ocr_state[ids[0]]
        assert state["state"] == "paused" and state["pause_reason"] == "window_active"
        assert interrupts == [True]  # the background worker's memory is given back
        assert ids[0] not in api._manga_background_ocr_tried  # resumed on the next idle check
        job = api.job_center.get(api._manga_ocr_job_ids[ids[0]])
        assert job["state"] == "cancelled" and job["message"] == "Paused (window_active)"
    finally:
        api.close()


def test_opened_volume_pauses_the_background_run_instead_of_waiting(tmp_path: Path, monkeypatch) -> None:
    api, ids, interrupts = _api(tmp_path, monkeypatch)
    try:
        pages = _fake_volume_ocr(api, monkeypatch)
        monkeypatch.setattr(api, "_manga_background_ocr_blocked", lambda: "")
        api.start_manga_ocr_book(ids[0], background=True)
        assert _wait(lambda: pages.get(ids[0], 0) >= 1)
        api.start_manga_ocr_book(ids[1])  # the user starts another volume
        assert _wait(lambda: pages.get(ids[1], 0) >= 3), "the user's volume is not queued behind it"
        assert api._manga_book_ocr_state[ids[0]]["pause_reason"] == "user_volume"
        assert True in interrupts
    finally:
        api._quiesce_manga_ocr(timeout=5)
        api.close()


def test_ocr_on_open_is_limited_and_stops_with_the_reader(tmp_path: Path, monkeypatch) -> None:
    api, ids, _interrupts = _api(tmp_path, monkeypatch)
    try:
        pages = _fake_volume_ocr(api, monkeypatch)
        battery = {"on": True}
        scheduler = api.manager.work_scheduler
        monkeypatch.setattr(scheduler, "power_snapshot", lambda **_k: {"on_battery": battery["on"]})
        assert api.start_manga_ocr_book(ids[0], auto=True)["auto_skipped"] == "on_battery"
        assert ids[0] not in api._manga_book_ocr_threads
        battery["on"] = False
        api.start_manga_ocr_book(ids[0], auto=True)
        assert _wait(lambda: pages.get(ids[0], 0) >= 1)
        api.stop_manga_ocr_book(ids[1])  # another volume: nothing to stop
        assert api._manga_book_ocr_threads[ids[0]].is_alive()
        api.stop_manga_ocr_book(ids[0])  # the reader closed
        _wait_idle(api, ids[0])
        assert api._manga_book_ocr_state[ids[0]]["pause_reason"] == "reader_closed"
        # A run started with the toolbar button is not stopped by closing the reader.
        api.start_manga_ocr_book(ids[1])
        api.stop_manga_ocr_book(ids[1])
        time.sleep(0.2)
        assert api._manga_book_ocr_threads[ids[1]].is_alive()
    finally:
        api._quiesce_manga_ocr(timeout=5)
        api.close()


def test_volume_with_only_failed_pages_is_not_restarted_on_every_open(tmp_path: Path, monkeypatch) -> None:
    api, ids, _interrupts = _api(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(api.manager.work_scheduler, "power_snapshot", lambda **_k: {"on_battery": False})
        monkeypatch.setattr(
            api.manga,
            "ocr_cache_status",
            lambda book_id: {
                "total_pages": 3,
                "completed_pages": 2,
                "failed_pages": 1,
                "not_started_pages": 0,
                "complete": False,
            },
        )
        assert api.start_manga_ocr_book(ids[0], auto=True)["auto_skipped"] == "only_failed_pages"
    finally:
        api.close()


def test_idle_check_remembers_finished_volumes(tmp_path: Path, monkeypatch) -> None:
    api, ids, _interrupts = _api(tmp_path, monkeypatch, books=3)
    try:
        monkeypatch.setattr(api, "_manga_background_ocr_blocked", lambda: "")
        monkeypatch.setattr(api.manager.work_scheduler, "background_wait_reason", lambda: None)
        reads: list[int] = []
        monkeypatch.setattr(
            api.manga, "ocr_cache_status", lambda book_id: reads.append(book_id) or {"complete": True}
        )
        assert api._manga_background_ocr_candidate() is None
        assert sorted(reads) == sorted(ids)
        reads.clear()
        assert api._manga_background_ocr_candidate() is None
        assert reads == []  # nothing re-read a minute later
    finally:
        api.close()


def test_background_conditions_are_rechecked_before_every_page(tmp_path: Path, monkeypatch) -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(tools=SimpleNamespace(manga_background_ocr=True))
    power = {"on_battery": False}
    api.manager = SimpleNamespace(work_scheduler=SimpleNamespace(power_snapshot=lambda: dict(power)))
    monkeypatch.setattr(web_app_module, "app_session_window_active", lambda: False)
    assert api._manga_background_ocr_blocked() == ""
    power["on_battery"] = True
    assert api._manga_background_ocr_blocked() == "on_battery"
    power["on_battery"] = None
    assert api._manga_background_ocr_blocked() == "on_battery"
    power["on_battery"] = False
    monkeypatch.setattr(web_app_module, "app_session_window_active", lambda: True)
    assert api._manga_background_ocr_blocked() == "window_active"
    api.config.tools.manga_background_ocr = False
    assert api._manga_background_ocr_blocked() == "setting_off"


def test_reader_starts_volume_ocr_on_open_and_stops_it_when_leaving(page) -> None:
    result = page.run(
        READER
        + r"""
        for (const book of window.MANGA.books) book.ocr_complete = false;
        window.MANGA.status = id => ({book_id: id, running: false, complete: false, total_pages: 6, completed: 0});
        await open(3);
        const out = {opened: {start: callsOf('start_manga_ocr_book'), stop: callsOf('stop_manga_ocr_book')}};
        await __settle(PudgeMangaReaderV2.openBook(4));          // straight to the next volume
        out.next = callsOf('stop_manga_ocr_book');
        await __click('[data-manga-v2-action="close"]'); await __tick(50);
        out.closed = callsOf('stop_manga_ocr_book');
        return out;
        """,
        before=MANGA,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    # auto=true: the backend may skip it (battery, only failed pages) and stops it with the reader.
    assert out["opened"] == {"start": [[3, False, "", False, True]], "stop": []}
    assert out["next"] == [[3, True]]
    assert out["closed"] == [[3, True], [4, True]]


def test_a_visible_page_the_volume_run_has_not_read_is_read_at_most_once_a_minute(page) -> None:
    result = page.run(
        READER
        + r"""
        // Mokuro shows pages as they are read; this page cannot be read (no text found).
        let done = 0;
        window.MANGA.status = id => ({book_id: id, running: true, state: 'running', complete: false, total_pages: 6, completed_pages: ++done});
        window.MANGA.cachedOnly = () => ({available: true, cached: false, regions: []});
        __api.manga_text_regions = async (id, index, refresh, cachedOnly) => ({book_id: id, page_index: index, available: true, cached: false, regions: []});
        await open(3);
        const reads = () => callsOf('manga_text_regions').filter(args => args[3] === false).length;
        const out = {opened: reads()};
        await __tick(10000);          // the volume run reports progress every 750 ms meanwhile
        out.tenSeconds = reads();
        out.polls = callsOf('manga_ocr_book_status').length;
        await __tick(52000);
        out.aMinuteLater = reads();
        await __click('[data-manga-v2-action="close"]'); await __tick(50);
        return out;
        """,
        before=MANGA,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    assert out["opened"] == 1  # the page being read does not wait for the whole run
    assert out["polls"] > 10 and out["tenSeconds"] == 1
    assert out["aMinuteLater"] == 2


# Jiten


def test_manga_add_keeps_its_sentence_without_image_and_jiten_plus_is_said_once(
    tmp_path: Path, monkeypatch
) -> None:
    from pudge import light_novels as ln_module
    from pudge.light_novels import LightNovelError, LightNovelService

    service = LightNovelService.__new__(LightNovelService)
    calls: list[tuple] = []
    service._jiten_mutation = lambda path, body: calls.append(("add", body["sentence"]))
    service._jiten_add_custom_sentence = lambda w, r, sentence: calls.append(("sentence", sentence))
    service.invalidate_jiten_live_state = lambda w, r: None
    long_sentence = "い" * 150 + "猫" + "い" * 149
    service.study_action(
        "jiten", "add", 5, 0, deck_id="12", sentence=long_sentence, media_source="manga", surface="猫"
    )
    # The card's custom sentence: the word marked, cut around it to Jiten's 150 characters.
    assert calls == [("add", "い" * 150), ("sentence", "い" * 72 + "**猫**" + "い" * 73)]
    calls.clear()
    service.study_action("jiten", "add", 5, 0, deck_id="12", sentence=long_sentence)
    assert calls == [("add", long_sentence)]  # light novel adds unchanged

    service.settings = lambda: SimpleNamespace(jiten_api_key="key")
    posts: list[str] = []
    monkeypatch.setattr(
        ln_module.httpx,
        "post",
        lambda url, **_k: posts.append(url) or SimpleNamespace(status_code=403, json=lambda: {}),
    )
    with pytest.raises(LightNovelError, match="Jiten\\+"):
        service._jiten_upload_card_image(5, 0, b"jpeg")
    service._jiten_upload_card_image(5, 0, b"jpeg")  # silent afterwards
    assert len(posts) == 1
