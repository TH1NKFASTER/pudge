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


def test_duplicate_irodori_click_returns_existing_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


# Light novel 6 on the shelf with audiobook generation (Irodori) on. window.LN is what
# light_novel_state answers; generating waits for window.answerGenerate().
GENERATION = r"""
window.LN = {version: 1, settings: {audiobook_generation_provider: 'irodori'}, irodori_tts_active: false,
             books: [{id: 6, title: 'Novel', volume: 1, character_count: 1000}]};
__api.light_novel_state = async () => JSON.parse(JSON.stringify(window.LN));
__api.generate_light_novel_tts = () => new Promise(resolve => {
  window.answerGenerate = () => resolve({started: true, job: {state: 'queued', current: 0, total: 1000}}); });
"""
SHELF = r"""
setPage('lightnovels'); await __tick(100);
const card = id => document.querySelector(`#lightNovelsContent [data-ln-book="${id}"]`);
const menu = async () => {
  await __fire('#lightNovelsContent [data-ln-book="6"]', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
  return [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].map(b => b.dataset.lnContextAction);
};
const progress = () => { const slot = card(6).querySelector('[data-ln-tts-status]');
  return {visible: slot.checkVisibility(), percent: slot.querySelector('[data-ln-tts-percent]').innerText,
          bar: slot.querySelector('[data-ln-tts-bar]').style.width}; };
const running = (current, extra = {}) => { window.LN.irodori_tts_active = true;
  Object.assign(window.LN.books[0], {irodori_tts: {active: true, state: 'running', current, total: 1000}}, extra); };
"""


def test_generating_an_audiobook_cannot_be_started_twice_and_shows_its_progress_in_place(page) -> None:
    result = page.run(
        SHELF
        + r"""
        const out = {menu: await menu()};
        const item = document.querySelector('#contextMenu [data-ln-context-action="generate-audiobook"]');
        item.click(); item.click();                                                        // an impatient second click
        await __tick(10);
        out.whileStarting = await menu();
        window.answerGenerate(); await __tick(10);
        out.started = progress();
        const original = card(6);
        // While it runs, chapters are found and the book's details change: the card is patched,
        // not redrawn (a full redraw made the whole shelf blink at every poll).
        running(400, {character_count: 1200, chapter_count: 3}); await __tick(1000);
        out.running = {...progress(), sameCard: card(6) === original};
        // A volume added meanwhile is a new book list: that is drawn at once.
        window.LN.books.push({id: 7, title: 'Novel', volume: 2}); await __tick(1000);
        out.added = Boolean(card(7));
        const redrawn = card(6);
        running(700); await __tick(1000);
        out.later = {...progress(), sameCard: card(6) === redrawn};
        // Finished: the shelf is drawn once more with the new details.
        window.LN.irodori_tts_active = false;
        window.LN.books[0].irodori_tts = {active: false, state: 'succeeded', current: 1000, total: 1000};
        window.LN.books[0].paired_audio = {ln_book_id: 6, book: {id: 80}};
        await __tick(1000);
        out.finished = {sameCard: card(6) === redrawn, badge: Boolean(card(6).querySelector('.ln-paired-audio-badge')),
                        progress: card(6).querySelector('[data-ln-tts-status]').checkVisibility()};
        const polls = __apiCalls.filter(c => c.name === 'light_novel_state').length;
        await __tick(5000);
        out.pollsAfterFinish = __apiCalls.filter(c => c.name === 'light_novel_state').length - polls;
        return out;
        """,
        before=GENERATION,
    )
    out = result.value
    assert "generate-audiobook" in out["menu"]
    assert result.calls_of("generate_light_novel_tts") == [[6]]
    assert "generate-audiobook" not in out["whileStarting"]
    assert out["started"]["visible"] is True and out["started"]["percent"] == "0%"
    assert out["running"] == {"visible": True, "percent": "40%", "bar": "40%", "sameCard": True}
    assert out["added"] is True
    assert out["later"] == {"visible": True, "percent": "70%", "bar": "70%", "sameCard": True}
    assert out["finished"] == {"sameCard": False, "badge": True, "progress": False}
    assert out["pollsAfterFinish"] == 0
