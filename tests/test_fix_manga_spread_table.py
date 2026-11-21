"""Manga two pages side by side: one table on the Mac and the iPhone/iPad (review follow-up).

The Mac's two-page mode paired 1–2, 3–4 (no cover alone) while the phone showed 1, 2–3, 4–5,
so a place saved on one opened on the other in another pair. Now the Mac serves one table
(pudge/manga_spreads.py) to its own reader and to phones, and the phone's copy of the rules is
tested against the same cases (tests/fixtures/manga_spreads.json, also read by
ios/PudgeCore/Tests/PudgeCoreTests/MangaSpreadsTests.swift): the cover alone, then pairs; a
wide page alone; pairs start again after a spread; per volume "Shift pairs by one page".
Both pages of a pair count as visited in Statistics. Invented pages only.
"""

from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_spreads import spread_of, spread_table
from t71g03_support import MANGA, READER
from t71js_support import pages

FIXTURE = Path(__file__).with_name("fixtures") / "manga_spreads.json"


# ------------------------------------------------------------------------------ the table
@pytest.mark.parametrize(
    "case", json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"], ids=lambda case: case["name"]
)
def test_the_shared_cases(case: dict) -> None:
    assert spread_table(case["pageCount"], case["wide"], shift=case["shift"]) == case["spreads"]


def test_every_page_is_in_exactly_one_spread() -> None:
    for count in range(0, 12):
        for wide in ([], [1], [3], [2, 3], [count - 1]):
            for shift in (False, True):
                table = spread_table(count, wide, shift=shift)
                assert [page for spread in table for page in spread] == list(range(count))
                assert all(len(spread) in {1, 2} for spread in table)
                assert all(len(spread) == 1 for spread in table if any(page in wide for page in spread))
    table = spread_table(7, [])
    assert spread_of(4, table) == [3, 4] and spread_of(-2, table) == [0] and spread_of(30, table) == [5, 6]


def _png(path: Path, width: int, height: int) -> Path:
    Image.new("RGB", (width, height), "white").save(path)
    return path


def _volume(tmp_path: Path, sizes: list[tuple[int, int]]) -> tuple[Database, MangaService, int, Path]:
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        for number, (width, height) in enumerate(sizes):
            zf.write(_png(tmp_path / f"{number:03d}.png", width, height), f"{number:03d}.png")
    db = Database(tmp_path / "pudge.db")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    return db, service, int(service.import_file(archive)["id"]), archive


def test_the_mac_serves_the_table_and_keeps_the_shift_per_volume(tmp_path: Path) -> None:
    from pudge.web_app import WebAppApi

    db, manga, book_id, _archive = _volume(tmp_path, [(700, 1000)] * 4 + [(1400, 1000)] + [(700, 1000)] * 3)
    api = SimpleNamespace(manga=manga)
    answer = WebAppApi.manga_spreads(api, book_id)
    assert answer["spreads"] == [[0], [1, 2], [3], [4], [5, 6], [7]] and answer["shift"] is False
    shifted = WebAppApi.manga_set_pair_shift(api, book_id, True)
    assert shifted["shift"] is True and shifted["spreads"] == [[0], [1], [2, 3], [4], [5, 6], [7]]
    # Kept on the Mac (a new start reads it again), for this volume only.
    assert MangaService(db, cache_dir=tmp_path / "cache").spread_pages(book_id)["shift"] is True
    assert WebAppApi.manga_set_pair_shift(api, book_id, False)["shift"] is False
    assert db.get_state(f"manga_pair_shift:v1:{book_id}", "") == ""
    manga.set_pair_shift(book_id, True)
    manga.remove_books([book_id])
    assert db.get_state(f"manga_pair_shift:v1:{book_id}", "") == ""
    with pytest.raises(KeyError):
        manga.set_pair_shift(book_id, True)
    from pudge.bridge_methods import PAGE_METHODS

    assert {"manga_spreads", "manga_set_pair_shift"} <= PAGE_METHODS


def test_a_damaged_page_is_only_that_page_unknown(tmp_path: Path) -> None:
    _db, manga, book_id, archive = _volume(tmp_path, [(700, 1000), (1400, 1000), (700, 1000)])
    # Damage the middle page's stored bytes (a bad CRC when it is read).
    data = bytearray(archive.read_bytes())
    with zipfile.ZipFile(archive) as zf:
        info = zf.getinfo("001.png")
    start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    for offset in range(start + 40, start + 60):
        data[offset] ^= 0xFF
    archive.write_bytes(bytes(data))
    answer = manga.spread_pages(book_id)
    assert answer["spreads"] == [[0], [1, 2]]  # page 1 is no longer known to be wide
    assert answer["aspect"] == pytest.approx(1000 / 700, abs=1e-4)


def _phone(tmp_path: Path, db: Database, manga: MangaService):
    from pudge.iphone_service import IPhoneService

    phone = IPhoneService(db, tmp_path / "mobile")
    phone.api = SimpleNamespace(manga=manga)
    return phone


def test_phones_get_and_change_the_shift(tmp_path: Path) -> None:
    from pudge.iphone_media import MediaError

    db, manga, book_id, _archive = _volume(tmp_path, [(700, 1000)] * 5)
    phone = _phone(tmp_path, db, manga)
    route = f"/api/v2/media/manga/{book_id}/spreads"
    assert phone.media.handle("GET", route, {}, lambda: {})["shift"] is False
    answer = phone.media.handle("POST", route, {}, lambda: {"shift": True})
    assert answer["shift"] is True and answer["spreads"] == [[0], [1], [2, 3], [4]]
    assert manga.pair_shift(book_id) is True
    with pytest.raises(MediaError):
        phone.media.handle("POST", route, {}, lambda: {"shift": "yes"})


def test_a_downloaded_volume_carries_its_wide_pages_and_shift(tmp_path: Path) -> None:
    from pudge.iphone_reading import PackageWriter

    db, manga, book_id, _archive = _volume(tmp_path, [(700, 1000), (700, 1000), (1400, 1000)])
    manga.set_pair_shift(book_id, True)
    phone = _phone(tmp_path, db, manga)
    reading = phone.media.reading
    reading.ocr_status = lambda _book: {"complete": False}
    phone.media.cover = lambda kind, book: (b"jpg", "image/jpeg")
    description = reading._manga(book_id, PackageWriter(tmp_path / "pack"), lambda value: None)
    # The files become page-<n>: a name such as "012-013" would be lost, so the flags go along.
    assert description["pages"] == ["page-0.png", "page-1.png", "page-2.png"]
    assert description["widePages"] == [2] and description["pairShift"] is True


# --------------------------------------------------------------------------- statistics
def _segment(start: float, seconds: float, locator: dict) -> dict:
    return {
        "kind": "manga",
        "start": start,
        "end": start + seconds,
        "media": [{"media_uuid": "m", "source_revision": "r", "locator_end": locator}],
    }


def test_both_pages_of_a_pair_count_once_each() -> None:
    from pudge.consumption_volume import _manga_visits
    from pudge.consumption_words import segment_units

    pair = {"page_index": 4, "page_id": "page:4", "page_ids": ["page:4", "page:5"]}
    visits = _manga_visits(
        [_segment(0, 5, pair), _segment(5, 5, pair), _segment(10, 5, {"page_index": 6, "page_id": "page:6"})]
    )
    assert visits[("m", "r")] == ["page:4", "page:5", "page:6"]
    # Words of both pages are counted (each page's text boxes once).
    units = segment_units(_segment(0, 5, pair))
    assert {value[1]["page_index"] for value in units.values()} == {4, 5}


def test_the_phone_reports_both_pages_of_a_pair(tmp_path: Path) -> None:
    db, manga, book_id, _archive = _volume(tmp_path, [(700, 1000)] * 6)
    phone = _phone(tmp_path, db, manga)
    recorded: list[dict] = []
    phone.api.consumption = SimpleNamespace(
        manga_media=lambda item: "ref",
        record_device_interval=lambda **kwargs: recorded.append(kwargs) or "applied",
    )
    base = {"kind": "manga", "item": book_id, "start": 100.0, "end": 105.0, "active": 5.0}
    items = [
        {**base, "id": str(uuid.uuid4()), "from": {"page": 3, "pageEnd": 4}, "to": {"page": 3, "pageEnd": 4}},
        {**base, "id": str(uuid.uuid4()), "from": {"page": 5}, "to": {"page": 5, "pageEnd": 9}},
    ]
    phone.media.record_activity("device", "iPhone", items, sent_at=105.0)
    assert recorded[0]["locator_end"] == {
        "page_index": 3,
        "page_id": "page:3",
        "page_ids": ["page:3", "page:4"],
    }
    assert recorded[1]["locator_end"] == {"page_index": 5, "page_id": "page:5"}  # no pair: one page


# ------------------------------------------------------------------- the Mac reader (page)
SHOWN = r"""
const shown = () => ({pages: pages().map(p => p.index), label: __text('#mangaV2PageLabel')});
const next = async () => { await __click('#mangaReaderV2 [data-manga-v2-action="next"]'); await __tick(150); };
const back = async () => { await __click('#mangaReaderV2 [data-manga-v2-action="previous"]'); await __tick(150); };
const saved = () => callsOf('manga_set_position').map(c => c[1]);
"""


def _double(extra: str = "") -> dict:
    return {
        "before": MANGA + pages(12, ocr=False) + "window.MANGA.books[0].position = 4;" + extra,
        "localStorage": {"pudge.manga.reader.v2": json.dumps({"mode": "double", "direction": "rtl"})},
    }


def test_the_mac_shows_the_cover_alone_and_the_pairs_of_the_table(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        await open(3);
        const out = {opened: shown(), savedOnOpen: saved()};
        await next(); out.next = shown(); out.savedAfterTurn = saved();
        await back(); out.back = shown();
        await back(); out.back2 = shown();
        await back(); out.cover = shown();
        await back(); out.stays = shown();
        out.context = window.PudgeMangaReaderV2.consumptionContext();
        return out;
        """,
        **_double(),
    )
    out = result.value
    # Saved page 5 (index 4) opens in its pair 4–5 (indices 3–4); opening saves nothing.
    assert out["opened"] == {"pages": [3, 4], "label": "4–5 / 12"}
    assert out["savedOnOpen"] == []
    assert out["next"] == {"pages": [5, 6], "label": "6–7 / 12"} and out["savedAfterTurn"] == [5]
    assert out["back"]["pages"] == [3, 4] and out["back2"]["pages"] == [1, 2]
    assert out["cover"] == {"pages": [0], "label": "1 / 12"} and out["stays"]["pages"] == [0]
    assert out["context"]["page_index"] == 0 and "page_ids" not in out["context"]


def test_a_wide_page_is_alone_and_the_pairs_start_again(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        await open(3);
        const out = {opened: shown()};
        await next(); out.wide = shown();
        await next(); out.after = shown();
        out.context = window.PudgeMangaReaderV2.consumptionContext();
        await back(); out.backToWide = shown();
        return out;
        """,
        **_double("window.MANGA.wide[3] = [5];"),
    )
    out = result.value
    assert out["opened"]["pages"] == [3, 4]
    assert out["wide"]["pages"] == [5] and out["after"]["pages"] == [6, 7]
    assert out["context"]["page_index"] == 6 and out["context"]["page_ids"] == ["p6.png", "p7.png"]
    assert out["backToWide"]["pages"] == [5]


def test_shift_pairs_by_one_page_and_back_to_one_page_keeps_the_place(page) -> None:
    result = page.run(
        READER
        + SHOWN
        + r"""
        await open(3);
        const box = document.querySelector('[data-manga-pair-shift]');
        const out = {visible: !box.closest('label').hidden, checked: box.checked, text: box.closest('label').textContent.trim()};
        box.checked = true; box.dispatchEvent(new Event('change', {bubbles: true})); await __tick(150);
        out.shifted = shown(); out.calls = callsOf('manga_set_pair_shift');
        out.checkedNow = document.querySelector('[data-manga-pair-shift]').checked;
        await __input('[data-manga-setting="mode"]', 'single'); await __tick(100);
        out.single = shown();
        out.hiddenInSingle = document.querySelector('[data-manga-pair-shift]').closest('label').hidden;
        out.saved = saved();
        return out;
        """,
        **_double(),
    )
    out = result.value
    assert out["visible"] is True and out["checked"] is False
    assert out["text"] == "Shift pairs by one page"
    # Shifted: [0] [1] [2, 3] [4, 5] ...: saved page index 4 is now in the pair 5–6.
    assert out["calls"] == [[3, True]]
    assert out["shifted"] == {"pages": [4, 5], "label": "5–6 / 12"} and out["checkedNow"] is True
    # Back to one page: the saved page itself, and it was never saved as another page.
    assert out["single"] == {"pages": [4], "label": "5 / 12"}
    assert out["hiddenInSingle"] is True
    assert out["saved"] == []


def test_the_shift_label_is_translated() -> None:
    from pudge.i18n import catalog

    assert catalog("en")["manga.shiftPairs"] == "Shift pairs by one page"
    assert catalog("ru")["manga.shiftPairs"] == "Сдвинуть развороты на страницу"
