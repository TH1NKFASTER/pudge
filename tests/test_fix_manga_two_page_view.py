"""iPhone/iPad manga: two pages side by side when the reader is wide enough.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac, so its part is
guarded by reading its Swift source (like tests/test_fix_ios_reader_guards.py); the pairing
rules are a pure function in PudgeCore with XCTest tests (ios/PudgeCore/Tests/PudgeCoreTests/
MangaSpreadsTests.swift, `swift test --package-path ios/PudgeCore` on the Mac). What the Mac
serves for it is behaviour-tested here.

Landscape iPhone/iPad (and the halves of an unfolded display) show two pages like a printed
book in the volume's reading direction (manga: the lower page on the right), decided by the
space and the page shape, never by the device model. As on the Mac: a wide picture (a double-
page spread) is shown alone, the cover alone, and the pairing starts again after a spread; a
turn moves by a spread; the title reads "4–5 / 211"; the saved and synced place stays one page
(the first of the pair in reading order), and the pair's second page counts as read.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService
from source_policy import source

READ = "ios/Pudge/UI/ReadScreen.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
SPREADS = "ios/PudgeCore/Sources/PudgeCore/MangaSpreads.swift"
CONTRACTS = "ios/PudgeCore/Sources/PudgeCore/MediaContracts.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/MangaSpreadsTests.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    """Swift text with whitespace collapsed; from `start` up to the next `end` when given."""
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def _png(path: Path, width: int, height: int) -> Path:
    Image.new("RGB", (width, height), "white").save(path)
    return path


PAGES = [
    ("000.png", 700, 1000),  # cover
    ("001.png", 700, 1000),
    ("002.png", 1400, 1000),  # one picture over two pages
    ("004-005.png", 700, 1000),  # a spread by its name
    ("006.png", 720, 1000),
]


def _volume(tmp_path: Path, pages=PAGES) -> tuple[Database, MangaService, int, Path]:
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, width, height in pages:
            zf.write(_png(tmp_path / name, width, height), name)
    db = Database(tmp_path / "pudge.db")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    return db, service, int(service.import_file(archive)["id"]), archive


# ------------------------------------------------------------------------ the Mac's answer
def test_the_mac_names_the_wide_pages_and_the_usual_page_shape(tmp_path: Path, monkeypatch) -> None:
    _db, service, book_id, archive = _volume(tmp_path)
    answer = service.spread_pages(book_id)
    # (Also "Align scanned pages" and the pages' transforms: tests/test_fix_manga_page_alignment.py.)
    assert {key: answer[key] for key in ("wide", "aspect", "shift", "spreads")} == {
        "wide": [2, 3],
        "aspect": pytest.approx(1000 / 700, abs=1e-4),
        "shift": False,
        "spreads": [[0], [1], [2], [3], [4]],  # the cover, page 1 before a spread, two spreads, page 4
    }
    assert answer["align"] is True
    # The same rule as the Mac reader's page answer.
    assert [service.page(book_id, index)["spread"] for index in range(5)] == [False, False, True, True, False]

    # Kept until the volume's files change: no page is read again.
    def unread(*_args, **_kwargs):
        raise AssertionError("read again")

    monkeypatch.setattr(MangaService, "_image_size", staticmethod(unread))
    # (The alignment is worked out meanwhile in the background.)
    again = service.spread_pages(book_id)
    assert {key: value for key, value in again.items() if key != "alignment"} == {
        key: value for key, value in answer.items() if key != "alignment"
    }
    monkeypatch.undo()
    with zipfile.ZipFile(archive, "a") as zf:
        zf.write(_png(tmp_path / "007.png", 1500, 1000), "007.png")
    assert 5 in service.spread_pages(book_id)["wide"]


def test_a_folder_volume_and_unreadable_pages(tmp_path: Path) -> None:
    folder = tmp_path / "vol"
    folder.mkdir()
    _png(folder / "01.png", 700, 1000)
    _png(folder / "02.png", 1600, 1000)
    (folder / "03.png").write_bytes(b"not an image")
    db = Database(tmp_path / "pudge.db")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(id,path,title,page_count,created_at,updated_at) VALUES(7,?,'Vol 1',3,0,0)",
            (str(folder),),
        )
    answer = service.spread_pages(7)
    assert answer["wide"] == [1]
    assert answer["aspect"] == pytest.approx(1000 / 700, abs=1e-4)


def _phone(tmp_path: Path, db: Database, manga: MangaService, calls: list) -> object:
    from pudge.iphone_service import IPhoneService

    phone = IPhoneService(db, tmp_path / "mobile")
    phone.api = SimpleNamespace(
        manga=manga,
        manga_set_position=lambda book, page: calls.append(("position", book, page)),
        manga_mark_read=lambda book, page: calls.append(("read", book, page)),
    )
    return phone


def _call(phone, method: str, route: str, body: dict | None = None):
    return phone.media.handle(method, "/api/v2/media/" + route, {}, lambda: body or {})


def test_the_phone_asks_the_mac_for_the_spreads(tmp_path: Path) -> None:
    from pudge.iphone_media import MediaError

    db, manga, book_id, _archive = _volume(tmp_path)
    phone = _phone(tmp_path, db, manga, [])
    assert _call(phone, "GET", f"manga/{book_id}/spreads") == manga.spread_pages(book_id)
    with pytest.raises(MediaError):
        _call(phone, "GET", "manga/999/spreads")


@pytest.mark.parametrize(
    ("extra", "read_through"),
    [
        ({}, 3),
        ({"readThrough": 4}, 4),  # the pair 3–4: page 4 was read too
        ({"readThrough": 9}, 3),  # never more than the page after the place
        ({"readThrough": True}, 3),
        ({"readThrough": "4"}, 3),
        ({"readThrough": 2}, 3),
    ],
)
def test_the_place_stays_the_first_page_of_the_pair_and_both_are_read(
    tmp_path: Path, extra, read_through
) -> None:
    db, manga, book_id, _archive = _volume(tmp_path)
    calls: list = []
    phone = _phone(tmp_path, db, manga, calls)
    assert _call(phone, "POST", f"manga/{book_id}/position", {"page": 3, "read": True, **extra}) == {
        "ok": True
    }
    assert calls == [("position", book_id, 3), ("read", book_id, read_through)]


def test_the_last_pair_finishes_the_volume(tmp_path: Path) -> None:
    db, manga, book_id, _archive = _volume(tmp_path)
    from pudge.iphone_service import IPhoneService

    phone = IPhoneService(db, tmp_path / "mobile")
    phone.api = SimpleNamespace(
        manga=manga,
        manga_set_position=lambda book, page: manga.set_position(book, page),
        manga_mark_read=lambda book, page: manga.mark_read(book, page),
    )
    _call(phone, "POST", f"manga/{book_id}/position", {"page": 3, "read": True, "readThrough": 4})
    with db.connect() as conn:
        row = conn.execute(
            "SELECT position,read_pages,page_count FROM manga_books WHERE id=?", (book_id,)
        ).fetchone()
    assert (int(row["position"]), int(row["read_pages"]), int(row["page_count"])) == (3, 5, 5)


# ------------------------------------------------------------------ the iPhone app (Swift)
def test_pairing_is_a_pure_core_function_with_the_macs_rules() -> None:
    core = _swift(SPREADS)
    # The Mac's threshold for a wide picture (pudge/manga.py) is the phone's.
    assert "width >= height * 1.15" in core
    assert MangaService._spread_by_size(1150, 1000) and not MangaService._spread_by_size(1149, 1000)
    spreads = _swift(SPREADS, "public static func spreads(", "public static func first(")
    assert "guard twoPages else { return (0..<pageCount).map { [$0] } }" in spreads
    assert "var result: [[Int]] = [[0]]" in spreads  # the cover alone
    assert "if !wide.contains(index), index + 1 < pageCount, !wide.contains(index + 1) {" in spreads
    # By the space and the page shape, never the device model.
    decide = _swift(SPREADS, "public static func showsTwoPages(", "public static func spreads(")
    assert "let single = min(width, height / pageAspect)" in decide
    assert "let pair = min(width / 2, height / pageAspect)" in decide
    assert "UIDevice" not in core and "userInterfaceIdiom" not in core and "import UIKit" not in core
    assert (
        'return low == high ? "\\(low + 1) / \\(pageCount)" : "\\(low + 1)–\\(high + 1) / \\(pageCount)"'
        in core
    )
    assert "rightToLeft ? Array(spread.reversed()) : spread" in core
    # The two pages meet in the middle (the gutter, where a fold is).
    frames = _swift(SPREADS, "public static func frames(", "public static func fit(")
    assert "fit(sizes[0], in: CGRect(x: 0, y: 0, width: half, height: box.height), alignX: 1)" in frames
    assert "fit(sizes[1], in: CGRect(x: half, y: 0, width: half, height: box.height), alignX: 0)" in frames
    tests = _swift(CORE_TESTS)
    for name in (
        "testCoverAloneThenPairsAndALastPageAlone",
        "testAWidePageIsAloneAndThePairingStartsAgainAfterIt",
        "testThePlaceIsTheFirstPageOfTheShownSpread",
        "testRightToLeftPutsTheLowerPageOnTheRight",
        "testTitleShowsBothPagesOfAPair",
        "testTwoPagesByTheSpaceAndThePageShapeNotTheDevice",
        "testAPairMeetsAtTheMiddleWhereAFoldIs",
        "testTheMacsSpreadsAnswerDecodes",
    ):
        assert f"func {name}()" in tests
    assert "public struct MangaSpreadPages: Codable, Equatable, Sendable {" in _swift(CONTRACTS)


def test_the_reader_turns_by_spreads_and_keeps_one_page_as_the_place() -> None:
    screen = _swift(READ, "struct MangaReaderScreen", "struct MangaPageView")
    # The place and the layout live in one value (MangaReadingPlace, XCTests in PudgeCore): the
    # view only asks it. A new layout makes a new pager; a page an old pager reports is no turn.
    assert "@State private var place = MangaReadingPlace(page: 0, layout: [])" in screen
    assert (
        "TabView(selection: Binding(get: { place.shown }, set: { tag in turn(to: tag, generation: generation) })) {"
        in screen
    )
    assert "ForEach(order, id: \\.self) { spread in" in screen
    assert ".tag(spread.first ?? 0)" in screen
    assert ".id(generation)" in screen
    assert "pages: MangaSpreads.onScreen(spread, rightToLeft: book.rightToLeft)" in screen
    assert (
        "private var order: [[Int]] { book.rightToLeft ? Array(place.layout.reversed()) : place.layout }"
        in screen
    )
    assert (
        "MangaSpreads.showsTwoPages(width: Double(screen.width), height: Double(screen.height), pageAspect: pageAspect)"
        in screen
    )
    # The reader's whole size: the bars showing or hiding never switch the layout.
    assert (
        "geometry.size.width + geometry.safeAreaInsets.leading + geometry.safeAreaInsets.trailing" in screen
    )
    assert ".navigationTitle(MangaSpreads.title(place.shownPages, pageCount: book.pages))" in screen
    # Rotation, a spread found, the pairs shifted: one handler makes the new layout; the place
    # stays (never saved, no pages marked read).
    assert "private func relayout() { place.relayout(layoutNow()) }" in screen
    assert ".onChange(of: whole) { _, value in screen = value; relayout() }" in screen
    assert (
        "MangaSpreads.spreads(pageCount: book.pages, wide: wide, twoPages: twoPages, shift: shift)" in screen
    )
    assert "place = MangaReadingPlace(page: start, layout: layoutNow())" in screen
    assert "onChange(of: page)" not in screen and "OpenedPlace" not in screen
    # Only a turn is saved: the first page, with the pair's second one read.
    turn = _swift(READ, "private func turn(to tag: Int, generation: Int) {", "private func toggleShift()")
    assert turn.index(
        "guard let save = place.turned(to: tag, generation: generation) else { return }"
    ) < turn.index("await media.saveMangaPosition(book.id, page: save.page, readThrough: save.readThrough)")
    assert screen.count("saveMangaPosition(") == 1
    # Spreads known before a page is seen; a wide picture that arrives is shown alone too.
    assert "guard let known = await media.mangaSpreadPages(book.id) else { return }" in screen
    assert "if MangaSpreads.isWide(width: width, height: height) {" in screen
    assert "#available" not in screen and "reservedRegions" not in screen  # iOS 17


def test_both_pages_zoom_together_and_keep_their_text_boxes() -> None:
    page = _swift(READ, "struct MangaPageView", "// MARK: Pinch zoom")
    assert page.count("ZoomableContainer(") == 1
    zoom = page.index("ZoomableContainer(")
    assert zoom < page.index("ForEach(Array(pages.enumerated()), id: \\.offset) { slot, index in")
    assert "if let regions = texts[index]?.regions {" in page
    assert ".accessibilityAction { onRegion(region.text) }" in page
    # (An aligned pair is drawn at one height: tests/test_fix_manga_page_alignment.py.)
    assert (
        "MangaSpreads.frames(pages.map { images[$0]?.size ?? .zero }, in: size, sameHeight: sameHeight)"
        in page
    )
    hit = _swift(READ, "private func region(at point", "private func rect(_ region: MangaRegion")
    assert (
        "for (slot, index) in pages.enumerated() where images[index] != nil && slot < layout.count {" in hit
    )
    load = _swift(READ, ".task(id: pages) {", "/// Where each page goes")
    assert 'await media.image("manga/\\(book)/pages/\\(index)")' in load
    assert "texts[index] = await media.mangaText(book, page: index)" in load
    assert "#available" not in page


def test_the_phone_reads_spreads_from_downloads_or_the_mac_and_sends_the_pair() -> None:
    media = _swift(MEDIA)
    assert "import ImageIO" in media
    spreads = _swift(MEDIA, "func mangaSpreadPages(", '/// "Shift pairs by one page" for a volume')
    assert "if let package = savedManga(book) {" in spreads
    assert "Task.detached(priority: .userInitiated) { mangaPageShapes(files) }.value" in spreads
    # The package's own wide pages (its files are renamed page-<n>: the "012-013" names are lost).
    assert (
        "known = MangaSpreadPages(wide: package.widePages ?? shapes.wide, aspect: shapes.aspect," in spreads
    )
    assert 'answer = try? await client.media("manga/\\(book)/spreads", as: MangaSpreadPages.self)' in spreads
    shapes = _swift(MEDIA, "func mangaPageShapes(", "return MangaSpreads.pages(sizes: sizes)")
    assert "CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any]" in shapes
    request = next(
        line for line in source(MEDIA).splitlines() if line.startswith("struct MangaPositionRequest:")
    )
    assert "var readThrough: Int? = nil" in request
    save = _swift(MEDIA, "func saveMangaPosition(", "// MARK: Reading on iPhone")
    assert "record.readThrough = readThrough" in save
    assert "MangaPositionRequest(page: page, read: true, readThrough: readThrough)" in save
    assert (
        "MangaPositionRequest(page: page, read: true, observedAt: record.positionAt, readThrough: record.readThrough)"
        in media
    )
    assert "record.page = item.position; record.readThrough = nil" in media
    doc = source("ios/PROTOCOL.md")
    assert "`manga/<id>/spreads`" in doc and "readThrough" in doc


def test_the_phone_shifts_pairs_counts_both_pages_and_reads_the_macs_cases() -> None:
    # Review follow-up: the same table as the Mac (shared cases), the shift per volume, and the
    # place kept by a small PudgeCore value with XCTests (MangaReadingPlace).
    core = _swift(SPREADS)
    assert "public struct MangaReadingPlace: Equatable {" in core
    assert (
        "guard generation == self.generation, tag != shown, let spread = layout.first(where: { $0.first == tag }) else { return nil }"
        in core
    )
    assert "guard new != layout else { return } layout = new generation += 1" in core
    assert "if shift, index < pageCount { result.append([index]) index += 1 }" in core
    tests = _swift(CORE_TESTS)
    for name in (
        "testTheSameSpreadsAsTheMac",
        "testShiftPutsThePageAfterTheCoverAlone",
        "testTurningTheDeviceNeverMovesOrSavesThePlace",
        "testATurnSavesTheFirstPageAndReadsTheSecond",
    ):
        assert f"func {name}()" in tests
    # The test file is five folders below the repository root, where the shared cases are.
    assert CORE_TESTS.count("/") == 4
    assert 'root.appendingPathComponent("tests/fixtures/manga_spreads.json")' in tests
    assert tests.count(".deletingLastPathComponent()") == 5
    screen = _swift(READ, "struct MangaReaderScreen", "struct MangaPageView")
    assert "Button { toggleShift() }" in screen and '.accessibilityLabel("Shift pairs by one page")' in screen
    assert (
        'if place.shownPages.count > 1, let last = place.shownPages.last { at["pageEnd"] = Double(last) }'
        in screen
    )
    shift = _swift(MEDIA, "func setMangaPairShift(", "private var pairShifts")
    assert "pairShiftsUnsent.insert(key)" in shift and "post: MangaPairShiftRequest(shift: shift)" in shift
    package = _swift("ios/PudgeCore/Sources/PudgeCore/ReadingPackage.swift")
    assert "public let widePages: [Int]?" in package and "public let pairShift: Bool?" in package
    assert "public let shift: Bool?" in _swift(CONTRACTS)
    doc = source("ios/PROTOCOL.md")
    assert "POST media/manga/<id>/spreads {shift}" in doc and "an older Mac ignores it" in doc
