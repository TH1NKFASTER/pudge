"""Manga reading in place: OCR text per page in the bubbles, zoom from the image's own size,
hover cards of the shared reading tools, full screen and the library's card actions
(behaviour; the reader runs in the page harness, audit 71Б rewrite)."""

from __future__ import annotations

import io
import threading
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from css_support import css_rules
from pudge import web_app as web_app_module
from pudge.database import Database
from pudge.manga import MangaService
from pudge.web_app import WebAppApi
from t71g03_support import MANGA, READER

NO_GATE = {"settings": {"review_gate_manga_enabled": False}}


def _two_page_cbz(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for index, value in enumerate((20, 220), start=1):
            image = Image.new("RGB", (40, 60), (value, 0, 0))
            payload = io.BytesIO()
            image.save(payload, format="PNG")
            archive.writestr(f"{index:03}.png", payload.getvalue())


def test_manga_text_regions_are_page_specific_and_cached(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    source = tmp_path / "One Piece 2.cbz"
    _two_page_cbz(source)
    book = service.import_file(source)
    calls: list[int] = []

    monkeypatch.setattr(service, "_mokuro_python", lambda: tmp_path / "mokuro" / "bin" / "python")

    def fake_mokuro(archive: Path, name: str, **_kwargs):  # Mokuro reads the page's colour
        with zipfile.ZipFile(archive) as volume:
            red = int(Image.open(io.BytesIO(volume.read(name))).convert("RGB").getpixel((0, 0))[0])
        calls.append(red)
        line = [[4, 6], [16, 6], [16, 40], [4, 40]]
        block = {
            "box": [4, 6, 16, 40],
            "vertical": True,
            "font_size": 6,
            "lines": [f"頁{red}"],
            "lines_coords": [line],
        }
        return {"img_width": 40, "img_height": 60, "blocks": [block]}

    monkeypatch.setattr(service, "_mokuro_recognize", fake_mokuro)

    second = service.text_regions(book["id"], 1)
    second_again = service.text_regions(book["id"], 1)
    uncached = service.text_regions(book["id"], 0, cached_only=True)
    first = service.text_regions(book["id"], 0)

    assert second["page_index"] == 1
    assert second["regions"][0]["text"].startswith("頁")
    assert second_again["cached"] is True
    assert uncached["cached"] is False
    assert uncached["regions"] == []
    assert first["page_index"] == 0
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_bubble_text_sits_on_the_page_and_zoom_grows_from_the_image_size(page) -> None:
    result = page.run(
        READER
        + r"""
        await open(3);
        const frame = document.querySelector('#mangaV2Pages .manga-v2-page-frame');
        const region = frame.querySelector('.manga-v2-text-layer .manga-v2-text-region');
        const out = {inPlace: {content: region.querySelector('.manga-v2-region-content')?.textContent, translateRoot: region.hasAttribute('data-pudge-translate-root'),
                               side: ['mangaV2OcrText', 'mangaV2BubbleStudy'].filter(id => document.getElementById(id))}};
        // Manga words open their card on click, not on hover.
        await __fire('.manga-v2-region-content [data-pudge-study-token]', 'pointerover', {}, 'PointerEvent'); await __tick(300);
        out.hoverCard = Boolean(document.getElementById('pudgeStudyCard')?.classList.contains('open'));
        // An 800x1200 page in a 1000x724 viewport, fit to height.
        const viewport = document.getElementById('mangaV2Viewport');
        viewport.clientWidth = 1000; viewport.clientHeight = 724;
        const img = frame.querySelector('img');
        img.__naturalWidth = 800; img.__naturalHeight = 1200;
        const size = () => [parseFloat(img.style.width), parseFloat(img.style.height)].map(Math.round);
        await __key('0', {}, 'body'); await __tick(20);
        out.sizes = [size()];
        for (let k = 0; k < 3; k++) { await __key('+', {}, 'body'); await __tick(20); out.sizes.push(size()); }
        await __key('-', {}, 'body'); await __tick(20);
        out.sizes.push(size());
        return out;
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    assert out["inPlace"] == {"content": "猫", "translateRoot": True, "side": []}
    assert out["hoverCard"] is False
    # 100 % fits the page height (700 px of 724); every step scales the natural size, so zoom
    # grows monotonically (WebKit's CSS zoom inverted the image above 100 %).
    assert out["sizes"] == [[467, 700], [513, 770], [560, 840], [607, 910], [560, 840]]
    for _media, _selector, declarations in css_rules("manga_reader_v2.css").rules:
        assert "--manga-zoom" not in declarations.get("zoom", "") + declarations.get("transform", "")


def test_hover_study_roots_open_one_card_after_a_short_pause(page) -> None:
    result = page.run(
        r"""
        // A live line with hover lookup (the visual-novel line and the sidebar companion work like this).
        const line = document.createElement('div');
        line.setAttribute('data-pudge-study-hover', '');
        document.body.appendChild(line);
        line.innerHTML = PudgeReadingTools.study.renderParsedText(await pywebview.api.study_parse_text('猫だ'));
        const card = () => Boolean(document.getElementById('pudgeStudyCard')?.classList.contains('open'));
        await __fire('[data-pudge-study-hover] [data-pudge-study-token]', 'pointerover', {}, 'PointerEvent');
        await __tick(100);
        const early = card();
        await __tick(150);
        return {early, open: card(), term: document.querySelector('#pudgeStudyCard .pudge-study-term')?.textContent};
        """,
        before=MANGA,
    )
    out = result.value
    assert out["early"] is False and out["open"] is True and "猫" in out["term"]


def test_fullscreen_button_and_library_card_actions(page) -> None:
    result = page.run(
        READER
        + r"""
        await open(3);
        const button = document.querySelector('.manga-v2-toolbar [data-manga-v2-action="fullscreen"]');
        const out = {label: button.textContent, expected: t('manga.fullscreen')};
        await __click('.manga-v2-toolbar [data-manga-v2-action="fullscreen"]');
        await __click('[data-manga-v2-action="close"]'); await __tick(100);
        const menu = async selector => {
          await __fire(selector, 'contextmenu', {clientX: 20, clientY: 20}, 'MouseEvent');
          return [...document.querySelectorAll('#contextMenu [data-manga-context-action]')].map(b => b.dataset.mangaContextAction);
        };
        out.volume = await menu('[data-manga-book="3"]');
        await __click('#contextMenu [data-manga-context-action="anilist"]'); await __tick(20);
        out.series = await menu('[data-manga-series-key] .ln-series-head');
        await __click('#contextMenu [data-manga-context-action="ocr-series"]'); await __tick(20);
        out.reading = reader().classList.contains('open');
        return out;
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    assert out["label"] == out["expected"]
    assert result.calls_of("toggle_fullscreen") == [[]]
    # A volume's menu: read, AniList, OCR of the volume; scoring belongs to the series.
    assert {"read", "anilist", "ocr-book"} <= set(out["volume"]) and "score" not in out["volume"]
    assert {"read-series", "anilist", "score-series", "ocr-series"} <= set(out["series"])
    assert result.calls_of("open_url") == [["https://anilist.co/manga/77"]]
    assert result.calls_of("start_manga_ocr_book") == [[3, True], [4, True]]
    assert out["reading"] is False


def test_native_fullscreen_toggles_the_key_window_once_per_request(monkeypatch) -> None:
    toggled: list[object] = []
    window = SimpleNamespace(toggleFullScreen_=toggled.append)
    application = SimpleNamespace(keyWindow=lambda: window, mainWindow=lambda: None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "AppKit",
        types.SimpleNamespace(NSApplication=SimpleNamespace(sharedApplication=lambda: application)),
    )
    monkeypatch.setattr(web_app_module.sys, "platform", "darwin")
    timers: list = []

    class Timer:
        def __init__(self, delay, function):
            self.delay, self.function, self.daemon = delay, function, False
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(web_app_module.threading, "Timer", Timer)
    api = WebAppApi.__new__(WebAppApi)
    api._fullscreen_exit_lock = threading.Lock()
    api._fullscreen_exit_pending = False
    api.logger = SimpleNamespace(exception=lambda *a, **k: None)

    assert api.toggle_fullscreen() == {"ok": True, "scheduled": True}
    # A second key press while macOS animates is ignored, not queued as a second toggle.
    assert api.toggle_fullscreen()["scheduled"] is False
    assert toggled == [None]
    timers[-1].function()
    assert api.toggle_fullscreen()["scheduled"] is True and toggled == [None, None]
