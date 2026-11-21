"""Manga two pages: scanned pages straightened and aligned (pudge/manga_align.py).

Scanned volumes are scanned page by page, each page a little rotated, cropped and scaled
differently, so in the two-page view the panel borders were not level and the art that goes on
over the gutter did not meet. The Mac now works out, once per page in the background, a
transform per page (straighten; same frame height and place for facing pages; the blank scan
margin at the gutter taken away and the vertical offset matched where the art crosses it) and
serves it with the two-page table; the Mac reader (CSS) and the iPhone/iPad draw the same
(tests/fixtures/manga_alignment.json). "Align scanned pages" is a per-volume toggle, on by
default, synced like "Shift pairs by one page"; both toggles now act at once in the open reader.
Synthetic pages only (tests/manga_align_support.py).
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

pytest.importorskip("numpy")

import manga_align_support as S  # noqa: E402
from pudge import manga_align  # noqa: E402
from pudge.database import Database  # noqa: E402
from pudge.manga import MangaService  # noqa: E402
from t71g03_support import MANGA, READER  # noqa: E402
from t71js_support import pages  # noqa: E402

FIXTURE = Path(__file__).with_name("fixtures") / "manga_alignment.json"
SPREADS = Path(__file__).with_name("fixtures") / "manga_spreads.json"
GUTTER = 700  # where the gutter is in draw_spread(height=1000) of two 700 × 1000 pages
ACROSS = range(90, 320)  # rows of the panel that runs over both pages


def _pair(across: bool, left_scan: dict, right_scan: dict, seed: int = 1):
    """Two facing pages scanned with `left_scan` / `right_scan` (screen left, screen right) and
    their transforms (screen order) for a right-to-left volume (the right page is the first)."""
    left, right = S.printed_spread(across=across, seed=seed)
    scanned_left = S.scan(left, side="right", **left_scan)
    scanned_right = S.scan(right, side="left", **right_scan)
    first, second = manga_align.pair_transforms(
        manga_align.analyze(scanned_right), manga_align.analyze(scanned_left), right_to_left=True
    )
    return scanned_left, scanned_right, [second, first]


# ------------------------------------------------------------------------------- one page
@pytest.mark.parametrize("angle", [-1.6, -0.7, 0.0, 0.35, 1.2])
def test_the_tilt_of_a_scanned_page_is_found(angle: float) -> None:
    left, _right = S.printed_spread(across=False)
    found = manga_align.analyze(S.scan(left, angle=angle, scale=0.99, shift=(4, -7)))
    assert found["angle"] == pytest.approx(angle, abs=0.08)
    transform = manga_align.single_transform(found)
    if abs(angle) < manga_align.MIN_ANGLE:
        assert transform is None  # nothing to straighten
    else:
        assert transform == {"angle": pytest.approx(-angle, abs=0.08), "scale": 1.0, "dx": 0.0, "dy": 0.0}


def test_a_page_without_clear_border_lines_or_tilted_too_much_is_left_as_it_is() -> None:
    from PIL import ImageDraw

    # Short strokes and dots only: nothing says which way is level.
    page = Image.new("L", (700, 1000), 255)
    draw = ImageDraw.Draw(page)
    for k in range(400):
        x, y = (k * 97) % 680 + 10, (k * 61) % 980 + 10
        draw.line((x, y, x + 6 + k % 5, y + 3 + (k * 7) % 9), fill=0, width=2)
    found = manga_align.analyze(page)
    assert found["angle"] is None and manga_align.single_transform(found) is None
    # A blank page, and one drawn at a slant (more than 2 degrees): left alone too.
    assert manga_align.analyze(Image.new("L", (700, 1000), 255))["angle"] is None
    left, _right = S.printed_spread(across=False)
    assert manga_align.analyze(S.scan(left, angle=3.4))["angle"] is None


# -------------------------------------------------------------------------- facing pages
@pytest.mark.parametrize(
    "left_scan, right_scan",
    [
        (
            {"angle": 0.6, "shift": (0, 12), "gutter_margin": 14},
            {"angle": -0.4, "scale": 0.98, "shift": (0, -6)},
        ),
        ({"angle": -1.2, "shift": (0, -10)}, {"angle": 0.8, "shift": (0, 8), "gutter_margin": 10}),
        (
            {"angle": 0.3, "scale": 1.02, "shift": (0, 5), "gutter_margin": 8},
            {"angle": 1.0, "shift": (0, -9)},
        ),
    ],
)
def test_art_over_the_gutter_meets(left_scan: dict, right_scan: dict) -> None:
    left, right, transforms = _pair(True, left_scan, right_scan)
    before = S.draw_spread(left, right, [None, None], height=1000)
    after = S.draw_spread(left, right, transforms, height=1000)
    # The art crossing the gutter was out by many pixels and had a blank strip between.
    assert abs(S.gutter_lag(before, GUTTER, ACROSS)) >= 8
    assert S.blank_columns_at_gutter(before, GUTTER, ACROSS) >= 8
    # Now it meets: the same height on both sides, nothing blank between.
    assert abs(S.gutter_lag(after, GUTTER, ACROSS)) <= 1
    assert S.blank_columns_at_gutter(after, GUTTER, ACROSS) <= 1
    for transform in transforms:
        assert transform is not None
        assert abs(transform["angle"]) <= manga_align.MAX_ANGLE
        assert abs(transform["scale"] - 1) <= manga_align.MAX_SCALE + 0.03
        assert abs(transform["dy"]) <= manga_align.MAX_SHIFT + 0.01
    assert transforms[0]["gutter"] == "right" and transforms[1]["gutter"] == "left"
    # Nothing drawn is cut away at the gutter: only blank margin goes.
    assert S.ink_inside(left, transforms[0]) and S.ink_inside(right, transforms[1])


def _frame_rows(image: Image.Image, columns: range) -> tuple[int, int]:
    """The top and bottom rows of the drawn frame (long dark rows) in `columns`."""
    import numpy as np

    values = np.asarray(image.convert("L"), dtype=np.float64)[:, columns.start : columns.stop]
    dark = np.nonzero((values < 100).mean(axis=1) > 0.5)[0]
    return int(dark[0]), int(dark[-1])


def test_facing_pages_frames_get_the_same_height_and_place() -> None:
    # No art over the gutter: each page has its own panels in a frame with paper around it.
    left, right, transforms = _pair(
        False, {"angle": 0.5, "shift": (0, 14)}, {"angle": -0.3, "scale": 0.975, "shift": (0, -8)}
    )
    # (On white: only the frames' lines are long dark rows.)
    before = S.draw_spread(left, right, [None, None], height=1000, background=255)
    after = S.draw_spread(left, right, transforms, height=1000, background=255)
    left_cols, right_cols = range(150, 550), range(850, 1250)
    top_l, bottom_l = _frame_rows(before, left_cols)
    top_r, bottom_r = _frame_rows(before, right_cols)
    assert abs(top_l - top_r) > 10 and abs((bottom_l - top_l) - (bottom_r - top_r)) > 10
    top_l, bottom_l = _frame_rows(after, left_cols)
    top_r, bottom_r = _frame_rows(after, right_cols)
    assert abs(top_l - top_r) <= 2 and abs(bottom_l - bottom_r) <= 2
    # Their own inner margins stay (no art crosses): nothing moves over the gutter.
    assert S.blank_columns_at_gutter(after, GUTTER, range(100, 900)) >= 40
    assert S.ink_inside(left, transforms[0]) and S.ink_inside(right, transforms[1])


def test_pages_that_need_nothing_are_left_as_they_are() -> None:
    left, right, transforms = _pair(True, {}, {})
    for transform in transforms:
        assert transform is None or (
            abs(transform["angle"]) < 0.05
            and abs(transform["scale"] - 1) < 0.002
            and abs(transform["dy"]) < 0.002
        )


def test_without_numpy_nothing_is_aligned(monkeypatch, tmp_path: Path) -> None:
    left, _right = S.printed_spread()
    monkeypatch.setattr(manga_align, "np", None)
    found = manga_align.analyze(S.scan(left, angle=0.8))
    assert found["angle"] is None and not manga_align.available()
    assert manga_align.pair_transforms(found, found, right_to_left=True) == [None, None]
    from pudge.manga_align_store import AlignmentStore

    store = AlignmentStore(tmp_path, lambda path: None, lambda archive, name: b"")
    assert store.served(tmp_path / "book.cbz", ["a.png"], []) is None


# ----------------------------------------------------------------------- the shared math
def test_the_shared_transform_cases() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 3
    for case in cases:
        transform, aspect = case["transform"], case["aspect"]
        for point, drawn in zip(case["points"], case["drawn"]):
            assert manga_align.apply(transform, *point, aspect) == pytest.approx(drawn, abs=1e-6)
            assert manga_align.invert(transform, *drawn, aspect) == pytest.approx(point, abs=1e-6)
        if case.get("backing"):
            assert list(manga_align.backing(transform, aspect, transform["gutter"])) == pytest.approx(
                case["backing"], abs=1e-6
            )


def test_the_mac_reader_draws_the_same(page) -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    tables = json.loads(SPREADS.read_text(encoding="utf-8"))["cases"]
    result = page.run(
        r"""
        const R = window.PudgeMangaReaderV2;
        const cases = %s, tables = %s;
        const box = {left: 40, top: 10, width: 0, height: 900};
        return {
          drawn: cases.map(c => {
            const b = {...box, width: 900 * c.aspect};
            return c.points.map(([x, y]) => {
              const p = R.alignPoint(c.transform, b, b.left + x * b.width, b.top + y * b.height);
              const back = R.alignPoint(c.transform, b, p.x, p.y, true);
              return [(p.x - b.left) / b.width, (p.y - b.top) / b.height, (back.x - b.left) / b.width, (back.y - b.top) / b.height];
            });
          }),
          css: cases.map(c => R.alignmentStyle(c.transform)),
          tables: tables.map(c => R.spreadTable(c.pageCount, c.wide, c.shift)),
        };
        """
        % (json.dumps(cases), json.dumps(tables)),
    )
    out = result.value
    for case, drawn in zip(cases, out["drawn"]):
        for point, expected, got in zip(case["points"], case["drawn"], drawn):
            assert got[:2] == pytest.approx(expected, abs=1e-6), case["name"]
            assert got[2:] == pytest.approx(point, abs=1e-6), case["name"]
    for case, css in zip(cases, out["css"]):
        t = case["transform"]
        numbers = [
            float(value) for value in __import__("re").findall(r"-?[\d.]+(?:e-?\d+)?", css["transform"])
        ]
        assert numbers == pytest.approx([t["dx"] * 100, t["dy"] * 100, t["angle"], t["scale"]], abs=1e-9)
        assert css["gutter"] == t.get("gutter", "")
    assert out["tables"] == [case["spreads"] for case in tables]


# -------------------------------------------------------------------------- the Mac serves
def _volume(tmp_path: Path) -> tuple[Database, MangaService, int, Path]:
    """Cover, a pair whose art crosses the gutter (scanned out of line), a plain pair."""
    left, right = S.printed_spread(across=True)
    plain_left, plain_right = S.printed_spread(across=False, seed=2)
    pages_ = [
        S.scan(plain_right, angle=0.4),
        S.scan(right, side="left", angle=-0.5, shift=(0, -9)),  # page 1: on the right
        S.scan(left, side="right", angle=0.7, shift=(0, 9), gutter_margin=12),  # page 2: on the left
        S.scan(plain_right, angle=-0.3),
        S.scan(plain_left, angle=0.2),
    ]
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        for number, image in enumerate(pages_):
            path = tmp_path / f"{number:03d}.png"
            image.save(path)
            zf.write(path, path.name)
    db = Database(tmp_path / "pudge.db")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    return db, service, int(service.import_file(archive)["id"]), archive


def _worked_out(service: MangaService, book_id: int) -> dict:
    """The answer once everything is worked out and the worker has saved it."""
    deadline = time.monotonic() + 60
    while True:
        answer = service.spread_pages(book_id)
        if answer["alignment"] is not None and answer["alignment"]["pending"] == 0:
            break
        assert time.monotonic() < deadline, answer["alignment"]
        time.sleep(0.05)
    while service._alignment_store()._job is not None:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return answer


def test_the_mac_works_the_alignment_out_once_and_serves_it(tmp_path: Path, monkeypatch) -> None:
    _db, service, book_id, archive = _volume(tmp_path)
    first = service.spread_pages(book_id)
    assert first["align"] is True and first["spreads"] == [[0], [1, 2], [3, 4]]
    assert first["alignment"]["pending"] > 0  # worked out in the background, not now
    answer = _worked_out(service, book_id)
    alignment = answer["alignment"]
    # Every pair of both tables (with and without the shift), both reading directions.
    rtl = alignment["pairs"]["1-2"]["rtl"]
    assert rtl[0]["gutter"] == "left" and rtl[1]["gutter"] == "right"
    assert rtl[0]["angle"] == pytest.approx(0.5, abs=0.1) and rtl[1]["angle"] == pytest.approx(-0.7, abs=0.1)
    assert rtl[1]["dx"] > 0.01  # the blank margin at the gutter goes
    assert "ltr" in alignment["pairs"]["1-2"]
    assert set(alignment["pairs"]) <= {"1-2", "3-4", "2-3"}
    assert alignment["single"]["0"]["angle"] == pytest.approx(-0.4, abs=0.1)  # the cover: straightened only
    assert list((tmp_path / "cache" / "manga-align").glob("*.json"))
    # Kept by page identity: a new start serves it without looking at a page again.
    calls: list[str] = []
    monkeypatch.setattr(manga_align, "analyze", lambda image: calls.append("page") or {"version": 0})
    again = MangaService(Database(tmp_path / "pudge.db"), cache_dir=tmp_path / "cache").spread_pages(book_id)
    assert again["alignment"] == alignment and calls == []
    # One page changed: only that one is looked at again.
    with zipfile.ZipFile(archive) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    changed = tmp_path / "changed.png"
    S.scan(S.printed_spread(across=False)[0], angle=1.1).save(changed)
    members["004.png"] = changed.read_bytes()
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    fresh = MangaService(Database(tmp_path / "pudge.db"), cache_dir=tmp_path / "cache")
    _worked_out(fresh, book_id)
    assert calls == ["page"]


def test_align_scanned_pages_per_volume_on_the_mac_and_phones(tmp_path: Path) -> None:
    from pudge.bridge_methods import PAGE_METHODS
    from pudge.iphone_media import MediaError
    from pudge.iphone_service import IPhoneService
    from pudge.web_app import WebAppApi

    db, manga, book_id, _archive = _volume(tmp_path)
    api = SimpleNamespace(manga=manga)
    assert WebAppApi.manga_spreads(api, book_id)["align"] is True
    assert WebAppApi.manga_set_page_align(api, book_id, False)["align"] is False
    assert db.get_state(f"manga_page_align:v1:{book_id}", "") == "0"
    assert MangaService(db, cache_dir=tmp_path / "cache").spread_pages(book_id)["align"] is False
    assert "manga_set_page_align" in PAGE_METHODS
    phone = IPhoneService(db, tmp_path / "mobile")
    phone.api = SimpleNamespace(manga=manga)
    route = f"/api/v2/media/manga/{book_id}/spreads"
    assert phone.media.handle("POST", route, {}, lambda: {"align": True})["align"] is True
    both = phone.media.handle("POST", route, {}, lambda: {"shift": True, "align": False})
    assert both["shift"] is True and both["align"] is False
    for wrong in ({"align": "no"}, {}):
        with pytest.raises(MediaError):
            phone.media.handle("POST", route, {}, lambda wrong=wrong: wrong)
    manga.remove_books([book_id])
    assert db.get_state(f"manga_page_align:v1:{book_id}", "") == ""


def test_a_downloaded_volume_carries_the_alignment(tmp_path: Path) -> None:
    from pudge.iphone_reading import PackageWriter
    from pudge.iphone_service import IPhoneService

    db, manga, book_id, _archive = _volume(tmp_path)
    expected = _worked_out(manga, book_id)["alignment"]
    phone = IPhoneService(db, tmp_path / "mobile")
    phone.api = SimpleNamespace(manga=manga)
    reading = phone.media.reading
    reading.ocr_status = lambda _book: {"complete": False}
    phone.media.cover = lambda kind, book: (b"jpg", "image/jpeg")
    description = reading._manga(book_id, PackageWriter(tmp_path / "pack"), lambda value: None)
    assert description["pageAlign"] is True and description["alignment"] == expected


def test_the_toggle_is_translated() -> None:
    from pudge.i18n import catalog

    assert catalog("en")["manga.alignPages"] == "Align scanned pages"
    assert catalog("ru")["manga.alignPages"] == "Выравнивать отсканированные страницы"


# ------------------------------------------------------------ the Mac reader (instantly)
SHOWN = r"""
const shown = () => ({pages: pages().map(p => p.index), label: __text('#mangaV2PageLabel')});
const next = async () => { await __click('#mangaReaderV2 [data-manga-v2-action="next"]'); await __tick(150); };
const frameOf = index => document.querySelector(`#mangaV2Pages .manga-v2-page-frame[data-page-index="${index}"]`);
const toggle = (selector, value) => {
  const box = document.querySelector(selector);
  box.checked = value;
  box.dispatchEvent(new Event('change', {bubbles: true}));
};
"""

# The Mac's answer with the alignment of the pair 3–4 (window.MANGA.alignment) and the toggle;
# window.MANGA.holdSave keeps the settings' saves waiting until window.release('save')
# (holdOnce: only the next one);
# window.MANGA.holdSpreads keeps manga_spreads answers (made when asked) until
# window.release('spreads'), window.MANGA.failSpreads makes them fail; window.MANGA.received
# is what the Mac received, in order.
ALIGNED = r"""
window.waiting.save = []; window.waiting.spreads = [];
window.MANGA.received = [];
window.MANGA.align = true;
window.MANGA.alignment = {version: 1, pending: 0, single: {}, pairs: {'3-4': {rtl: [
  {angle: -0.5, scale: 0.99, dx: -0.01, dy: 0.005, gutter: 'left', backing: [0, 0.01, 0.004, 0.99], fill: '#f0f0f0'},
  {angle: 0.3, scale: 1.01, dx: 0.02, dy: -0.005, gutter: 'right'}]}}};
const servedSpreads = __api.manga_spreads;
const answerOf = async id => ({...(await servedSpreads(id)), align: window.MANGA.align, alignment: window.MANGA.alignment});
__api.manga_spreads = async id => {
  const answer = await answerOf(id);  // as the Mac has it when asked
  if (window.MANGA.holdSpreads) await window.held('spreads');
  if (window.MANGA.failSpreads) throw new Error('The Mac is busy');
  return answer;
};
__api.manga_set_pair_shift = async (id, shift) => {
  if (window.MANGA.holdSave || window.MANGA.holdOnce) { window.MANGA.holdOnce = false; await window.held('save'); }
  if (window.MANGA.failSave) throw new Error('The Mac could not save it');
  window.MANGA.received.push(['shift', Boolean(shift)]);
  window.MANGA.shift[id] = Boolean(shift);
  return answerOf(id);
};
__api.manga_set_page_align = async (id, align) => {
  if (window.MANGA.holdSave || window.MANGA.holdOnce) { window.MANGA.holdOnce = false; await window.held('save'); }
  if (window.MANGA.failSave) throw new Error('The Mac could not save it');
  window.MANGA.received.push(['align', Boolean(align)]);
  window.MANGA.align = Boolean(align);
  return answerOf(id);
};
"""


def _double() -> dict:
    return {
        "before": MANGA + pages(12, ocr=False) + "window.MANGA.books[0].position = 4;" + ALIGNED,
        "localStorage": {"pudge.manga.reader.v2": json.dumps({"mode": "double", "direction": "rtl"})},
    }


def test_shift_pairs_by_one_page_re_pairs_at_once_and_keeps_the_page_in_view(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        await open(3);
        const out = {opened: shown()};
        window.MANGA.holdSave = true;
        toggle('[data-manga-pair-shift]', true);
        await __tick(20);  // the Mac has not answered yet
        out.at_once = shown();
        out.calls = callsOf('manga_set_pair_shift');
        window.release('save'); await __tick(100);
        out.saved = shown();
        // A save that fails puts the pairs back, with the error.
        window.MANGA.holdSave = false; window.MANGA.failSave = true;
        toggle('[data-manga-pair-shift]', false);
        await __tick(100);
        out.failed = shown();
        out.checked = document.querySelector('[data-manga-pair-shift]').checked;
        out.toasts = __toasts();
        out.positions = callsOf('manga_set_position').map(c => c[1]);
        return out;
        """,
        **_double(),
    )
    out = result.value
    # Saved page 5 (index 4) opens in the pair 4–5; shifted, it is in 5–6 at once.
    assert out["opened"]["pages"] == [3, 4]
    assert out["at_once"] == {"pages": [4, 5], "label": "5–6 / 12"}
    assert out["calls"] == [[3, True]] and out["saved"]["pages"] == [4, 5]
    assert out["failed"]["pages"] == [4, 5] and out["checked"] is True
    assert any("could not save" in toast for toast in out["toasts"])
    assert out["positions"] in ([], [4])  # the place is still page 5 (index 4)


def test_align_scanned_pages_applies_at_once_to_picture_and_text(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        await open(3);
        const frame = frameOf(3), other = frameOf(4);
        const out = {
          aligned: [frame.classList.contains('aligned'), other.classList.contains('aligned')],
          transform: frame.style.getPropertyValue('--manga-align'),
          gutters: [frame.dataset.alignGutter, other.dataset.alignGutter],
          backing: [frame.dataset.alignBacking || '', frame.style.getPropertyValue('--manga-align-fill')],
          toggle: document.querySelector('[data-manga-page-align]').checked,
          label: document.querySelector('[data-manga-page-align]').closest('label').textContent.trim(),
          textLayer: Boolean(frame.querySelector(':scope > .manga-v2-text-layer')),
        };
        window.MANGA.holdSave = true;
        toggle('[data-manga-page-align]', false);
        await __tick(20);  // not saved yet: already drawn as scanned
        out.off = [frameOf(3).classList.contains('aligned'), frameOf(4).classList.contains('aligned')];
        window.release('save'); await __tick(100);
        out.offSaved = frameOf(3).classList.contains('aligned');
        toggle('[data-manga-page-align]', true);
        await __tick(20);
        out.on = frameOf(3).classList.contains('aligned');
        window.release('save'); await __tick(100);
        out.calls = callsOf('manga_set_page_align');
        return out;
        """,
        **_double(),
    )
    out = result.value
    assert out["aligned"] == [True, True]
    assert out["transform"] == "translate(-1%, 0.5%) rotate(-0.5deg) scale(0.99)"
    # Right to left: page 3 (the lower one) is on the right, its left side at the gutter.
    assert out["gutters"] == ["left", "right"]
    assert out["backing"] == ["1", "#f0f0f0"]
    assert out["toggle"] is True and out["label"] == "Align scanned pages"
    # The text layer is one of the frame's layers the transform moves (CSS .aligned > *).
    assert out["textLayer"] is True
    assert out["off"] == [False, False] and out["calls"] == [[3, False], [3, True]]
    assert out["offSaved"] is False and out["on"] is True


def test_a_change_on_a_phone_shows_on_the_next_turn_without_jumping(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        await open(3);
        const out = {opened: shown()};
        window.MANGA.shift[3] = true;  // changed on the iPhone
        await next();
        out.turned = shown();
        out.checked = document.querySelector('[data-manga-pair-shift]').checked;
        return out;
        """,
        **_double(),
    )
    out = result.value
    assert out["opened"]["pages"] == [3, 4]
    # Turned to page 6 (index 5): now paired as the phone has it, page 6 still on screen.
    assert out["turned"] == {"pages": [4, 5], "label": "5–6 / 12"} and out["checked"] is True


def test_the_first_pairs_update_when_the_mac_has_worked_them_out(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        const full = window.MANGA.alignment;
        window.MANGA.alignment = {version: 1, pending: 7, single: {}, pairs: {}};
        await open(3);
        const out = {before: frameOf(3).classList.contains('aligned')};
        const asked = callsOf('manga_spreads').length;
        window.MANGA.alignment = full;  // worked out meanwhile
        await __tick(2600);
        out.asked = callsOf('manga_spreads').length - asked;
        out.after = frameOf(3).classList.contains('aligned');
        out.pages = shown().pages;
        await __tick(6000);
        out.stopped = callsOf('manga_spreads').length - asked;
        return out;
        """,
        **_double(),
    )
    out = result.value
    assert out["before"] is False and out["asked"] >= 1
    assert out["after"] is True and out["pages"] == [3, 4]
    assert out["stopped"] == out["asked"]  # nothing pending: no more asking


# ------------------------------------------------------------------- taps and styles
def test_a_word_is_tapped_where_its_aligned_page_draws_it(page) -> None:
    from t71g05_support import READER as WORDS_READER
    from t71g05_support import READER_SETTINGS, reader_before, vertical_segments

    bubble = {
        "text": "猫が好き",
        "x": 0.7,
        "y": 0.5,
        "width": 0.1,
        "height": 0.3,
        "orientation": "vertical",
        "source": "vision",
        "segments": vertical_segments("猫が好き", 0.7, 0.8, 0.1, 0.075),
    }
    # Pages 2–3 (indices 1–2) together; page 2 (on the right) drawn 10 % lower.
    extra = r"""
__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 4, position: 1, ocr_complete: true}], ocr_available: true});
__api.manga_spreads = async () => ({spreads: [[0], [1, 2], [3]], shift: false, wide: [], align: true,
  alignment: {version: 1, pending: 0, single: {}, pairs: {'1-2': {rtl: [{angle: 0, scale: 1, dx: 0, dy: 0.1, gutter: 'left'}, null]}}}});
"""
    result = page.run(
        WORDS_READER
        + r"""
        await openReader();
        const frame = document.querySelector('.manga-v2-page-frame[data-page-index="1"]');
        frame.__rect = {x: 0, y: 0, width: W, height: H};  // where the page is laid out
        const tap = async (clientX, clientY) => {
          frame.querySelector('img').dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX, clientY, detail: 1}));
          await __tick(50);
          const term = cardTerm();
          if (term !== null) { PudgeReadingTools.study.closeIfOpen(); await __tick(10); }
          return term;
        };
        // 猫's box centre is at (0.75, 0.7625) of the page: drawn 30 px lower on the screen.
        return {aligned: frame.classList.contains('aligned'),
                drawn: await tap(150, (1 - .7625) * H + 30), unmoved: await tap(150, (1 - .7625) * H)};
        """,
        before=reader_before({1: [bubble]}, {"猫が好き": ["猫", "が", "好き"]}, extra=extra),
        state=READER_SETTINGS,
        localStorage={"pudge.manga.reader.v2": json.dumps({"mode": "double", "direction": "rtl"})},
    )
    out = result.value
    assert out["aligned"] is True
    # The word under the finger is the word drawn there, not the one where the page would be.
    assert out["drawn"] == "猫" and out["unmoved"] != "猫"


def test_the_picture_and_its_text_layers_move_together() -> None:
    from css_support import css_rules

    rules = css_rules("manga_reader_v2.css")
    assert rules.get(".manga-v2-page-frame.aligned > *", "transform") == "var(--manga-align)"
    assert rules.get(".manga-v2-page-frame.aligned > *", "transform-origin") == "50% 50%"
    # Cut off at the gutter only (the other sides keep a turned page's corners).
    assert (
        rules.get('.manga-v2-page-frame.aligned[data-align-gutter="right"]', "clip-path")
        == "inset(-50% 0 -50% -50%)"
    )
    assert (
        rules.get('.manga-v2-page-frame.aligned[data-align-gutter="left"]', "clip-path")
        == "inset(-50% -50% -50% 0)"
    )
    # The picture's shadow would darken the open band at the gutter.
    assert rules.get(".manga-v2-page-frame.aligned[data-align-gutter] img", "box-shadow") == "none"
    backing = rules.declarations('.manga-v2-page-frame.aligned[data-align-backing="1"]::before')
    assert backing["background"] == "var(--manga-align-fill)" and backing["z-index"] == "-1"


def test_an_answer_asked_before_a_change_never_undoes_it(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        await open(3);
        window.MANGA.holdSpreads = true;
        await next();  // shows 6–7 and asks the Mac for its table (the answer waits)
        const out = {turned: shown().pages};
        toggle('[data-manga-pair-shift]', true);
        await __tick(100);  // saved on the Mac
        out.shifted = shown().pages;
        window.MANGA.holdSpreads = false;
        window.release('spreads'); await __tick(100);  // the older answer: shift off
        out.after = shown().pages;
        out.checked = document.querySelector('[data-manga-pair-shift]').checked;
        return out;
        """,
        **_double(),
    )
    out = result.value
    assert out["turned"] == [5, 6] and out["shifted"] == [4, 5]
    assert out["after"] == [4, 5] and out["checked"] is True


def test_quick_changes_reach_the_mac_in_order(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        await open(3);
        window.MANGA.holdOnce = true;  // the first save is slow, the next ones are quick
        toggle('[data-manga-pair-shift]', true);
        await __tick(10);
        toggle('[data-manga-pair-shift]', false);
        toggle('[data-manga-page-align]', false);
        await __tick(50);
        const out = {waiting: window.MANGA.received.length, shown: shown().pages};
        window.release('save'); await __tick(200);
        out.received = window.MANGA.received;
        out.mac = {shift: Boolean(window.MANGA.shift[3]), align: window.MANGA.align};
        out.after = shown().pages;
        out.checked = [document.querySelector('[data-manga-pair-shift]').checked, document.querySelector('[data-manga-page-align]').checked];
        out.aligned = frameOf(3).classList.contains('aligned');
        return out;
        """,
        **_double(),
    )
    out = result.value
    # One save at a time: the later ones wait for the first, then go in the order made.
    assert out["waiting"] == 0 and out["shown"] == [3, 4]
    assert out["received"] == [["shift", True], ["shift", False], ["align", False]]
    assert out["mac"] == {"shift": False, "align": False}
    assert out["after"] == [3, 4] and out["checked"] == [False, False] and out["aligned"] is False


def test_a_pair_with_one_page_left_as_it_is_is_still_drawn_at_one_height(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        window.MANGA.alignment = {version: 1, pending: 0, single: {}, pairs: {'3-4': {rtl: [
          null, {angle: 0.3, scale: 1, dx: 0.01, dy: 0, gutter: 'right'}]}}};
        await open(3);
        return [3, 4].map(index => ({pair: frameOf(index).dataset.alignPair || '', aligned: frameOf(index).classList.contains('aligned')}));
        """,
        **_double(),
    )
    assert result.value == [{"pair": "1", "aligned": False}, {"pair": "1", "aligned": True}]


def test_the_mac_keeps_asking_after_a_failed_answer_or_during_a_save(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        const full = window.MANGA.alignment;
        window.MANGA.alignment = {version: 1, pending: 7, single: {}, pairs: {}};
        await open(3);
        const asked = () => callsOf('manga_spreads').length;
        const start = asked();
        window.MANGA.failSpreads = true;
        await __tick(2600);  // asks, the Mac fails
        const out = {failed: asked() - start};
        window.MANGA.failSpreads = false;
        await __tick(2600);  // and asks again
        out.again = asked() - start;
        // It asks while a save waits, and that save fails: it still goes on asking.
        window.MANGA.holdSave = true; window.MANGA.failSave = true;
        toggle('[data-manga-page-align]', false);
        await __tick(2600);
        window.MANGA.holdSave = false; window.release('save'); await __tick(50);
        window.MANGA.failSave = false;
        window.MANGA.alignment = full;
        await __tick(5200);
        out.after = frameOf(3).classList.contains('aligned');
        return out;
        """,
        **_double(),
    )
    out = result.value
    assert out["failed"] == 1 and out["again"] == 2
    assert out["after"] is True  # it went on asking and the pair got its alignment


def test_tiny_and_huge_pictures(tmp_path: Path) -> None:
    import numpy as np

    # Too small to be a page (it raised before): left as it is.
    noise = np.random.default_rng(1)
    for width, height in ((5, 5), (2000, 8), (8, 2000), (63, 900)):
        picture = Image.fromarray(((noise.random((height, width)) > 0.5) * 255).astype(np.uint8))
        assert manga_align.analyze(picture)["angle"] is None
    # A very wide one is analysed at a bounded size (12000 × 400 took 0.9 s and 115 MB).
    page = Image.new("L", (12000, 400), 255)
    page.paste(0, (100, 100, 11900, 110))
    levels = manga_align._darkness(page)
    assert levels is not None and levels[0].size <= 2_000_000


def test_a_page_is_analysed_the_way_it_is_shown(tmp_path: Path) -> None:
    from pudge.manga_align_store import AlignmentStore

    left, _right = S.printed_spread(across=False)
    upright = S.scan(left, angle=0.6)
    # Stored turned a quarter with EXIF orientation 6 ("turn 90° clockwise to show").
    stored = upright.rotate(90, expand=True)
    exif = Image.Exif()
    exif[0x0112] = 6
    data = tmp_path / "page.jpg"
    stored.save(data, "JPEG", quality=95, exif=exif)
    store = AlignmentStore(tmp_path, lambda path: None, lambda archive, name: data.read_bytes())
    found = store._analyse(None, "page.jpg")
    assert (found["width"], found["height"]) == (700, 1000)
    assert found["angle"] == pytest.approx(0.6, abs=0.1)
    # A large JPEG is decoded at a fraction of its size (at least the working size).
    big = tmp_path / "big.jpg"
    upright.resize((2800, 4000)).save(big, "JPEG", quality=90)
    store = AlignmentStore(tmp_path, lambda path: None, lambda archive, name: big.read_bytes())
    found = store._analyse(None, "big.jpg")
    assert found["height"] == 2000 and found["angle"] == pytest.approx(0.6, abs=0.1)


def test_a_removed_volume_stops_its_work_and_leaves_no_file(tmp_path: Path, monkeypatch) -> None:
    from pudge.manga_align_store import AlignmentStore

    page = tmp_path / "p.png"
    S.scan(S.printed_spread(across=False)[0], angle=0.4).save(page)
    names = [f"{index:03d}.png" for index in range(40)]
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        for name in names:
            zf.write(page, name)
    analysed: list[str] = []
    real = manga_align.analyze

    def slow(image):
        analysed.append("page")
        time.sleep(0.05)
        return real(image)

    monkeypatch.setattr(manga_align, "analyze", slow)
    store = AlignmentStore(tmp_path / "cache", zipfile.ZipFile, lambda zf, name: zf.read(name))
    assert store.served(archive, names, [])["pending"] > 0
    deadline = time.monotonic() + 30
    while len(analysed) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    worker = store._thread
    store.forget(archive)
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert len(analysed) < len(names)  # it stopped
    assert not list((tmp_path / "cache" / "manga-align").glob("*.json"))
    assert store._volumes == {}
    # In the library again: worked out again.
    assert store.served(archive, names, [], work=False)["pending"] > 0


def test_one_alignment_store_per_service(tmp_path: Path) -> None:
    import threading

    _db, service, _book, _archive = _volume(tmp_path)
    service.__dict__.pop("_align_store", None)
    stores: list[object] = []
    barrier = threading.Barrier(8)

    def make() -> None:
        barrier.wait()
        stores.append(service._alignment_store())

    threads = [threading.Thread(target=make) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(store) for store in stores}) == 1
