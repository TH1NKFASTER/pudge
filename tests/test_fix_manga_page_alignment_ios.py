"""Manga two pages on the iPhone/iPad: scanned pages straightened and aligned as on the Mac
(tests/test_fix_manga_page_alignment.py). The Swift app is built only in Xcode on the Mac:
its parts are checked here by their source; the math is checked by PudgeCore's XCTests
against the same cases as the Mac (tests/fixtures/manga_alignment.json).
"""

from __future__ import annotations

import re

from source_policy import source


def _swift(relative: str) -> str:
    return re.sub(r"\s+", " ", source(relative))


def test_the_iphone_draws_the_same_transforms_and_has_the_toggle() -> None:
    core = _swift("ios/PudgeCore/Sources/PudgeCore/MangaSpreads.swift")
    assert "public struct MangaPageTransform: Codable, Equatable, Sendable {" in core
    assert "public struct MangaAlignment: Codable, Equatable, Sendable {" in core
    assert (
        "public static func frames(_ sizes: [CGSize], in box: CGSize, sameHeight: Bool = false) -> [CGRect] {"
        in core
    )
    assert 'pairs?["\\(spread[0])-\\(spread[1])"]?[rightToLeft ? "rtl" : "ltr"]' in core
    tests = _swift("ios/PudgeCore/Tests/PudgeCoreTests/MangaSpreadsTests.swift")
    # Lenient: one odd transform is that page left as it is (a downloaded volume still opens).
    assert "init(from decoder: Decoder) throws { value = try? MangaPageTransform(from: decoder) }" in core
    assert "guard let values = try? decoder.container(keyedBy: CodingKeys.self) else {" in core
    assert "angle = (try? values.decodeIfPresent(Double.self, forKey: .angle)) ?? 0" in core
    for name in (
        "testAnOddAlignmentNeverStopsAVolumeFromOpening",
        "testTheSamePageTransformsAsTheMac",
        "testTheMacsAlignmentAnswerDecodesAndGivesEachPageItsTransform",
        "testAnAlignedPairIsDrawnAtOneHeight",
    ):
        assert f"func {name}()" in tests
    assert '"../../../../tests/fixtures/manga_alignment.json"' in tests
    read = _swift("ios/Pudge/UI/ReadScreen.swift")
    # The picture and its text boxes with the same transform; taps mapped back through it.
    assert ".modifier(MangaAlignedPage(transform: aligned, size: frame.size))" in read
    assert "let centre = aligned?.screenPoint(CGPoint(x: box.midX, y: box.midY), in: frame)" in read
    assert "let onPage = aligned?.pagePoint(point, in: frame) ?? point" in read
    assert "if let aligned, !aligned.keeps(point, in: frame) { continue }" in read
    # The toggle next to the shift, at once (the save goes after).
    assert "Button { toggleAlign() }" in read and '.accessibilityLabel("Align scanned pages")' in read
    assert "align.toggle() let value = align Task { await media.setMangaPageAlign(book.id, value) }" in read
    assert "onImage: { index, size in learn(index, size) }, alignment: transforms(spread))" in read
    # Asked again only while the Mac works it out (only while aligning: with it off the Mac
    # works nothing out and `pending` stays), and once after turning aligning on here.
    assert "let working = align && ((alignment?.pending ?? 0) > 0 || alignAskPending)" in read
    assert "guard twoPages, working else { continue }" in read
    assert "waited >= 30" not in read
    media = _swift("ios/Pudge/Features/Media/MediaModel.swift")
    assert "MangaPageAlignRequest(align: align)" in media and "pageAlignsUnsent.insert(key)" in media
    # A change made on the phone and not yet on the Mac, or made while the phone asked, stays as
    # it is on the phone: an answer to a request sent before it is older.
    assert "let changes = spreadSettingChanges[key, default: 0]" in media
    assert "let current = spreadSettingChanges[key, default: 0] == changes" in media
    assert "let shift = !current || pairShiftsUnsent.contains(key) ? pairShifts[key] :" in media
    assert "let align = !current || pageAlignsUnsent.contains(key) ? pageAligns[key] :" in media
    assert media.count("spreadSettingChanges[key, default: 0] += 1") == 2  # both setters
    # Marked sent only when the value sent is still the one here.
    assert "if answer != nil, pairShifts[key] == shift { pairShiftsUnsent.remove(key) }" in media
    assert media.count("if pageAligns[key] == align { pageAlignsUnsent.remove(key) }") == 2
    # A Mac from before the setting refuses {align} (it wants shift): not sent again.
    assert "return [400, 404, 422].contains(status) ? .refused : .later" in media
    assert media.count("case .refused: pageAlignsUnsent.remove(key) // an older Mac") == 2
    contracts = _swift("ios/PudgeCore/Sources/PudgeCore/MediaContracts.swift")
    assert (
        "public let alignment: MangaAlignment?" in contracts
        and "public struct MangaPageAlignRequest" in contracts
    )
    package = _swift("ios/PudgeCore/Sources/PudgeCore/ReadingPackage.swift")
    assert "public let pageAlign: Bool?" in package and "public let alignment: MangaAlignment?" in package
    doc = _swift("ios/PROTOCOL.md")
    assert "POST media/manga/<id>/spreads {align}" in doc and "tests/fixtures/manga_alignment.json" in doc
