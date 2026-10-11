"""Mokuro as the default manga OCR engine.

Invented pages and readings only (see tests/OCR_TESTS.md). Mokuro itself is
GPL-3.0 and is never imported: a fake interpreter stands in for its environment.
"""

from __future__ import annotations

import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge import manga_mokuro_session as mokuro_sessions
from pudge.config import load_config
from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_mokuro import LINE_SPLIT_SOURCE, MokuroImportError, convert_mokuro, mokuro_page_regions
from pudge.web_app import WebAppApi
from tests.t71g06_support import STUDY, reader

ROOT = Path(__file__).resolve().parents[1]


def _line(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _page(*blocks: dict, width: int = 400, height: int = 600) -> dict:
    return {"img_width": width, "img_height": height, "blocks": list(blocks)}


def test_vertical_line_is_split_into_even_character_cells_top_to_bottom() -> None:
    block = {
        "box": [300, 100, 340, 300],
        "vertical": True,
        "font_size": 40,
        "lines": ["さかなだ"],
        "lines_coords": [_line(300, 100, 340, 300)],
    }
    [region] = mokuro_page_regions(_page(block), 400, 600, "p1.png")
    segments = region["segments"]
    assert [segment["text"] for segment in segments] == list("さかなだ")
    assert region["word_geometry"] == "mapped_segments" and region["geometry_status"] == "approximate"
    assert all(
        segment["source"] == LINE_SPLIT_SOURCE and not segment["source"].startswith("mokuro-")
        for segment in segments
    )
    # 200 px column / 4 characters = 50 px each, first character at the top.
    tops = [round((1 - segment["y"] - segment["height"]) * 600, 3) for segment in segments]
    assert tops == [100, 150, 200, 250]
    assert all(round(segment["height"] * 600, 3) == 50 for segment in segments)
    assert all(segment["x"] == 0.75 and segment["width"] == 0.1 for segment in segments)


def test_ellipsis_dots_share_one_cell() -> None:
    block = {
        "box": [300, 100, 340, 300],
        "vertical": True,
        "font_size": 40,
        "lines": ["あ．．．い"],
        "lines_coords": [_line(300, 100, 340, 220)],
    }
    [region] = mokuro_page_regions(_page(block), 400, 600, "p1.png")
    heights = [round(segment["height"] * 600, 3) for segment in region["segments"]]
    # three cells: あ, the dots, い -> 40 px each; each dot gets a third.
    assert heights[0] == 40 and heights[-1] == 40
    assert abs(sum(heights[1:4]) - 40) < 0.01


def test_horizontal_japanese_lines_join_without_spaces_latin_keeps_them() -> None:
    japanese = {
        "box": [10, 10, 200, 80],
        "vertical": False,
        "font_size": 30,
        "lines": ["きょうは", "はれです"],
        "lines_coords": [_line(10, 10, 130, 40), _line(10, 45, 130, 75)],
    }
    latin = {
        "box": [10, 100, 200, 170],
        "vertical": False,
        "font_size": 30,
        "lines": ["ONE", "WAY"],
        "lines_coords": [_line(10, 100, 100, 130), _line(10, 135, 100, 165)],
    }
    first, second = mokuro_page_regions(_page(japanese, latin), 400, 600, "p1.png")
    assert first["text"] == "きょうははれです"
    assert second["text"] == "ONE WAY"
    assert "".join(segment["text"] for segment in second["segments"]) == "ONEWAY"


def test_furigana_line_on_the_ruby_side_of_kanji_is_dropped() -> None:
    # Main column 300-340 px with kanji; a thin kana line hugging its right side.
    block = {
        "box": [295, 100, 360, 300],
        "vertical": True,
        "font_size": 40,
        "lines": ["ほしぞら", "星空だ"],
        "lines_coords": [_line(342, 110, 356, 190), _line(300, 100, 340, 250)],
    }
    [region] = mokuro_page_regions(_page(block), 400, 600, "p1.png")
    assert region["text"] == "星空だ"
    assert region["provenance"]["ruby_lines"] == ["ほしぞら"]
    assert [line["text"] for line in region["provenance"]["line_boxes"]] == ["星空だ"]
    assert "".join(segment["text"] for segment in region["segments"]) == "星空だ"


def test_small_kana_line_on_the_reading_side_is_kept() -> None:
    # Same thin kana line, but left of the kanji column: the next line of dialogue.
    block = {
        "box": [270, 100, 345, 300],
        "vertical": True,
        "font_size": 40,
        "lines": ["星空だ", "ほしぞら"],
        "lines_coords": [_line(300, 100, 340, 250), _line(282, 110, 296, 190)],
    }
    [region] = mokuro_page_regions(_page(block), 400, 600, "p1.png")
    assert region["text"] == "星空だほしぞら"
    assert "ruby_lines" not in region["provenance"]


def test_block_box_slightly_over_the_page_edge_is_clamped_far_outside_rejected() -> None:
    over = {
        "box": [350, 10, 420, 200],
        "vertical": True,
        "lines": ["そら"],
        "lines_coords": [_line(360, 10, 395, 120)],
    }
    [region] = mokuro_page_regions(_page(over), 400, 600, "p1.png")
    assert region["x"] + region["width"] <= 1.0
    outside = {"box": [380, 10, 520, 200], "vertical": True, "lines": ["そら"]}
    try:
        mokuro_page_regions(_page(outside), 400, 600, "p1.png")
    except MokuroImportError:
        pass
    else:
        raise AssertionError("a block mostly outside the page must be rejected")


def test_sidecar_conversion_uses_the_same_page_conversion() -> None:
    block = {
        "box": [300, 100, 340, 300],
        "vertical": True,
        "font_size": 40,
        "lines": ["さかなだ"],
        "lines_coords": [_line(300, 100, 340, 300)],
    }
    page = {"img_path": "p1.png", **_page(block)}
    assert convert_mokuro({"pages": [page]}, ["p1.png"])[0] == mokuro_page_regions(page, 400, 600, "p1.png")


# ---------------------------------------------------------------- single page OCR

_FAKE_WORKER = """#!{python}
import json, os, sys, zipfile
from pathlib import Path
here = Path(__file__)
assert sys.argv[2] == "serve"
fail = set(here.with_suffix(".fail").read_text().split()) if here.with_suffix(".fail").exists() else set()
for line in sys.stdin:
    request = json.loads(line)
    reply = {{"id": request["id"]}}
    with here.with_suffix(".log").open("a") as log:
        log.write(request["op"] + " " + str(request.get("name")) + "\\n")
    with here.with_suffix(".priority").open("a") as log:
        log.write(os.environ.get("PUDGE_BACKGROUND_PRIORITY", "normal") + "\\n")
    with zipfile.ZipFile(request["archive"]) as archive:
        archive.read(request["name"])
    if request["name"] in fail:
        reply["error"] = "RuntimeError: unreadable page"
    else:
        reply["page"] = {{"img_width": 400, "img_height": 600, "blocks": [
            {{"box": [300, 100, 340, 300], "vertical": True, "font_size": 40, "lines": ["さかなだ"],
              "lines_coords": [[[300, 100], [340, 100], [340, 300], [300, 300]]]}}]}}
    sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\\n")
    sys.stdout.flush()
"""


def _book(tmp_path: Path, *, mokuro_python: Path | None, pages: int = 1) -> tuple[MangaService, int]:
    archive = tmp_path / "book.cbz"
    image = tmp_path / "page.png"
    Image.new("RGB", (400, 600), "white").save(image)
    with zipfile.ZipFile(archive, "w") as zf:
        for number in range(1, pages + 1):
            zf.write(image, f"{number:02d}.png")
    service = MangaService(
        Database(tmp_path / "pudge.db"),
        cache_dir=tmp_path / "cache",
        mokuro_python=str(mokuro_python or ""),
    )
    return service, int(service.import_file(archive)["id"])


def _fake_mokuro(tmp_path: Path, *, fail: tuple[str, ...] = ()) -> Path:
    path = tmp_path / "fake-mokuro-python"
    path.write_text(_FAKE_WORKER.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    if fail:
        path.with_suffix(".fail").write_text(" ".join(fail), encoding="utf-8")
    return path


def _requests(fake: Path) -> list[str]:
    log = fake.with_suffix(".log")
    return log.read_text(encoding="utf-8").split("\n")[:-1] if log.exists() else []


def _spy_popen(monkeypatch) -> list[list[str]]:
    started: list[list[str]] = []
    real_popen = subprocess.Popen

    def spy(command, *args, **kwargs):
        started.append([str(part) for part in command])
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return started


def test_mokuro_pages_are_read_by_one_long_lived_worker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PUDGE_MOKURO_PYTHON", raising=False)
    started = _spy_popen(monkeypatch)
    fake = _fake_mokuro(tmp_path)
    service, book_id = _book(tmp_path, mokuro_python=fake, pages=2)
    try:
        result = service.text_regions(book_id, 0)
        assert result["status"] == "ready"
        [region] = result["regions"]
        assert region["text"] == "さかなだ" and region["provenance"]["source"] == "mokuro"
        assert region["word_geometry"] == "mapped_segments" and len(region["segments"]) == 4
        assert service.text_regions(book_id, 1)["status"] == "ready"
        # Models load once: both pages went to the same worker process.
        [command] = started
        assert Path(command[1]).name == "manga_mokuro_worker.py" and command[2] == "serve"
        assert _requests(fake) == ["ocr 01.png", "ocr 02.png"]
        # Published like a volume page: cached afterwards, no further request.
        assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "さかなだ"
        assert len(_requests(fake)) == 2
    finally:
        mokuro_sessions.close_all()


def test_idle_worker_exits_and_restarts_on_the_next_request(tmp_path: Path) -> None:
    fake = _fake_mokuro(tmp_path)
    archive = tmp_path / "one.cbz"
    Image.new("RGB", (10, 10), "white").save(tmp_path / "p.png")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(tmp_path / "p.png", "01.png")
    session = mokuro_sessions.MokuroSession(fake, ROOT / "pudge" / "manga_mokuro_worker.py", idle_seconds=1.0)
    try:
        assert "page" in session.request({"op": "ocr", "archive": str(archive), "name": "01.png"}, timeout=20)
        assert session.alive
        deadline = time.time() + 10
        while session.alive and time.time() < deadline:
            time.sleep(0.2)
        assert not session.alive, "an idle worker gives its memory back"
        assert "page" in session.request({"op": "ocr", "archive": str(archive), "name": "01.png"}, timeout=20)
    finally:
        session.close()


def test_mokuro_worker_failure_is_a_retryable_failed_page(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PUDGE_MOKURO_PYTHON", raising=False)
    broken = tmp_path / "broken-python"
    broken.write_text("#!/bin/sh\necho 'no torch here' >&2\nexit 1\n", encoding="utf-8")
    broken.chmod(broken.stat().st_mode | stat.S_IXUSR)
    service, book_id = _book(tmp_path, mokuro_python=broken)
    try:
        result = service.text_regions(book_id, 0)
        assert result["status"] == "failed" and result["retryable"] is True
        assert "no torch here" in result["reason"]
        assert service.text_regions(book_id, 0, cached_only=True)["status"] == "missing"
    finally:
        mokuro_sessions.close_all()


# ---------------------------------------------------------------- volume OCR


def test_volume_starts_at_the_reading_position_and_skips_finished_pages(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PUDGE_MOKURO_PYTHON", raising=False)
    started = _spy_popen(monkeypatch)
    fake = _fake_mokuro(tmp_path)
    service, book_id = _book(tmp_path, mokuro_python=fake, pages=5)
    progress: list[tuple[int, int, object, str]] = []
    try:
        assert service.text_regions(book_id, 3)["status"] == "ready"  # read alone before the volume run
        service.set_position(book_id, 2)
        result = service.ocr_book_mokuro(
            book_id, mokuro_python=fake, progress=lambda *args: progress.append(tuple(args))
        )
        assert result["ok"] is True and result["complete"] is True and result["backend"] == "mokuro"
        assert _requests(fake) == ["ocr 04.png", "ocr 03.png", "ocr 05.png", "ocr 01.png", "ocr 02.png"]
        assert len(started) == 1
        assert progress[-1][:2] == (5, 5)
        # Every page is published as it is read, not at the end.
        assert [entry[2] for entry in progress if entry[2] is not None][:2] == [2, 2]
    finally:
        mokuro_sessions.close_all()


def test_background_library_ocr_uses_its_own_low_priority_worker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PUDGE_MOKURO_PYTHON", raising=False)
    monkeypatch.delenv("PUDGE_BACKGROUND_PRIORITY", raising=False)
    started = _spy_popen(monkeypatch)
    fake = _fake_mokuro(tmp_path)
    service, book_id = _book(tmp_path, mokuro_python=fake, pages=2)
    try:
        assert service.text_regions(book_id, 0)["status"] == "ready"
        assert service.ocr_book_mokuro(book_id, mokuro_python=fake, background=True)["complete"] is True
        assert fake.with_suffix(".priority").read_text().split() == ["normal", "1"]
        assert len(started) == 2  # the reader's worker keeps normal priority
    finally:
        mokuro_sessions.close_all()


class _OneMinute:
    """The background loop's cancel event: each wait is one idle minute; then the app quits."""

    def __init__(self, minutes: int) -> None:
        self.minutes = minutes

    def wait(self, _seconds: float) -> bool:
        self.minutes -= 1
        return self.minutes < 0


def test_background_library_ocr_loop_starts_volumes_as_background_runs() -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.logger = SimpleNamespace(info=lambda *_a: None, exception=lambda *_a: None)
    candidates = iter([7, None, 9])
    api._manga_background_ocr_candidate = lambda: next(candidates)
    started: list[tuple[int, dict]] = []
    api.start_manga_ocr_book = lambda book_id, **kwargs: started.append((book_id, kwargs))
    api._manga_background_ocr_loop(_OneMinute(3))
    # Started on the low-priority worker that a volume the user opens can interrupt.
    assert started == [(7, {"background": True}), (9, {"background": True})]


def test_cancelled_volume_keeps_finished_pages_and_bad_pages_are_retryable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("PUDGE_MOKURO_PYTHON", raising=False)
    fake = _fake_mokuro(tmp_path, fail=("02.png",))
    service, book_id = _book(tmp_path, mokuro_python=fake, pages=4)
    try:
        calls = {"n": 0}

        def cancelled() -> bool:
            calls["n"] += 1
            return len(_requests(fake)) >= 2

        stopped = service.ocr_book_mokuro(book_id, mokuro_python=fake, cancelled=cancelled)
        assert stopped["cancelled"] is True
        status = service.ocr_cache_status(book_id)
        assert status["completed_pages"] == 1 and status["failed_pages"] == 1
        resumed = service.ocr_book_mokuro(book_id, mokuro_python=fake)
        assert resumed["ok"] is False and any("page 2" in error for error in resumed["errors"])
        assert service.ocr_cache_status(book_id)["completed_pages"] == 3
        # 01 was never read twice; the failed page was retried once in the new run.
        assert _requests(fake).count("ocr 01.png") == 1 and _requests(fake).count("ocr 02.png") == 2
    finally:
        mokuro_sessions.close_all()


# ---------------------------------------------------------------- settings and backend selection


def _api(tmp_path: Path, configured: str) -> WebAppApi:
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        tools=SimpleNamespace(manga_mokuro_python=configured),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
    )
    return api


def test_existing_mokuro_environment_is_used_without_install(tmp_path: Path) -> None:
    external = tmp_path / "my-mokuro" / "bin" / "python"
    external.parent.mkdir(parents=True)
    external.write_text("", encoding="utf-8")
    api = _api(tmp_path, str(external))
    assert api._mokuro_ocr_python_path() == external
    assert api._manga_ocr_backend_available() is True
    missing = _api(tmp_path, str(tmp_path / "nope" / "python"))
    assert missing._mokuro_ocr_python_path() == tmp_path / "cache" / "mokuro-ocr" / "venv" / "bin" / "python"
    assert missing._manga_ocr_backend_available() is False


def test_settings_show_and_save_the_mokuro_environment(page) -> None:
    assert load_config(Path("/nonexistent/pudge-config.toml")).tools.manga_mokuro_python == ""
    # Settings: the Mokuro environment path is shown and saved without spaces (there is no
    # engine choice: tests/test_v0728_g23_mokuro_backend.py).
    result = page.run(
        r"""
        const out = {python: $('s_manga_mokuro_python').value, empty: $('s_manga_mokuro_python').placeholder,
                     emptyText: t('ui.emptyInstallFromSettings')};
        await __input('#s_manga_mokuro_python', '  /opt/mokuro/bin/python  ');
        await __click('#saveSettings'); await __tick(10);
        return out;
        """,
        state={"settings": {"manga_mokuro_python": "/Users/me/mokuro/bin/python"}},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    out = result.value
    assert out["python"] == "/Users/me/mokuro/bin/python" and out["empty"] == out["emptyText"]
    [[values]] = result.calls_of("save_settings")
    assert values["manga_mokuro_python"] == "/opt/mokuro/bin/python"


def test_saved_mokuro_environment_is_used_for_page_ocr(tmp_path: Path, monkeypatch) -> None:
    from tests.test_web_app import make_api

    monkeypatch.delenv("PUDGE_MOKURO_PYTHON", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    # Settings take only the Python of a Python environment (<env>/bin/python).
    env = tmp_path / "mokuro-env"
    (env / "bin").mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    fake = _fake_mokuro(tmp_path).rename(env / "bin" / "python")
    api = make_api(tmp_path / "app")
    archive = tmp_path / "book.cbz"
    Image.new("RGB", (400, 600), "white").save(tmp_path / "page.png")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(tmp_path / "page.png", "01.png")
    book_id = int(api.manga.import_file(archive)["id"])
    try:
        api.save_settings({"manga_mokuro_python": f"  {fake}  "})
        assert api.get_state()["settings"]["manga_mokuro_python"] == str(fake)
        result = api.manga_text_regions(book_id, 0, False, False)
        assert result["regions"][0]["provenance"]["source"] == "mokuro"
        assert _requests(fake) == ["ocr 01.png"]
        # An engine choice saved by an older window changes nothing: Mokuro reads the page.
        api.save_settings({"manga_ocr_backend": "pudge"})
        api.manga.invalidate_region_cache(book_id)
        assert (
            api.manga_text_regions(book_id, 0, False, False)["regions"][0]["provenance"]["source"] == "mokuro"
        )
        assert _requests(fake) == ["ocr 01.png", "ocr 01.png"]
    finally:
        mokuro_sessions.close_all()


def test_installing_mokuro_never_touches_the_users_own_environment(tmp_path: Path, monkeypatch) -> None:
    from pudge import web_app
    from tests.test_web_app import make_api

    api = make_api(tmp_path)
    own = tmp_path / "my-mokuro" / "bin" / "python"
    own.parent.mkdir(parents=True)
    own.write_text("", encoding="utf-8")
    api.config.tools.manga_mokuro_python = str(own)
    commands: list[list[str]] = []

    def run(command, *args, **kwargs):
        commands.append([str(part) for part in command])
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[3]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(web_app.subprocess, "run", run)
    monkeypatch.setattr(web_app, "DEFAULT_LOG_PATH", tmp_path / "logs" / "pudge.log")
    api._run_mokuro_ocr_install()
    managed = str(api.config.paths.cache_dir / "mokuro-ocr" / "venv" / "bin" / "python")
    assert [command[0] for command in commands[1:]] == [managed, managed]
    assert not any(str(own) in part for command in commands for part in command)


def test_reader_gives_mokuro_regions_with_character_segments_word_hitboxes(page) -> None:
    # A Mokuro block as the converter makes it (line split into character cells), and the
    # same block from an older import without the cells (line geometry only).
    block = {
        "box": [300, 100, 340, 300],
        "vertical": True,
        "font_size": 40,
        "lines": ["さかなだ"],
        "lines_coords": [_line(300, 100, 340, 300)],
    }
    [split] = mokuro_page_regions(_page(block), 400, 600, "p1.png")
    older = {key: value for key, value in split.items() if key not in {"segments", "word_geometry"}}
    older["text"] = older["raw_text"] = "うみだ"
    older["x"] = 0.2
    older["provenance"] = {
        **split["provenance"],
        "line_boxes": [{**line, "x": 0.2, "text": "うみだ"} for line in split["provenance"]["line_boxes"]],
    }
    result = reader(
        page,
        r"""
        await __key('D', {shiftKey: true, code: 'KeyD'}); await __tick(10);
        await __key('D', {shiftKey: true, code: 'KeyD'}); await __tick(10);    // OCR debug overlay: words
        const boxes = [...document.querySelectorAll('#mangaV2Pages .manga-v2-debug-token-core')].map(node => ({
          word: node.dataset.word, source: node.dataset.source,
          top: Math.round(parseFloat(node.style.top) * 6), height: Math.round(parseFloat(node.style.height) * 6)}));
        return {boxes, marks: marks().map(m => [m.word, m.state, m.approximate])};
        """,
        regions={0: [split, older]},
        parse={"さかなだ": [["さかな", 1, 0], ["だ", 2, 0]], "うみだ": [["うみ", 3, 0], ["だ", 2, 0]]},
        prefs={"configured": True, "statusHighlight": True},
        before=STUDY
        + "window.__live = {'1:0': {states: ['new'], normalizedState: 'new'}, '3:0': {states: ['new'], normalizedState: 'new'}};",
    )
    out = result.value
    # One box per word on its own character cells (600 px page: さかな 100-250 px, だ 250-300 px);
    # the older import has its line only, for lookup of the whole line, never per word.
    assert out["boxes"] == [
        {"word": "さかな", "source": LINE_SPLIT_SOURCE, "top": 100, "height": 150},
        {"word": "だ", "source": LINE_SPLIT_SOURCE, "top": 250, "height": 50},
        {"word": "うみだ", "source": "mokuro-line-approximate-v1", "top": 100, "height": 200},
    ]
    # Word states are painted on the character geometry (approximate), never on a line.
    assert out["marks"] == [["さかな", "new", True]]
